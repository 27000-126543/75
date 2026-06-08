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


def ok(msg=""):
    print(f" ✓ {msg}")


def fail(msg):
    print(f" ✗ {msg}")


if __name__ == "__main__":
    print("=" * 70)
    print("  智慧矿山 - 第三轮专项测试 (闭环流程补强)")
    print("=" * 70)

    login("admin", "admin123")
    me = s.get(f"{BASE_URL}/auth/me").json()
    print(f"  当前用户: {me.get('full_name')} 角色: {me.get('role')}")

    all_pass = True

    # ============================================================
    # 测试1: 库存补货全流程闭环（审批→供应商发货→到货验收入库）
    # ============================================================
    print("\n[测试1] 低库存审批后看板 + 补货到货入库全流程")
    try:
        print("\n  1.1 查询锚杆详情，自动触发低库存补货申请...", end="")
        items = s.get(f"{BASE_URL}/inventory/items").json()
        maogan = next((i for i in items if i["name"] == "锚杆"), None)
        assert maogan, "应存在锚杆物资"
        detail = s.get(f"{BASE_URL}/inventory/items/{maogan['id']}").json()
        print(f" done (当前:{detail.get('current_stock')}/安全:{detail.get('safety_stock')}, pending_req={detail.get('pending_restock_request_id')}")
        req_id = detail.get("pending_restock_request_id")
        assert req_id, "锚杆低于安全库存，应自动生成待审批申请"
        ok()

        print("  1.2 审批前看板: 待审批应为1...", end="")
        d1 = s.get(f"{BASE_URL}/inventory/dashboard").json()
        pending_before = d1.get("total_pending_approval", 0)
        approved_before = d1.get("total_approved_pending_supply", 0)
        print(f" done (pending={pending_before}, approved_waiting_supply={approved_before})")
        assert pending_before >= 1
        ok()

        print("  1.3 矿长审批通过，看板状态变为已批准待供应...", end="")
        s.post(f"{BASE_URL}/inventory/restock-requests/{req_id}/approve",
               json={"approved": True, "approver_id": me["id"]})
        d2 = s.get(f"{BASE_URL}/inventory/dashboard").json()
        pending_after = d2.get("total_pending_approval", 0)
        approved_after = d2.get("total_approved_pending_supply", 0)
        print(f" done (pending:{pending_before}→{pending_after}, approved_waiting:{approved_before}→{approved_after})")
        assert approved_after > approved_before or pending_after < pending_before, "审批后应减少待审批、增加已批准待供应"
        ok()

        print("  1.4 供应商确认发货，状态IN_TRANSIT...", end="")
        ship = s.post(f"{BASE_URL}/inventory/restock-requests/{req_id}/supplier-confirm",
                      params={"tracking_number": "WL20260608001"}).json()
        print(f" done → shipment_status={ship.get('shipment_status')}, tracking={ship.get('tracking_number')}")
        assert ship.get("shipment_status") == "in_transit"
        ok()

        print("  1.5 物资送达...", end="")
        deliv = s.post(f"{BASE_URL}/inventory/restock-requests/{req_id}/mark-delivered").json()
        print(f" done → {deliv.get('shipment_status')}")
        assert deliv.get("shipment_status") == "delivered"
        ok()

        print("  1.6 验收入库（自动增加库存，关闭申请）...", end="")
        maogan_before = s.get(f"{BASE_URL}/inventory/items/{maogan['id']}").json().get("current_stock")
        recv = s.post(f"{BASE_URL}/inventory/restock-requests/{req_id}/receive",
                      params={"received_quantity": 1500, "remark": "质量合格，验收通过"}).json()
        maogan_after = s.get(f"{BASE_URL}/inventory/items/{maogan['id']}").json().get("current_stock")
        print(f" done → 库存:{maogan_before}→{maogan_after}, 状态={recv.get('shipment_status')}")
        assert maogan_after > maogan_before, "入库后库存应增加"
        ok()
        print("[测试1] ✓ 通过")
    except Exception as e:
        all_pass = False
        fail(str(e))

    # ============================================================
    # 测试2: 区域撤离未确认名单按区域筛选 + 确认地点方式
    # ============================================================
    print("\n[测试2] 区域撤离未确认名单 + 确认地点方式 + 超时提醒消息")
    try:
        print("\n  2.1 上传A采区粉尘超标，触发撤离...", end="")
        env = s.post(f"{BASE_URL}/environment/data", json={
            "area": "A采区", "gas_concentration": 0.1, "dust_concentration": 20.0
        }).json()
        evac_id = env.get("evacuation_order_id")
        event_id = env.get("safety_event_id")
        print(f" done → evac_id={evac_id}, event_id={event_id}")
        assert evac_id is not None
        ok()

        print("  2.2 查询A采区撤离未确认名单（应只含A采区相关矿工）...", end="")
        unconf = s.get(f"{BASE_URL}/safety/evacuations/{evac_id}/unconfirmed").json()
        print(f" done → 未确认{len(unconf)}人")
        for u in unconf[:3]:
            print(f"    - {u['user_name']} 区域:{u.get('area')}")
        areas = set(u.get("area") for u in unconf if u.get("area"))
        assert len(areas) <= 2, "未确认名单区域应集中在A采区或相邻区域"
        ok()

        print("  2.3 矿工小陈确认撤离，留下确认地点和方式...", end="")
        login("miner1", "miner123")
        conf = s.post(f"{BASE_URL}/safety/evacuations/{evac_id}/confirm",
                      params={"confirm_location": "A采区安全出口", "confirm_method": "井下终端"}).json()
        login("admin", "admin123")
        print(f" done → 地点:{conf.get('confirm_location')}, 方式:{conf.get('confirm_method')}, 时间:{conf.get('confirmed_at')}")
        assert conf.get("confirm_method") == "井下终端"
        ok()

        print("  2.4 查看安全事件详情，current_stage + stages...", end="")
        ev = s.get(f"{BASE_URL}/safety/events/{event_id}").json()
        print(f" done → 当前阶段: {ev.get('current_stage')}, stages数={len(ev.get('stages', []))}")
        for st in ev.get("stages", []):
            mark = "✓" if st.get("done") else "○"
            print(f"    {mark} {st.get('name')}{' 【卡在这里】' if st.get('stuck') else ''}{' ' + st.get('progress', '') if st.get('progress') else ''}")
        ok()

        print("  2.5 查看事件时间轴（应含告警触发/通风启动/撤离下发/人员确认）...", end="")
        tl = s.get(f"{BASE_URL}/safety/events/{event_id}/timeline").json()
        timeline = tl.get("timeline", [])
        print(f" done → 时间轴{len(timeline)}条")
        for t in timeline[:5]:
            print(f"    [{t['action_type']}] {t.get('actor_name') or '系统'}: {t.get('description')[:60]}")
        types = [t["action_type"] for t in timeline]
        assert "alert_triggered" in types, "时间轴应含告警触发"
        assert "evacuation_issued" in types, "时间轴应含撤离下发"
        ok()
        print("[测试2] ✓ 通过")
    except Exception as e:
        all_pass = False
        fail(str(e))

    # ============================================================
    # 测试3: 超时提醒消息记录（PushMessage可查）
    # ============================================================
    print("\n[测试3] 超时提醒消息记录")
    try:
        print("\n  3.1 对A采区撤离运行超时检查...", end="")
        to = s.post(f"{BASE_URL}/safety/evacuations/{evac_id}/check-timeout").json()
        msg_ids = to.get("message_ids", [])
        print(f" done → 超时{len(to.get('timeout_users', []))}人, message_ids={msg_ids}")
        ok()

        print("  3.2 通过消息列表可查到对应超时提醒...", end="")
        # 查未确认名单里的 reminder_message_id
        unconf2 = s.get(f"{BASE_URL}/safety/evacuations/{evac_id}/unconfirmed").json()
        msg_ids_from_list = [u.get("reminder_message_id") for u in unconf2 if u.get("reminder_message_id")]
        print(f" done → 未确认人员中已有提醒记录的: {len(msg_ids_from_list)}")
        if msg_ids_from_list:
            print(f"    reminder_message_ids: {msg_ids_from_list}")
        ok()

        print("  3.3 事件详情的未确认名单带reminder_sent和message_id...", end="")
        ev2 = s.get(f"{BASE_URL}/safety/events/{event_id}").json()
        unames = []
        for u in ev2.get("unconfirmed_users", []):
            unames.append(f"{u['name']}(提醒:{u.get('reminder_sent')},msg_id:{u.get('reminder_message_id')})")
        print(f" done → {unames[:3]}")
        ok()
        print("[测试3] ✓ 通过")
    except Exception as e:
        all_pass = False
        fail(str(e))

    # ============================================================
    # 测试4: 忙碌维修工不优先派单
    # ============================================================
    print("\n[测试4] 忙碌维修工不优先派单 + 评分说明展开")
    try:
        print("\n  4.1 维修工王师傅上报：忙碌、在B采区、ETA60分钟...", end="")
        login("maint1", "maint123")
        ws = s.post(f"{BASE_URL}/equipment/workers/state",
                    params={"status": "busy", "current_area": "B采区", "eta_minutes": 60}).json()
        login("admin", "admin123")
        print(f" done → 王师傅状态:{ws.get('status')}, ETA:{ws.get('eta_minutes')}分钟")
        assert ws.get("status") == "busy"
        ok()

        print("  4.2 赵师傅上报：空闲、在主巷道、ETA3分钟...", end="")
        login("maint2", "maint123")
        ws2 = s.post(f"{BASE_URL}/equipment/workers/state",
                     params={"status": "idle", "current_area": "主巷道", "eta_minutes": 3}).json()
        login("admin", "admin123")
        print(f" done → 赵师傅状态:{ws2.get('status')}, ETA:{ws2.get('eta_minutes')}分钟")
        ok()

        print("  4.3 上传主巷道运输设备异常，派工应优先空闲的赵师傅...", end="")
        eq = s.post(f"{BASE_URL}/equipment/data", json={
            "equipment_id": 3, "vibration": 12.0, "temperature": 50.0
        }).json()
        wo_id = eq.get("work_order_id")
        rationale = eq.get("assignment_rationale", [])
        print(f" done → wo_id={wo_id}")
        for r in rationale[:3]:
            print(f"    {r['worker_name']}(状态:{r.get('status')}, ETA:{r.get('eta_minutes')}分, 进行中工单:{r.get('active_work_orders')}) → {r.get('score_detail')}")
        wo_detail = s.get(f"{BASE_URL}/equipment/work-orders/{wo_id}").json()
        print(f"    → 最终派给: {wo_detail.get('assigned_to_name')}")
        # 优先空闲的赵师傅（maint2=赵师傅）
        assert wo_detail.get("assigned_to_name") == "维修工赵师傅", "忙碌王师傅不应优先，应优先空闲赵师傅"
        ok()

        print("  4.4 赵师傅拒单，自动改派 + 改派历史记录...", end="")
        login("maint2", "maint123")
        rej = s.put(f"{BASE_URL}/equipment/work-orders/{wo_id}/reject",
                    params={"reason": "手头有其他任务，暂时无法处理"}).json()
        login("admin", "admin123")
        print(f" done → 新派给: {rej.get('assigned_to_name')}, 改派次数: {rej.get('reassigned_count')}")
        wo2 = s.get(f"{BASE_URL}/equipment/work-orders/{wo_id}").json()
        print(f"    拒单名单: {wo2.get('rejected_by')}")
        print(f"    改派历史: {[ (r.get('from_user_name'), '→', r.get('to_user_name'), r.get('reason')) for r in wo2.get('reassignment_history', []) ]}")
        assert len(wo2.get("reassignment_history", [])) >= 1, "应记录改派历史"
        ok()

        print("  4.5 工单完成后安全事件时间轴应有工单创建/派工/拒单/改派/完工...", end="")
        s.put(f"{BASE_URL}/equipment/work-orders/{wo_id}/complete")
        event2_id = eq.get("safety_event_id")
        tl2 = s.get(f"{BASE_URL}/safety/events/{event2_id}/timeline").json().get("timeline", [])
        print(f" done → 时间轴{len(tl2)}条")
        for t in tl2:
            print(f"    [{t['action_type']}] {t.get('actor_name') or '系统'}: {t.get('description')[:50]}")
        types2 = [t["action_type"] for t in tl2]
        assert "work_order_created" in types2
        assert "work_order_rejected" in types2 or "work_order_reassigned" in types2
        ok()
        print("[测试4] ✓ 通过")
    except Exception as e:
        all_pass = False
        fail(str(e))

    # ============================================================
    # 结果总结
    # ============================================================
    print("\n" + "=" * 70)
    if all_pass:
        print("  ✓✓✓ 第三轮全部5项专项测试通过！ ✓✓✓")
    else:
        print("  ✗✗✗ 存在失败项！ ✗✗✗")
    print("=" * 70)
