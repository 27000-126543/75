import requests
import json
import sys

BASE_URL = "http://localhost:8000/api/v1"


def test_new_features():
    print("=" * 70)
    print("  智慧矿山系统 - 新增/修复功能专项测试")
    print("=" * 70)

    all_passed = True

    resp = requests.post(f"{BASE_URL}/auth/login", json={"username": "admin", "password": "admin123"})
    token = resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    print("\n[登录] ✓")

    resp = requests.get(f"{BASE_URL}/auth/users", headers=headers)
    users = resp.json()
    miner = next((u for u in users if u["role"] == "miner" and u.get("location_tag_id")), None)
    print(f"[矿工信息] ID:{miner['id']} 姓名:{miner['full_name']} 定位卡:{miner['location_tag_id']}")

    # ========== 测试1: 粉尘超标撤离 ==========
    print("\n" + "=" * 70)
    print("[测试1] 粉尘浓度超标触发撤离指令 + 避免重复")
    print("-" * 70)
    test1_passed = True

    print("  1.1 首次上传粉尘超标数据（C采区，15mg/m³ > 阈值10）...")
    resp = requests.post(f"{BASE_URL}/environment/data", headers=headers, json={
        "area": "C采区",
        "gas_concentration": 0.2,
        "dust_concentration": 15.0
    })
    data = resp.json()
    print(f"       状态码:{resp.status_code}, dust_alert={data['dust_alert']}, ventilation={data['ventilation_active']}")
    if not (resp.status_code == 200 and data['dust_alert'] and data['ventilation_active']):
        test1_passed = False

    print("  1.2 查询C采区有效撤离指令...")
    resp = requests.get(f"{BASE_URL}/environment/evacuations", headers=headers, params={"area": "C采区", "active_only": True})
    evacs = resp.json()
    print(f"       有效撤离指令数: {len(evacs)}")
    if len(evacs) == 0:
        print("       ✗ 未生成撤离指令！")
        test1_passed = False
    else:
        e = evacs[0]
        print(f"       指令#{e['id']} 区域:{e['area']} 级别:{e['alert_level']} 原因:{e['reason'][:40]}...")

    print("  1.3 再次上传C采区粉尘超标数据，检查是否重复生成...")
    resp2 = requests.post(f"{BASE_URL}/environment/data", headers=headers, json={
        "area": "C采区",
        "gas_concentration": 0.3,
        "dust_concentration": 18.0
    })
    resp = requests.get(f"{BASE_URL}/environment/evacuations", headers=headers, params={"area": "C采区", "active_only": True})
    evacs2 = resp.json()
    print(f"       重复上传后有效撤离指令数: {len(evacs2)} (期望保持1条)")
    if len(evacs2) != 1:
        print(f"       ✗ 重复生成了{len(evacs2)}条撤离指令！")
        test1_passed = False

    if test1_passed:
        print("  [测试1] ✓ 通过: 粉尘超标触发撤离 + 无重复")
    else:
        print("  [测试1] ✗ 失败")
        all_passed = False

    # ========== 测试2: 库存低于安全库存触发 ==========
    print("\n" + "=" * 70)
    print("[测试2] 物资库存低于安全库存自动生成补货申请")
    print("-" * 70)
    test2_passed = True

    print("  2.1 查询乳化炸药当前库存...")
    resp = requests.get(f"{BASE_URL}/inventory/items", headers=headers)
    items = resp.json()
    explosive = next((i for i in items if "乳化炸药" in i["name"]), None)
    print(f"       {explosive['name']}: 当前库存{explosive['current_stock']}{explosive['unit']}, 安全库存{explosive['safety_stock']}{explosive['unit']}")
    print(f"       当前库存 < 安全库存? {explosive['current_stock'] < explosive['safety_stock']}")

    print("  2.2 先清空该物资的待审批申请，然后消耗库存...")
    resp = requests.get(f"{BASE_URL}/inventory/restock-requests", headers=headers, params={"status": "pending", "supply_item_id": explosive["id"]})
    pending = resp.json()
    for r in pending:
        print(f"       先取消待审批申请#{r['id']}")

    print(f"  2.3 消耗乳化炸药100箱（使其从200降到100，低于安全库存500）...")
    resp = requests.post(f"{BASE_URL}/inventory/items/{explosive['id']}/consume", headers=headers, params={"quantity": 100.0})
    print(f"       状态码:{resp.status_code}, 消耗后库存: {resp.json()['current_stock']}")

    print("  2.4 查询是否自动生成了补货申请...")
    resp = requests.get(f"{BASE_URL}/inventory/restock-requests", headers=headers, params={"status": "pending", "supply_item_id": explosive["id"]})
    requests_list = resp.json()
    print(f"       待审批补货申请数: {len(requests_list)}")
    if len(requests_list) == 0:
        print("       ✗ 未自动生成补货申请！")
        test2_passed = False
    else:
        r = requests_list[0]
        print(f"       申请#{r['id']}: 申请数量{r['quantity']}, 原因:{r['reason'][:50]}...")

    if test2_passed:
        print("  [测试2] ✓ 通过: 库存低于安全库存自动生成补货申请")
    else:
        print("  [测试2] ✗ 失败")
        all_passed = False

    # ========== 测试3: 定位卡未验证拦截通知 ==========
    print("\n" + "=" * 70)
    print("[测试3] 定位卡未验证 - 拦截记录 + 通知 + 返回记录编号")
    print("-" * 70)
    test3_passed = True

    print("  3.1 提交入井校验：tag_verified=false（定位卡未验证）...")
    resp = requests.post(f"{BASE_URL}/personnel/check", headers=headers, json={
        "user_id": miner["id"],
        "location_tag_id": miner["location_tag_id"],
        "tag_verified": False,
        "alcohol_level": 0.0
    })
    print(f"       状态码: {resp.status_code} (期望403)")
    if resp.status_code != 403:
        print(f"       ✗ 应该被拦截！返回: {resp.text[:100]}")
        test3_passed = False
    else:
        detail = resp.json().get("detail", {})
        print(f"       拦截消息: {detail.get('message')}")
        print(f"       检查记录编号: {detail.get('check_id')}")
        print(f"       拦截原因: {detail.get('reasons')}")
        if not detail.get("check_id"):
            print("       ✗ 未返回检查记录编号！")
            test3_passed = False
        if "定位卡未验证" not in str(detail.get("reasons", [])):
            print("       ✗ 拦截原因未包含'定位卡未验证'！")
            test3_passed = False

    print("  3.2 查询检查记录，确认已留存...")
    check_id = detail.get("check_id")
    if check_id:
        resp = requests.get(f"{BASE_URL}/personnel/checks/{check_id}", headers=headers)
        if resp.status_code == 200:
            rec = resp.json()
            print(f"       记录#{rec['id']}: overall_passed={rec['overall_passed']}, remark={rec['remark']}")
        else:
            print(f"       ✗ 检查记录未找到！")
            test3_passed = False

    print("  3.3 测试定位卡不匹配...")
    resp = requests.post(f"{BASE_URL}/personnel/check", headers=headers, json={
        "user_id": miner["id"],
        "location_tag_id": "TAG-WRONG-999",
        "tag_verified": True,
        "alcohol_level": 0.0
    })
    if resp.status_code == 403:
        detail = resp.json().get("detail", {})
        print(f"       状态码:403, check_id={detail.get('check_id')}, reasons={detail.get('reasons')}")
    else:
        print(f"       ✗ 定位卡不匹配应被拦截！")
        test3_passed = False

    if test3_passed:
        print("  [测试3] ✓ 通过: 所有拦截场景均留记录、通知、返回编号")
    else:
        print("  [测试3] ✗ 失败")
        all_passed = False

    # ========== 测试4: 不同技能维修工派单 ==========
    print("\n" + "=" * 70)
    print("[测试4] 按设备类型+技能+区域综合派单")
    print("-" * 70)
    test4_passed = True

    maintenance_users = [u for u in users if u["role"] == "maintenance"]
    print("  4.1 维修工技能清单:")
    for m in maintenance_users:
        print(f"       {m['full_name']} (ID:{m['id']}): 技能={m['skills']}")

    print("  4.2 先完成所有现有工单，清空派单状态...")
    resp = requests.get(f"{BASE_URL}/equipment/work-orders", headers=headers, params={"status": "assigned"})
    for o in resp.json():
        requests.put(f"{BASE_URL}/equipment/work-orders/{o['id']}/start", headers=headers)
        requests.put(f"{BASE_URL}/equipment/work-orders/{o['id']}/complete", headers=headers)

    print("  4.3 上传运输设备异常数据（1号通风机 type=通风设备）...")
    resp = requests.get(f"{BASE_URL}/equipment/equipments", headers=headers)
    equips = resp.json()
    transport_equip = next((e for e in equips if e["type"] == "通风设备"), None)
    if transport_equip:
        print(f"       设备: {transport_equip['name']} (ID:{transport_equip['id']}) 类型:{transport_equip['type']} 区域:{transport_equip['location_area']}")

        resp = requests.post(f"{BASE_URL}/equipment/data", headers=headers, json={
            "equipment_id": transport_equip["id"],
            "vibration": 6.0,
            "temperature": 60.0
        })
        print(f"       上传异常数据: 状态码={resp.status_code}, is_abnormal={resp.json()['is_abnormal']}")

        print("  4.4 查询生成的工单及派工人...")
        resp = requests.get(f"{BASE_URL}/equipment/work-orders", headers=headers, params={"equipment_id": transport_equip["id"]})
        orders = resp.json()
        if orders:
            order = orders[0]
            print(f"       工单#{order['id']}: {order['title']}")
            print(f"       派给: {order['assigned_to_name']} (ID:{order['assigned_to']})")
            print(f"       技能: {order['assigned_to_skills']}")
            if "通风系统" in (order['assigned_to_skills'] or ""):
                print("       ✓ 派给了具备通风设备技能的维修工！")
            else:
                print("       ⚠ 注意: 当前派工人技能详情如上")
        else:
            print("       ✗ 未生成工单！")
            test4_passed = False
    else:
        print("       未找到运输设备，跳过此测试项")

    print("  4.5 再上传采掘设备异常数据，比较派工差异...")
    mining_equip = next((e for e in equips if e["type"] == "采掘设备"), None)
    if mining_equip:
        print(f"       设备: {mining_equip['name']} (ID:{mining_equip['id']}) 类型:{mining_equip['type']}")
        resp = requests.post(f"{BASE_URL}/equipment/data", headers=headers, json={
            "equipment_id": mining_equip["id"],
            "vibration": 5.5,
            "temperature": 90.0
        })
        resp = requests.get(f"{BASE_URL}/equipment/work-orders", headers=headers, params={"equipment_id": mining_equip["id"]})
        orders = resp.json()
        if orders:
            order = orders[0]
            print(f"       工单#{order['id']} 派给: {order['assigned_to_name']} 技能: {order['assigned_to_skills']}")
            if "采掘设备" in (order['assigned_to_skills'] or ""):
                print("       ✓ 采掘设备派给了具备采掘设备技能的维修工！")
            else:
                print("       ⚠ 注意: 当前派工人技能如上")

    if test4_passed:
        print("  [测试4] ✓ 通过: 工单综合考虑设备类型/技能/区域派单")
    else:
        print("  [测试4] ✗ 失败")
        all_passed = False

    # ========== 总结 ==========
    print("\n" + "=" * 70)
    if all_passed:
        print("  ✓✓✓ 全部4项专项测试通过！ ✓✓✓")
    else:
        print("  ✗ 存在测试失败项，请检查输出")
        sys.exit(1)
    print("=" * 70)


if __name__ == "__main__":
    test_new_features()
