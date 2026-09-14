# -*- coding: utf-8 -*-
import json
from app import evaluate

DEVS = {"D1": {"type": "smoke"}, "F1": {"type": "fan"},
        "FB": {"type": "fan"}, "C1": {"type": "controller"}}

PULSES = [{"device": d, "device_ts": 0, "master_ts": 0}
          for d in DEVS]

FAILOVER = {
    "fault": "C1:trip",
    "standby": "FB:start",
    "switch_wait_ms": 5000,
    "total_ms": 30000,
    "parallel_ms": 2000,
}


def payload(events, **fo_over):
    fo = dict(FAILOVER)
    fo.update(fo_over)
    return {
        "sync_tolerance_ms": 150,
        "devices": DEVS,
        "aliases": {},
        "matrix": [{"id": "S",
                    "trigger": {"device": "D1", "signal": "alarm"},
                    "respond": [{"device": "F1", "signal": "start",
                                 "within_ms": 60000, "failover": fo}]}],
        "sync_pulses": [dict(x) for x in PULSES],
        "events": events,
    }


def ev(d, s, ts, seq=None):
    return {"device": d, "seq": seq if seq is not None else 1,
            "signal": s, "device_ts": ts}


def block(r):
    return r["scenarios"][0]["instances"][0]["failover"][0]


# 1. 主机成功
r = evaluate(payload([ev("D1", "alarm", 1000),
                      ev("F1", "start", 2000)]))
b = block(r)
print("1 primary_success:", r["status"], b["status"], b["outcome"])

# 2. 合法切换：3s 跳闸，等满 5s，8s 启备用
r = evaluate(payload([ev("D1", "alarm", 1000),
                      ev("C1", "trip", 4000),
                      ev("FB", "start", 9000)]))
b = block(r)
print("2 legal_switch:", r["status"], b["status"], b["outcome"])

# 3. 跳闸后过早切换（未等满 5s）
r = evaluate(payload([ev("D1", "alarm", 1000),
                      ev("C1", "trip", 4000),
                      ev("FB", "start", 6000)]))
b = block(r)
print("3 early switch:", r["status"], b["status"], b["outcome"],
      b["findings"][0]["reason"])

# 4. 主机只是反馈迟到，无故障即启备机（误切换，并行短）
r = evaluate(payload([ev("D1", "alarm", 1000),
                      ev("FB", "start", 3000),
                      ev("F1", "start", 4000),
                      ev("C1", "trip", 100000)]))
b = block(r)
print("4 spurious_switch:", r["status"], b["status"], b["outcome"],
      b["findings"][0]["reason"])

# 5. 双机超时并行：备机先启，主机 8s 后才起（>parallel 2s）
r = evaluate(payload([ev("D1", "alarm", 1000),
                      ev("FB", "start", 3000),
                      ev("F1", "start", 12000),
                      ev("C1", "trip", 100000)]))
b = block(r)
print("5 parallel_overrun:", r["status"], b["status"], b["outcome"],
      b["findings"][0]["reason"], b["findings"][0].get("over_ms"))

# 6. 故障后总时限内未启备用
r = evaluate(payload([ev("D1", "alarm", 1000),
                      ev("C1", "trip", 4000),
                      ev("FB", "start", 40000)]))
b = block(r)
print("6 standby_timeout:", r["status"], b["status"], b["outcome"])

# 7. 故障日志缺号 -> unknown
r = evaluate(payload([ev("D1", "alarm", 1000),
                      ev("C1", "run", 3000),
                      {"device": "C1", "seq": 3, "signal": "trip",
                       "device_ts": 4000},
                      ev("FB", "start", 10000)]))
b = block(r)
print("7 fault gap unknown:", r["status"], b["status"], b["outcome"],
      b["findings"][0]["reason"])

# 8. 时钟不可信 -> unknown
p = payload([ev("D1", "alarm", 1000),
             ev("C1", "trip", 4000),
             ev("FB", "start", 10000)])
p["sync_pulses"].append({"device": "C1", "device_ts": 100000,
                         "master_ts": 100500})
r = evaluate(p)
b = block(r)
print("8 clock unknown:", r["status"], b["status"],
      b["findings"][0]["reason"])

# 9. 敞开窗口：无故障无备机，日志末端 -> unknown
r = evaluate(payload([ev("D1", "alarm", 1000),
                      ev("F1", "start", 2000)]))
b = block(r)
# 主机反馈 ok + 敞开无故障 -> primary_success?
print("9 open primary ok:", r["status"], b["status"], b["outcome"])

# 10. 无故障无主机无备机（敞开）
r = evaluate(payload([ev("D1", "alarm", 1000)]))
b = block(r)
print("10 open no events:", r["status"], b["status"], b["outcome"],
      b["findings"][0]["reason"])
