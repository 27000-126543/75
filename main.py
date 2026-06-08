import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from config import settings
from database import engine, SessionLocal, Base
from models import User, UserRole, SupplyItem, Crusher, MiningEquipment, TransportVehicle
from routers import auth, personnel, equipment, environment, transport, ore_quality, inventory, reports, notifications
from scheduler import start_scheduler, shutdown_scheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def init_database():
    Base.metadata.create_all(bind=engine)
    logger.info("数据库表已创建")


def seed_initial_data():
    from routers.auth import get_password_hash
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.username == "admin").first()
        if not admin:
            admin = User(
                username="admin",
                password_hash=get_password_hash("admin123"),
                full_name="系统管理员",
                role=UserRole.ADMIN,
                phone="13800000000",
                email="admin@mine.com"
            )
            db.add(admin)

        users_to_create = [
            {"username": "dispatcher", "password": "disp123", "full_name": "调度员张工", "role": UserRole.DISPATCHER, "phone": "13800000001"},
            {"username": "security", "password": "sec123", "full_name": "安保员李工", "role": UserRole.SECURITY, "phone": "13800000002"},
            {"username": "maint1", "password": "maint123", "full_name": "维修工王师傅", "role": UserRole.MAINTENANCE, "phone": "13800000003", "skills": "采掘设备,液压系统,电气"},
            {"username": "maint2", "password": "maint123", "full_name": "维修工赵师傅", "role": UserRole.MAINTENANCE, "phone": "13800000004", "skills": "运输设备,通风系统"},
            {"username": "driver1", "password": "drv123", "full_name": "司机老孙", "role": UserRole.DRIVER, "phone": "13800000005"},
            {"username": "driver2", "password": "drv123", "full_name": "司机老周", "role": UserRole.DRIVER, "phone": "13800000006"},
            {"username": "miner1", "password": "miner123", "full_name": "矿工小陈", "role": UserRole.MINER, "phone": "13800000007", "location_tag_id": "TAG-001"},
            {"username": "miner2", "password": "miner123", "full_name": "矿工小刘", "role": UserRole.MINER, "phone": "13800000008", "location_tag_id": "TAG-002"},
            {"username": "engineer", "password": "eng123", "full_name": "配矿工程师吴工", "role": UserRole.ENGINEER, "phone": "13800000009"},
            {"username": "manager", "password": "mgr123", "full_name": "矿长郑总", "role": UserRole.MANAGER, "phone": "13800000010"},
            {"username": "supplier", "password": "sup123", "full_name": "供应商联系人", "role": UserRole.SUPPLIER, "phone": "13800000011"},
        ]

        for u in users_to_create:
            existing = db.query(User).filter(User.username == u["username"]).first()
            if not existing:
                user = User(
                    username=u["username"],
                    password_hash=get_password_hash(u["password"]),
                    full_name=u["full_name"],
                    role=u["role"],
                    phone=u.get("phone"),
                    location_tag_id=u.get("location_tag_id"),
                    skills=u.get("skills")
                )
                db.add(user)

        supplies = [
            {"name": "锚杆", "code": "SUP-ANCHOR-001", "category": "支护材料", "unit": "根", "current_stock": 500, "safety_stock": 1000, "supplier": "山东恒泰矿山设备有限公司"},
            {"name": "乳化炸药", "code": "SUP-EXPL-001", "category": "爆破材料", "unit": "箱", "current_stock": 200, "safety_stock": 500, "supplier": "北方化工有限公司"},
            {"name": "雷管", "code": "SUP-DET-001", "category": "爆破材料", "unit": "发", "current_stock": 5000, "safety_stock": 10000, "supplier": "北方化工有限公司"},
            {"name": "液压油", "code": "SUP-OIL-001", "category": "润滑材料", "unit": "桶", "current_stock": 50, "safety_stock": 100, "supplier": "长城润滑油有限公司"},
            {"name": "输送带", "code": "SUP-BELT-001", "category": "输送设备", "unit": "米", "current_stock": 100, "safety_stock": 500, "supplier": "青岛橡六有限公司"},
        ]
        for s in supplies:
            existing = db.query(SupplyItem).filter(SupplyItem.code == s["code"]).first()
            if not existing:
                db.add(SupplyItem(**s))

        crushers = [
            {"name": "1号破碎机", "max_load": 100.0, "efficiency": 1.0},
            {"name": "2号破碎机", "max_load": 120.0, "efficiency": 0.9},
        ]
        for c in crushers:
            existing = db.query(Crusher).filter(Crusher.name == c["name"]).first()
            if not existing:
                db.add(Crusher(**c))

        equipments = [
            {"name": "1号综采机", "code": "EQ-CM-001", "type": "采掘设备", "location_area": "A采区"},
            {"name": "2号综采机", "code": "EQ-CM-002", "type": "采掘设备", "location_area": "B采区"},
            {"name": "1号掘进机", "code": "EQ-EB-001", "type": "掘进设备", "location_area": "C采区"},
            {"name": "1号通风机", "code": "EQ-VT-001", "type": "通风设备", "location_area": "主巷道"},
            {"name": "1号液压支架", "code": "EQ-HY-001", "type": "支护设备", "location_area": "A采区"},
        ]
        for e in equipments:
            existing = db.query(MiningEquipment).filter(MiningEquipment.code == e["code"]).first()
            if not existing:
                db.add(MiningEquipment(**e))

        vehicles = [
            {"plate_number": "矿A-001", "capacity": 30.0, "current_location": "A采区"},
            {"plate_number": "矿A-002", "capacity": 35.0, "current_location": "B采区"},
            {"plate_number": "矿A-003", "capacity": 40.0, "current_location": "主井车场"},
        ]
        driver_users = db.query(User).filter(User.role == UserRole.DRIVER).all()
        for i, v in enumerate(vehicles):
            existing = db.query(TransportVehicle).filter(TransportVehicle.plate_number == v["plate_number"]).first()
            if not existing:
                tv = TransportVehicle(**v)
                if i < len(driver_users):
                    tv.driver_id = driver_users[i].id
                db.add(tv)

        db.commit()
        logger.info("初始数据已加载")
    except Exception as e:
        logger.error(f"初始数据加载失败: {e}")
        db.rollback()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_database()
    seed_initial_data()
    start_scheduler()
    yield
    shutdown_scheduler()


app = FastAPI(
    title=settings.APP_NAME,
    description="智慧矿山安全生产与智能调度系统 API",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix=settings.API_PREFIX)
app.include_router(personnel.router, prefix=settings.API_PREFIX)
app.include_router(equipment.router, prefix=settings.API_PREFIX)
app.include_router(environment.router, prefix=settings.API_PREFIX)
app.include_router(transport.router, prefix=settings.API_PREFIX)
app.include_router(ore_quality.router, prefix=settings.API_PREFIX)
app.include_router(inventory.router, prefix=settings.API_PREFIX)
app.include_router(reports.router, prefix=settings.API_PREFIX)
app.include_router(notifications.router, prefix=settings.API_PREFIX)


@app.get("/")
def root():
    return {
        "name": settings.APP_NAME,
        "version": "1.0.0",
        "docs": "/docs",
        "api_prefix": settings.API_PREFIX
    }


@app.get("/health")
def health_check():
    return {"status": "ok", "timestamp": __import__("datetime").datetime.utcnow().isoformat()}
