from datetime import datetime, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
from config import settings
from models import (
    EnvironmentMonitor, EvacuationOrder, Alert, AlertLevel, User,
    SafetyEvent, SafetyEventStatus, EvacuationConfirmation, UserRole,
    SafetyActionType, WorkOrder, WorkOrderStatus, MinerLocationReport
)
from routers.safety import log_safety_action
from schemas import (
    EnvironmentDataCreate, EnvironmentMonitorResponse,
    EvacuationOrderResponse
)
from routers.auth import get_current_user
from services.notification_service import (
    push_alert_notification, push_evacuation_notification, push_system_notification
)

router = APIRouter(prefix="/environment", tags=["环境监测与应急调度"])


def _init_evacuation_confirmations(db: Session, evac_id: int, evac_area: Optional[str]):
    existing = db.query(EvacuationConfirmation).filter(
        EvacuationConfirmation.evacuation_id == evac_id
    ).first()
    if existing:
        return
    from routers.equipment import ADJACENT_AREAS
    adjacent_areas = ADJACENT_AREAS.get(evac_area, [])
    target_areas = {evac_area} | set(adjacent_areas) if evac_area else None

    miners = db.query(User).filter(
        User.role == UserRole.MINER,
        User.is_active == True
    ).all()
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
                evacuation_id=evac_id, user_id=m.id,
                area=user_area or evac_area,
                confirmed=False
            )
            db.add(conf)
    db.commit()


def _create_or_update_safety_event(
    db: Session,
    title: str,
    description: str,
    level: AlertLevel,
    source_type: str,
    area: str,
    alert_id: Optional[int] = None,
    evacuation_id: Optional[int] = None,
    ventilation_active: bool = False
) -> SafetyEvent:
    active_event = db.query(SafetyEvent).filter(
        SafetyEvent.area == area,
        SafetyEvent.source_type == source_type,
        SafetyEvent.status != SafetyEventStatus.RESOLVED
    ).first()
    if active_event:
        active_event.level = level
        active_event.description = description
        if alert_id:
            active_event.related_alert_id = alert_id
        if evacuation_id:
            active_event.related_evacuation_id = evacuation_id
        active_event.ventilation_active = ventilation_active
        active_event.status = SafetyEventStatus.HANDLING
        db.commit()
        db.refresh(active_event)
        return active_event
    event = SafetyEvent(
        title=title,
        description=description,
        level=level,
        source_type=source_type,
        area=area,
        status=SafetyEventStatus.HANDLING if evacuation_id else SafetyEventStatus.OPEN,
        related_alert_id=alert_id,
        related_evacuation_id=evacuation_id,
        ventilation_active=ventilation_active
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


@router.post("/data")
async def upload_environment_data(
    data: EnvironmentDataCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    gas_alert = data.gas_concentration > settings.GAS_THRESHOLD
    dust_alert = data.dust_concentration > settings.DUST_THRESHOLD
    any_alert = gas_alert or dust_alert

    monitor = EnvironmentMonitor(
        area=data.area,
        gas_concentration=data.gas_concentration,
        dust_concentration=data.dust_concentration,
        gas_alert=gas_alert,
        dust_alert=dust_alert,
        ventilation_active=any_alert
    )
    db.add(monitor)

    result = {
        "id": None,
        "area": data.area,
        "gas_concentration": data.gas_concentration,
        "dust_concentration": data.dust_concentration,
        "gas_alert": gas_alert,
        "dust_alert": dust_alert,
        "ventilation_active": any_alert,
        "safety_event_id": None,
        "evacuation_order_id": None
    }

    if any_alert:
        alert_reasons = []
        if gas_alert:
            alert_reasons.append(f"瓦斯浓度超标({data.gas_concentration}% > {settings.GAS_THRESHOLD}%)")
        if dust_alert:
            alert_reasons.append(f"粉尘浓度超标({data.dust_concentration}mg/m³ > {settings.DUST_THRESHOLD}mg/m³)")

        if gas_alert and dust_alert:
            level = AlertLevel.CRITICAL
        elif gas_alert:
            level = AlertLevel.CRITICAL
        else:
            level = AlertLevel.DANGER
        alert_title = f"{data.area}环境告警"
        alert_content = "; ".join(alert_reasons)

        alert = Alert(
            title=alert_title,
            content=alert_content,
            level=level,
            source_type="environment",
            area=data.area
        )
        db.add(alert)
        db.flush()

        existing_evacuation = db.query(EvacuationOrder).filter(
            EvacuationOrder.area == data.area,
            EvacuationOrder.is_active == True
        ).first()

        evacuation_id = None
        if not existing_evacuation:
            evacuation_level = AlertLevel.CRITICAL if gas_alert else AlertLevel.DANGER
            evacuation = EvacuationOrder(
                area=data.area,
                reason=alert_content,
                alert_level=evacuation_level,
                is_active=True
            )
            db.add(evacuation)
            db.flush()
            evacuation_id = evacuation.id
            _init_evacuation_confirmations(db, evacuation_id, data.area)
        else:
            evacuation_id = existing_evacuation.id

        event = _create_or_update_safety_event(
            db,
            title=alert_title,
            description=alert_content,
            level=level,
            source_type="environment",
            area=data.area,
            alert_id=alert.id,
            evacuation_id=evacuation_id,
            ventilation_active=any_alert
        )
        result["safety_event_id"] = event.id
        result["evacuation_order_id"] = evacuation_id

        log_safety_action(db, event.id, SafetyActionType.ALERT_TRIGGERED,
                          f"{data.area}环境告警: {alert_content}")
        if any_alert:
            log_safety_action(db, event.id, SafetyActionType.VENTILATION_STARTED,
                              f"{data.area}通风设备已启动")
        if not existing_evacuation:
            log_safety_action(db, event.id, SafetyActionType.EVACUATION_ISSUED,
                              f"下发撤离指令: {alert_content}")

        db.commit()
        db.refresh(monitor)
        result["id"] = monitor.id

        if not existing_evacuation:
            await push_alert_notification(db, alert)
            await push_evacuation_notification(db, evacuation)
            await push_system_notification(
                db,
                f"{data.area}通风设备已启动",
                f"触发原因: {alert_content}",
                None
            )
        else:
            await push_alert_notification(db, alert)
    else:
        db.commit()
        db.refresh(monitor)
        result["id"] = monitor.id

    return result


@router.get("/data", response_model=List[EnvironmentMonitorResponse])
def list_environment_data(
    area: Optional[str] = None,
    alert_only: bool = False,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(EnvironmentMonitor)
    if area:
        query = query.filter(EnvironmentMonitor.area == area)
    if alert_only:
        query = query.filter(
            (EnvironmentMonitor.gas_alert == True) | (EnvironmentMonitor.dust_alert == True)
        )
    return query.order_by(EnvironmentMonitor.collected_at.desc()).limit(limit).all()


@router.get("/data/latest")
def get_latest_by_area(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    areas = db.query(EnvironmentMonitor.area).distinct().all()
    result = []
    for (area,) in areas:
        latest = db.query(EnvironmentMonitor).filter(
            EnvironmentMonitor.area == area
        ).order_by(EnvironmentMonitor.collected_at.desc()).first()
        if latest:
            result.append(EnvironmentMonitorResponse.model_validate(latest).model_dump())
    return result


@router.post("/ventilation/{area}/stop")
async def stop_ventilation(
    area: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    latest = db.query(EnvironmentMonitor).filter(
        EnvironmentMonitor.area == area
    ).order_by(EnvironmentMonitor.collected_at.desc()).first()
    if not latest:
        raise HTTPException(status_code=404, detail="该区域无监测数据")
    if latest.gas_alert or latest.dust_alert:
        raise HTTPException(status_code=400, detail="环境仍超标，无法停止通风")
    latest.ventilation_active = False
    db.commit()

    await push_system_notification(
        db,
        f"{area}通风设备已停止",
        "环境参数已恢复正常",
        None
    )
    return {"message": f"{area}通风设备已停止"}


@router.get("/evacuations", response_model=List[EvacuationOrderResponse])
def list_evacuations(
    area: Optional[str] = None,
    active_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(EvacuationOrder)
    if area:
        query = query.filter(EvacuationOrder.area == area)
    if active_only:
        query = query.filter(EvacuationOrder.is_active == True)
    return query.order_by(EvacuationOrder.created_at.desc()).all()


@router.post("/evacuations/{evac_id}/cancel")
async def cancel_evacuation(
    evac_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    evacuation = db.query(EvacuationOrder).filter(EvacuationOrder.id == evac_id).first()
    if not evacuation:
        raise HTTPException(status_code=404, detail="撤离指令不存在")
    evacuation.is_active = False
    evacuation.cancelled_at = datetime.utcnow()
    db.commit()
    db.refresh(evacuation)

    event = db.query(SafetyEvent).filter(
        SafetyEvent.related_evacuation_id == evac_id
    ).first()
    if event:
        if event.related_alert_id:
            alert = db.query(Alert).filter(Alert.id == event.related_alert_id).first()
            if alert and not alert.is_resolved:
                alert.is_resolved = True
                alert.resolved_at = datetime.utcnow()
        if event.related_work_order_id:
            from models import WorkOrder, WorkOrderStatus
            wo = db.query(WorkOrder).filter(WorkOrder.id == event.related_work_order_id).first()
            if wo and wo.status != WorkOrderStatus.COMPLETED:
                pass
        all_resolved = True
        if event.related_work_order_id:
            wo = db.query(WorkOrder).filter(WorkOrder.id == event.related_work_order_id).first()
            if wo and wo.status != WorkOrderStatus.COMPLETED:
                all_resolved = False
        if all_resolved:
            event.status = SafetyEventStatus.RESOLVED
            event.resolved_at = datetime.utcnow()
            db.commit()

        if event:
            log_safety_action(db, event.id, SafetyActionType.EVACUATION_CANCELLED,
                              f"{evacuation.area}撤离指令已解除，环境恢复正常")

    await push_system_notification(
        db,
        f"{evacuation.area}撤离指令已解除",
        "环境恢复正常，可恢复作业",
        None
    )
    return {
        "id": evacuation.id,
        "area": evacuation.area,
        "reason": evacuation.reason,
        "is_active": evacuation.is_active,
        "alert_level": evacuation.alert_level.value if hasattr(evacuation.alert_level, 'value') else str(evacuation.alert_level),
        "created_at": evacuation.created_at,
        "cancelled_at": evacuation.cancelled_at
    }


@router.post("/evacuations/manual", response_model=EvacuationOrderResponse)
async def create_manual_evacuation(
    area: str,
    reason: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    evacuation = EvacuationOrder(
        area=area,
        reason=reason,
        alert_level=AlertLevel.DANGER,
        is_active=True
    )
    db.add(evacuation)
    db.commit()
    db.refresh(evacuation)

    await push_evacuation_notification(db, evacuation)
    return evacuation
