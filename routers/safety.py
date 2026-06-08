import json
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
from models import (
    SafetyEvent, SafetyEventStatus, Alert, AlertLevel,
    EvacuationOrder, WorkOrder, WorkOrderStatus,
    EvacuationConfirmation, User, UserRole, EnvironmentMonitor,
    SafetyEventActionLog, SafetyActionType, PushMessage, MessageType, MinerLocationReport
)
from routers.auth import get_current_user
from services.notification_service import push_system_notification, push_alert_notification

router = APIRouter(prefix="/safety", tags=["安全事件与应急闭环"])

EVACUATION_CONFIRM_TIMEOUT_MINUTES = 5


def log_safety_action(
    db: Session,
    safety_event_id: int,
    action_type: SafetyActionType,
    description: str,
    actor: Optional[User] = None,
    detail: Optional[Dict[str, Any]] = None
):
    try:
        log = SafetyEventActionLog(
            safety_event_id=safety_event_id,
            action_type=action_type,
            actor_user_id=actor.id if actor else None,
            actor_name=actor.full_name if actor else None,
            description=description,
            detail_json=json.dumps(detail, ensure_ascii=False) if detail else None
        )
        db.add(log)
        db.commit()
    except Exception as e:
        db.rollback()


def _check_and_resolve_event(db: Session, event: SafetyEvent, actor: Optional[User] = None):
    if not event:
        return
    all_resolved = True
    if event.related_alert_id:
        alert = db.query(Alert).filter(Alert.id == event.related_alert_id).first()
        if alert and not alert.is_resolved:
            all_resolved = False
    if event.related_evacuation_id:
        evac = db.query(EvacuationOrder).filter(EvacuationOrder.id == event.related_evacuation_id).first()
        if evac and evac.is_active:
            all_resolved = False
    if event.related_work_order_id:
        wo = db.query(WorkOrder).filter(WorkOrder.id == event.related_work_order_id).first()
        if wo and wo.status != WorkOrderStatus.COMPLETED:
            all_resolved = False
    if event.ventilation_active:
        latest = db.query(EnvironmentMonitor).filter(
            EnvironmentMonitor.area == event.area
        ).order_by(EnvironmentMonitor.collected_at.desc()).first()
        if latest and latest.ventilation_active:
            all_resolved = False
    if all_resolved and event.status != SafetyEventStatus.RESOLVED:
        event.status = SafetyEventStatus.RESOLVED
        event.resolved_at = datetime.utcnow()
        db.commit()
        db.refresh(event)
        log_safety_action(db, event.id, SafetyActionType.EVENT_RESOLVED,
                          "事件所有处置项完成，自动标记为已处置", actor)


def _get_event_current_stage(event: SafetyEvent, db: Session) -> Dict[str, Any]:
    stages = []
    if event.related_alert_id:
        alert = db.query(Alert).filter(Alert.id == event.related_alert_id).first()
        stages.append({"name": "告警触发", "done": True, "time": event.created_at})
    if event.ventilation_active:
        stages.append({"name": "启动通风", "done": True})
    if event.related_evacuation_id:
        evac = db.query(EvacuationOrder).filter(EvacuationOrder.id == event.related_evacuation_id).first()
        if evac:
            stages.append({"name": "下发撤离指令", "done": True, "time": evac.created_at})
            total_conf = db.query(EvacuationConfirmation).filter(
                EvacuationConfirmation.evacuation_id == evac.id
            ).count()
            confirmed = db.query(EvacuationConfirmation).filter(
                EvacuationConfirmation.evacuation_id == evac.id,
                EvacuationConfirmation.confirmed == True
            ).count()
            stages.append({
                "name": "人员撤离确认",
                "done": confirmed >= total_conf and total_conf > 0,
                "progress": f"{confirmed}/{total_conf}"
            })
            if not evac.is_active:
                stages.append({"name": "撤离解除", "done": True, "time": evac.cancelled_at})
    if event.related_work_order_id:
        wo = db.query(WorkOrder).filter(WorkOrder.id == event.related_work_order_id).first()
        if wo:
            stages.append({"name": "生成检修工单", "done": True})
            if wo.status == WorkOrderStatus.ASSIGNED:
                stages.append({"name": "工单派工", "done": True, "stuck": True})
            elif wo.status == WorkOrderStatus.IN_PROGRESS:
                stages.append({"name": "维修中", "done": False, "stuck": True})
            elif wo.status == WorkOrderStatus.COMPLETED:
                stages.append({"name": "工单完成", "done": True, "time": wo.completed_at})
    current = "已处置" if event.status == SafetyEventStatus.RESOLVED else None
    if not current:
        for s in reversed(stages):
            if not s.get("done", True) or s.get("stuck"):
                current = s["name"]
                break
        if not current:
            current = "处置中"
    return {"stages": stages, "current_stage": current}


STAGE_ACTION_MAP = {
    "告警": [SafetyActionType.ALERT_TRIGGERED],
    "通风处置": [SafetyActionType.VENTILATION_STARTED],
    "撤离": [SafetyActionType.EVACUATION_ISSUED],
    "撤离确认": [SafetyActionType.EVACUATION_CONFIRMED, SafetyActionType.EVACUATION_TIMEOUT],
    "维修处置": [
        SafetyActionType.WORK_ORDER_CREATED, SafetyActionType.WORK_ORDER_ASSIGNED,
        SafetyActionType.WORK_ORDER_ACCEPTED, SafetyActionType.WORK_ORDER_REJECTED,
        SafetyActionType.WORK_ORDER_REASSIGNED, SafetyActionType.WORK_ORDER_COMPLETED
    ],
    "撤离解除": [SafetyActionType.EVACUATION_CANCELLED],
    "事件完结": [SafetyActionType.EVENT_RESOLVED]
}


def _build_event_review(db: Session, event: SafetyEvent) -> Dict[str, Any]:
    logs = db.query(SafetyEventActionLog).filter(
        SafetyEventActionLog.safety_event_id == event.id
    ).order_by(SafetyEventActionLog.created_at.asc()).all()

    stage_groups = {}
    for stage_key, action_types in STAGE_ACTION_MAP.items():
        stage_groups[stage_key] = {
            "stage": stage_key,
            "actions": [],
            "start_time": None,
            "end_time": None,
            "duration_seconds": None,
            "actors": [],
            "is_blocked": False,
            "pending_items": []
        }

    for log in logs:
        matched = False
        for stage_key, action_types in STAGE_ACTION_MAP.items():
            if log.action_type in action_types:
                detail = None
                if log.detail_json:
                    try:
                        detail = json.loads(log.detail_json)
                    except Exception:
                        detail = None
                action = {
                    "id": log.id,
                    "action_type": log.action_type.value if hasattr(log.action_type, 'value') else str(log.action_type),
                    "description": log.description,
                    "actor_id": log.actor_user_id,
                    "actor_name": log.actor_name,
                    "time": log.created_at,
                    "detail": detail
                }
                stage_groups[stage_key]["actions"].append(action)
                if log.actor_name and log.actor_name not in stage_groups[stage_key]["actors"]:
                    stage_groups[stage_key]["actors"].append(log.actor_name)
                matched = True
                break
        if not matched and log.action_type == SafetyActionType.NOTE:
            pass

    total_start = logs[0].created_at if logs else None
    total_end = logs[-1].created_at if logs else None
    overall_duration = None
    if total_start and total_end:
        overall_duration = int((total_end - total_start).total_seconds())

    stage_info = _get_event_current_stage(event, db)
    blocked_stages = [s["name"] for s in stage_info["stages"] if s.get("stuck") or (not s.get("done", True))]

    pending_items = []
    for s in stage_info["stages"]:
        if not s.get("done", True):
            pending_items.append(s["name"] + (f" {s.get('progress', '')}" if s.get("progress") else ""))

    result = {
        "stages": [],
        "total_duration_seconds": overall_duration,
        "start_time": total_start,
        "end_time": total_end,
        "blocked_stages": blocked_stages,
        "pending_items": pending_items,
        "bottleneck_stage": None,
        "bottleneck_reason": None
    }

    max_duration = -1
    for stage_key, sg in stage_groups.items():
        if sg["actions"]:
            sg["start_time"] = sg["actions"][0]["time"]
            sg["end_time"] = sg["actions"][-1]["time"]
            sg["duration_seconds"] = int((sg["end_time"] - sg["start_time"]).total_seconds())
            if sg["duration_seconds"] > max_duration:
                max_duration = sg["duration_seconds"]
                result["bottleneck_stage"] = sg["stage"]
                result["bottleneck_reason"] = f"{sg['stage']} 阶段耗时 {sg['duration_seconds']} 秒，为全流程最长"
            if sg["stage"] in blocked_stages:
                sg["is_blocked"] = True
                sg["pending_items"] = [f"{sg['stage']} 未完成"]
        result["stages"].append(sg)

    return result


@router.get("/events")
def list_safety_events(
    status: Optional[SafetyEventStatus] = None,
    area: Optional[str] = None,
    level: Optional[AlertLevel] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(SafetyEvent)
    if status:
        query = query.filter(SafetyEvent.status == status)
    if area:
        query = query.filter(SafetyEvent.area == area)
    if level:
        query = query.filter(SafetyEvent.level == level)
    events = query.order_by(SafetyEvent.created_at.desc()).limit(limit).all()
    result = []
    for e in events:
        d = {
            "id": e.id,
            "title": e.title,
            "description": e.description,
            "level": e.level.value if hasattr(e.level, 'value') else str(e.level),
            "source_type": e.source_type,
            "area": e.area,
            "status": e.status.value if hasattr(e.status, 'value') else str(e.status),
            "related_alert_id": e.related_alert_id,
            "related_evacuation_id": e.related_evacuation_id,
            "related_work_order_id": e.related_work_order_id,
            "ventilation_active": e.ventilation_active,
            "created_at": e.created_at,
            "resolved_at": e.resolved_at
        }
        stage_info = _get_event_current_stage(e, db)
        d["current_stage"] = stage_info["current_stage"]
        result.append(d)
    return result


@router.get("/events/{event_id}")
def get_safety_event_detail(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    event = db.query(SafetyEvent).filter(SafetyEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="安全事件不存在")

    _check_and_resolve_event(db, event)
    db.refresh(event)

    detail = {
        "id": event.id,
        "title": event.title,
        "description": event.description,
        "level": event.level.value,
        "source_type": event.source_type,
        "area": event.area,
        "status": event.status.value,
        "ventilation_active": event.ventilation_active,
        "created_at": event.created_at,
        "resolved_at": event.resolved_at,
        "alert": None,
        "evacuation": None,
        "work_order": None,
        "unconfirmed_users": []
    }

    stage_info = _get_event_current_stage(event, db)
    detail["stages"] = stage_info["stages"]
    detail["current_stage"] = stage_info["current_stage"]

    if event.related_alert_id:
        alert = db.query(Alert).filter(Alert.id == event.related_alert_id).first()
        if alert:
            detail["alert"] = {
                "id": alert.id,
                "title": alert.title,
                "content": alert.content,
                "is_resolved": alert.is_resolved
            }

    if event.related_evacuation_id:
        evac = db.query(EvacuationOrder).filter(EvacuationOrder.id == event.related_evacuation_id).first()
        if evac:
            detail["evacuation"] = {
                "id": evac.id,
                "area": evac.area,
                "reason": evac.reason,
                "is_active": evac.is_active,
                "alert_level": evac.alert_level.value
            }
            confirmations = db.query(EvacuationConfirmation).filter(
                EvacuationConfirmation.evacuation_id == evac.id
            ).all()
            unconfirmed = []
            for c in confirmations:
                if not c.confirmed:
                    u = db.query(User).filter(User.id == c.user_id).first()
                    if u:
                        unconfirmed.append({
                            "confirmation_id": c.id,
                            "user_id": u.id,
                            "name": u.full_name,
                            "phone": u.phone,
                            "area": c.area,
                            "area_before_confirm": c.area_before_confirm,
                            "confirm_location": c.confirm_location,
                            "confirm_method": c.confirm_method,
                            "confirmed": c.confirmed,
                            "confirmed_at": c.confirmed_at,
                            "reminder_sent": c.reminder_sent,
                            "reminder_sent_at": c.reminder_sent_at,
                            "reminder_message_id": c.reminder_message_id,
                            "created_at": c.created_at
                        })
            detail["unconfirmed_users"] = unconfirmed

    if event.related_work_order_id:
        wo = db.query(WorkOrder).filter(WorkOrder.id == event.related_work_order_id).first()
        if wo:
            worker = db.query(User).filter(User.id == wo.assigned_to).first() if wo.assigned_to else None
            detail["work_order"] = {
                "id": wo.id,
                "title": wo.title,
                "description": wo.description,
                "status": wo.status.value,
                "priority": wo.priority,
                "assigned_to": wo.assigned_to,
                "assigned_to_name": worker.full_name if worker else None
            }

    detail["review"] = _build_event_review(db, event)

    return detail


@router.get("/events/{event_id}/timeline")
def get_safety_event_timeline(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    event = db.query(SafetyEvent).filter(SafetyEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="安全事件不存在")

    logs = db.query(SafetyEventActionLog).filter(
        SafetyEventActionLog.safety_event_id == event_id
    ).order_by(SafetyEventActionLog.created_at.asc()).all()

    timeline = []
    for log in logs:
        detail_obj = None
        if log.detail_json:
            try:
                detail_obj = json.loads(log.detail_json)
            except Exception:
                detail_obj = None
        timeline.append({
            "id": log.id,
            "action_type": log.action_type.value if hasattr(log.action_type, 'value') else str(log.action_type),
            "actor_user_id": log.actor_user_id,
            "actor_name": log.actor_name,
            "description": log.description,
            "detail": detail_obj,
            "time": log.created_at
        })

    return {
        "event_id": event_id,
        "title": event.title,
        "timeline": timeline
    }


@router.post("/events/{event_id}/timeline")
def add_event_timeline_note(
    event_id: int,
    description: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    event = db.query(SafetyEvent).filter(SafetyEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="安全事件不存在")
    log_safety_action(db, event_id, SafetyActionType.NOTE, description, current_user)
    return {"message": "备注已添加到时间轴"}


@router.post("/events/{event_id}/resolve")
async def resolve_safety_event(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    event = db.query(SafetyEvent).filter(SafetyEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="安全事件不存在")
    event.status = SafetyEventStatus.RESOLVED
    event.resolved_at = datetime.utcnow()
    db.commit()
    db.refresh(event)
    log_safety_action(db, event_id, SafetyActionType.EVENT_RESOLVED,
                      f"人工标记事件已处置", current_user)

    await push_system_notification(
        db,
        f"安全事件已处置: {event.title}",
        f"区域[{event.area}]事件已由{current_user.full_name}处置完成"
    )
    return {"message": "事件已标记为已处置", "event_id": event.id}


@router.post("/evacuations/{evac_id}/init-confirmations")
async def init_evacuation_confirmations(
    evac_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    evac = db.query(EvacuationOrder).filter(EvacuationOrder.id == evac_id).first()
    if not evac:
        raise HTTPException(status_code=404, detail="撤离指令不存在")

    existing = db.query(EvacuationConfirmation).filter(
        EvacuationConfirmation.evacuation_id == evac_id
    ).first()
    if existing:
        return {"message": "确认清单已初始化", "evacuation_id": evac_id}

    evac_area = evac.area
    miners = db.query(User).filter(
        User.role == UserRole.MINER,
        User.is_active == True
    ).all()

    from routers.equipment import ADJACENT_AREAS
    adjacent_areas = ADJACENT_AREAS.get(evac_area, [])
    target_areas = {evac_area} | set(adjacent_areas) if evac_area else None

    created = 0
    for m in miners:
        latest_loc = db.query(MinerLocationReport).filter(
            MinerLocationReport.user_id == m.id
        ).order_by(MinerLocationReport.reported_at.desc()).first()
        user_area = latest_loc.area if latest_loc else getattr(m, 'location_tag_id', None)

        include = False
        if not evac_area or not user_area:
            include = True
        elif target_areas and user_area in target_areas:
            include = True
        if include:
            conf = EvacuationConfirmation(
                evacuation_id=evac_id,
                user_id=m.id,
                area=user_area or evac_area,
                confirmed=False
            )
            db.add(conf)
            created += 1

    db.commit()
    return {"message": f"已为{created}名矿工生成撤离确认清单（区域:{evac_area}）", "evacuation_id": evac_id}


@router.get("/evacuations/{evac_id}/unconfirmed")
def get_unconfirmed_users(
    evac_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    evac = db.query(EvacuationOrder).filter(EvacuationOrder.id == evac_id).first()
    if not evac:
        raise HTTPException(status_code=404, detail="撤离指令不存在")

    confirmations = db.query(EvacuationConfirmation).filter(
        EvacuationConfirmation.evacuation_id == evac_id,
        EvacuationConfirmation.confirmed == False
    ).all()

    result = []
    for c in confirmations:
        user = db.query(User).filter(User.id == c.user_id).first()
        if user:
            timeout = (datetime.utcnow() - c.created_at) > timedelta(minutes=EVACUATION_CONFIRM_TIMEOUT_MINUTES)
            result.append({
                "confirmation_id": c.id,
                "user_id": user.id,
                "user_name": user.full_name,
                "phone": user.phone,
                "area": c.area or evac.area,
                "created_at": c.created_at,
                "reminder_sent": c.reminder_sent,
                "reminder_sent_at": c.reminder_sent_at,
                "reminder_message_id": c.reminder_message_id,
                "is_timeout": timeout
            })
    return result


@router.post("/evacuations/{evac_id}/confirm")
async def confirm_evacuation(
    evac_id: int,
    confirm_location: Optional[str] = None,
    confirm_method: str = "terminal",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    latest_loc = db.query(MinerLocationReport).filter(
        MinerLocationReport.user_id == current_user.id
    ).order_by(MinerLocationReport.reported_at.desc()).first()
    current_area = latest_loc.area if latest_loc else None

    conf = db.query(EvacuationConfirmation).filter(
        EvacuationConfirmation.evacuation_id == evac_id,
        EvacuationConfirmation.user_id == current_user.id
    ).first()
    if not conf:
        conf = EvacuationConfirmation(
            evacuation_id=evac_id,
            user_id=current_user.id,
            area_before_confirm=current_area,
            confirmed=True,
            confirmed_at=datetime.utcnow(),
            confirm_location=confirm_location,
            confirm_method=confirm_method
        )
        db.add(conf)
    else:
        conf.area_before_confirm = conf.area_before_confirm or current_area
        conf.confirmed = True
        conf.confirmed_at = datetime.utcnow()
        conf.confirm_location = confirm_location
        conf.confirm_method = confirm_method
    db.commit()
    db.refresh(conf)

    event = db.query(SafetyEvent).filter(
        SafetyEvent.related_evacuation_id == evac_id
    ).first()
    if event:
        log_safety_action(db, event.id, SafetyActionType.EVACUATION_CONFIRMED,
                          f"{current_user.full_name} 确认撤离（{confirm_method}）",
                          current_user, {"location": confirm_location, "method": confirm_method})

    return {
        "message": "撤离确认成功",
        "confirmed_at": conf.confirmed_at,
        "confirm_location": conf.confirm_location,
        "confirm_method": conf.confirm_method
    }


@router.post("/evacuations/{evac_id}/check-timeout")
async def check_evacuation_timeout(
    evac_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    unconfirmed = db.query(EvacuationConfirmation).filter(
        EvacuationConfirmation.evacuation_id == evac_id,
        EvacuationConfirmation.confirmed == False,
        EvacuationConfirmation.reminder_sent == False
    ).all()

    timeout_users = []
    message_ids = []
    for c in unconfirmed:
        if (datetime.utcnow() - c.created_at) > timedelta(minutes=EVACUATION_CONFIRM_TIMEOUT_MINUTES):
            user = db.query(User).filter(User.id == c.user_id).first()
            if user:
                c.reminder_sent = True
                c.reminder_sent_at = datetime.utcnow()

                title = f"撤离超时提醒: {user.full_name} 未确认撤离"
                content = f"矿工[{user.full_name}]在{c.area or '未知区域'}已超过{EVACUATION_CONFIRM_TIMEOUT_MINUTES}分钟未确认撤离，请安保人员现场确认"
                msg = PushMessage(
                    message_type=MessageType.EVACUATION,
                    title=title,
                    content=content,
                    target_roles=",".join([UserRole.SECURITY.value, UserRole.DISPATCHER.value])
                )
                db.add(msg)
                db.flush()
                c.reminder_message_id = msg.id
                message_ids.append(msg.id)
                timeout_users.append(user.full_name)

    db.commit()

    event = db.query(SafetyEvent).filter(
        SafetyEvent.related_evacuation_id == evac_id
    ).first()
    if event and timeout_users:
        log_safety_action(db, event.id, SafetyActionType.EVACUATION_TIMEOUT,
                          f"以下矿工超时未确认撤离: {', '.join(timeout_users)}",
                          current_user,
                          {"timeout_users": timeout_users, "message_ids": message_ids})

    if timeout_users:
        await push_system_notification(
            db,
            f"撤离超时提醒: {len(timeout_users)}人未确认",
            f"以下矿工超时未确认撤离: {', '.join(timeout_users)}",
            [UserRole.SECURITY.value, UserRole.DISPATCHER.value]
        )

    return {
        "message": f"已检查，{len(timeout_users)}人超时未确认，已发送提醒",
        "timeout_users": timeout_users,
        "message_ids": message_ids
    }
