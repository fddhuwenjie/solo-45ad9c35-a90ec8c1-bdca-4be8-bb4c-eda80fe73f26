# -*- coding: utf-8 -*-
"""
模拟请求：覆盖时钟漂移、漏事件、合法旁路、响应佐证（触点粘连/双佐证正常），
并演示修订/差异/签结（含佐证规则修订后旧版规则、样本与结论的还原）。
运行:  python3 simulate.py            # 自起服务（线程内）跑完全部用例
"""
import json
import threading
import urllib.request
from wsgiref.simple_server import make_server

from app import make_app

PORT = 8073


def call(method, path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data,
                                 method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def show(title, code, obj):
    print(f"\n=== {title}  [HTTP {code}] ===")
    print(json.dumps(obj, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- 用例 1：时钟漂移
# 风机 F1 的校时脉冲残差 400ms > 容限 150ms -> F1 时标不可信，
# “烟感->风机启动”环节保持 unknown；阀门链路正常。
CLOCK_DRIFT = {
    "project": "1#楼三层-时钟漂移",
    "sync_tolerance_ms": 150,
    "devices": {"D1": {"type": "smoke"}, "V1": {"type": "valve"},
                "F1": {"type": "fan"}},
    "aliases": {"走廊烟感": "D1"},
    "matrix": [{
        "id": "S1-防烟",
        "trigger": {"device": "走廊烟感", "signal": "alarm"},
        "respond": [
            {"device": "V1", "signal": "open", "within_ms": 30000},
            {"device": "F1", "signal": "start", "within_ms": 60000,
             "after": ["V1:open"]}]}],
    "sync_pulses": [
        {"device": "D1", "device_ts": 0, "master_ts": 50},
        {"device": "D1", "device_ts": 100000, "master_ts": 100050},
        {"device": "V1", "device_ts": 0, "master_ts": 80},
        {"device": "V1", "device_ts": 100000, "master_ts": 100080},
        {"device": "F1", "device_ts": 0, "master_ts": 100},
        {"device": "F1", "device_ts": 100000, "master_ts": 100500}],  # 残差 400
    "events": [
        {"device": "D1", "seq": 1, "signal": "alarm", "device_ts": 10000},
        {"device": "V1", "seq": 1, "signal": "open", "device_ts": 15000},
        {"device": "F1", "seq": 1, "signal": "start", "device_ts": 20000}],
}

# ---------------------------------------------------------------- 用例 2：漏事件
# 电梯 LIFT1 日志序号 1,2,4（缺 3），空洞与迫降确认窗口重叠 -> unknown；
# 广播 PA1 在 45s 时限内无切换事件 -> 首个超时。
MISSING_EVENTS = {
    "project": "2#楼五层-漏事件",
    "devices": {"D2": {"type": "smoke"}, "LIFT1": {"type": "lift"},
                "PA1": {"type": "broadcast"}},
    "matrix": [{
        "id": "S2-迫降与广播",
        "trigger": {"device": "D2", "signal": "alarm"},
        "respond": [
            {"device": "LIFT1", "signal": "homed", "within_ms": 60000},
            {"device": "PA1", "signal": "switch", "within_ms": 45000}]}],
    "sync_pulses": [
        {"device": "D2", "device_ts": 0, "master_ts": 0},
        {"device": "LIFT1", "device_ts": 0, "master_ts": 20},
        {"device": "PA1", "device_ts": 0, "master_ts": 10}],
    "events": [
        {"device": "D2", "seq": 1, "signal": "alarm", "device_ts": 5000},
        {"device": "LIFT1", "seq": 1, "signal": "run", "device_ts": 6000},
        {"device": "LIFT1", "seq": 2, "signal": "door_close", "device_ts": 20000},
        # 缺 seq=3
        {"device": "LIFT1", "seq": 4, "signal": "homed", "device_ts": 40000},
        {"device": "PA1", "seq": 1, "signal": "switch", "device_ts": 90000}],  # 迟到
}

# ---------------------------------------------------------------- 用例 3：合法旁路
# 排烟阀 V9 在许可窗口内旁路并已复位 -> 合法；联动链全部在时限内 -> pass。
LEGAL_BYPASS = {
    "project": "3#楼地下一层-合法旁路",
    "devices": {"D3": {"type": "smoke"}, "V9": {"type": "valve"},
                "F9": {"type": "fan"}},
    "matrix": [{
        "id": "S3-排烟",
        "trigger": {"device": "D3", "signal": "alarm"},
        "respond": [
            {"device": "V9", "signal": "open", "within_ms": 30000},
            {"device": "F9", "signal": "start", "within_ms": 60000,
             "after": ["V9:open"]}]}],
    "bypass_permits": [
        {"device": "V9", "start": 0, "end": 200000, "reason": "年度检修旁路"}],
    "sync_pulses": [
        {"device": "D3", "device_ts": 0, "master_ts": 0},
        {"device": "V9", "device_ts": 0, "master_ts": 30},
        {"device": "F9", "device_ts": 0, "master_ts": 40}],
    "events": [
        {"device": "V9", "seq": 1, "signal": "bypass_on", "device_ts": 1000},
        {"device": "V9", "seq": 2, "signal": "bypass_off", "device_ts": 3000},
        {"device": "D3", "seq": 1, "signal": "alarm", "device_ts": 10000},
        {"device": "V9", "seq": 3, "signal": "open", "device_ts": 25000},
        {"device": "F9", "seq": 1, "signal": "start", "device_ts": 40000}],
}


# ---------------------------------------------------------------- 用例 4：触点粘连（离散反馈到、设备没动）
# 排烟风机 F4 的运行触点 start 已上报，但电流表 I4 窗口内电流始终≈0A，
# 且没有任何在范围样本 -> 佐证相斥，响应 fail，给出最早分歧区间。
def _stuck_events():
    return [
        {"device": "D4", "seq": 1, "signal": "alarm", "device_ts": 1000},
        {"device": "F4", "seq": 1, "signal": "start", "device_ts": 2000},
        # 电流始终 0A：触点粘连，风机实际未运转
        {"device": "I4", "seq": 1, "signal": "I", "device_ts": 2200,
         "value": 0.1, "unit": "A"},
        {"device": "I4", "seq": 2, "signal": "I", "device_ts": 4000,
         "value": 0.0, "unit": "A"},
        {"device": "I4", "seq": 3, "signal": "I", "device_ts": 7000,
         "value": 0.0, "unit": "A"}]


CONTACT_STUCK = {
    "project": "4#楼地下二层-触点粘连",
    "devices": {"D4": {"type": "smoke"}, "F4": {"type": "fan"},
                "I4": {"type": "ammeter"}},
    "matrix": [{
        "id": "S4-排烟风机",
        "trigger": {"device": "D4", "signal": "alarm"},
        "respond": [{
            "device": "F4", "signal": "start", "within_ms": 60000,
            "evidence": {
                "combine": "all", "window_ms": 8000,
                "duration_ms": 2000, "missing_ms": 3000,
                "sources": [{
                    "device": "I4", "signal": "I",
                    "range": {"min": 5, "max": 20, "unit": "A"}}]}}]}],
    "sync_pulses": [
        {"device": "D4", "device_ts": 0, "master_ts": 0},
        {"device": "F4", "device_ts": 0, "master_ts": 0},
        {"device": "I4", "device_ts": 0, "master_ts": 0}],
    "events": _stuck_events(),
}

# 同一条链路的正常版本：电流（mA 上报，换算到 A）+ 风压（Pa）双佐证，
# any 组合下任一持续在范围即确认；用于演示单位换算与采用样本。
CONTACT_HEALTHY = json.loads(json.dumps(CONTACT_STUCK))
CONTACT_HEALTHY["project"] = "4#楼地下二层-双佐证正常"
CONTACT_HEALTHY["devices"]["P4"] = {"type": "pressure"}
CONTACT_HEALTHY["sync_pulses"].append(
    {"device": "P4", "device_ts": 0, "master_ts": 0})
CONTACT_HEALTHY["matrix"][0]["respond"][0]["evidence"] = {
    "combine": "any", "window_ms": 8000, "duration_ms": 2000,
    "missing_ms": 3000,
    "sources": [
        {"device": "I4", "signal": "I",
         "range": {"min": 5, "max": 20, "unit": "A"}},
        {"device": "P4", "signal": "W",
         "range": {"min": 300, "max": 800, "unit": "Pa"}}]}
CONTACT_HEALTHY["events"] = [
    e for e in _stuck_events() if e["device"] != "I4"] + [
    {"device": "I4", "seq": 1, "signal": "I", "device_ts": 2200,
     "value": 6000, "unit": "mA"},      # 6.0 A
    {"device": "I4", "seq": 2, "signal": "I", "device_ts": 4000,
     "value": 6200, "unit": "mA"},
    {"device": "I4", "seq": 3, "signal": "I", "device_ts": 7000,
     "value": 6100, "unit": "mA"},
    {"device": "P4", "seq": 1, "signal": "W", "device_ts": 3000,
     "value": 520, "unit": "Pa"},
    {"device": "P4", "seq": 2, "signal": "W", "device_ts": 6000,
     "value": 510, "unit": "Pa"}]


def main():
    httpd = make_server("127.0.0.1", PORT, make_app(":memory:"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    for title, payload in [("用例1 时钟漂移", CLOCK_DRIFT),
                           ("用例2 漏事件", MISSING_EVENTS),
                           ("用例3 合法旁路", LEGAL_BYPASS)]:
        code, obj = call("POST", "/projects", payload)
        show(f"{title} -> 创建", code, obj)
        pid = obj["project"]
        code, obj = call("GET", f"/projects/{pid}/revisions/1")
        show(f"{title} -> 重放 JSON", code, obj["replay"])

    # ---- 用例 4：响应佐证 ----
    code, obj = call("POST", "/projects", CONTACT_STUCK)
    show("用例4 触点粘连 -> 创建(应 fail)", code, obj)
    pid = obj["project"]
    code, obj = call("GET", f"/projects/{pid}/revisions/1")
    f4 = obj["replay"]["scenarios"][0]["instances"][0]["findings"][0]
    show("用例4 -> 佐证块(分歧区间/采用样本)", code,
         {"finding": {k: f4[k] for k in
                      ("status", "type", "reason", "evidence")}})

    # 同一试验链：把佐证规则放宽为 any（电流/风压任一即可）并重采样本，
    # 附 justification 另起修订；rev1 的旧规则/样本/结论仍可还原。
    code, obj = call("POST", f"/projects/{pid}/revisions", {
        "justification": "粘连排查后更换电流表并加装风压测点，佐证规则改为"
                         "电流/风压 any，并按校准报告 JC-2026-104 重采样本",
        "payload": CONTACT_HEALTHY})
    show("用例4 -> 修订佐证规则 rev2(应 pass)", code, obj)
    code, obj = call("GET", f"/projects/{pid}/revisions/1")
    old = obj["replay"]["scenarios"][0]["instances"][0]["findings"][0]
    show("用例4 -> 旧版重放仍为 fail(规则/样本固定)", code,
         {"status": old["status"], "combine": old["evidence"]["combine"],
          "divergence": old["evidence"]["divergence"]})
    code, obj = call("GET", f"/projects/{pid}/diff?from=1&to=2")
    show("用例4 -> 佐证修订差异", code, {
        "verdict_changes": obj["verdict_changes"],
        "evidence_changes": obj["evidence_changes"]})

    code, obj = call("POST", "/projects", CONTACT_HEALTHY)
    show("用例4b 双佐证正常 -> 创建(应 pass)", code, obj)

    # ---- 修订：无依据被拒 -> 附依据重绑设备 -> 差异 -> 签结 -> 签结后拒修订 ----
    pid = call("POST", "/projects", CLOCK_DRIFT)[1]["project"]

    code, obj = call("POST", f"/projects/{pid}/revisions",
                     {"patch": {"aliases": {"走廊烟感": "D1"}}})
    show("修订(缺 justification) -> 拒绝", code, obj)

    code, obj = call("POST", f"/projects/{pid}/revisions", {
        "justification": "F1 时钟锚点改以 NTP 服务器为准（校准报告 JC-2026-091），"
                         "并重采校时脉冲",
        "patch": {"sync_pulses": [
            {"device": "D1", "device_ts": 0, "master_ts": 50},
            {"device": "D1", "device_ts": 100000, "master_ts": 100050},
            {"device": "V1", "device_ts": 0, "master_ts": 80},
            {"device": "V1", "device_ts": 100000, "master_ts": 100080},
            {"device": "F1", "device_ts": 0, "master_ts": 100},
            {"device": "F1", "device_ts": 100000, "master_ts": 100120}]}})
    show("修订(改时钟锚点, 附依据) -> rev2", code, obj)

    code, obj = call("GET", f"/projects/{pid}/diff?from=1&to=2")
    show("修订差异 rev1->rev2", code, obj)

    code, obj = call("POST", f"/projects/{pid}/signoff")
    show("签结", code, obj)

    code, obj = call("POST", f"/projects/{pid}/revisions",
                     {"justification": "签结后试图再改", "patch": {"events": []}})
    show("签结后修订 -> 拒绝", code, obj)

    httpd.shutdown()


if __name__ == "__main__":
    main()
