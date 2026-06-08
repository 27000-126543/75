from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
from config import settings
from models import (
    EnvironmentMonitor, EvacuationOrder, Alert, AlertLevel, User
)
from schemas import (
    EnvironmentDataCreate, EnvironmentMonitorResponse,
    EvacuationOrderResponse
)
from routers.auth import get_current_user
from services.notification_service import (
    push_alert_notification, push_evacuation_notification, push_system_notification
)

router = APIRouter(prefix="/environment", tags=["环境监测与应急调度"])


@router.post("/data", response_model=EnvironmentMonitorResponse)
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

        existing_evacuation = db.query(EvacuationOrder).filter(
            EvacuationOrder.area == data.area,
            EvacuationOrder.is_active == True
        ).first()

        if not existing_evacuation:
            evacuation_level = AlertLevel.CRITICAL if gas_alert else AlertLevel.DANGER
            evacuation = EvacuationOrder(
                area=data.area,
                reason=alert_content,
                alert_level=evacuation_level,
                is_active=True
            )
            db.add(evacuation)
            db.commit()
            db.refresh(evacuation)
            db.refresh(alert)
            db.refresh(monitor)

            await push_alert_notification(db, alert)
            await push_evacuation_notification(db, evacuation)
            await push_system_notification(
                db,
                f"{data.area}通风设备已启动",
                f"触发原因: {alert_content}",
                None
            )
        else:
            db.commit()
            db.refresh(alert)
            db.refresh(monitor)
            await push_alert_notification(db, alert)
    else:
        db.commit()
        db.refresh(monitor)

    return monitor


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


@router.post("/evacuations/{evac_id}/cancel", response_model=EvacuationOrderResponse)
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

    await push_system_notification(
        db,
        f"{evacuation.area}撤离指令已解除",
        "环境恢复正常，可恢复作业",
        None
    )
    return evacuation


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
