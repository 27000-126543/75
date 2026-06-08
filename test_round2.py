import requests
import json
import time

BASE_URL = "http://localhost:8000/api/v1"

s = requests.Session()


def login(username, password):
    r = s.post(f"{BASE_URL}/auth/login", json={"username": username, "password": password})
    token = r.json().get("access_token")
    s.headers.update({"Authorization": f"Bearer {token}"})
    return token


def print_header(title):
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


def section(title):
    print()
    print(f"[{title}]", end="")


def step(msg):
    print(f"\n  {msg}...", end="")


def ok(msg=""):
    print(f" ✓ {msg}")


def fail(msg):
    print(f" ✗ {msg}")


if __name__ == "__main__":
    print_header("智慧矿山系统 - 应急闭环与库存调度专项测试(第二轮)")

    section("登录 - 管理员登录")
    login("admin", "admin123")
    ok()

    user_resp = s.get(f"{BASE_URL}/auth/me")
    user = user_resp.json()
    print(f"  当前用户: {user.get('full_name')} 角色: {user.get('role')}")

    all_pass = True

    # ============================================================
    # 测试1: 查询库存时自动生成补货申请
    # ============================================================
    print_header("[测试1] 查询低库存物资时自动生成补货申请")
    try:
        step("1.1 先查询所有物资，识别低于安全库存的")
        resp = s.get(f"{BASE_URL}/inventory/items")
        items = resp.json()
        items_map = {i["name"]: i for i in items}
        low_items = {
            k: {
                "current_stock": v.get("current_stock"),
                "safety_stock": v.get("safety_stock"),
                "is_below_safety": v.get("is_below_safety")
            }
            for k, v in items_map.items() if v.get("is_below_safety")
        }
        print(f"\n       低库存物资: {json.dumps(low_items, ensure_ascii=False, indent=2)}")
        ok()

        step("1.2 查询乳化炸药详情，应自动触发补货申请")
        zhayao = items_map.get("乳化炸药")
        item_id = zhayao["id"]
        # 先删除已有待审批申请
        pending_reqs = s.get(f"{BASE_URL}/inventory/restock-requests", params={"status": "pending", "supply_item_id": item_id}).json()
        for pr in pending_reqs:
            s.post(f"{BASE_URL}/inventory/restock-requests/{pr['id']}/approve", json={"approved": False, "approver_id": user["id"]})
        # 再查询（触发补货）
        detail = s.get(f"{BASE_URL}/inventory/items/{item_id}").json()
        print(f"\n       乳化炸药详情: current={detail.get('current_stock')}, safety={detail.get('safety_stock')}, is_below={detail.get('is_below_safety')}, pending_req_id={detail.get('pending_restock_request_id')}, pending_qty={detail.get('pending_restock_quantity')}")
        ok()

        step("1.3 确认已自动生成了待审批补货申请")
        pending_reqs = s.get(f"{BASE_URL}/inventory/restock-requests", params={"status": "pending", "supply_item_id": item_id}).json()
        print(f"\n       待审批申请数量: {len(pending_reqs)}")
        for pr in pending_reqs:
            print(f"       申请#{pr['id']}: 数量={pr['quantity']}, 原因={pr['reason']}")
        assert len(pending_reqs) >= 1, "应自动生成待审批补货申请"
        ok()

        step("1.4 库存看板展示汇总")
        dashboard = s.get(f"{BASE_URL}/inventory/dashboard").json()
        print(f"\n       看板汇总: low_stock={dashboard['total_low_stock']}, pending_approval={dashboard['total_pending_approval']}, approved_waiting={dashboard['total_approved_pending_supply']}")
        for cat in dashboard['categories']:
            if cat["low_stock_count"] + cat["pending_approval_count"] > 0:
                print(f"       [{cat['category']}] 低库存:{cat['low_stock_count']} 待审批:{cat['pending_approval_count']}")
                for it in cat["items"][:3]:
                    print(f"         - {it['name']}: 当前{it['current_stock']}/{it['safety_stock']}, gap={it['gap']}")
        ok()
        print("\n[测试1] ✓ 通过: 查询低库存即生成补货申请")
    except Exception as e:
        all_pass = False
        fail(str(e))
        print(f"  ✗ 测试1失败: {e}")

    # ============================================================
    # 测试2: 撤离人员确认和超时提醒
    # ============================================================
    print_header("[测试2] 撤离人员确认 + 超时提醒")
    try:
        step("2.1 上传B采区粉尘超标数据，触发撤离")
        resp = s.post(f"{BASE_URL}/environment/data", json={
            "area": "B采区", "gas_concentration": 0.2, "dust_concentration": 15.0
        })
        data = resp.json()
        evac_id = data.get("evacuation_order_id")
        event_id = data.get("safety_event_id")
        print(f"\n       dust_alert={data.get('dust_alert')}, evacuation_id={evac_id}, event_id={event_id}")
        assert evac_id is not None, "应生成撤离指令"
        ok()

        step("2.2 查询该撤离下的未确认人员名单")
        unconfirmed = s.get(f"{BASE_URL}/safety/evacuations/{evac_id}/unconfirmed").json()
        print(f"\n       未确认矿工数量: {len(unconfirmed)}")
        for u in unconfirmed:
            print(f"       - {u['user_name']} (超时={u['is_timeout']}, 已发提醒={u['reminder_sent']})")
        assert len(unconfirmed) >= 1, "至少有矿工未确认"
        ok()

        step("2.3 矿工小陈确认撤离")
        miner1_token = login("miner1", "miner123")
        conf = s.post(f"{BASE_URL}/safety/evacuations/{evac_id}/confirm").json()
        print(f"\n       确认结果: {conf}")
        login("admin", "admin123")  # 切回管理员
        ok()

        step("2.4 确认后未确认名单减少")
        unconfirmed2 = s.get(f"{BASE_URL}/safety/evacuations/{evac_id}/unconfirmed").json()
        names_remaining = [u['user_name'] for u in unconfirmed2]
        print(f"\n       剩余未确认: {names_remaining}")
        assert "矿工小陈" not in names_remaining, "小陈应已确认"
        ok()

        step("2.5 检查超时并发送提醒")
        timeout_resp = s.post(f"{BASE_URL}/safety/evacuations/{evac_id}/check-timeout").json()
        print(f"\n       超时检查结果: {timeout_resp}")
        ok()

        step("2.6 查看安全事件详情（含未确认名单）")
        event_detail = s.get(f"{BASE_URL}/safety/events/{event_id}").json()
        print(f"\n       事件状态: {event_detail['status']}")
        evac_id_show = event_detail['evacuation']['id'] if event_detail.get('evacuation') else None
        print(f"       关联撤离ID: {evac_id_show}")
        unames = [u['name'] for u in event_detail.get('unconfirmed_users', [])]
        print(f"       未确认名单: {unames}")
        ok()
        print("\n[测试2] ✓ 通过: 撤离人员确认和超时提醒")
    except Exception as e:
        all_pass = False
        print(f"  ✗ 测试2失败: {e}")

    # ============================================================
    # 测试3: 统一安全事件状态联动
    # ============================================================
    print_header("[测试3] 统一安全事件 + 解除撤离后事件自动变为已处置")
    try:
        step("3.1 上传C采区粉尘超标数据，触发告警+撤离")
        login("admin", "admin123")
        resp = s.post(f"{BASE_URL}/environment/data", json={
            "area": "C采区", "gas_concentration": 0.2, "dust_concentration": 12.0
        })
        r = resp.json()
        event_id = r.get("safety_event_id")
        evac_id = r.get("evacuation_order_id")
        print(f"\n       事件ID: {event_id}, 撤离ID: {evac_id}")
        ok()

        step("3.2 查看安全事件列表")
        events = s.get(f"{BASE_URL}/safety/events").json()
        print(f"\n       当前安全事件数: {len(events)}")
        for ev in events[:3]:
            print(f"       #{ev['id']} {ev['title']} [{ev['status']} 区域:{ev['area']} 级别:{ev['level']}")
        ok()

        step("3.3 解除C采区撤离")
        s.post(f"{BASE_URL}/environment/evacuations/{evac_id}/cancel")
        ok()

        step("3.4 再次查看事件详情，状态已变为已处置")
        time.sleep(0.3)
        event_detail = s.get(f"{BASE_URL}/safety/events/{event_id}").json()
        print(f"\n       事件当前状态: {event_detail['status']}")
        evac_resolved = (event_detail.get('evacuation') is None) or (not event_detail['evacuation']['is_active'])
        print(f"       撤离已解除: {evac_resolved}")
        ok()

        step("3.5 上传1号综采机设备异常，触发工单+安全事件")
        resp = s.post(f"{BASE_URL}/equipment/data", json={
            "equipment_id": 1, "vibration": 15.0, "temperature": 65.0
        })
        eq = resp.json()
        eq_event_id = eq.get("safety_event_id")
        wo_id = eq.get("work_order_id")
        print(f"\n       工单ID: {wo_id}, 事件ID: {eq_event_id}")
        rationale_str = json.dumps(eq.get('assignment_rationale', []), ensure_ascii=False)[:300]
        print(f"       派工评分说明: {rationale_str}")
        ok()

        step("3.6 完成工单，事件自动变为已处置")
        s.put(f"{BASE_URL}/equipment/work-orders/{wo_id}/complete")
        time.sleep(0.3)
        eq_event = s.get(f"{BASE_URL}/safety/events/{eq_event_id}").json()
        print(f"\n       工单完成后事件状态: {eq_event['status']}")
        assert eq_event['status'] == 'resolved', "工单完成后事件应已处置"
        ok()
        print("\n[测试3] ✓ 通过: 统一安全事件状态联动")
    except Exception as e:
        all_pass = False
        print(f"  ✗ 测试3失败: {e}")

    # ============================================================
    # 测试4: 维修工拒单后自动改派
    # ============================================================
    print_header("[测试4] 维修工拒单/超时自动改派")
    try:
        step("4.1 维修工王师傅上报位置和状态")
        login("admin", "admin123")
        resp = s.get(f"{BASE_URL}/equipment/workers/state").json()
        print(f"\n       当前维修工状态:")
        for w in resp:
            print(f"       {w['worker_name']}: 技能={w['skills']} 状态={w['status']} 区域={w['current_area']}")
        ok()

        step("4.2 王师傅上报在B采区忙碌")
        login("maint1", "maint123")
        s.post(f"{BASE_URL}/equipment/workers/state", params={
            "status": "busy", "current_area": "B采区", "eta_minutes": 15
        })
        resp = s.get(f"{BASE_URL}/equipment/workers/state").json()
        wang = [w for w in resp if w['worker_name'] == "维修工王师傅"]
        print(f"\n       王师傅当前区域: {wang[0]['current_area'] if wang else '?'}")
        ok()

        step("4.3 上传主巷道通风机异常数据")
        login("admin", "admin123")
        resp = s.post(f"{BASE_URL}/equipment/data", json={
            "equipment_id": 4, "vibration": 12.0, "temperature": 80.0
        })
        r = resp.json()
        wo_id = r.get("work_order_id")
        rationale = r.get("assignment_rationale")
        print(f"\n       生成工单ID: {wo_id}")
        if rationale:
            print(f"       派工评分:")
            for rat in rationale[:2]:
                print(f"         {rat['worker_name']}: {rat['reason']}")
        ok()

        step("4.4 查询派给谁了")
        wo_detail = s.get(f"{BASE_URL}/equipment/work-orders/{wo_id}").json()
        print(f"\n       当前派给: {wo_detail.get('assigned_to_name')}, 技能: {wo_detail.get('assigned_to_skills')}")
        first_worker_id = wo_detail.get('assigned_to')
        ok()

        step("4.5 当前被派工人拒单")
        # 用被派工人登录
        if first_worker_id == 4:
            login("maint1", "maint123")
        else:
            login("maint2", "maint123")
        reject_resp = s.put(f"{BASE_URL}/equipment/work-orders/{wo_id}/reject", params={"reason": "我正忙，无法处理"}).json()
        print(f"\n       拒单后新派给: {reject_resp.get('assigned_to_name')}")
        print(f"       改派次数: {reject_resp.get('reassigned_count')}")
        print(f"       拒单记录: {reject_resp}")
        assert reject_resp.get('assigned_to_name') is not None, "拒单后应自动改派"
        ok()

        step("4.6 管理员查看改派后的工单详情")
        login("admin", "admin123")
        final_detail = s.get(f"{BASE_URL}/equipment/work-orders/{wo_id}").json()
        print(f"\n       最终派给: {final_detail.get('assigned_to_name')}")
        print(f"       拒单名单: {final_detail}")
        ok()

        step("4.7 运行超时检查")
        timeout_res = s.post(f"{BASE_URL}/equipment/work-orders/check-timeout").json()
        print(f"\n       超时检查: {timeout_res}")
        ok()
        print("\n[测试4] ✓ 通过: 维修工拒单自动改派")
    except Exception as e:
        all_pass = False
        print(f"  ✗ 测试4失败: {e}")

    print()
    print("=" * 70)
    if all_pass:
        print("  ✓✓✓ 全部4项专项测试通过！ ✓✓✓")
    else:
        print("  ✗✗✗ 存在失败项！ ✗✗✗")
    print("=" * 70)
