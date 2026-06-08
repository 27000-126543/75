from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any, Tuple
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
from config import settings
from models import (
    MiningEquipment, EquipmentData, WorkOrder,
    WorkOrderStatus, User, UserRole, Alert, AlertLevel,
    MaintenanceWorkerState, WorkerStatus, SafetyEvent, SafetyEventStatus,
    EvacuationOrder, WorkOrderReassignment, SafetyActionType, ReassignmentType
)
from routers.safety import log_safety_action
from schemas import (
    MiningEquipmentCreate, MiningEquipmentResponse,
    EquipmentDataCreate, EquipmentDataResponse,
    WorkOrderAssign
)
from routers.auth import get_current_user
from services.notification_service import (
    push_work_order_notification, push_alert_notification, push_system_notification
)

router = APIRouter(prefix="/equipment", tags=["设备监测与检修"])

WORK_ORDER_ACCEPT_TIMEOUT_MINUTES = 10

ADJACENT_AREAS = {
    "A采区": ["B采区", "主巷道"],
    "B采区": ["A采区", "C采区", "主巷道"],
    "C采区": ["B采区", "主巷道"],
    "主巷道": ["A采区", "B采区", "C采区", "主井车场"]
}


def _get_worker_current_area(db: Session, worker: User) -> Optional[str]:
    state = db.query(MaintenanceWorkerState).filter(
        MaintenanceWorkerState.user_id == worker.id
    ).first()
    if state and state.current_area:
        return state.current_area
    return worker.location_tag_id


def _is_worker_available(db: Session, worker: User) -> bool:
    state = db.query(MaintenanceWorkerState).filter(
        MaintenanceWorkerState.user_id == worker.id
    ).first()
    if not state:
        return True
    return state.status != WorkerStatus.OFFLINE


def _area_distance_score(worker_location: Optional[str], equipment_area: Optional[str]) -> int:
    if not worker_location or not equipment_area:
        return 0
    if worker_location == equipment_area:
        return 3
    adjacent = ADJACENT_AREAS.get(equipment_area, [])
    if worker_location in adjacent:
        return 2
    return 0


def _skill_match_score(worker: User, equipment_type: Optional[str]) -> int:
    if not worker.skills or not equipment_type:
        return 0
    et = equipment_type.lower()
    ws = worker.skills.lower()
    if et in ws or ws in et:
        return 3
    worker_skills = [s.strip() for s in ws.split(",")]
    keywords_map = {
        "采掘": ["采掘", "综采", "掘进", "采矿"],
        "通风": ["通风", "风机"],
        "运输": ["运输", "车辆", "皮带", "输送"],
        "液压": ["液压", "支护", "支架"],
        "电气": ["电气", "电控", "电力"]
    }
    for skill in worker_skills:
        if not skill:
            continue
        if skill in et or et in skill:
            return 3
        for kw, aliases in keywords_map.items():
            if kw in skill and any(a in et for a in aliases):
                return 2
            if kw in et and any(a in skill for a in aliases):
                return 2
    return 0


def _workload_score(db: Session, worker_id: int) -> int:
    active_orders = db.query(WorkOrder).filter(
        WorkOrder.assigned_to == worker_id,
        WorkOrder.status.in_([WorkOrderStatus.PENDING, WorkOrderStatus.ASSIGNED, WorkOrderStatus.IN_PROGRESS])
    ).count()
    if active_orders == 0:
        return 3
    elif active_orders == 1:
        return 2
    elif active_orders == 2:
        return 1
    return 0


def _worker_status_score(db: Session, worker: User) -> int:
    state = db.query(MaintenanceWorkerState).filter(
        MaintenanceWorkerState.user_id == worker.id
    ).first()
    if not state or state.status == WorkerStatus.IDLE:
        return 3
    if state.status == WorkerStatus.BUSY:
        return 1
    return 0


def _eta_score(db: Session, worker: User) -> Tuple[int, int]:
    state = db.query(MaintenanceWorkerState).filter(
        MaintenanceWorkerState.user_id == worker.id
    ).first()
    eta = state.eta_minutes if state and state.eta_minutes else 0
    if eta <= 5:
        return 3, eta
    elif eta <= 15:
        return 2, eta
    elif eta <= 30:
        return 1, eta
    return 0, eta


def _find_best_maintenance_workers(
    db: Session,
    equipment_type: Optional[str],
    equipment_area: Optional[str],
    exclude_worker_ids: Optional[List[int]] = None
) -> List[Tuple[int, int, int, int, int, int, int, User]]:
    exclude = exclude_worker_ids or []
    workers = db.query(User).filter(
        User.role == UserRole.MAINTENANCE,
        User.is_active == True,
        User.id.notin_(exclude)
    ).all()
    if not workers:
        return []

    scored_workers = []
    for worker in workers:
        if not _is_worker_available(db, worker):
            continue
        worker_area = _get_worker_current_area(db, worker)
        skill_score = _skill_match_score(worker, equipment_type)
        area_score = _area_distance_score(worker_area, equipment_area)
        workload_score = _workload_score(db, worker.id)
        status_score = _worker_status_score(db, worker)
        eta_score, eta_min = _eta_score(db, worker)
        # 技能10 + 区域5 + 忙闲6 + ETA3 + 工单负荷1
        total_score = skill_score * 10 + area_score * 5 + status_score * 6 + eta_score * 3 + workload_score
        scored_workers.append((total_score, skill_score, area_score, status_score, eta_score, eta_min, workload_score, worker))

    scored_workers.sort(key=lambda x: (-x[0], -x[1], -x[2], -x[3], -x[4], -x[6]))
    return scored_workers


def _build_assignment_rationale(
    db: Session,
    equipment_type: Optional[str],
    equipment_area: Optional[str],
    top_workers: List[Tuple[int, int, int, int, int, int, int, User]],
    max_show: int = 3
) -> List[Dict[str, Any]]:
    rationale = []
    for total, ss, as_, sts, ets, eta, wl, worker in top_workers[:max_show]:
        worker_area = _get_worker_current_area(db, worker)
        state = db.query(MaintenanceWorkerState).filter(
            MaintenanceWorkerState.user_id == worker.id
        ).first()
        status_label = "离线"
        if not state or state.status == WorkerStatus.IDLE:
            status_label = "空闲"
        elif state.status == WorkerStatus.BUSY:
            status_label = "忙碌"
        active_orders = db.query(WorkOrder).filter(
            WorkOrder.assigned_to == worker.id,
            WorkOrder.status.in_([WorkOrderStatus.PENDING, WorkOrderStatus.ASSIGNED, WorkOrderStatus.IN_PROGRESS])
        ).count()
        rationale.append({
            "worker_id": worker.id,
            "worker_name": worker.full_name,
            "skills": worker.skills,
            "current_area": worker_area,
            "status": status_label,
            "eta_minutes": eta,
            "active_work_orders": active_orders,
            "scores": {
                "skill_match": ss,
                "area_distance": as_,
                "worker_status": sts,
                "eta": ets,
                "workload": wl,
                "total_weighted": total
            },
            "score_detail": (
                f"技能匹配×10={ss * 10} + 区域距离×5={as_ * 5} + "
                f"忙闲状态×6={sts * 6} + ETA×3={ets * 3} + 工单负荷={wl} = {total}"
            )
        })
    return rationale


def _work_order_to_dict(order: WorkOrder, db: Session, include_rationale: bool = False) -> Dict[str, Any]:
    assigned_user = None
    if order.assigned_to:
        assigned_user = db.query(User).filter(User.id == order.assigned_to).first()
    reassignments = db.query(WorkOrderReassignment).filter(
        WorkOrderReassignment.work_order_id == order.id
    ).order_by(WorkOrderReassignment.created_at.asc()).all()
    reassignment_history = []
    for r in reassignments:
        rationale = None
        if r.rationale_json:
            try:
                import json
                rationale = json.loads(r.rationale_json)
            except Exception:
                rationale = None
        reassignment_history.append({
            "id": r.id,
            "reassignment_type": r.reassignment_type.value if hasattr(r.reassignment_type, 'value') else str(r.reassignment_type),
            "from_user_id": r.from_user_id,
            "from_user_name": r.from_user_name,
            "to_user_id": r.to_user_id,
            "to_user_name": r.to_user_name,
            "reason": r.reason,
            "operator_user_id": r.operator_user_id,
            "operator_name": r.operator_name,
            "rationale": rationale,
            "created_at": r.created_at
        })
    d = {
        "id": order.id,
        "title": order.title,
        "description": order.description,
        "equipment_id": order.equipment_id,
        "assigned_to": order.assigned_to,
        "assigned_to_name": assigned_user.full_name if assigned_user else None,
        "assigned_to_skills": assigned_user.skills if assigned_user else None,
        "status": order.status.value if hasattr(order.status, 'value') else str(order.status),
        "priority": order.priority,
        "rejected_by": order.rejected_by if hasattr(order, 'rejected_by') else None,
        "reassigned_count": order.reassigned_count if hasattr(order, 'reassigned_count') else 0,
        "reassignment_history": reassignment_history,
        "assigned_at": order.assigned_at if hasattr(order, 'assigned_at') else None,
        "accepted_at": order.accepted_at if hasattr(order, 'accepted_at') else None,
        "created_at": order.created_at,
        "completed_at": order.completed_at
    }
    if include_rationale and order.equipment_id:
        equip = db.query(MiningEquipment).filter(MiningEquipment.id == order.equipment_id).first()
        if equip:
            scored = _find_best_maintenance_workers(db, equip.type, equip.location_area)
            d["assignment_rationale"] = _build_assignment_rationale(db, equip.type, equip.location_area, scored)
    return d


def _resolve_safety_event_for_work_order(db: Session, work_order_id: int):
    event = db.query(SafetyEvent).filter(
        SafetyEvent.related_work_order_id == work_order_id
    ).first()
    if not event:
        return
    if event.related_alert_id:
        alert = db.query(Alert).filter(Alert.id == event.related_alert_id).first()
        if alert and not alert.is_resolved:
            alert.is_resolved = True
            alert.resolved_at = datetime.utcnow()
    all_resolved = True
    if event.related_evacuation_id:
        evac = db.query(EvacuationOrder).filter(EvacuationOrder.id == event.related_evacuation_id).first()
        if evac and evac.is_active:
            all_resolved = False
    if all_resolved:
        event.status = SafetyEventStatus.RESOLVED
        event.resolved_at = datetime.utcnow()
    db.commit()


@router.post("/equipments", response_model=MiningEquipmentResponse, status_code=201)
def create_equipment(
    data: MiningEquipmentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    existing = db.query(MiningEquipment).filter(MiningEquipment.code == data.code).first()
    if existing:
        raise HTTPException(status_code=400, detail="设备编号已存在")
    equip = MiningEquipment(**data.model_dump())
    db.add(equip)
    db.commit()
    db.refresh(equip)
    return equip


@router.get("/equipments", response_model=List[MiningEquipmentResponse])
def list_equipments(
    location_area: Optional[str] = None,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(MiningEquipment)
    if location_area:
        query = query.filter(MiningEquipment.location_area == location_area)
    if status:
        query = query.filter(MiningEquipment.status == status)
    return query.all()


@router.get("/equipments/{equipment_id}", response_model=MiningEquipmentResponse)
def get_equipment(
    equipment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    equip = db.query(MiningEquipment).filter(MiningEquipment.id == equipment_id).first()
    if not equip:
        raise HTTPException(status_code=404, detail="设备不存在")
    return equip


@router.post("/data")
async def upload_equipment_data(
    data: EquipmentDataCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    equip = db.query(MiningEquipment).filter(MiningEquipment.id == data.equipment_id).first()
    if not equip:
        raise HTTPException(status_code=404, detail="设备不存在")

    is_abnormal = (
        data.vibration > settings.VIBRATION_THRESHOLD
        or data.temperature > settings.TEMPERATURE_THRESHOLD
    )

    equip_data = EquipmentData(
        equipment_id=data.equipment_id,
        vibration=data.vibration,
        temperature=data.temperature,
        is_abnormal=is_abnormal
    )
    db.add(equip_data)
    db.flush()

    result = {
        "id": None,
        "equipment_id": data.equipment_id,
        "vibration": data.vibration,
        "temperature": data.temperature,
        "is_abnormal": is_abnormal,
        "work_order_id": None,
        "safety_event_id": None,
        "assignment_rationale": []
    }

    if is_abnormal:
        equip.status = "abnormal"
        abnormal_reasons = []
        if data.vibration > settings.VIBRATION_THRESHOLD:
            abnormal_reasons.append(f"振动值过高({data.vibration}mm/s)")
        if data.temperature > settings.TEMPERATURE_THRESHOLD:
            abnormal_reasons.append(f"温度过高({data.temperature}°C)")

        existing_order = db.query(WorkOrder).filter(
            WorkOrder.equipment_id == data.equipment_id,
            WorkOrder.status.in_([WorkOrderStatus.PENDING, WorkOrderStatus.ASSIGNED, WorkOrderStatus.IN_PROGRESS])
        ).first()

        if not existing_order:
            scored_workers = _find_best_maintenance_workers(db, equip.type, equip.location_area)
            top_worker = scored_workers[0][7] if scored_workers else None
            priority = "high" if data.temperature > settings.TEMPERATURE_THRESHOLD else "medium"

            work_order = WorkOrder(
                title=f"{equip.name}异常检修",
                description="; ".join(abnormal_reasons),
                equipment_id=data.equipment_id,
                assigned_to=top_worker.id if top_worker else None,
                status=WorkOrderStatus.ASSIGNED if top_worker else WorkOrderStatus.PENDING,
                priority=priority,
                abnormal_data_id=equip_data.id,
                assigned_at=datetime.utcnow() if top_worker else None
            )
            db.add(work_order)
            db.flush()

            alert = Alert(
                title=f"{equip.name}运行异常",
                content="; ".join(abnormal_reasons),
                level=AlertLevel.WARNING,
                source_type="equipment",
                source_id=data.equipment_id,
                area=equip.location_area
            )
            db.add(alert)
            db.flush()

            event = db.query(SafetyEvent).filter(
                SafetyEvent.area == equip.location_area,
                SafetyEvent.source_type == "equipment",
                SafetyEvent.status != SafetyEventStatus.RESOLVED
            ).first()
            if not event:
                event = SafetyEvent(
                    title=f"{equip.location_area}设备异常: {equip.name}",
                    description="; ".join(abnormal_reasons),
                    level=AlertLevel.WARNING,
                    source_type="equipment",
                    area=equip.location_area,
                    status=SafetyEventStatus.HANDLING,
                    related_alert_id=alert.id,
                    related_work_order_id=work_order.id
                )
                db.add(event)
            else:
                event.related_alert_id = alert.id
                event.related_work_order_id = work_order.id
                event.status = SafetyEventStatus.HANDLING
                event.description = "; ".join(abnormal_reasons)

            db.commit()
            db.refresh(work_order)
            db.refresh(alert)
            db.refresh(event)
            db.refresh(equip_data)

            log_safety_action(db, event.id, SafetyActionType.WORK_ORDER_CREATED,
                              f"生成检修工单#{work_order.id}")
            if top_worker:
                log_safety_action(db, event.id, SafetyActionType.WORK_ORDER_ASSIGNED,
                                  f"工单派给 {top_worker.full_name}",
                                  top_worker,
                                  {"worker_id": top_worker.id, "worker_skills": top_worker.skills})

            result["id"] = equip_data.id
            result["work_order_id"] = work_order.id
            result["safety_event_id"] = event.id
            result["assignment_rationale"] = _build_assignment_rationale(
                db, equip.type, equip.location_area, scored_workers
            )

            await push_work_order_notification(db, work_order, top_worker)
            await push_alert_notification(db, alert)
        else:
            db.commit()
            db.refresh(equip_data)
            result["id"] = equip_data.id
            result["work_order_id"] = existing_order.id
    else:
        if equip.status != "normal":
            equip.status = "normal"
        db.commit()
        db.refresh(equip_data)
        result["id"] = equip_data.id

    return result


@router.get("/data", response_model=List[EquipmentDataResponse])
def list_equipment_data(
    equipment_id: Optional[int] = None,
    abnormal_only: bool = False,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(EquipmentData)
    if equipment_id:
        query = query.filter(EquipmentData.equipment_id == equipment_id)
    if abnormal_only:
        query = query.filter(EquipmentData.is_abnormal == True)
    return query.order_by(EquipmentData.collected_at.desc()).limit(limit).all()


@router.get("/work-orders")
def list_work_orders(
    status: Optional[WorkOrderStatus] = None,
    assigned_to: Optional[int] = None,
    equipment_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(WorkOrder)
    if status:
        query = query.filter(WorkOrder.status == status)
    if assigned_to:
        query = query.filter(WorkOrder.assigned_to == assigned_to)
    if equipment_id:
        query = query.filter(WorkOrder.equipment_id == equipment_id)
    orders = query.order_by(WorkOrder.created_at.desc()).all()
    return [_work_order_to_dict(o, db) for o in orders]


@router.get("/work-orders/{order_id}")
def get_work_order(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    order = db.query(WorkOrder).filter(WorkOrder.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="工单不存在")
    return _work_order_to_dict(order, db)


@router.put("/work-orders/{order_id}/assign")
async def assign_work_order(
    order_id: int,
    data: WorkOrderAssign,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    order = db.query(WorkOrder).filter(WorkOrder.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="工单不存在")

    worker = db.query(User).filter(
        User.id == data.assigned_to,
        User.role == UserRole.MAINTENANCE
    ).first()
    if not worker:
        raise HTTPException(status_code=400, detail="无效的维修人员ID")

    order.assigned_to = data.assigned_to
    order.status = WorkOrderStatus.ASSIGNED
    db.commit()
    db.refresh(order)

    await push_work_order_notification(db, order, worker)
    return _work_order_to_dict(order, db)


@router.put("/work-orders/{order_id}/start")
def start_work_order(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    order = db.query(WorkOrder).filter(WorkOrder.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="工单不存在")
    if order.status not in [WorkOrderStatus.PENDING, WorkOrderStatus.ASSIGNED]:
        raise HTTPException(status_code=400, detail="当前状态无法开始")
    order.status = WorkOrderStatus.IN_PROGRESS
    db.commit()
    db.refresh(order)
    return _work_order_to_dict(order, db)


@router.put("/work-orders/{order_id}/complete")
def complete_work_order(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    order = db.query(WorkOrder).filter(WorkOrder.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="工单不存在")
    order.status = WorkOrderStatus.COMPLETED
    order.completed_at = datetime.utcnow()

    if order.equipment_id:
        equip = db.query(MiningEquipment).filter(MiningEquipment.id == order.equipment_id).first()
        if equip:
            equip.status = "normal"
            equip.last_maintenance = datetime.utcnow()

    db.commit()
    db.refresh(order)
    event = db.query(SafetyEvent).filter(SafetyEvent.related_work_order_id == order.id).first()
    if event:
        log_safety_action(db, event.id, SafetyActionType.WORK_ORDER_COMPLETED,
                          f"{current_user.full_name} 完成工单", current_user,
                          {"order_id": order.id, "completed_at": str(order.completed_at)})
    _resolve_safety_event_for_work_order(db, order.id)
    return _work_order_to_dict(order, db, include_rationale=True)


@router.post("/workers/state")
def update_worker_state(
    status: WorkerStatus,
    current_area: Optional[str] = None,
    eta_minutes: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != UserRole.MAINTENANCE:
        raise HTTPException(status_code=403, detail="仅维修工可上报状态")

    state = db.query(MaintenanceWorkerState).filter(
        MaintenanceWorkerState.user_id == current_user.id
    ).first()
    if not state:
        state = MaintenanceWorkerState(user_id=current_user.id)
        db.add(state)

    state.status = status
    state.current_area = current_area
    state.eta_minutes = eta_minutes
    state.last_updated = datetime.utcnow()
    db.commit()
    db.refresh(state)

    return {
        "user_id": current_user.id,
        "user_name": current_user.full_name,
        "status": state.status.value if hasattr(state.status, 'value') else str(state.status),
        "current_area": state.current_area,
        "eta_minutes": state.eta_minutes,
        "last_updated": state.last_updated
    }


@router.get("/workers/state")
def list_worker_states(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    workers = db.query(User).filter(
        User.role == UserRole.MAINTENANCE,
        User.is_active == True
    ).all()
    result = []
    for w in workers:
        state = db.query(MaintenanceWorkerState).filter(
            MaintenanceWorkerState.user_id == w.id
        ).first()
        result.append({
            "worker_id": w.id,
            "worker_name": w.full_name,
            "skills": w.skills,
            "status": state.status.value if state and hasattr(state.status, 'value') else (str(state.status) if state else "idle"),
            "current_area": state.current_area if state else w.location_tag_id,
            "eta_minutes": state.eta_minutes if state else 0,
            "last_updated": state.last_updated if state else None
        })
    return result


@router.put("/work-orders/{order_id}/accept")
async def accept_work_order(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    order = db.query(WorkOrder).filter(WorkOrder.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="工单不存在")
    if order.assigned_to != current_user.id:
        raise HTTPException(status_code=403, detail="此工单未派给您")
    if order.status != WorkOrderStatus.ASSIGNED:
        raise HTTPException(status_code=400, detail="当前状态无法接单")

    order.status = WorkOrderStatus.IN_PROGRESS
    order.accepted_at = datetime.utcnow()
    db.commit()
    db.refresh(order)

    event = db.query(SafetyEvent).filter(SafetyEvent.related_work_order_id == order.id).first()
    if event:
        log_safety_action(db, event.id, SafetyActionType.WORK_ORDER_ACCEPTED,
                          f"{current_user.full_name} 已接单", current_user)

    await push_system_notification(
        db,
        f"工单已接单: {order.title}",
        f"{current_user.full_name}已接单，开始处理"
    )
    return _work_order_to_dict(order, db)


@router.put("/work-orders/{order_id}/reject")
async def reject_work_order(
    order_id: int,
    reason: Optional[str] = "无法处理",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    order = db.query(WorkOrder).filter(WorkOrder.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="工单不存在")
    if order.assigned_to != current_user.id:
        raise HTTPException(status_code=403, detail="此工单未派给您")
    if order.status != WorkOrderStatus.ASSIGNED:
        raise HTTPException(status_code=400, detail="当前状态无法拒单")

    rejected_worker = db.query(User).filter(User.id == current_user.id).first()
    rejected_name = rejected_worker.full_name if rejected_worker else f"ID{current_user.id}"
    existing_rejected = order.rejected_by or ""
    new_rejected_by = f"{existing_rejected},{rejected_name}" if existing_rejected else rejected_name
    order.rejected_by = new_rejected_by
    order.reassigned_count = (order.reassigned_count or 0) + 1

    equip = db.query(MiningEquipment).filter(MiningEquipment.id == order.equipment_id).first()
    equip_type = equip.type if equip else None
    equip_area = equip.location_area if equip else None

    exclude_ids = [current_user.id]
    if order.rejected_by:
        for rn in order.rejected_by.split(","):
            u = db.query(User).filter(User.full_name == rn).first()
            if u:
                exclude_ids.append(u.id)

    event = db.query(SafetyEvent).filter(SafetyEvent.related_work_order_id == order.id).first()
    if event:
        log_safety_action(db, event.id, SafetyActionType.WORK_ORDER_REJECTED,
                          f"{rejected_name} 拒单，原因: {reason}", current_user,
                          {"reason": reason})

    next_workers = _find_best_maintenance_workers(db, equip_type, equip_area, exclude_ids)

    if not next_workers:
        order.status = WorkOrderStatus.PENDING
        order.assigned_to = None
        db.commit()
        db.refresh(order)
        await push_system_notification(
            db,
            f"工单派单失败: {order.title}",
            f"{rejected_name}拒单，且无其他合适维修工，需调度人工处理",
            [UserRole.DISPATCHER.value]
        )
        return _work_order_to_dict(order, db)

    next_worker = next_workers[0][7]
    rationale = _build_assignment_rationale(db, equip_type, equip_area, next_workers)
    import json
    reassignment = WorkOrderReassignment(
        work_order_id=order.id,
        reassignment_type=ReassignmentType.REJECT_AUTO,
        from_user_id=current_user.id,
        from_user_name=rejected_name,
        to_user_id=next_worker.id,
        to_user_name=next_worker.full_name,
        reason=f"拒单: {reason}",
        rationale_json=json.dumps(rationale, ensure_ascii=False)
    )
    db.add(reassignment)

    order.assigned_to = next_worker.id
    order.status = WorkOrderStatus.ASSIGNED
    order.assigned_at = datetime.utcnow()
    db.commit()
    db.refresh(order)

    if event:
        log_safety_action(db, event.id, SafetyActionType.WORK_ORDER_REASSIGNED,
                          f"自动改派给 {next_worker.full_name}",
                          None,
                          {"from": rejected_name, "to": next_worker.full_name, "reason": reason})

    await push_work_order_notification(db, order, next_worker)
    await push_system_notification(
        db,
        f"工单已改派: {order.title}",
        f"{rejected_name}拒单({reason})，已自动改派给{next_worker.full_name}"
    )

    result = _work_order_to_dict(order, db)
    result["assignment_rationale"] = rationale
    return result


@router.post("/work-orders/check-timeout")
async def check_work_order_timeout(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    timeout_orders = db.query(WorkOrder).filter(
        WorkOrder.status == WorkOrderStatus.ASSIGNED,
        WorkOrder.assigned_at.isnot(None)
    ).all()

    reassigned = []
    for order in timeout_orders:
        if order.assigned_at and (datetime.utcnow() - order.assigned_at) > timedelta(minutes=WORK_ORDER_ACCEPT_TIMEOUT_MINUTES):
            equip = db.query(MiningEquipment).filter(MiningEquipment.id == order.equipment_id).first()
            equip_type = equip.type if equip else None
            equip_area = equip.location_area if equip else None

            current_assignee = order.assigned_to
            exclude_ids = [current_assignee] if current_assignee else []
            if order.rejected_by:
                for rn in order.rejected_by.split(","):
                    u = db.query(User).filter(User.full_name == rn).first()
                    if u:
                        exclude_ids.append(u.id)

            next_workers = _find_best_maintenance_workers(db, equip_type, equip_area, exclude_ids)
            if next_workers:
                old_worker = db.query(User).filter(User.id == current_assignee).first() if current_assignee else None
                next_worker = next_workers[0][7]
                old_name = old_worker.full_name if old_worker else f"ID{current_assignee}"
                rationale = _build_assignment_rationale(db, equip_type, equip_area, next_workers)
                import json
                reassignment = WorkOrderReassignment(
                    work_order_id=order.id,
                    reassignment_type=ReassignmentType.TIMEOUT_AUTO,
                    from_user_id=current_assignee,
                    from_user_name=old_name,
                    to_user_id=next_worker.id,
                    to_user_name=next_worker.full_name,
                    reason=f"超时{WORK_ORDER_ACCEPT_TIMEOUT_MINUTES}分钟未接单",
                    rationale_json=json.dumps(rationale, ensure_ascii=False)
                )
                db.add(reassignment)

                order.assigned_to = next_worker.id
                order.reassigned_count = (order.reassigned_count or 0) + 1
                order.assigned_at = datetime.utcnow()
                order.rejected_by = f"{order.rejected_by},{old_name}(超时)" if order.rejected_by else f"{old_name}(超时)"
                db.commit()
                db.refresh(order)

                event = db.query(SafetyEvent).filter(SafetyEvent.related_work_order_id == order.id).first()
                if event:
                    log_safety_action(db, event.id, SafetyActionType.WORK_ORDER_REASSIGNED,
                                      f"超时未接单，自动改派给 {next_worker.full_name}",
                                      None,
                                      {"from": old_name, "to": next_worker.full_name, "reason": "超时未接单"})

                await push_work_order_notification(db, order, next_worker)
                reassigned.append({
                    "order_id": order.id,
                    "title": order.title,
                    "from": old_name,
                    "to": next_worker.full_name
                })

    return {
        "message": f"已检查{len(timeout_orders)}个已分配工单，{len(reassigned)}个因超时已自动改派",
        "reassigned": reassigned
    }


class ManualReassignIn(BaseModel):
    to_user_id: int
    reason: str


@router.post("/work-orders/{order_id}/manual-reassign")
async def manual_reassign_work_order(
    order_id: int,
    data: ManualReassignIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = db.query(WorkOrder).filter(WorkOrder.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="工单不存在")
    if order.status == WorkOrderStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="已完工工单不可改派")

    new_worker = db.query(User).filter(User.id == data.to_user_id).first()
    if not new_worker:
        raise HTTPException(status_code=400, detail="目标维修工不存在")

    old_user = db.query(User).filter(User.id == order.assigned_to).first() if order.assigned_to else None
    old_name = old_user.full_name if old_user else None

    equip = db.query(MiningEquipment).filter(MiningEquipment.id == order.equipment_id).first()
    equip_type = equip.type if equip else None
    equip_area = equip.location_area if equip else None

    next_workers = _find_best_maintenance_workers(db, equip_type, equip_area, [])
    rationale = _build_assignment_rationale(db, equip_type, equip_area, next_workers)
    import json
    reassignment = WorkOrderReassignment(
        work_order_id=order.id,
        reassignment_type=ReassignmentType.DISPATCHER_MANUAL,
        from_user_id=order.assigned_to,
        from_user_name=old_name,
        to_user_id=new_worker.id,
        to_user_name=new_worker.full_name,
        reason=data.reason,
        operator_user_id=current_user.id,
        operator_name=current_user.full_name,
        rationale_json=json.dumps(rationale, ensure_ascii=False)
    )
    db.add(reassignment)

    order.assigned_to = new_worker.id
    order.status = WorkOrderStatus.ASSIGNED
    order.reassigned_count = (order.reassigned_count or 0) + 1
    order.assigned_at = datetime.utcnow()
    db.commit()
    db.refresh(order)

    event = db.query(SafetyEvent).filter(SafetyEvent.related_work_order_id == order.id).first()
    if event:
        log_safety_action(db, event.id, SafetyActionType.WORK_ORDER_REASSIGNED,
                          f"调度{current_user.full_name} 手动改派: {old_name or '无'} → {new_worker.full_name}",
                          current_user,
                          {"from": old_name, "to": new_worker.full_name, "reason": data.reason, "manual": True})

    await push_work_order_notification(db, order, new_worker)
    await push_system_notification(
        db,
        f"工单调度改派: {order.title}",
        f"调度{current_user.full_name}手动改派: {old_name or '无'} → {new_worker.full_name}，原因:{data.reason}"
    )

    result = _work_order_to_dict(order, db, include_rationale=True)
    return result
