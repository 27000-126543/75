import json
from datetime import datetime
from typing import List, Optional, Set
from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session
from models import (
    PushMessage, MessageType, UserRole, User, Alert, WorkOrder,
    EvacuationOrder, RestockRequest, TransportTask
)
from database import SessionLocal


class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[int, WebSocket] = {}
        self.role_subscribers: dict[str, Set[int]] = {}

    async def connect(self, websocket: WebSocket, user_id: int, roles: List[str]):
        await websocket.accept()
        self.active_connections[user_id] = websocket
        for role in roles:
            if role not in self.role_subscribers:
                self.role_subscribers[role] = set()
            self.role_subscribers[role].add(user_id)

    def disconnect(self, user_id: int):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
        for role in self.role_subscribers:
            if user_id in self.role_subscribers[role]:
                self.role_subscribers[role].remove(user_id)

    async def send_personal_message(self, message: dict, user_id: int):
        if user_id in self.active_connections:
            try:
                await self.active_connections[user_id].send_json(message)
            except Exception:
                self.disconnect(user_id)

    async def send_to_roles(self, message: dict, roles: List[str]):
        target_users: Set[int] = set()
        for role in roles:
            if role in self.role_subscribers:
                target_users.update(self.role_subscribers[role])
        for user_id in target_users:
            await self.send_personal_message(message, user_id)

    async def broadcast(self, message: dict):
        for user_id in list(self.active_connections.keys()):
            await self.send_personal_message(message, user_id)


manager = ConnectionManager()


def _save_push_message(
    db: Session,
    message_type: MessageType,
    title: str,
    content: str,
    target_roles: Optional[List[str]] = None,
    target_user_ids: Optional[List[int]] = None
) -> PushMessage:
    msg = PushMessage(
        message_type=message_type,
        title=title,
        content=content,
        target_roles=",".join(target_roles) if target_roles else None,
        target_user_ids=",".join(str(u) for u in target_user_ids) if target_user_ids else None
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


def _build_message_payload(msg_obj: PushMessage) -> dict:
    return {
        "id": msg_obj.id,
        "type": msg_obj.message_type.value,
        "title": msg_obj.title,
        "content": msg_obj.content,
        "timestamp": msg_obj.created_at.isoformat()
    }


async def push_alert_notification(db: Session, alert: Alert):
    roles = [UserRole.DISPATCHER.value, UserRole.MANAGER.value, UserRole.SECURITY.value]
    content = f"区域[{alert.area}]发生{alert.level.value}级别告警: {alert.title} - {alert.content}"
    msg = _save_push_message(
        db, MessageType.ALERT, alert.title, content, target_roles=roles
    )
    await manager.send_to_roles(_build_message_payload(msg), roles)


async def push_work_order_notification(db: Session, work_order: WorkOrder, assigned_user: Optional[User] = None):
    roles = [UserRole.DISPATCHER.value, UserRole.MAINTENANCE.value]
    content = f"检修工单#{work_order.id}: {work_order.title} - 优先级: {work_order.priority}"
    target_users = [assigned_user.id] if assigned_user else None
    msg = _save_push_message(
        db, MessageType.WORK_ORDER, work_order.title, content,
        target_roles=roles, target_user_ids=target_users
    )
    await manager.send_to_roles(_build_message_payload(msg), roles)
    if assigned_user:
        await manager.send_personal_message(_build_message_payload(msg), assigned_user.id)


async def push_evacuation_notification(db: Session, evacuation: EvacuationOrder):
    roles = [
        UserRole.DISPATCHER.value, UserRole.MANAGER.value,
        UserRole.MINER.value, UserRole.SECURITY.value
    ]
    content = f"紧急撤离指令! 区域[{evacuation.area}] - 原因: {evacuation.reason}"
    msg = _save_push_message(
        db, MessageType.EVACUATION, "人员撤离指令", content, target_roles=roles
    )
    payload = _build_message_payload(msg)
    payload["area"] = evacuation.area
    payload["alert_level"] = evacuation.alert_level.value
    await manager.send_to_roles(payload, roles)


async def push_transport_notification(db: Session, task: TransportTask, driver: Optional[User] = None):
    roles = [UserRole.DISPATCHER.value, UserRole.DRIVER.value]
    content = f"运输任务#{task.id}: {task.origin} -> {task.destination}, {task.ore_amount}吨"
    if task.is_detour:
        content += f" (路线调整: {task.detour_reason})"
    target_users = [driver.id] if driver else None
    msg = _save_push_message(
        db, MessageType.DISPATCH, "运输调度", content,
        target_roles=roles, target_user_ids=target_users
    )
    await manager.send_to_roles(_build_message_payload(msg), roles)
    if driver:
        await manager.send_personal_message(_build_message_payload(msg), driver.id)


async def push_approval_notification(db: Session, request: RestockRequest):
    roles = [UserRole.MANAGER.value]
    content = f"补货申请#{request.id}: {request.quantity}件等待审批"
    msg = _save_push_message(
        db, MessageType.APPROVAL, "补货审批通知", content, target_roles=roles
    )
    await manager.send_to_roles(_build_message_payload(msg), roles)


async def push_approval_result_notification(db: Session, request: RestockRequest):
    roles = [UserRole.DISPATCHER.value, UserRole.SUPPLIER.value]
    status_text = "已批准" if request.status.value == "approved" else "已拒绝"
    content = f"补货申请#{request.id} {status_text}"
    msg = _save_push_message(
        db, MessageType.APPROVAL, "审批结果通知", content, target_roles=roles
    )
    await manager.send_to_roles(_build_message_payload(msg), roles)


async def push_security_notification(db: Session, user: User, reason: str):
    roles = [UserRole.SECURITY.value, UserRole.DISPATCHER.value]
    content = f"入井安全检查未通过 - {user.full_name}: {reason}"
    msg = _save_push_message(
        db, MessageType.SYSTEM, "入井安全预警", content, target_roles=roles
    )
    await manager.send_to_roles(_build_message_payload(msg), roles)


async def push_system_notification(db: Session, title: str, content: str, roles: Optional[List[str]] = None):
    if roles is None:
        roles = [UserRole.DISPATCHER.value, UserRole.MANAGER.value]
    msg = _save_push_message(db, MessageType.SYSTEM, title, content, target_roles=roles)
    await manager.send_to_roles(_build_message_payload(msg), roles)
