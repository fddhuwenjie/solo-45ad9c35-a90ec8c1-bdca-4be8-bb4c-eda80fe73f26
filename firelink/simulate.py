# -*- coding: utf-8 -*-
"""
模拟请求：覆盖时钟漂移、漏事件、合法旁路，并演示修订/差异/签结。
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
