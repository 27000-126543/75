import requests, json
from database import SessionLocal
from models import User, UserRole, MinerLocationReport, EvacuationConfirmation

db = SessionLocal()

print("=== Users ===")
users = db.query(User).filter(User.role == UserRole.MINER, User.is_active == True).all()
for u in users:
    print(f"  id={u.id}, name={u.full_name}, role={u.role}, location_tag_id={u.location_tag_id}")

print("\n=== MinerLocationReport ===")
for r in db.query(MinerLocationReport).all():
    print(f"  id={r.id}, user_id={r.user_id}, area={r.area}")

print("\n=== EvacuationConfirmation ===")
for c in db.query(EvacuationConfirmation).all():
    print(f"  id={c.id}, evac_id={c.evacuation_id}, user_id={c.user_id}, area={c.area}, confirmed={c.confirmed}")
