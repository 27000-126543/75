from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
from models import (
    TransportVehicle, TransportTask, TransportStatus,
    Crusher, OreBatch, User, UserRole
)
from schemas import (
    TransportVehicleCreate, TransportVehicleResponse,
    TransportTaskCreate, TransportTaskResponse,
    CrusherCreate, CrusherResponse
)
from routers.auth import get_current_user
from services.notification_service import push_transport_notification

router = APIRouter(prefix="/transport", tags=["运输调度"])


def _find_available_vehicle(db: Session, required_load: float) -> Optional[TransportVehicle]:
    vehicles = db.query(TransportVehicle).filter(
        TransportVehicle.status == TransportStatus.IDLE,
        TransportVehicle.capacity >= required_load
    ).order_by(TransportVehicle.capacity.asc()).all()

    for v in vehicles:
        active_tasks = db.query(TransportTask).filter(
            TransportTask.vehicle_id == v.id,
            TransportTask.status.in_([
                TransportStatus.ASSIGNED,
                TransportStatus.LOADING,
                TransportStatus.TRANSPORTING
            ])
        ).count()
        if active_tasks == 0:
            return v
    return vehicles[0] if vehicles else None


def _select_crusher(db: Session, origin_area: str) -> Optional[Crusher]:
    crushers = db.query(Crusher).all()
    if not crushers:
        return None
    best = min(
        crushers,
        key=lambda c: (c.current_load / c.max_load) / c.efficiency if c.efficiency > 0 else 999
    )
    return best


def _generate_route(origin: str, destination: str) -> str:
    return f"{origin} -> 主运输巷道 -> {destination}"


def _generate_detour_route(origin: str, destination: str, blocked_segment: str) -> str:
    return f"{origin} -> 备用巷道(避开{blocked_segment}) -> {destination}"


@router.post("/vehicles", response_model=TransportVehicleResponse, status_code=201)
def create_vehicle(
    data: TransportVehicleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    existing = db.query(TransportVehicle).filter(
        TransportVehicle.plate_number == data.plate_number
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="车牌号已存在")
    v = TransportVehicle(**data.model_dump())
    db.add(v)
    db.commit()
    db.refresh(v)
    return v


@router.get("/vehicles", response_model=List[TransportVehicleResponse])
def list_vehicles(
    status: Optional[TransportStatus] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(TransportVehicle)
    if status:
        query = query.filter(TransportVehicle.status == status)
    return query.all()


@router.get("/vehicles/{vehicle_id}", response_model=TransportVehicleResponse)
def get_vehicle(
    vehicle_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    v = db.query(TransportVehicle).filter(TransportVehicle.id == vehicle_id).first()
    if not v:
        raise HTTPException(status_code=404, detail="车辆不存在")
    return v


@router.put("/vehicles/{vehicle_id}/location")
def update_vehicle_location(
    vehicle_id: int,
    location: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    v = db.query(TransportVehicle).filter(TransportVehicle.id == vehicle_id).first()
    if not v:
        raise HTTPException(status_code=404, detail="车辆不存在")
    v.current_location = location
    db.commit()
    return {"message": "位置已更新", "location": location}


@router.post("/crushers", response_model=CrusherResponse, status_code=201)
def create_crusher(
    data: CrusherCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    c = Crusher(**data.model_dump())
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


@router.get("/crushers", response_model=List[CrusherResponse])
def list_crushers(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return db.query(Crusher).all()


@router.put("/crushers/{crusher_id}/load")
def update_crusher_load(
    crusher_id: int,
    current_load: float,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    c = db.query(Crusher).filter(Crusher.id == crusher_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="破碎机不存在")
    c.current_load = min(max(current_load, 0), c.max_load)
    db.commit()
    return {"message": "负荷已更新", "current_load": c.current_load}


@router.post("/tasks", response_model=TransportTaskResponse)
async def create_transport_task(
    data: TransportTaskCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    vehicle_id = data.vehicle_id
    if not vehicle_id:
        vehicle = _find_available_vehicle(db, data.ore_amount)
        if not vehicle:
            raise HTTPException(status_code=400, detail="无可用运输车辆")
        vehicle_id = vehicle.id
    else:
        vehicle = db.query(TransportVehicle).filter(TransportVehicle.id == vehicle_id).first()
        if not vehicle:
            raise HTTPException(status_code=404, detail="车辆不存在")

    destination = data.destination
    if not destination:
        crusher = _select_crusher(db, data.origin)
        if not crusher:
            raise HTTPException(status_code=400, detail="无可用破碎机")
        destination = crusher.name

    route = _generate_route(data.origin, destination)

    task = TransportTask(
        vehicle_id=vehicle_id,
        origin=data.origin,
        destination=destination,
        ore_amount=data.ore_amount,
        route=route,
        status=TransportStatus.ASSIGNED
    )
    db.add(task)

    vehicle.status = TransportStatus.ASSIGNED
    vehicle.current_load = data.ore_amount
    vehicle.route = route

    db.commit()
    db.refresh(task)
    db.refresh(vehicle)

    driver = None
    if vehicle.driver_id:
        driver = db.query(User).filter(User.id == vehicle.driver_id).first()
    await push_transport_notification(db, task, driver)

    return task


@router.get("/tasks", response_model=List[TransportTaskResponse])
def list_tasks(
    vehicle_id: Optional[int] = None,
    status: Optional[TransportStatus] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(TransportTask)
    if vehicle_id:
        query = query.filter(TransportTask.vehicle_id == vehicle_id)
    if status:
        query = query.filter(TransportTask.status == status)
    return query.order_by(TransportTask.created_at.desc()).all()


@router.get("/tasks/{task_id}", response_model=TransportTaskResponse)
def get_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    task = db.query(TransportTask).filter(TransportTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@router.put("/tasks/{task_id}/status", response_model=TransportTaskResponse)
def update_task_status(
    task_id: int,
    status: TransportStatus,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    task = db.query(TransportTask).filter(TransportTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    task.status = status
    vehicle = db.query(TransportVehicle).filter(TransportVehicle.id == task.vehicle_id).first()
    if vehicle:
        vehicle.status = status
        if status == TransportStatus.COMPLETED:
            vehicle.current_load = 0.0
            task.completed_at = datetime.utcnow()

    db.commit()
    db.refresh(task)
    return task


@router.put("/tasks/{task_id}/detour", response_model=TransportTaskResponse)
async def report_congestion_and_detour(
    task_id: int,
    blocked_segment: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    task = db.query(TransportTask).filter(TransportTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status in [TransportStatus.COMPLETED, TransportStatus.UNLOADING]:
        raise HTTPException(status_code=400, detail="任务已完成或即将完成，无法改道")

    new_route = _generate_detour_route(task.origin, task.destination, blocked_segment)
    task.route = new_route
    task.is_detour = True
    task.detour_reason = f"{blocked_segment}路段拥堵"

    vehicle = db.query(TransportVehicle).filter(TransportVehicle.id == task.vehicle_id).first()
    if vehicle:
        vehicle.route = new_route

    db.commit()
    db.refresh(task)

    driver = None
    if vehicle and vehicle.driver_id:
        driver = db.query(User).filter(User.id == vehicle.driver_id).first()
    await push_transport_notification(db, task, driver)

    return task
