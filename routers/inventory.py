from datetime import datetime
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
from config import settings
from models import (
    SupplyItem, RestockRequest, ApprovalStatus,
    User, UserRole, Alert, AlertLevel
)
from schemas import (
    SupplyItemCreate, SupplyItemResponse, StockUpdate,
    RestockRequestResponse, ApprovalAction
)
from routers.auth import get_current_user
from services.notification_service import (
    push_approval_notification, push_approval_result_notification,
    push_alert_notification, push_system_notification
)

router = APIRouter(prefix="/inventory", tags=["物资库存管理"])


async def _check_and_create_restock(db: Session, item: SupplyItem, applicant_id: Optional[int] = None):
    safety_line = item.safety_stock
    if item.current_stock < safety_line:
        existing = db.query(RestockRequest).filter(
            RestockRequest.supply_item_id == item.id,
            RestockRequest.status == ApprovalStatus.PENDING
        ).first()
        if not existing:
            quantity = max(item.safety_stock * 2 - item.current_stock, item.safety_stock)
            request = RestockRequest(
                supply_item_id=item.id,
                quantity=quantity,
                reason=f"库存不足: 当前{item.current_stock}{item.unit}, 低于安全库存{safety_line}{item.unit}",
                applicant_id=applicant_id
            )
            db.add(request)

            alert = Alert(
                title=f"物资库存预警: {item.name}",
                content=f"{item.name}库存已低于安全库存, 当前{item.current_stock}{item.unit}, 安全库存{safety_line}{item.unit}",
                level=AlertLevel.WARNING,
                source_type="inventory",
                source_id=item.id
            )
            db.add(alert)
            db.commit()
            db.refresh(request)
            db.refresh(alert)
            await push_approval_notification(db, request)
            await push_alert_notification(db, alert)
            return request, alert
    return None, None


@router.post("/items", response_model=SupplyItemResponse, status_code=201)
def create_supply_item(
    data: SupplyItemCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    existing = db.query(SupplyItem).filter(SupplyItem.code == data.code).first()
    if existing:
        raise HTTPException(status_code=400, detail="物资编码已存在")
    item = SupplyItem(**data.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.get("/items")
async def list_items(
    category: Optional[str] = None,
    low_stock_only: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(SupplyItem)
    if category:
        query = query.filter(SupplyItem.category == category)
    items = query.all()

    for item in items:
        await _check_and_create_restock(db, item, current_user.id)

    if low_stock_only:
        items = [
            i for i in items
            if i.current_stock < i.safety_stock
        ]

    result = []
    for i in items:
        d = SupplyItemResponse.model_validate(i).model_dump()
        d["is_below_safety"] = i.current_stock < i.safety_stock
        result.append(d)
    return result


@router.get("/items/{item_id}")
async def get_item(
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    item = db.query(SupplyItem).filter(SupplyItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="物资不存在")

    await _check_and_create_restock(db, item, current_user.id)
    db.refresh(item)

    d = SupplyItemResponse.model_validate(item).model_dump()
    d["is_below_safety"] = item.current_stock < item.safety_stock

    pending_req = db.query(RestockRequest).filter(
        RestockRequest.supply_item_id == item.id,
        RestockRequest.status == ApprovalStatus.PENDING
    ).first()
    if pending_req:
        d["pending_restock_request_id"] = pending_req.id
        d["pending_restock_quantity"] = pending_req.quantity

    return d


@router.put("/items/{item_id}/stock", response_model=SupplyItemResponse)
async def update_stock(
    item_id: int,
    data: StockUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    item = db.query(SupplyItem).filter(SupplyItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="物资不存在")

    item.current_stock = max(0, data.quantity)
    if data.quantity > 0:
        item.last_restock = datetime.utcnow()
    db.commit()
    db.refresh(item)

    await _check_and_create_restock(db, item, current_user.id)
    return item


@router.post("/items/{item_id}/consume", response_model=SupplyItemResponse)
async def consume_stock(
    item_id: int,
    quantity: float,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    item = db.query(SupplyItem).filter(SupplyItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="物资不存在")
    if item.current_stock < quantity:
        raise HTTPException(status_code=400, detail="库存不足")

    item.current_stock -= quantity
    db.commit()
    db.refresh(item)

    await _check_and_create_restock(db, item, current_user.id)
    return item


@router.get("/dashboard")
def get_restock_dashboard(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    items = db.query(SupplyItem).all()
    categories = {}
    for item in items:
        cat = item.category or "未分类"
        if cat not in categories:
            categories[cat] = {
                "category": cat,
                "low_stock_count": 0,
                "pending_approval_count": 0,
                "approved_pending_supply_count": 0,
                "items": []
            }

        pending = db.query(RestockRequest).filter(
            RestockRequest.supply_item_id == item.id,
            RestockRequest.status == ApprovalStatus.PENDING
        ).first()
        approved = db.query(RestockRequest).filter(
            RestockRequest.supply_item_id == item.id,
            RestockRequest.status == ApprovalStatus.APPROVED,
            RestockRequest.supplier_notified == False
        ).first()

        is_low = item.current_stock < item.safety_stock
        if is_low:
            categories[cat]["low_stock_count"] += 1
        if pending:
            categories[cat]["pending_approval_count"] += 1
        if approved:
            categories[cat]["approved_pending_supply_count"] += 1

        if is_low or pending or approved:
            categories[cat]["items"].append({
                "item_id": item.id,
                "name": item.name,
                "code": item.code,
                "current_stock": item.current_stock,
                "safety_stock": item.safety_stock,
                "unit": item.unit,
                "is_low_stock": is_low,
                "gap": max(0, item.safety_stock - item.current_stock),
                "pending_request_id": pending.id if pending else None,
                "pending_quantity": pending.quantity if pending else None,
                "approved_request_id": approved.id if approved else None,
                "approved_quantity": approved.quantity if approved else None
            })

    sorted_cats = sorted(
        list(categories.values()),
        key=lambda c: (
            c["pending_approval_count"] * 3 + c["approved_pending_supply_count"] * 2 + c["low_stock_count"]
        ),
        reverse=True
    )

    return {
        "total_categories": len(sorted_cats),
        "total_low_stock": sum(c["low_stock_count"] for c in sorted_cats),
        "total_pending_approval": sum(c["pending_approval_count"] for c in sorted_cats),
        "total_approved_pending_supply": sum(c["approved_pending_supply_count"] for c in sorted_cats),
        "categories": sorted_cats
    }


@router.get("/restock-requests", response_model=List[RestockRequestResponse])
def list_restock_requests(
    status: Optional[ApprovalStatus] = None,
    supply_item_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(RestockRequest)
    if status:
        query = query.filter(RestockRequest.status == status)
    if supply_item_id:
        query = query.filter(RestockRequest.supply_item_id == supply_item_id)
    return query.order_by(RestockRequest.created_at.desc()).all()


@router.get("/restock-requests/{request_id}", response_model=RestockRequestResponse)
def get_restock_request(
    request_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    req = db.query(RestockRequest).filter(RestockRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="申请不存在")
    return req


@router.post("/restock-requests", response_model=RestockRequestResponse)
async def create_restock_request(
    supply_item_id: int,
    quantity: float,
    reason: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    item = db.query(SupplyItem).filter(SupplyItem.id == supply_item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="物资不存在")

    req = RestockRequest(
        supply_item_id=supply_item_id,
        quantity=quantity,
        reason=reason or "人工申请补货",
        applicant_id=current_user.id
    )
    db.add(req)
    db.commit()
    db.refresh(req)

    await push_approval_notification(db, req)
    return req


@router.post("/restock-requests/{request_id}/approve", response_model=RestockRequestResponse)
async def approve_restock_request(
    request_id: int,
    data: ApprovalAction,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role not in [UserRole.MANAGER, UserRole.ADMIN]:
        raise HTTPException(status_code=403, detail="无审批权限，需矿长或管理员")

    req = db.query(RestockRequest).filter(RestockRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="申请不存在")
    if req.status != ApprovalStatus.PENDING:
        raise HTTPException(status_code=400, detail="申请已处理")

    req.status = ApprovalStatus.APPROVED if data.approved else ApprovalStatus.REJECTED
    req.approver_id = data.approver_id
    req.approved_at = datetime.utcnow()

    if data.approved:
        req.supplier_notified = True

    db.commit()
    db.refresh(req)

    await push_approval_result_notification(db, req)

    if data.approved:
        item = db.query(SupplyItem).filter(SupplyItem.id == req.supply_item_id).first()
        if item and item.supplier:
            await push_system_notification(
                db,
                f"供应商通知: {item.name}",
                f"请供应{item.name} {req.quantity}{item.unit or ''}, 联系人供应商: {item.supplier}",
                [UserRole.SUPPLIER.value, UserRole.DISPATCHER.value]
            )

    return req
