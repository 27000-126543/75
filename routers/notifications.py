from typing import List, Optional
from datetime import datetime, timedelta
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from jose import JWTError, jwt
from database import get_db, SessionLocal
from config import settings
from models import User, PushMessage, MessageType
from schemas import PushMessageResponse
from services.notification_service import manager
from routers.auth import get_current_user

router = APIRouter(prefix="/ws", tags=["实时消息推送"])


def _get_user_from_token(token: str, db: Session) -> Optional[User]:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            return None
        user = db.query(User).filter(User.username == username).first()
        return user
    except JWTError:
        return None


@router.websocket("/connect")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str = Query(...)
):
    db = SessionLocal()
    try:
        user = _get_user_from_token(token, db)
        if not user:
            await websocket.close(code=1008, reason="认证失败")
            return
        if not user.is_active:
            await websocket.close(code=1008, reason="用户已禁用")
            return

        roles = [user.role.value]
        await manager.connect(websocket, user.id, roles)

        try:
            while True:
                data = await websocket.receive_text()
                if data == "ping":
                    await websocket.send_json({"type": "pong", "timestamp": datetime.utcnow().isoformat()})
        except WebSocketDisconnect:
            manager.disconnect(user.id)
    finally:
        db.close()


@router.get("/messages", response_model=List[PushMessageResponse])
def get_push_messages(
    message_type: Optional[MessageType] = None,
    unread_only: bool = False,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(PushMessage)
    if message_type:
        query = query.filter(PushMessage.message_type == message_type)
    if unread_only:
        query = query.filter(PushMessage.is_read == False)

    if current_user:
        role_filter = PushMessage.target_roles.contains(current_user.role.value)
        user_filter = PushMessage.target_user_ids.contains(str(current_user.id))
        query = query.filter(role_filter | user_filter)

    return query.order_by(PushMessage.created_at.desc()).limit(limit).all()


@router.put("/messages/{msg_id}/read")
def mark_message_read(
    msg_id: int,
    db: Session = Depends(get_db)
):
    msg = db.query(PushMessage).filter(PushMessage.id == msg_id).first()
    if not msg:
        raise HTTPException(status_code=404, detail="消息不存在")
    msg.is_read = True
    db.commit()
    return {"message": "已标记为已读"}


@router.get("/stats")
def get_connection_stats():
    return {
        "online_users": len(manager.active_connections),
        "role_subscribers": {k: len(v) for k, v in manager.role_subscribers.items()}
    }
