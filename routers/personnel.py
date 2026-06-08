from typing import List
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from database import get_db
from config import settings
from models import User, PersonnelCheck, UserRole, MinerLocationReport
from schemas import PersonnelCheckCreate, PersonnelCheckResponse
from routers.auth import get_current_user
from services.notification_service import push_security_notification

router = APIRouter(prefix="/personnel", tags=["人员安全校验"])


@router.post("/check")
async def personnel_safety_check(
    check_data: PersonnelCheckCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    user = db.query(User).filter(User.id == check_data.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    remark_parts = []
    tag_passed = True
    alcohol_passed = True

    if not check_data.tag_verified:
        tag_passed = False
        remark_parts.append("定位卡未验证")

    if tag_passed and not user.location_tag_id:
        tag_passed = False
        remark_parts.append("该用户未绑定定位卡")

    if tag_passed and user.location_tag_id != check_data.location_tag_id:
        tag_passed = False
        remark_parts.append("定位卡不匹配")

    alcohol_passed = check_data.alcohol_level <= settings.ALCOHOL_THRESHOLD
    if not alcohol_passed:
        remark_parts.append(f"酒精含量超标({check_data.alcohol_level} > {settings.ALCOHOL_THRESHOLD})")

    overall_passed = tag_passed and alcohol_passed

    check_record = PersonnelCheck(
        user_id=check_data.user_id,
        location_tag_id=check_data.location_tag_id,
        tag_verified=tag_passed,
        alcohol_level=check_data.alcohol_level,
        alcohol_passed=alcohol_passed,
        overall_passed=overall_passed,
        remark="; ".join(remark_parts) if remark_parts else None
    )
    db.add(check_record)
    db.commit()
    db.refresh(check_record)

    if not overall_passed:
        await push_security_notification(
            db, user,
            "; ".join(remark_parts) if remark_parts else "安全检查未通过"
        )
        raise HTTPException(
            status_code=403,
            detail={
                "message": "安全校验未通过，禁止入井",
                "check_id": check_record.id,
                "reasons": remark_parts
            }
        )

    return {
        "id": check_record.id,
        "user_id": check_record.user_id,
        "location_tag_id": check_record.location_tag_id,
        "tag_verified": check_record.tag_verified,
        "alcohol_level": check_record.alcohol_level,
        "alcohol_passed": check_record.alcohol_passed,
        "overall_passed": check_record.overall_passed,
        "check_time": check_record.check_time,
        "remark": check_record.remark,
        "message": "安全校验通过，允许入井",
        "check_id": check_record.id
    }


@router.get("/checks", response_model=List[PersonnelCheckResponse])
def list_checks(
    user_id: int = None,
    passed: bool = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(PersonnelCheck)
    if user_id:
        query = query.filter(PersonnelCheck.user_id == user_id)
    if passed is not None:
        query = query.filter(PersonnelCheck.overall_passed == passed)
    return query.order_by(PersonnelCheck.check_time.desc()).limit(limit).all()


@router.get("/checks/{check_id}", response_model=PersonnelCheckResponse)
def get_check(
    check_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    check = db.query(PersonnelCheck).filter(PersonnelCheck.id == check_id).first()
    if not check:
        raise HTTPException(status_code=404, detail="记录不存在")
    return check


class LocationReportIn(BaseModel):
    user_id: int
    area: str


@router.post("/location-report")
def report_location(
    data: LocationReportIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    user = db.query(User).filter(User.id == data.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    report = MinerLocationReport(user_id=data.user_id, area=data.area, reported_at=datetime.utcnow())
    db.add(report)
    db.commit()
    db.refresh(report)
    return {"id": report.id, "user_id": data.user_id, "area": data.area, "reported_at": report.reported_at,
            "message": "位置上报成功"}


@router.get("/location/latest/{user_id}")
def get_latest_location(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    report = db.query(MinerLocationReport).filter(MinerLocationReport.user_id == user_id).order_by(
        MinerLocationReport.reported_at.desc()).first()
    return {
        "user_id": user_id,
        "area": report.area if report else None,
        "reported_at": report.reported_at if report else None
    }
