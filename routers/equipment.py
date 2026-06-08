from datetime import datetime
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
from config import settings
from models import (
    MiningEquipment, EquipmentData, WorkOrder,
    WorkOrderStatus, User, UserRole, Alert, AlertLevel
)
from schemas import (
    MiningEquipmentCreate, MiningEquipmentResponse,
    EquipmentDataCreate, EquipmentDataResponse,
    WorkOrderAssign
)
from routers.auth import get_current_user
from services.notification_service import push_work_order_notification, push_alert_notification

router = APIRouter(prefix="/equipment", tags=["设备监测与检修"])


ADJACENT_AREAS = {
    "A采区": ["B采区", "主巷道"],
    "B采区": ["A采区", "C采区", "主巷道"],
    "C采区": ["B采区", "主巷道"],
    "主巷道": ["A采区", "B采区", "C采区", "主井车场"]
}


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


def _find_best_maintenance_worker(
    db: Session,
    equipment_type: Optional[str],
    equipment_area: Optional[str]
) -> Optional[User]:
    workers = db.query(User).filter(
        User.role == UserRole.MAINTENANCE,
        User.is_active == True
    ).all()
    if not workers:
        return None

    scored_workers = []
    for worker in workers:
        skill_score = _skill_match_score(worker, equipment_type)
        area_score = _area_distance_score(worker.location_tag_id, equipment_area)
        workload_score = _workload_score(db, worker.id)
        total_score = skill_score * 10 + area_score * 5 + workload_score
        scored_workers.append((total_score, skill_score, area_score, workload_score, worker))

    scored_workers.sort(key=lambda x: (-x[0], -x[1], -x[2], -x[3]))
    return scored_workers[0][4]


def _work_order_to_dict(order: WorkOrder, db: Session) -> Dict[str, Any]:
    assigned_user = None
    if order.assigned_to:
        assigned_user = db.query(User).filter(User.id == order.assigned_to).first()
    return {
        "id": order.id,
        "title": order.title,
        "description": order.description,
        "equipment_id": order.equipment_id,
        "assigned_to": order.assigned_to,
        "assigned_to_name": assigned_user.full_name if assigned_user else None,
        "assigned_to_skills": assigned_user.skills if assigned_user else None,
        "status": order.status.value if hasattr(order.status, 'value') else str(order.status),
        "priority": order.priority,
        "created_at": order.created_at,
        "completed_at": order.completed_at
    }


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


@router.post("/data", response_model=EquipmentDataResponse)
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
            worker = _find_best_maintenance_worker(db, equip.type, equip.location_area)
            priority = "high" if data.temperature > settings.TEMPERATURE_THRESHOLD else "medium"

            work_order = WorkOrder(
                title=f"{equip.name}异常检修",
                description="; ".join(abnormal_reasons),
                equipment_id=data.equipment_id,
                assigned_to=worker.id if worker else None,
                status=WorkOrderStatus.ASSIGNED if worker else WorkOrderStatus.PENDING,
                priority=priority,
                abnormal_data_id=equip_data.id
            )
            db.add(work_order)

            alert = Alert(
                title=f"{equip.name}运行异常",
                content="; ".join(abnormal_reasons),
                level=AlertLevel.WARNING,
                source_type="equipment",
                source_id=data.equipment_id,
                area=equip.location_area
            )
            db.add(alert)

            db.commit()
            db.refresh(work_order)
            db.refresh(alert)
            db.refresh(equip_data)

            await push_work_order_notification(db, work_order, worker)
            await push_alert_notification(db, alert)
        else:
            db.commit()
            db.refresh(equip_data)
    else:
        if equip.status != "normal":
            equip.status = "normal"
        db.commit()
        db.refresh(equip_data)

    return equip_data


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
    return _work_order_to_dict(order, db)
