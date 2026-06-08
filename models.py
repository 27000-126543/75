from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean, ForeignKey, Text, Enum as SQLEnum
)
from sqlalchemy.orm import relationship
import enum
from database import Base


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    DISPATCHER = "dispatcher"
    SECURITY = "security"
    MAINTENANCE = "maintenance"
    DRIVER = "driver"
    MINER = "miner"
    ENGINEER = "engineer"
    MANAGER = "manager"
    SUPPLIER = "supplier"


class AlertLevel(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    DANGER = "danger"
    CRITICAL = "critical"


class WorkOrderStatus(str, enum.Enum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ShipmentStatus(str, enum.Enum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED_WAITING_SUPPLIER = "approved_waiting_supplier"
    IN_TRANSIT = "in_transit"
    DELIVERED = "delivered"
    PARTIALLY_RECEIVED = "partially_received"
    RECEIVED = "received"
    CANCELLED = "cancelled"


class TransportStatus(str, enum.Enum):
    IDLE = "idle"
    ASSIGNED = "assigned"
    LOADING = "loading"
    TRANSPORTING = "transporting"
    UNLOADING = "unloading"
    COMPLETED = "completed"


class OreBatchStatus(str, enum.Enum):
    PENDING = "pending"
    QUALIFIED = "qualified"
    LOCKED = "locked"
    BLENDED = "blended"


class MessageType(str, enum.Enum):
    ALERT = "alert"
    WORK_ORDER = "work_order"
    DISPATCH = "dispatch"
    APPROVAL = "approval"
    EVACUATION = "evacuation"
    SYSTEM = "system"


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(100), nullable=False)
    role = Column(SQLEnum(UserRole), nullable=False)
    phone = Column(String(20))
    email = Column(String(100))
    location_tag_id = Column(String(50), unique=True)
    skills = Column(String(255))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class PersonnelCheck(Base):
    __tablename__ = "personnel_checks"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    location_tag_id = Column(String(50), nullable=False)
    tag_verified = Column(Boolean, default=False)
    alcohol_level = Column(Float, default=0.0)
    alcohol_passed = Column(Boolean, default=False)
    overall_passed = Column(Boolean, default=False)
    check_time = Column(DateTime, default=datetime.utcnow)
    remark = Column(Text)


class MiningEquipment(Base):
    __tablename__ = "mining_equipments"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    code = Column(String(50), unique=True, nullable=False)
    type = Column(String(50))
    location_area = Column(String(100))
    status = Column(String(50), default="normal")
    last_maintenance = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)


class EquipmentData(Base):
    __tablename__ = "equipment_data"
    id = Column(Integer, primary_key=True, index=True)
    equipment_id = Column(Integer, ForeignKey("mining_equipments.id"), nullable=False)
    vibration = Column(Float, nullable=False)
    temperature = Column(Float, nullable=False)
    is_abnormal = Column(Boolean, default=False)
    collected_at = Column(DateTime, default=datetime.utcnow)


class WorkOrder(Base):
    __tablename__ = "work_orders"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(200), nullable=False)
    description = Column(Text)
    equipment_id = Column(Integer, ForeignKey("mining_equipments.id"))
    assigned_to = Column(Integer, ForeignKey("users.id"))
    status = Column(SQLEnum(WorkOrderStatus), default=WorkOrderStatus.PENDING)
    priority = Column(String(20), default="medium")
    abnormal_data_id = Column(Integer, ForeignKey("equipment_data.id"))
    created_at = Column(DateTime, default=datetime.utcnow)
    assigned_at = Column(DateTime)
    accepted_at = Column(DateTime)
    rejected_by = Column(String(255))
    reassigned_count = Column(Integer, default=0)
    completed_at = Column(DateTime)


class EnvironmentMonitor(Base):
    __tablename__ = "environment_monitors"
    id = Column(Integer, primary_key=True, index=True)
    area = Column(String(100), nullable=False)
    gas_concentration = Column(Float, default=0.0)
    dust_concentration = Column(Float, default=0.0)
    gas_alert = Column(Boolean, default=False)
    dust_alert = Column(Boolean, default=False)
    ventilation_active = Column(Boolean, default=False)
    collected_at = Column(DateTime, default=datetime.utcnow)


class EvacuationOrder(Base):
    __tablename__ = "evacuation_orders"
    id = Column(Integer, primary_key=True, index=True)
    area = Column(String(100), nullable=False)
    reason = Column(Text)
    alert_level = Column(SQLEnum(AlertLevel), default=AlertLevel.DANGER)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    cancelled_at = Column(DateTime)


class TransportVehicle(Base):
    __tablename__ = "transport_vehicles"
    id = Column(Integer, primary_key=True, index=True)
    plate_number = Column(String(20), unique=True, nullable=False)
    driver_id = Column(Integer, ForeignKey("users.id"))
    capacity = Column(Float, default=30.0)
    current_load = Column(Float, default=0.0)
    current_location = Column(String(100))
    status = Column(SQLEnum(TransportStatus), default=TransportStatus.IDLE)
    route = Column(Text)


class TransportTask(Base):
    __tablename__ = "transport_tasks"
    id = Column(Integer, primary_key=True, index=True)
    vehicle_id = Column(Integer, ForeignKey("transport_vehicles.id"), nullable=False)
    origin = Column(String(100), nullable=False)
    destination = Column(String(100), nullable=False)
    ore_amount = Column(Float, nullable=False)
    route = Column(Text)
    is_detour = Column(Boolean, default=False)
    detour_reason = Column(String(255))
    status = Column(SQLEnum(TransportStatus), default=TransportStatus.ASSIGNED)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)


class Crusher(Base):
    __tablename__ = "crushers"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    current_load = Column(Float, default=0.0)
    max_load = Column(Float, default=100.0)
    efficiency = Column(Float, default=1.0)


class OreBatch(Base):
    __tablename__ = "ore_batches"
    id = Column(Integer, primary_key=True, index=True)
    batch_no = Column(String(50), unique=True, nullable=False)
    mining_area = Column(String(100))
    weight = Column(Float, default=0.0)
    grade = Column(Float)
    iron_content = Column(Float)
    sulfur_content = Column(Float)
    status = Column(SQLEnum(OreBatchStatus), default=OreBatchStatus.PENDING)
    locked_reason = Column(Text)
    sampled_at = Column(DateTime, default=datetime.utcnow)
    tested_at = Column(DateTime)


class SupplyItem(Base):
    __tablename__ = "supply_items"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    code = Column(String(50), unique=True, nullable=False)
    category = Column(String(50))
    unit = Column(String(20))
    current_stock = Column(Float, default=0.0)
    safety_stock = Column(Float, default=0.0)
    supplier = Column(String(200))
    last_restock = Column(DateTime)


class RestockRequest(Base):
    __tablename__ = "restock_requests"
    id = Column(Integer, primary_key=True, index=True)
    supply_item_id = Column(Integer, ForeignKey("supply_items.id"), nullable=False)
    quantity = Column(Float, nullable=False)
    reason = Column(Text)
    status = Column(SQLEnum(ApprovalStatus), default=ApprovalStatus.PENDING)
    applicant_id = Column(Integer, ForeignKey("users.id"))
    approver_id = Column(Integer, ForeignKey("users.id"))
    approved_at = Column(DateTime)
    supplier_notified = Column(Boolean, default=False)
    shipment_status = Column(SQLEnum(ShipmentStatus), default=ShipmentStatus.PENDING_APPROVAL)
    supplier_confirmed_at = Column(DateTime)
    tracking_number = Column(String(100))
    shipped_at = Column(DateTime)
    delivered_at = Column(DateTime)
    received_quantity = Column(Float, default=0)
    received_by = Column(Integer, ForeignKey("users.id"))
    received_at = Column(DateTime)
    received_remark = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class DailyReport(Base):
    __tablename__ = "daily_reports"
    id = Column(Integer, primary_key=True, index=True)
    report_date = Column(DateTime, unique=True, nullable=False)
    mining_area_data = Column(Text)
    total_output = Column(Float, default=0.0)
    equipment_utilization = Column(Float, default=0.0)
    safety_event_count = Column(Integer, default=0)
    generated_at = Column(DateTime, default=datetime.utcnow)


class Alert(Base):
    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(200), nullable=False)
    content = Column(Text)
    level = Column(SQLEnum(AlertLevel), default=AlertLevel.WARNING)
    source_type = Column(String(50))
    source_id = Column(Integer)
    area = Column(String(100))
    is_resolved = Column(Boolean, default=False)
    resolved_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)


class PushMessage(Base):
    __tablename__ = "push_messages"
    id = Column(Integer, primary_key=True, index=True)
    message_type = Column(SQLEnum(MessageType), nullable=False)
    title = Column(String(200), nullable=False)
    content = Column(Text)
    target_roles = Column(String(255))
    target_user_ids = Column(String(255))
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class SafetyEventStatus(str, enum.Enum):
    OPEN = "open"
    HANDLING = "handling"
    RESOLVED = "resolved"


class SafetyEvent(Base):
    __tablename__ = "safety_events"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(200), nullable=False)
    description = Column(Text)
    level = Column(SQLEnum(AlertLevel), default=AlertLevel.WARNING)
    source_type = Column(String(50))
    source_id = Column(Integer)
    area = Column(String(100))
    status = Column(SQLEnum(SafetyEventStatus), default=SafetyEventStatus.OPEN)
    related_alert_id = Column(Integer, ForeignKey("alerts.id"))
    related_evacuation_id = Column(Integer, ForeignKey("evacuation_orders.id"))
    related_work_order_id = Column(Integer, ForeignKey("work_orders.id"))
    ventilation_active = Column(Boolean, default=False)
    resolved_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)


class EvacuationConfirmation(Base):
    __tablename__ = "evacuation_confirmations"
    id = Column(Integer, primary_key=True, index=True)
    evacuation_id = Column(Integer, ForeignKey("evacuation_orders.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    area = Column(String(100))
    area_before_confirm = Column(String(100))
    confirmed = Column(Boolean, default=False)
    confirmed_at = Column(DateTime)
    confirm_location = Column(String(200))
    confirm_method = Column(String(50))
    reminder_sent = Column(Boolean, default=False)
    reminder_sent_at = Column(DateTime)
    reminder_message_id = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)


class WorkerStatus(str, enum.Enum):
    IDLE = "idle"
    BUSY = "busy"
    OFFLINE = "offline"


class MaintenanceWorkerState(Base):
    __tablename__ = "maintenance_worker_states"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    status = Column(SQLEnum(WorkerStatus), default=WorkerStatus.IDLE)
    current_area = Column(String(100))
    eta_minutes = Column(Integer, default=0)
    last_updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SafetyActionType(str, enum.Enum):
    ALERT_TRIGGERED = "alert_triggered"
    VENTILATION_STARTED = "ventilation_started"
    EVACUATION_ISSUED = "evacuation_issued"
    EVACUATION_CONFIRMED = "evacuation_confirmed"
    EVACUATION_TIMEOUT = "evacuation_timeout"
    EVACUATION_CANCELLED = "evacuation_cancelled"
    WORK_ORDER_CREATED = "work_order_created"
    WORK_ORDER_ASSIGNED = "work_order_assigned"
    WORK_ORDER_ACCEPTED = "work_order_accepted"
    WORK_ORDER_REJECTED = "work_order_rejected"
    WORK_ORDER_REASSIGNED = "work_order_reassigned"
    WORK_ORDER_COMPLETED = "work_order_completed"
    EVENT_RESOLVED = "event_resolved"
    NOTE = "note"


class SafetyEventActionLog(Base):
    __tablename__ = "safety_event_action_logs"
    id = Column(Integer, primary_key=True, index=True)
    safety_event_id = Column(Integer, ForeignKey("safety_events.id"), nullable=False)
    action_type = Column(SQLEnum(SafetyActionType), nullable=False)
    actor_user_id = Column(Integer, ForeignKey("users.id"))
    actor_name = Column(String(100))
    description = Column(Text)
    detail_json = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class ReassignmentType(str, enum.Enum):
    AUTO_INITIAL = "auto_initial"
    REJECT_AUTO = "reject_auto"
    TIMEOUT_AUTO = "timeout_auto"
    DISPATCHER_MANUAL = "dispatcher_manual"


class WorkOrderReassignment(Base):
    __tablename__ = "work_order_reassignments"
    id = Column(Integer, primary_key=True, index=True)
    work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=False)
    reassignment_type = Column(SQLEnum(ReassignmentType), default=ReassignmentType.AUTO_INITIAL)
    from_user_id = Column(Integer, ForeignKey("users.id"))
    from_user_name = Column(String(100))
    to_user_id = Column(Integer, ForeignKey("users.id"))
    to_user_name = Column(String(100))
    reason = Column(String(500))
    rationale_json = Column(Text)
    operator_user_id = Column(Integer, ForeignKey("users.id"))
    operator_name = Column(String(100))
    created_at = Column(DateTime, default=datetime.utcnow)


class MinerLocationReport(Base):
    __tablename__ = "miner_location_reports"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    area = Column(String(100), nullable=False)
    reported_at = Column(DateTime, default=datetime.utcnow)


class RestockReceipt(Base):
    __tablename__ = "restock_receipts"
    id = Column(Integer, primary_key=True, index=True)
    restock_request_id = Column(Integer, ForeignKey("restock_requests.id"), nullable=False)
    batch_no = Column(String(100))
    received_quantity = Column(Float, default=0)
    qualified_quantity = Column(Float, default=0)
    unqualified_quantity = Column(Float, default=0)
    inspection_result = Column(String(50))
    inspection_remark = Column(Text)
    received_by = Column(Integer, ForeignKey("users.id"))
    received_by_name = Column(String(100))
    created_at = Column(DateTime, default=datetime.utcnow)
