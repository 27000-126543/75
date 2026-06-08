from datetime import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
from models import (
    UserRole, AlertLevel, WorkOrderStatus, ApprovalStatus,
    TransportStatus, OreBatchStatus, MessageType
)


class UserBase(BaseModel):
    username: str
    full_name: str
    role: UserRole
    phone: Optional[str] = None
    email: Optional[str] = None
    location_tag_id: Optional[str] = None
    skills: Optional[str] = None


class UserCreate(UserBase):
    password: str


class UserResponse(UserBase):
    id: int
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class PersonnelCheckCreate(BaseModel):
    user_id: int
    location_tag_id: str
    tag_verified: bool = False
    alcohol_level: float = 0.0


class PersonnelCheckResponse(BaseModel):
    id: int
    user_id: int
    location_tag_id: str
    tag_verified: bool
    alcohol_level: float
    alcohol_passed: bool
    overall_passed: bool
    check_time: datetime
    remark: Optional[str] = None

    class Config:
        from_attributes = True


class MiningEquipmentBase(BaseModel):
    name: str
    code: str
    type: Optional[str] = None
    location_area: Optional[str] = None


class MiningEquipmentCreate(MiningEquipmentBase):
    pass


class MiningEquipmentResponse(MiningEquipmentBase):
    id: int
    status: str
    last_maintenance: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class EquipmentDataCreate(BaseModel):
    equipment_id: int
    vibration: float
    temperature: float


class EquipmentDataResponse(BaseModel):
    id: int
    equipment_id: int
    vibration: float
    temperature: float
    is_abnormal: bool
    collected_at: datetime

    class Config:
        from_attributes = True


class WorkOrderResponse(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    equipment_id: Optional[int] = None
    assigned_to: Optional[int] = None
    status: WorkOrderStatus
    priority: str
    created_at: datetime
    completed_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class WorkOrderAssign(BaseModel):
    assigned_to: int


class EnvironmentDataCreate(BaseModel):
    area: str
    gas_concentration: float = 0.0
    dust_concentration: float = 0.0


class EnvironmentMonitorResponse(BaseModel):
    id: int
    area: str
    gas_concentration: float
    dust_concentration: float
    gas_alert: bool
    dust_alert: bool
    ventilation_active: bool
    collected_at: datetime

    class Config:
        from_attributes = True


class EvacuationOrderResponse(BaseModel):
    id: int
    area: str
    reason: Optional[str] = None
    alert_level: AlertLevel
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class TransportVehicleBase(BaseModel):
    plate_number: str
    driver_id: Optional[int] = None
    capacity: float = 30.0
    current_location: Optional[str] = None


class TransportVehicleCreate(TransportVehicleBase):
    pass


class TransportVehicleResponse(TransportVehicleBase):
    id: int
    current_load: float
    status: TransportStatus

    class Config:
        from_attributes = True


class TransportTaskCreate(BaseModel):
    vehicle_id: Optional[int] = None
    origin: str
    destination: str
    ore_amount: float


class TransportTaskResponse(BaseModel):
    id: int
    vehicle_id: int
    origin: str
    destination: str
    ore_amount: float
    route: Optional[str] = None
    is_detour: bool
    detour_reason: Optional[str] = None
    status: TransportStatus
    created_at: datetime

    class Config:
        from_attributes = True


class CrusherBase(BaseModel):
    name: str
    max_load: float = 100.0
    efficiency: float = 1.0


class CrusherCreate(CrusherBase):
    pass


class CrusherResponse(CrusherBase):
    id: int
    current_load: float

    class Config:
        from_attributes = True


class OreBatchBase(BaseModel):
    batch_no: str
    mining_area: Optional[str] = None
    weight: float = 0.0


class OreBatchCreate(OreBatchBase):
    pass


class OreQualityTest(BaseModel):
    batch_id: int
    iron_content: float
    sulfur_content: float


class OreBatchResponse(BaseModel):
    id: int
    batch_no: str
    mining_area: Optional[str] = None
    weight: float
    grade: Optional[float] = None
    iron_content: Optional[float] = None
    sulfur_content: Optional[float] = None
    status: OreBatchStatus
    locked_reason: Optional[str] = None
    sampled_at: datetime
    tested_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class SupplyItemBase(BaseModel):
    name: str
    code: str
    category: Optional[str] = None
    unit: Optional[str] = None
    safety_stock: float = 0.0
    supplier: Optional[str] = None


class SupplyItemCreate(SupplyItemBase):
    pass


class SupplyItemResponse(SupplyItemBase):
    id: int
    current_stock: float
    last_restock: Optional[datetime] = None

    class Config:
        from_attributes = True


class StockUpdate(BaseModel):
    quantity: float


class RestockRequestResponse(BaseModel):
    id: int
    supply_item_id: int
    quantity: float
    reason: Optional[str] = None
    status: ApprovalStatus
    applicant_id: Optional[int] = None
    approver_id: Optional[int] = None
    approved_at: Optional[datetime] = None
    supplier_notified: bool
    created_at: datetime

    class Config:
        from_attributes = True


class ApprovalAction(BaseModel):
    approved: bool
    approver_id: int
    remark: Optional[str] = None


class DailyReportResponse(BaseModel):
    id: int
    report_date: datetime
    mining_area_data: Optional[str] = None
    total_output: float
    equipment_utilization: float
    safety_event_count: int
    generated_at: datetime

    class Config:
        from_attributes = True


class AlertResponse(BaseModel):
    id: int
    title: str
    content: Optional[str] = None
    level: AlertLevel
    source_type: Optional[str] = None
    source_id: Optional[int] = None
    area: Optional[str] = None
    is_resolved: bool
    created_at: datetime

    class Config:
        from_attributes = True


class PushMessageResponse(BaseModel):
    id: int
    message_type: MessageType
    title: str
    content: Optional[str] = None
    target_roles: Optional[str] = None
    target_user_ids: Optional[str] = None
    is_read: bool
    created_at: datetime

    class Config:
        from_attributes = True


class Token(BaseModel):
    access_token: str
    token_type: str


class LoginRequest(BaseModel):
    username: str
    password: str
