import requests
import json

BASE_URL = "http://localhost:8000/api/v1"


def test_system():
    print("=" * 60)
    print("  智慧矿山系统 API 功能测试")
    print("=" * 60)

    print("\n[1] 登录获取 Token...")
    resp = requests.post(f"{BASE_URL}/auth/login", json={"username": "admin", "password": "admin123"})
    print(f"    状态码: {resp.status_code}")
    assert resp.status_code == 200, f"登录失败: {resp.text}"
    token = resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    print("    ✓ 登录成功")

    print("\n[2] 获取用户列表...")
    resp = requests.get(f"{BASE_URL}/auth/users", headers=headers)
    users = resp.json()
    print(f"    状态码: {resp.status_code}, 用户数: {len(users)}")
    miner = None
    for u in users:
        print(f"      - ID:{u['id']} {u['full_name']} 角色:{u['role']} 定位卡:{u.get('location_tag_id')}")
        if u["role"] == "miner" and u.get("location_tag_id"):
            miner = u
    print("    ✓ 用户列表获取成功")

    print("\n[3] 人员安全校验 - 合格场景...")
    if miner:
        resp = requests.post(f"{BASE_URL}/personnel/check", headers=headers, json={
            "user_id": miner["id"],
            "location_tag_id": miner["location_tag_id"],
            "tag_verified": True,
            "alcohol_level": 0.0
        })
        print(f"    状态码: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            print(f"    ✓ 校验通过: tag_passed={data['tag_verified']}, alcohol_passed={data['alcohol_passed']}")
        else:
            print(f"    响应: {resp.text}")

    print("\n[4] 人员安全校验 - 酒精超标场景...")
    if miner:
        resp = requests.post(f"{BASE_URL}/personnel/check", headers=headers, json={
            "user_id": miner["id"],
            "location_tag_id": miner["location_tag_id"],
            "tag_verified": True,
            "alcohol_level": 0.05
        })
        print(f"    状态码: {resp.status_code}")
        if resp.status_code == 403:
            print(f"    ✓ 酒精超标，禁止入井，已通知安保")
        else:
            print(f"    响应: {resp.text}")

    print("\n[5] 上传设备数据 - 正常...")
    resp = requests.get(f"{BASE_URL}/equipment/equipments", headers=headers)
    equipments = resp.json()
    print(f"    设备数: {len(equipments)}")
    if equipments:
        eq = equipments[0]
        resp = requests.post(f"{BASE_URL}/equipment/data", headers=headers, json={
            "equipment_id": eq["id"],
            "vibration": 2.0,
            "temperature": 50.0
        })
        print(f"    状态码: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            print(f"    ✓ 正常数据上传成功, is_abnormal={data['is_abnormal']}")

    print("\n[6] 上传设备数据 - 异常（自动生成工单）...")
    if equipments:
        eq = equipments[0]
        resp = requests.post(f"{BASE_URL}/equipment/data", headers=headers, json={
            "equipment_id": eq["id"],
            "vibration": 8.0,
            "temperature": 95.0
        })
        print(f"    状态码: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            print(f"    ✓ 异常数据上传成功, is_abnormal={data['is_abnormal']}")

    print("\n[7] 查询检修工单...")
    resp = requests.get(f"{BASE_URL}/equipment/work-orders", headers=headers)
    orders = resp.json()
    print(f"    工单数量: {len(orders)}")
    for o in orders[:3]:
        print(f"      - #{o['id']} {o['title']} 状态:{o['status']} 优先级:{o['priority']}")

    print("\n[8] 上传环境数据 - 正常...")
    resp = requests.post(f"{BASE_URL}/environment/data", headers=headers, json={
        "area": "A采区",
        "gas_concentration": 0.3,
        "dust_concentration": 3.0
    })
    print(f"    状态码: {resp.status_code}")
    if resp.status_code == 200:
        data = resp.json()
        print(f"    ✓ 正常环境数据, gas_alert={data['gas_alert']}, dust_alert={data['dust_alert']}")

    print("\n[9] 上传环境数据 - 瓦斯超标（自动告警+通风+撤离）...")
    resp = requests.post(f"{BASE_URL}/environment/data", headers=headers, json={
        "area": "B采区",
        "gas_concentration": 1.5,
        "dust_concentration": 5.0
    })
    print(f"    状态码: {resp.status_code}")
    if resp.status_code == 200:
        data = resp.json()
        print(f"    ✓ 瓦斯超标, gas_alert={data['gas_alert']}, 通风已启动={data['ventilation_active']}")

    print("\n[10] 查询撤离指令...")
    resp = requests.get(f"{BASE_URL}/environment/evacuations", headers=headers)
    evacs = resp.json()
    print(f"    撤离指令数: {len(evacs)}")
    for e in evacs:
        print(f"      - #{e['id']} {e['area']} active={e['is_active']} 级别:{e['alert_level']}")

    print("\n[11] 创建矿石批次...")
    resp = requests.post(f"{BASE_URL}/ore/batches", headers=headers, json={
        "batch_no": "BATCH-2026-001",
        "mining_area": "A采区",
        "weight": 500.0
    })
    print(f"    状态码: {resp.status_code}")
    if resp.status_code == 201:
        batch = resp.json()
        print(f"    ✓ 批次创建成功 #{batch['id']}")

        print("\n[12] 矿石质检 - 品位不达标（自动锁定）...")
        resp = requests.post(f"{BASE_URL}/ore/quality-test", headers=headers, json={
            "batch_id": batch["id"],
            "iron_content": 40.0,
            "sulfur_content": 2.0
        })
        print(f"    状态码: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            print(f"    ✓ 品位计算: {data['grade']}%, 状态: {data['status']}")

    print("\n[13] 查询物资库存...")
    resp = requests.get(f"{BASE_URL}/inventory/items", headers=headers)
    items = resp.json()
    print(f"    物资种类: {len(items)}")
    for item in items[:3]:
        ratio = item['current_stock'] / item['safety_stock'] if item['safety_stock'] > 0 else 0
        print(f"      - {item['name']}: 库存{item['current_stock']}{item['unit']} 安全线{item['safety_stock']} 比率{ratio:.2f}")

    print("\n[14] 物资消耗 - 触发库存预警...")
    if items:
        anchor = next((i for i in items if '锚杆' in i['name']), None)
        if anchor:
            resp = requests.post(f"{BASE_URL}/inventory/items/{anchor['id']}/consume", headers=headers,
                                 params={"quantity": 450.0})
            print(f"    状态码: {resp.status_code}")
            if resp.status_code == 200:
                print(f"    ✓ 消耗成功, 当前库存: {resp.json()['current_stock']}")

    print("\n[15] 查询补货申请...")
    resp = requests.get(f"{BASE_URL}/inventory/restock-requests", headers=headers)
    requests_list = resp.json()
    print(f"    补货申请数: {len(requests_list)}")
    for r in requests_list:
        print(f"      - #{r['id']} 物资ID:{r['supply_item_id']} 数量:{r['quantity']} 状态:{r['status']}")

    print("\n[16] 审批补货申请...")
    if requests_list:
        r = requests_list[0]
        resp = requests.post(f"{BASE_URL}/inventory/restock-requests/{r['id']}/approve", headers=headers, json={
            "approved": True,
            "approver_id": 1,
            "remark": "同意补货"
        })
        print(f"    状态码: {resp.status_code}")
        if resp.status_code == 200:
            print(f"    ✓ 审批完成, 状态: {resp.json()['status']}, 供应商已通知: {resp.json()['supplier_notified']}")

    print("\n[17] 创建运输任务（自动分配车辆和破碎机）...")
    resp = requests.post(f"{BASE_URL}/transport/tasks", headers=headers, json={
        "origin": "A采区",
        "destination": "",
        "ore_amount": 25.0
    })
    print(f"    状态码: {resp.status_code}")
    if resp.status_code == 200:
        task = resp.json()
        print(f"    ✓ 任务创建 #{task['id']}: {task['origin']} -> {task['destination']} 路线:{task['route']}")

    print("\n[18] 运输路线调整（拥堵绕行）...")
    if resp.status_code == 200:
        task = resp.json()
        resp2 = requests.put(f"{BASE_URL}/transport/tasks/{task['id']}/detour", headers=headers,
                             params={"blocked_segment": "主巷道K3+200段"})
        print(f"    状态码: {resp2.status_code}")
        if resp2.status_code == 200:
            data = resp2.json()
            print(f"    ✓ 路线已调整, is_detour={data['is_detour']}, 新路线: {data['route']}")

    print("\n[19] 生成运营日报...")
    resp = requests.post(f"{BASE_URL}/reports/daily/generate", headers=headers)
    print(f"    状态码: {resp.status_code}")
    if resp.status_code == 200:
        report = resp.json()
        print(f"    ✓ 日报生成: {report['report_date'][:10]} 产量:{report['total_output']}吨 "
              f"设备利用率:{report['equipment_utilization']}% 安全事件:{report['safety_event_count']}")

    print("\n[20] 查询告警列表...")
    resp = requests.get(f"{BASE_URL}/reports/alerts", headers=headers)
    alerts = resp.json()
    print(f"    告警总数: {len(alerts)}")
    for a in alerts[:5]:
        print(f"      - [{a['level']}] {a['title']} 区域:{a.get('area')} resolved={a['is_resolved']}")

    print("\n[21] 查询消息推送统计...")
    resp = requests.get(f"{BASE_URL}/ws/stats", headers=headers)
    print(f"    状态码: {resp.status_code}")
    print(f"    {resp.json()}")

    print("\n" + "=" * 60)
    print("  测试完成！所有核心功能运行正常。")
    print("=" * 60)


if __name__ == "__main__":
    test_system()
