from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
from config import settings
from models import OreBatch, OreBatchStatus, User, UserRole, Alert, AlertLevel
from schemas import (
    OreBatchCreate, OreBatchResponse, OreQualityTest
)
from routers.auth import get_current_user
from services.notification_service import push_alert_notification, push_system_notification

router = APIRouter(prefix="/ore", tags=["矿石质检与配矿"])


def _calculate_grade(iron_content: float, sulfur_content: float) -> float:
    sulfur_penalty = sulfur_content * 2
    grade = max(0.0, iron_content - sulfur_penalty)
    return round(grade, 2)


@router.post("/batches", response_model=OreBatchResponse, status_code=201)
def create_ore_batch(
    data: OreBatchCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    existing = db.query(OreBatch).filter(OreBatch.batch_no == data.batch_no).first()
    if existing:
        raise HTTPException(status_code=400, detail="批次号已存在")
    batch = OreBatch(**data.model_dump())
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


@router.get("/batches", response_model=List[OreBatchResponse])
def list_batches(
    status: Optional[OreBatchStatus] = None,
    mining_area: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(OreBatch)
    if status:
        query = query.filter(OreBatch.status == status)
    if mining_area:
        query = query.filter(OreBatch.mining_area == mining_area)
    return query.order_by(OreBatch.sampled_at.desc()).all()


@router.get("/batches/{batch_id}", response_model=OreBatchResponse)
def get_batch(
    batch_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    batch = db.query(OreBatch).filter(OreBatch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    return batch


@router.post("/quality-test", response_model=OreBatchResponse)
async def submit_quality_test(
    data: OreQualityTest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    batch = db.query(OreBatch).filter(OreBatch.id == data.batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")

    grade = _calculate_grade(data.iron_content, data.sulfur_content)
    batch.iron_content = data.iron_content
    batch.sulfur_content = data.sulfur_content
    batch.grade = grade
    batch.tested_at = datetime.utcnow()

    if grade < settings.ORE_GRADE_THRESHOLD:
        batch.status = OreBatchStatus.LOCKED
        batch.locked_reason = (
            f"品位不足: 计算品位{grade}% < 阈值{settings.ORE_GRADE_THRESHOLD}% "
            f"(铁含量{data.iron_content}%, 硫含量{data.sulfur_content}%)"
        )

        alert = Alert(
            title=f"矿石批次{batch.batch_no}品位不达标",
            content=batch.locked_reason,
            level=AlertLevel.WARNING,
            source_type="ore_quality",
            source_id=batch.id,
            area=batch.mining_area
        )
        db.add(alert)
        db.commit()
        db.refresh(batch)
        db.refresh(alert)

        await push_alert_notification(db, alert)
        await push_system_notification(
            db,
            f"批次{batch.batch_no}已锁定，请配矿工程师调整",
            batch.locked_reason or "品位不达标",
            [UserRole.ENGINEER.value, UserRole.DISPATCHER.value]
        )
    else:
        batch.status = OreBatchStatus.QUALIFIED
        db.commit()
        db.refresh(batch)

    return batch


@router.post("/batches/{batch_id}/unlock", response_model=OreBatchResponse)
async def unlock_batch(
    batch_id: int,
    blend_note: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role not in [UserRole.ENGINEER, UserRole.MANAGER, UserRole.ADMIN]:
        raise HTTPException(status_code=403, detail="无权限解锁批次")

    batch = db.query(OreBatch).filter(OreBatch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    if batch.status != OreBatchStatus.LOCKED:
        raise HTTPException(status_code=400, detail="批次未锁定")

    batch.status = OreBatchStatus.BLENDED
    batch.locked_reason = (batch.locked_reason or "") + f" [已配矿: {blend_note or '已调整'}]"
    db.commit()
    db.refresh(batch)

    await push_system_notification(
        db,
        f"批次{batch.batch_no}已解锁配矿",
        blend_note or "配矿工程师已调整方案",
        [UserRole.DISPATCHER.value, UserRole.MANAGER.value]
    )
    return batch


@router.post("/batches/{batch_id}/blend-with")
async def blend_batches(
    batch_id: int,
    other_batch_ids: List[int],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role not in [UserRole.ENGINEER, UserRole.MANAGER, UserRole.ADMIN]:
        raise HTTPException(status_code=403, detail="无权限执行配矿")

    main_batch = db.query(OreBatch).filter(OreBatch.id == batch_id).first()
    if not main_batch:
        raise HTTPException(status_code=404, detail="主批次不存在")

    other_batches = db.query(OreBatch).filter(OreBatch.id.in_(other_batch_ids)).all()
    if len(other_batches) != len(other_batch_ids):
        raise HTTPException(status_code=400, detail="存在无效的批次ID")

    total_weight = main_batch.weight
    total_iron = main_batch.iron_content * main_batch.weight if main_batch.iron_content else 0
    total_sulfur = main_batch.sulfur_content * main_batch.weight if main_batch.sulfur_content else 0

    for b in other_batches:
        total_weight += b.weight
        if b.iron_content:
            total_iron += b.iron_content * b.weight
        if b.sulfur_content:
            total_sulfur += b.sulfur_content * b.weight

    if total_weight > 0:
        avg_iron = total_iron / total_weight
        avg_sulfur = total_sulfur / total_weight
        blended_grade = _calculate_grade(avg_iron, avg_sulfur)
    else:
        blended_grade = 0
        avg_iron = 0
        avg_sulfur = 0

    passed = blended_grade >= settings.ORE_GRADE_THRESHOLD

    if passed:
        main_batch.status = OreBatchStatus.BLENDED
        main_batch.grade = blended_grade
        main_batch.iron_content = avg_iron
        main_batch.sulfur_content = avg_sulfur
        main_batch.locked_reason = None

        for b in other_batches:
            b.status = OreBatchStatus.BLENDED
            b.locked_reason = f"已与批次{main_batch.batch_no}配矿合并"

        db.commit()
        await push_system_notification(
            db,
            f"配矿成功: 综合品位{blended_grade}%",
            f"批次{main_batch.batch_no}与{len(other_batches)}个批次配矿完成",
            [UserRole.DISPATCHER.value, UserRole.MANAGER.value]
        )
    else:
        raise HTTPException(
            status_code=400,
            detail=f"配矿后品位仍不足: {blended_grade}% < {settings.ORE_GRADE_THRESHOLD}%"
        )

    return {
        "success": True,
        "blended_grade": blended_grade,
        "avg_iron": avg_iron,
        "avg_sulfur": avg_sulfur,
        "total_weight": total_weight
    }


@router.get("/grade-threshold")
def get_grade_threshold():
    return {"threshold": settings.ORE_GRADE_THRESHOLD}
