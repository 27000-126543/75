from datetime import datetime
from typing import List, Optional, Dict, Any
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
from config import settings
from models import (
    SupplyItem, RestockRequest, ApprovalStatus,
    User, UserRole, Alert, AlertLevel, ShipmentStatus, RestockReceipt
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
            RestockRequest.shipment_status.in_([
                ShipmentStatus.APPROVED_WAITING_SUPPLIER,
                ShipmentStatus.IN_TRANSIT,
                ShipmentStatus.DELIVERED
            ])
        ).first()
        in_transit = db.query(RestockRequest).filter(
            RestockRequest.supply_item_id == item.id,
            RestockRequest.shipment_status == ShipmentStatus.IN_TRANSIT
        ).first()

        is_low = item.current_stock < item.safety_stock
        if is_low:
            categories[cat]["low_stock_count"] += 1
        if pending:
            categories[cat]["pending_approval_count"] += 1
        if approved:
            categories[cat]["approved_pending_supply_count"] += 1
        if in_transit:
            categories[cat].setdefault("in_transit_count", 0)
            categories[cat]["in_transit_count"] += 1

        shipment_status = None
        shipment_quantity = None
        shipment_request_id = None
        tracking = None
        if approved:
            shipment_status = approved.shipment_status.value if hasattr(approved.shipment_status, 'value') else str(approved.shipment_status)
            shipment_quantity = approved.quantity
            shipment_request_id = approved.id
            tracking = approved.tracking_number
        elif in_transit:
            shipment_status = in_transit.shipment_status.value if hasattr(in_transit.shipment_status, 'value') else str(in_transit.shipment_status)
            shipment_quantity = in_transit.quantity
            shipment_request_id = in_transit.id
            tracking = in_transit.tracking_number

        if is_low or pending or approved or in_transit:
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
                "shipment_status": shipment_status,
                "shipment_request_id": shipment_request_id,
                "shipment_quantity": shipment_quantity,
                "tracking_number": tracking
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
        req.shipment_status = ShipmentStatus.APPROVED_WAITING_SUPPLIER
    else:
        req.shipment_status = ShipmentStatus.CANCELLED

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


@router.post("/restock-requests/{request_id}/supplier-confirm")
async def supplier_confirm_shipment(
    request_id: int,
    tracking_number: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role not in [UserRole.SUPPLIER, UserRole.ADMIN, UserRole.DISPATCHER]:
        raise HTTPException(status_code=403, detail="无权限")

    req = db.query(RestockRequest).filter(RestockRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="申请不存在")
    if req.status != ApprovalStatus.APPROVED:
        raise HTTPException(status_code=400, detail="申请尚未审批通过")
    if req.shipment_status not in [ShipmentStatus.APPROVED_WAITING_SUPPLIER, ShipmentStatus.DELIVERED]:
        raise HTTPException(status_code=400, detail=f"当前状态{req.shipment_status}无法确认发货")

    req.shipment_status = ShipmentStatus.IN_TRANSIT
    req.supplier_confirmed_at = datetime.utcnow()
    req.tracking_number = tracking_number
    req.shipped_at = datetime.utcnow()
    db.commit()
    db.refresh(req)

    item = db.query(SupplyItem).filter(SupplyItem.id == req.supply_item_id).first()
    await push_system_notification(
        db,
        f"物资已发货: {item.name if item else '物资'}",
        f"补货申请#{req.id}已发货，数量{req.quantity}，运单号:{tracking_number or '无'}",
        [UserRole.MANAGER.value, UserRole.DISPATCHER.value]
    )
    return {
        "message": "供应商已确认发货",
        "request_id": req.id,
        "shipment_status": req.shipment_status.value if hasattr(req.shipment_status, 'value') else str(req.shipment_status),
        "tracking_number": req.tracking_number,
        "shipped_at": req.shipped_at
    }


@router.post("/restock-requests/{request_id}/mark-delivered")
async def mark_shipment_delivered(
    request_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    req = db.query(RestockRequest).filter(RestockRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="申请不存在")
    if req.shipment_status != ShipmentStatus.IN_TRANSIT:
        raise HTTPException(status_code=400, detail="物资还未发货")

    req.shipment_status = ShipmentStatus.DELIVERED
    req.delivered_at = datetime.utcnow()
    db.commit()
    db.refresh(req)
    return {
        "message": "已标记为送达，等待验收入库",
        "request_id": req.id,
        "shipment_status": req.shipment_status.value if hasattr(req.shipment_status, 'value') else str(req.shipment_status),
        "delivered_at": req.delivered_at
    }


class RestockReceiveIn(BaseModel):
    received_quantity: float
    qualified_quantity: Optional[float] = None
    unqualified_quantity: Optional[float] = 0
    inspection_result: str = "qualified"
    inspection_remark: Optional[str] = None
    batch_no: Optional[str] = None


@router.post("/restock-requests/{request_id}/receive")
async def receive_restock(
    request_id: int,
    data: RestockReceiveIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    req = db.query(RestockRequest).filter(RestockRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="申请不存在")
    if req.shipment_status not in [ShipmentStatus.DELIVERED, ShipmentStatus.IN_TRANSIT, ShipmentStatus.PARTIALLY_RECEIVED]:
        raise HTTPException(status_code=400, detail="物资尚未送达或已完成入库")

    item = db.query(SupplyItem).filter(SupplyItem.id == req.supply_item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="关联物资不存在")

    qualified = data.qualified_quantity if data.qualified_quantity is not None else data.received_quantity
    unqualified = data.unqualified_quantity or 0

    if qualified < 0 or data.received_quantity < 0:
        raise HTTPException(status_code=400, detail="入库数量不能为负数")

    if qualified + unqualified > data.received_quantity + 0.0001:
        raise HTTPException(status_code=400, detail="合格+不合格数量不能超过本批到货数量")

    receipt = RestockReceipt(
        restock_request_id=request_id,
        batch_no=data.batch_no,
        received_quantity=data.received_quantity,
        qualified_quantity=qualified,
        unqualified_quantity=unqualified,
        inspection_result=data.inspection_result,
        inspection_remark=data.inspection_remark,
        received_by=current_user.id,
        received_by_name=current_user.full_name,
        created_at=datetime.utcnow()
    )
    db.add(receipt)

    item.current_stock += qualified
    item.last_restock = datetime.utcnow()

    previous_receipts = db.query(RestockReceipt).filter(
        RestockReceipt.restock_request_id == request_id
    ).all()
    total_qualified = qualified
    for pr in previous_receipts:
        total_qualified += (pr.qualified_quantity or 0)

    if total_qualified >= req.quantity - 0.0001:
        req.shipment_status = ShipmentStatus.RECEIVED
    else:
        req.shipment_status = ShipmentStatus.PARTIALLY_RECEIVED

    req.received_quantity = (req.received_quantity or 0) + data.received_quantity
    req.received_by = current_user.id
    req.received_at = datetime.utcnow()
    req.received_remark = data.inspection_remark or req.received_remark
    db.commit()
    db.refresh(req)
    db.refresh(item)
    db.refresh(receipt)

    await push_system_notification(
        db,
        f"物资验收入库: {item.name}",
        f"{item.name} 本批到货{data.received_quantity}{item.unit or ''}，合格{qualified}{item.unit or ''}，不合格{unqualified}{item.unit or ''}，由{current_user.full_name}验收" +
        (f"，备注:{data.inspection_remark}" if data.inspection_remark else ""),
        [UserRole.MANAGER.value, UserRole.DISPATCHER.value]
    )

    return {
        "message": "入库成功",
        "request_id": req.id,
        "item_name": item.name,
        "receipt_id": receipt.id,
        "batch_no": data.batch_no,
        "received_quantity": data.received_quantity,
        "qualified_quantity": qualified,
        "unqualified_quantity": unqualified,
        "inspection_result": data.inspection_result,
        "total_qualified_so_far": total_qualified,
        "new_stock": item.current_stock,
        "shipment_status": req.shipment_status.value if hasattr(req.shipment_status, 'value') else str(req.shipment_status),
        "received_by": current_user.full_name,
        "received_at": req.received_at
    }


@router.get("/restock-requests/{request_id}/receipts")
def list_restock_receipts(
    request_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    req = db.query(RestockRequest).filter(RestockRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="申请不存在")
    receipts = db.query(RestockReceipt).filter(
        RestockReceipt.restock_request_id == request_id
    ).order_by(RestockReceipt.created_at.asc()).all()
    result = []
    total_qualified = 0
    total_received = 0
    for r in receipts:
        total_qualified += (r.qualified_quantity or 0)
        total_received += (r.received_quantity or 0)
        result.append({
            "id": r.id,
            "batch_no": r.batch_no,
            "received_quantity": r.received_quantity,
            "qualified_quantity": r.qualified_quantity,
            "unqualified_quantity": r.unqualified_quantity,
            "inspection_result": r.inspection_result,
            "inspection_remark": r.inspection_remark,
            "received_by_id": r.received_by,
            "received_by_name": r.received_by_name,
            "created_at": r.created_at
        })
    return {
        "request_id": request_id,
        "requested_quantity": req.quantity,
        "total_received": total_received,
        "total_qualified": total_qualified,
        "remaining_pending": max(req.quantity - total_qualified, 0),
        "shipment_status": req.shipment_status.value if hasattr(req.shipment_status, 'value') else str(req.shipment_status),
        "receipts": result
    }
