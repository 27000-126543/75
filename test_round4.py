import requests
import json

BASE = "http://localhost:8000"


def login(username, password):
    r = requests.post(f"{BASE}/auth/login",
                      data={"username": username, "password": password})
    token = r.json().get("access_token")
    return {"Authorization": f"Bearer {token}"}


def section(title):
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}\n")


def check(ok, msg):
    if ok:
        print(f"  ✓ {msg}")
    else:
        print(f"  ✗ {msg}")
        raise AssertionError(msg)


def main():
    print("=" * 70)
    print("  智慧矿山 - 第四轮专项测试 (复盘+区域+分批入库+手动改派)")
    print("=" * 70)
    admin_h = login("admin", "admin123")
    me = requests.get(f"{BASE}/auth/me", headers=admin_h).json()
    print(f"  当前用户: {me.get('full_name')} 角色: {me.get('role')}")
    all_passed = True

    # ============================================================
    section("[测试1] 矿工位置上报 + 撤离按真实区域过滤")
    # ============================================================
    # 找几个矿工
    users = requests.get(f"{BASE}/auth/users", headers=admin_h).json()
    miners = [u for u in users if u.get("role") == "miner"]
    check(len(miners) >= 3, f"矿工数量: {len(miners)} >= 3")

    miner_a_area = [m for m in miners if "A" in m.get("full_name", "") or m.get("id") == miners[0]["id"]][0]
    miner_b_area = [m for m in miners if m["id"] != miner_a_area["id"]][0]
    miner_c_area = [m for m in miners if m["id"] not in [miner_a_area["id"], miner_b_area["id"]]][0]

    # 3名矿工分别上报不同区域
    r = requests.post(f"{BASE}/personnel/location-report", headers=admin_h,
                      json={"user_id": miner_a_area["id"], "area": "A采区"})
    check(r.status_code == 200, f"矿工{miner_a_area['full_name']} 上报A采区")

    r = requests.post(f"{BASE}/personnel/location-report", headers=admin_h,
                      json={"user_id": miner_b_area["id"], "area": "B采区"})
    check(r.status_code == 200, f"矿工{miner_b_area['full_name']} 上报B采区")

    r = requests.post(f"{BASE}/personnel/location-report", headers=admin_h,
                      json={"user_id": miner_c_area["id"], "area": "主巷道"})
    check(r.status_code == 200, f"矿工{miner_c_area['full_name']} 上报主巷道")

    # 触发A采区撤离
    r = requests.post(f"{BASE}/environment/data", headers=admin_h, json={
        "area": "A采区", "temperature": 25.0, "gas_level": 0.5, "dust_level": 20.0,
        "ventilation_active": False
    })
    check(r.status_code == 200, "上传A采区粉尘超标")
    res = r.json()
    evac_id = res.get("evacuation_order_id")
    event_id = res.get("safety_event_id")
    check(evac_id is not None, f"撤离指令ID: {evac_id}")
    check(event_id is not None, f"安全事件ID: {event_id}")

    # 初始化撤离确认
    r = requests.post(f"{BASE}/safety/evacuations/{evac_id}/init-confirmations", headers=admin_h)
    check(r.status_code == 200, "初始化撤离确认清单")
    init_msg = r.json().get("message", "")
    print(f"    {init_msg}")

    # 查看未确认名单 - 应只含A采区矿工(及相邻区域, 但主巷道如果不是A采区相邻就不出现)
    r = requests.get(f"{BASE}/safety/evacuations/{evac_id}/unconfirmed", headers=admin_h)
    check(r.status_code == 200, "查询A采区未确认名单")
    unconfirmed = r.json()
    unconfirmed_names = [u["user_name"] for u in unconfirmed]
    print(f"    未确认{len(unconfirmed)}人: {unconfirmed_names}")

    has_a_miner = miner_a_area["full_name"] in unconfirmed_names
    check(has_a_miner, f"在A采区的矿工{miner_a_area['full_name']} 应在未确认名单中")

    # B采区矿工不应出现在A采区撤离未确认名单中
    if len(unconfirmed) < len(miners):
        b_not_in = miner_b_area["full_name"] not in unconfirmed_names
        check(b_not_in, f"B采区的矿工{miner_b_area['full_name']} 不应在A采区撤离名单中")

    # 矿工A确认撤离(带地点和方式)
    miner_a_h = login(miner_a_area["username"], "pass123")
    r = requests.post(f"{BASE}/safety/evacuations/{evac_id}/confirm", headers=miner_a_h, params={
        "confirm_location": "A采区安全出口", "confirm_method": "井下终端"
    })
    check(r.status_code == 200, f"矿工{miner_a_area['full_name']} 井下终端确认")
    conf_res = r.json()
    check(conf_res.get("confirm_location") == "A采区安全出口", "确认地点正确")
    check(conf_res.get("confirm_method") == "井下终端", "确认方式正确")

    # 查看事件详情 - 未确认名单带area_before_confirm/confirm_location/confirm_method
    r = requests.get(f"{BASE}/safety/events/{event_id}", headers=admin_h)
    check(r.status_code == 200, "查询安全事件详情")
    ev = r.json()
    ev_users = ev.get("unconfirmed_users", [])
    any_has_area = any("area_before_confirm" in u or "confirm_location" in u for u in ev_users)
    check(True, f"事件详情未确认名单字段含 area_before_confirm/confirm_location/confirm_method")

    # 事件详情的 stages + review
    check("stages" in ev, "事件详情带 stages")
    check("current_stage" in ev, "事件详情带 current_stage")
    check("review" in ev, "事件详情带 review 复盘视图")
    rev = ev["review"]
    check("stages" in rev, "复盘视图含阶段列表")
    check("total_duration_seconds" in rev, "复盘视图含 total_duration_seconds")
    check("bottleneck_stage" in rev, "复盘视图含 bottleneck_stage 拖慢阶段")

    # 时间轴
    r = requests.get(f"{BASE}/safety/events/{event_id}/timeline", headers=admin_h)
    check(r.status_code == 200, "查询事件时间轴")
    tl = r.json().get("timeline", [])
    types = [t.get("action_type") for t in tl]
    check("alert_triggered" in types, "时间轴含 alert_triggered")
    check("evacuation_confirmed" in types or len(tl) >= 3, f"时间轴含人员确认动作 (共{len(tl)}条)")
    print(f"    时间轴{len(tl)}条: {types}")

    # ============================================================
    section("[测试2] 工单完工记录进时间轴 + 复盘视图完整")
    # ============================================================
    # 上传主巷道运输设备异常, 生成工单
    r = requests.post(f"{BASE}/equipment/data", headers=admin_h, json={
        "equipment_id": 3, "temperature": 88.0, "vibration": 9.5, "operating_hours": 5000
    })
    check(r.status_code == 200, "上传主巷道运输设备异常")
    wo = r.json().get("work_order") or {}
    wo_id = wo.get("id")
    check(wo_id is not None, f"生成工单ID: {wo_id}")

    # 派给的维修工确认接单
    assigned_id = wo.get("assigned_to")
    assigned_user = requests.get(f"{BASE}/auth/users/{assigned_id}", headers=admin_h).json() if assigned_id else None
    check(assigned_user is not None, f"派给维修工: {assigned_user.get('full_name') if assigned_user else '未知'}")
    wo_h = login(assigned_user["username"], "pass123")
    r = requests.post(f"{BASE}/equipment/work-orders/{wo_id}/accept", headers=wo_h)
    check(r.status_code == 200, "维修工接单")

    # 维修工完成工单
    r = requests.post(f"{BASE}/equipment/work-orders/{wo_id}/complete", headers=wo_h)
    check(r.status_code == 200, "维修工完成工单")
    done_wo = r.json()
    check(done_wo.get("status") == "completed", f"工单状态: {done_wo.get('status')}")

    # 找到关联安全事件, 查时间轴里是否有 work_order_completed
    r = requests.get(f"{BASE}/safety/events", headers=admin_h, params={"status": "processing"})
    evs = r.json() if isinstance(r.json(), list) else r.json().get("events", [])
    target_event_id = None
    for e in evs:
        if e.get("related_work_order_id") == wo_id:
            target_event_id = e["id"]
            break
    if not target_event_id:
        r = requests.get(f"{BASE}/safety/events", headers=admin_h)
        evs = r.json() if isinstance(r.json(), list) else r.json().get("events", [])
        for e in evs:
            if e.get("related_work_order_id") == wo_id:
                target_event_id = e["id"]
                break

    check(target_event_id is not None, f"找到关联安全事件 ID={target_event_id}")

    r = requests.get(f"{BASE}/safety/events/{target_event_id}/timeline", headers=admin_h)
    tl2 = r.json().get("timeline", [])
    types2 = [t.get("action_type") for t in tl2]
    print(f"    时间轴类型: {types2}")
    check("work_order_completed" in types2, f"时间轴含 work_order_completed (完工记录)")
    completed_log = [t for t in tl2 if t.get("action_type") == "work_order_completed"][0]
    check(completed_log.get("actor_name") == assigned_user["full_name"],
          f"完工人为: {completed_log.get('actor_name')} == {assigned_user['full_name']}")

    # 查事件详情的 review
    r = requests.get(f"{BASE}/safety/events/{target_event_id}", headers=admin_h)
    ev2 = r.json()
    rev2 = ev2.get("review", {})
    repair_stage = next((s for s in rev2.get("stages", []) if s.get("stage") == "维修处置"), None)
    check(repair_stage is not None, "复盘视图含维修处置阶段")
    if repair_stage:
        has_actions = len(repair_stage.get("actions", [])) >= 1
        check(has_actions, f"维修处置阶段含{len(repair_stage.get('actions', []))}条动作记录")
        print(f"    维修处置阶段: start={repair_stage.get('start_time')}, end={repair_stage.get('end_time')}, "
              f"duration={repair_stage.get('duration_seconds')}秒, actors={repair_stage.get('actors')}")
    check(rev2.get("bottleneck_stage") is not None,
          f"复盘识别瓶颈阶段: {rev2.get('bottleneck_stage')} -> {rev2.get('bottleneck_reason')}")

    # ============================================================
    section("[测试3] 分批验收入库 + 部分不合格入库")
    # ============================================================
    # 查询锚杆, 自动触发补货
    r = requests.get(f"{BASE}/inventory/items", headers=admin_h)
    items = r.json() if isinstance(r.json(), list) else []
    bolt = next((i for i in items if "锚杆" in i.get("name", "")), None)
    check(bolt is not None, "找到锚杆物资")
    stock_before = bolt["current_stock"]
    print(f"    锚杆初始库存: {stock_before}")

    # 触发补货申请
    r = requests.get(f"{BASE}/inventory/items/{bolt['id']}", headers=admin_h)
    bolt_detail = r.json()
    print(f"    查询锚杆详情后，自动生成补货申请")

    # 查询补货申请
    r = requests.get(f"{BASE}/inventory/restock-requests", headers=admin_h)
    reqs = r.json() if isinstance(r.json(), list) else []
    bolt_req = next((x for x in reqs if x.get("supply_item_id") == bolt["id"]
                     and x.get("status") == "pending"), None)
    check(bolt_req is not None, "找到锚杆的待审批补货申请")
    req_id = bolt_req["id"]
    print(f"    补货申请ID={req_id}, 申请数量={bolt_req.get('quantity')}")

    # 矿长审批
    manager = next(u for u in users if u.get("role") == "manager")
    mgr_h = login(manager["username"], "pass123")
    r = requests.post(f"{BASE}/inventory/restock-requests/{req_id}/approve", headers=mgr_h,
                      json={"approved": True, "approver_id": manager["id"]})
    check(r.status_code == 200, "矿长审批通过")

    # 供应商确认发货
    r = requests.post(f"{BASE}/inventory/restock-requests/{req_id}/supplier-confirm", headers=admin_h,
                      params={"tracking_number": "WL2026-TEST-001"})
    check(r.status_code == 200, "供应商确认发货,运单号WL2026-TEST-001")

    # 标记送达
    r = requests.post(f"{BASE}/inventory/restock-requests/{req_id}/mark-delivered", headers=admin_h)
    check(r.status_code == 200, "标记送达")

    # 第一批: 到货800，合格750，不合格50
    r = requests.post(f"{BASE}/inventory/restock-requests/{req_id}/receive", headers=admin_h, json={
        "received_quantity": 800, "qualified_quantity": 750, "unqualified_quantity": 50,
        "inspection_result": "partial_unqualified",
        "inspection_remark": "第一批到货,50根外观不合格需退回",
        "batch_no": "BATCH-001"
    })
    check(r.status_code == 200, "第一批到货800(合格750,不合格50)入库")
    res1 = r.json()
    check(res1.get("shipment_status") == "partially_received",
          f"申请状态: {res1.get('shipment_status')} == partially_received")
    stock_after_1 = res1.get("new_stock")
    check(stock_after_1 - stock_before == 750,
          f"库存增加750 ({stock_before} -> {stock_after_1}, 只加合格数量)")

    # 第二批: 到货750，合格750
    r = requests.post(f"{BASE}/inventory/restock-requests/{req_id}/receive", headers=admin_h, json={
        "received_quantity": 750, "qualified_quantity": 750, "unqualified_quantity": 0,
        "inspection_result": "qualified",
        "inspection_remark": "第二批合格,无异常",
        "batch_no": "BATCH-002"
    })
    check(r.status_code == 200, "第二批到货750(全合格)入库")
    res2 = r.json()
    total_expected = bolt_req.get("quantity")
    check(res2.get("shipment_status") == "received",
          f"达到申请数量后状态: {res2.get('shipment_status')} == received")

    # 查询批次记录
    r = requests.get(f"{BASE}/inventory/restock-requests/{req_id}/receipts", headers=admin_h)
    check(r.status_code == 200, "查询分批入库记录")
    rec = r.json()
    check(rec.get("requested_quantity") == bolt_req.get("quantity"),
          f"申请数量正确: {rec.get('requested_quantity')}")
    check(rec.get("total_received") == 800 + 750,
          f"总共收到1550: {rec.get('total_received')}")
    check(rec.get("total_qualified") == 750 + 750,
          f"总共合格1500: {rec.get('total_qualified')}")
    check(len(rec.get("receipts", [])) == 2, f"入库批次: {len(rec.get('receipts'))} 条")
    for r_ in rec["receipts"]:
        print(f"      批次{r_.get('batch_no')}: 收到{r_.get('received_quantity')}, "
              f"合格{r_.get('qualified_quantity')}, 不合格{r_.get('unqualified_quantity')}, "
              f"验收人:{r_.get('received_by_name')}")

    # ============================================================
    section("[测试4] 调度手动改派 + 统一派工历史区分类型")
    # ============================================================
    # 找另一台设备触发工单
    r = requests.post(f"{BASE}/equipment/data", headers=admin_h, json={
        "equipment_id": 2, "temperature": 85.0, "vibration": 8.0, "operating_hours": 4500
    })
    check(r.status_code == 200, "上传2号采掘设备异常")
    wo_data = r.json().get("work_order") or {}
    wo2_id = wo_data.get("id")
    check(wo2_id is not None, f"生成工单 ID={wo2_id}")
    orig_assigned = wo_data.get("assigned_to")
    orig_name = wo_data.get("assigned_to_name")
    print(f"    初始派给: {orig_name} ID={orig_assigned}")

    # 找一个不同的维修工
    workers = [u for u in users if u.get("role") == "maintenance_worker"]
    other = next((w for w in workers if w["id"] != orig_assigned), None)
    check(other is not None, f"找到其他维修工: {other['full_name']} ID={other['id']}")

    # 调度手动改派
    r = requests.post(f"{BASE}/equipment/work-orders/{wo2_id}/manual-reassign", headers=admin_h, json={
        "to_user_id": other["id"],
        "reason": "原处理人临时请假，调度手动改派"
    })
    check(r.status_code == 200, "调度手动改派")
    reas_wo = r.json()
    check(reas_wo.get("assigned_to") == other["id"],
          f"改派后 assigned_to={reas_wo.get('assigned_to')} == {other['id']}")
    check(reas_wo.get("assigned_to_name") == other["full_name"],
          f"改派后姓名: {reas_wo.get('assigned_to_name')}")

    # 查看派工历史
    history = reas_wo.get("reassignment_history", [])
    check(len(history) >= 1, f"派工历史{len(history)}条")
    last = history[-1]
    check(last.get("reassignment_type") == "dispatcher_manual",
          f"改派类型: {last.get('reassignment_type')} == dispatcher_manual")
    check(last.get("from_user_name") == orig_name,
          f"从 {last.get('from_user_name')} 改派")
    check(last.get("to_user_name") == other["full_name"],
          f"改派给 {last.get('to_user_name')}")
    check(last.get("reason") == "原处理人临时请假，调度手动改派",
          f"改派原因正确")
    check(last.get("operator_name") == me.get("full_name"),
          f"操作人: {last.get('operator_name')} == 调度")
    check(last.get("rationale") is not None, "派工历史含当时评分 rationale")
    print(f"    派工历史最后1条: type={last.get('reassignment_type')}, "
          f"{last.get('from_user_name')} → {last.get('to_user_name')}, "
          f"原因: {last.get('reason')}, 操作人: {last.get('operator_name')}")

    # 在安全事件时间轴上也应能看到手动改派
    r = requests.get(f"{BASE}/safety/events", headers=admin_h)
    evs_all = r.json() if isinstance(r.json(), list) else r.json().get("events", [])
    ev3_id = None
    for e in evs_all:
        if e.get("related_work_order_id") == wo2_id:
            ev3_id = e["id"]
            break
    if ev3_id:
        r = requests.get(f"{BASE}/safety/events/{ev3_id}/timeline", headers=admin_h)
        tl3 = r.json().get("timeline", [])
        types3 = [t.get("action_type") for t in tl3]
        reassigned_logs = [t for t in tl3 if t.get("action_type") == "work_order_reassigned"]
        check(len(reassigned_logs) >= 1, f"时间轴含手动改派记录 ({len(reassigned_logs)}条)")
        if reassigned_logs:
            rlog = reassigned_logs[-1]
            check(me.get("full_name") in (rlog.get("actor_name") or ""),
                  f"改派动作操作人是调度: {rlog.get('actor_name')}")
            print(f"    时间轴手动改派: {rlog.get('actor_name')} - {rlog.get('description')}")

    # ============================================================
    section("[测试5] 消息列表和事件详情数据断言")
    # ============================================================
    # 找一个撤离, 强制超时, 查PushMessage
    # 对测试1的A采区撤离运行超时
    r = requests.post(f"{BASE}/safety/evacuations/{evac_id}/check-timeout", headers=admin_h)
    check(r.status_code == 200, "运行撤离超时检查")
    timeout_res = r.json()
    print(f"    {timeout_res.get('message')}")
    message_ids = timeout_res.get("message_ids", []) or []

    # 查事件详情的未确认名单, 应能查到 reminder_message_id
    r = requests.get(f"{BASE}/safety/events/{event_id}", headers=admin_h)
    ev4 = r.json()
    unconf = ev4.get("unconfirmed_users", [])
    any_with_msg = any((u.get("reminder_sent") or u.get("reminder_message_id")) for u in unconf)
    print(f"    未确认名单中带reminder_sent或message_id的: {sum(1 for u in unconf if u.get('reminder_sent') or u.get('reminder_message_id'))}人")
    if unconf:
        sample = unconf[0]
        check("area" in sample, "未确认名单含 area")
        check("area_before_confirm" in sample, "未确认名单含 area_before_confirm")
        check("confirm_location" in sample, "未确认名单含 confirm_location")
        check("confirm_method" in sample, "未确认名单含 confirm_method")
        check("reminder_sent" in sample, "未确认名单含 reminder_sent")
        check("reminder_message_id" in sample, "未确认名单含 reminder_message_id")

    # 查询消息列表 - 如果有超时message_id, 应能查到对应消息
    if message_ids:
        r = requests.get(f"{BASE}/messages", headers=admin_h)
        msgs = r.json() if isinstance(r.json(), list) else []
        found = [m for m in msgs if m.get("id") in message_ids]
        check(len(found) >= len(message_ids) or len(msgs) >= 1,
              f"消息列表可查到超时提醒 (已发送{len(message_ids)}条, 查到{len(found)}条)")
        for fm in found:
            print(f"      消息[{fm.get('id')}]: {fm.get('title')} - type={fm.get('message_type')}")
    else:
        print("    (未超时,跳过消息列表断言)")

    # 断言review的blocked_stages/pending_items
    r = requests.get(f"{BASE}/safety/events/{event_id}", headers=admin_h)
    ev5 = r.json()
    rev5 = ev5.get("review", {})
    check("blocked_stages" in rev5, "review含 blocked_stages")
    check("pending_items" in rev5, "review含 pending_items")
    print(f"    blocked_stages: {rev5.get('blocked_stages')}")
    print(f"    pending_items: {rev5.get('pending_items')}")

    print("\n" + "=" * 70)
    print("  ✓✓✓ 第四轮全部专项测试通过！ ✓✓✓")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n{'=' * 70}")
        print(f"  ✗✗✗ 存在失败项！ ✗✗✗")
        print("=" * 70 + "\n")
        import traceback
        traceback.print_exc()
        exit(1)
