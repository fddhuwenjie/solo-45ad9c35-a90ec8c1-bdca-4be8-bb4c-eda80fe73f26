# -*- coding: utf-8 -*-
"""
消防联动试验链复核 API
======================

- wsgiref 接 HTTP，json 解载荷，sqlite3 存修订。
- 统一时间轴：按校时脉冲拟合各设备时钟偏移，残差越界则该设备时标不可信。
- 逐场景回放：探测确认 -> 阀门到位 -> 风机启停 -> 电梯迫降 -> 广播切换，
  报告首个超时、倒序、互斥输出、未复位旁路，并给出上游链路。
- 日志缺号 / 别名多解 / 校时残差越界 / 前置状态不明 -> 相应环节保持 unknown。
- 重绑设备或改时钟锚点必须附 justification，系统另起修订并保留旧演算。
- 签结（signoff）后冻结矩阵、事件与别名映射，拒绝新修订；提供修订差异与重放 JSON。

运行:  python3 app.py [--port 8073] [--db firelink.db]

请求体（创建项目 / 新修订）示例见 simulate.py。字段约定:

  devices          {设备号: {type, zone, ...}}
  aliases          {别名: 设备号 或 [设备号, ...]}   # 多解即歧义
  matrix           [{id, trigger:{device,signal},
                     respond:[{device,signal,within_ms,after:["DEV:SIG"]}]}]
  mutex            [["DEV:SIG","DEV:SIG"], ...] 或 [{members:[...], window_ms}]
  sync_tolerance_ms  校时残差容限，默认 150
  sync_pulses      [{device, device_ts, master_ts}]
  bypass_permits   [{device, start, end, reason}]   # 统一时轴上的许可窗口
  events           [{device, seq, signal, device_ts, value?}]
"""

import json
import re
import sqlite3
import sys
import threading
import time
import uuid
from wsgiref.simple_server import make_server

# ---------------------------------------------------------------- 存储

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
  id TEXT PRIMARY KEY, name TEXT, created REAL, signed_off INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS revisions(
  project TEXT, rev INTEGER, payload TEXT, result TEXT,
  justification TEXT, created REAL,
  PRIMARY KEY(project, rev));
"""


def db_connect(path):
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------- 复核引擎

def evaluate(p):
    """对一份载荷做全量复核，返回结果 dict（可 JSON 序列化）。"""
    devices = set(p.get("devices", {}) or [])
    aliases = p.get("aliases", {}) or {}
    tol = p.get("sync_tolerance_ms", 150)

    # ---- 统一时间轴：校时脉冲 -> 偏移（中位数）与残差 ----
    pulses = {}
    for pu in p.get("sync_pulses", []) or []:
        pulses.setdefault(pu["device"], []).append(pu)
    offset, residual, untrusted = {}, {}, set()
    for d, ps in pulses.items():
        cands = sorted(x["master_ts"] - x["device_ts"] for x in ps)
        off = cands[len(cands) // 2]
        offset[d] = off
        r = max(abs(x["device_ts"] + off - x["master_ts"]) for x in ps)
        residual[d] = r
        if r > tol:
            untrusted.add(d)

    # ---- 事件统一到主时轴；无脉冲设备的锚点不明 ----
    events = []
    for e in p.get("events", []) or []:
        d = e["device"]
        if d not in offset:
            untrusted.add(d)
        events.append({**e, "ts": e["device_ts"] + offset.get(d, 0)})

    by_dev = {}
    for e in events:
        by_dev.setdefault(e["device"], []).append(e)

    # ---- 日志缺号：每台设备按序号找空洞区间 ----
    gaps = {}
    for d, lst in by_dev.items():
        lst.sort(key=lambda e: (e["seq"], e["ts"]))
        for a, b in zip(lst, lst[1:]):
            if b["seq"] > a["seq"] + 1:
                gaps.setdefault(d, []).append({
                    "from_seq": a["seq"] + 1, "to_seq": b["seq"] - 1,
                    "from_ts": a["ts"], "to_ts": b["ts"]})

    def resolve(name):
        """别名 -> 唯一设备；多解/无解都不算硬失败，只让环节保持未知。"""
        if name in devices:
            return name, None
        tgt = aliases.get(name)
        if tgt is None:
            return None, "alias_unresolved"
        if isinstance(tgt, str):
            return tgt, None
        if len(tgt) == 1:
            return tgt[0], None
        return None, "alias_ambiguous"

    def gap_between(d, lo, hi):
        for g in gaps.get(d, []):
            if g["from_ts"] <= hi and g["to_ts"] >= lo:
                return g
        return None

    def check_resp(t0, resp, rule):
        name, sig = resp["device"], resp["signal"]
        chain = [rule["trigger"]["device"]] + list(resp.get("after", [])) + [f"{name}:{sig}"]
        base = {"type": "response", "target": f"{name}:{sig}", "upstream": chain}
        d, err = resolve(name)
        if err:
            return {**base, "status": "unknown", "reason": err}
        within = resp.get("within_ms")
        hi = t0 + within if within is not None else None
        if d in untrusted:
            return {**base, "status": "unknown", "reason": "clock_residual_out_of_bounds"}
        if hi is not None:
            g = gap_between(d, t0, hi)
            if g:
                return {**base, "status": "unknown", "reason": "log_gap", "gap": g}
        cands = [e for e in by_dev.get(d, [])
                 if e["signal"] == sig and e["ts"] >= t0 and (hi is None or e["ts"] <= hi)]
        if not cands:
            late = [e for e in by_dev.get(d, []) if e["signal"] == sig and e["ts"] > t0]
            f = {**base, "status": "fail", "type": "timeout",
                 "deadline": hi, "detail": "时限内未见动作"}
            if late and hi is not None:
                f["actual"] = min(late, key=lambda e: e["ts"])["ts"]
                f["over_ms"] = f["actual"] - hi
            return f
        ev = min(cands, key=lambda e: e["ts"])
        for dep in resp.get("after", []):
            dd, ds = dep.split(":", 1)
            rd, rerr = resolve(dd)
            deps = [] if (rerr or rd is None) else [
                e for e in by_dev.get(rd, [])
                if e["signal"] == ds and t0 <= e["ts"] <= ev["ts"]]
            if not deps:
                return {**base, "status": "fail", "type": "out_of_order",
                        "actual": ev["ts"], "missing_predecessor": dep}
        return {**base, "status": "ok", "actual": ev["ts"], "elapsed_ms": ev["ts"] - t0}

    # ---- 逐场景回放 ----
    scenarios = []
    for rule in p.get("matrix", []) or []:
        trig = rule["trigger"]
        tdev, terr = resolve(trig["device"])
        trig_evs = []
        if tdev:
            trig_evs = sorted((e for e in by_dev.get(tdev, [])
                               if e["signal"] == trig["signal"]),
                              key=lambda e: e["ts"])
        instances = []
        if terr or not trig_evs:
            instances.append({
                "trigger_ts": None, "status": "unknown",
                "findings": [{"type": "precondition_unknown", "status": "unknown",
                              "reason": terr or "trigger_not_observed",
                              "upstream": [trig["device"]]}]})
        for tev in trig_evs:
            t0 = tev["ts"]
            findings = []
            if tdev in untrusted:
                findings.append({"type": "precondition_unknown", "status": "unknown",
                                 "reason": "clock_residual_out_of_bounds",
                                 "upstream": [trig["device"]]})
            for resp in rule.get("respond", []):
                findings.append(check_resp(t0, resp, rule))
            timeouts = [f for f in findings if f.get("type") == "timeout"]
            if timeouts:  # 首个超时 = 截止时限最早者
                min(timeouts, key=lambda f: f["deadline"])["first"] = True
            st = ("fail" if any(f["status"] == "fail" for f in findings)
                  else "unknown" if any(f["status"] == "unknown" for f in findings)
                  else "pass")
            instances.append({"trigger_ts": t0, "status": st, "findings": findings})
        sc_st = ("fail" if any(i["status"] == "fail" for i in instances)
                 else "unknown" if any(i["status"] == "unknown" for i in instances)
                 else "pass")
        scenarios.append({"id": rule.get("id"), "status": sc_st, "instances": instances})

    # ---- 互斥输出 ----
    mutex_findings = []
    for grp in p.get("mutex", []) or []:
        if isinstance(grp, dict):
            members, win = grp["members"], grp.get("window_ms", 5000)
        else:
            members, win = grp, p.get("mutex_window_ms", 5000)
        ons = []
        for m in members:
            dd, ds = m.split(":", 1)
            rd, rerr = resolve(dd)
            if rerr:
                mutex_findings.append({"group": members, "status": "unknown",
                                       "reason": rerr, "member": m})
                continue
            for e in by_dev.get(rd, []):
                if e["signal"] == ds:
                    ons.append((m, e["ts"]))
        ons.sort(key=lambda x: x[1])
        viol = None
        for i in range(len(ons)):
            for j in range(i + 1, len(ons)):
                if ons[j][1] - ons[i][1] > win:
                    break
                if ons[i][0] != ons[j][0]:
                    viol = {"group": members, "status": "fail", "type": "mutex",
                            "members": [ons[i][0], ons[j][0]],
                            "at": [ons[i][1], ons[j][1]]}
                    break
            if viol:
                break
        if viol:
            mutex_findings.append(viol)

    # ---- 旁路：许可窗口内且复位为合法；未复位/越窗为失败 ----
    bypass_findings = []
    permits = p.get("bypass_permits", []) or []
    for d, lst in by_dev.items():
        on = None
        for e in sorted(lst, key=lambda x: x["ts"]):
            if e["signal"] == "bypass_on":
                on = e
            elif e["signal"] == "bypass_off" and on:
                ok = any(pm["device"] == d and pm["start"] <= on["ts"]
                         and e["ts"] <= pm["end"] for pm in permits)
                bypass_findings.append({
                    "device": d, "on": on["ts"], "off": e["ts"],
                    "status": "ok" if ok else "fail",
                    "type": None if ok else "bypass_outside_permit"})
                on = None
        if on:
            bypass_findings.append({"device": d, "on": on["ts"], "off": None,
                                    "status": "fail", "type": "bypass_not_reset"})

    overall = ("fail" if (any(s["status"] == "fail" for s in scenarios)
                          or any(f["status"] == "fail" for f in mutex_findings + bypass_findings))
               else "unknown" if (any(s["status"] == "unknown" for s in scenarios)
                                  or any(f["status"] == "unknown"
                                         for f in mutex_findings + bypass_findings))
               else "pass")
    return {
        "status": overall,
        "clock": {"tolerance_ms": tol, "offset_ms": offset,
                  "residual_ms": residual, "untrusted": sorted(untrusted)},
        "log_gaps": gaps,
        "scenarios": scenarios,
        "mutex": mutex_findings,
        "bypass": bypass_findings,
    }


# ---------------------------------------------------------------- 修订差异

def diff_json(a, b, path=""):
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append({"path": f"{path}/{k}", "op": "add", "to": b[k]})
            elif k not in b:
                out.append({"path": f"{path}/{k}", "op": "remove", "from": a[k]})
            else:
                out += diff_json(a[k], b[k], f"{path}/{k}")
    elif isinstance(a, list) and isinstance(b, list) and path.endswith("/events"):
        key = lambda e: (e.get("device"), e.get("seq"))
        ea, eb = {key(e): e for e in a}, {key(e): e for e in b}
        for k in sorted(set(ea) | set(eb)):
            if k not in ea:
                out.append({"path": f"{path}/{k}", "op": "add", "to": eb[k]})
            elif k not in eb:
                out.append({"path": f"{path}/{k}", "op": "remove", "from": ea[k]})
            elif ea[k] != eb[k]:
                out.append({"path": f"{path}/{k}", "op": "replace",
                            "from": ea[k], "to": eb[k]})
    elif a != b:
        out.append({"path": path, "op": "replace", "from": a, "to": b})
    return out


def verdict_changes(ra, rb):
    sa = {s["id"]: s["status"] for s in ra.get("scenarios", [])}
    sb = {s["id"]: s["status"] for s in rb.get("scenarios", [])}
    return [{"scenario": k, "from": sa.get(k), "to": sb.get(k)}
            for k in sorted(set(sa) | set(sb)) if sa.get(k) != sb.get(k)]


# ---------------------------------------------------------------- HTTP 层

REASONS = {200: "OK", 201: "Created", 400: "Bad Request",
           404: "Not Found", 409: "Conflict"}


def respond(start_response, code, obj):
    body = json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")
    start_response(f"{code} {REASONS[code]}",
                   [("Content-Type", "application/json; charset=utf-8"),
                    ("Content-Length", str(len(body)))])
    return [body]


def read_json(environ):
    try:
        n = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        n = 0
    raw = environ["wsgi.input"].read(n) if n else b""
    if not raw.strip():
        return {}, None
    try:
        return json.loads(raw.decode("utf-8")), None
    except Exception as ex:
        return None, f"JSON 解析失败: {ex}"


def get_rev(conn, pid, rev):
    row = conn.execute(
        "SELECT payload, result, justification, created FROM revisions"
        " WHERE project=? AND rev=?", (pid, rev)).fetchone()
    if not row:
        return None
    return {"payload": json.loads(row[0]), "result": json.loads(row[1]),
            "justification": row[2], "created": row[3]}


def make_app(db_path):
    conn = db_connect(db_path)
    lock = threading.Lock()

    def create_project(body):
        pid = uuid.uuid4().hex[:8]
        name = body.get("project") or body.get("name") or pid
        payload = body.get("payload", body)
        result = evaluate(payload)
        conn.execute("INSERT INTO projects VALUES(?,?,?,0)", (pid, name, time.time()))
        conn.execute("INSERT INTO revisions VALUES(?,?,?,?,?,?)",
                     (pid, 1, json.dumps(payload, ensure_ascii=False),
                      json.dumps(result, ensure_ascii=False), "initial", time.time()))
        conn.commit()
        return 201, {"project": pid, "rev": 1, "status": result["status"]}

    def add_revision(pid, body):
        row = conn.execute("SELECT signed_off FROM projects WHERE id=?", (pid,)).fetchone()
        if not row:
            return 404, {"error": "project not found"}
        if row[0]:
            return 409, {"error": "已签结，矩阵/事件/别名映射已冻结，拒绝新修订"}
        just = (body.get("justification") or "").strip()
        if not just:
            return 400, {"error": "重绑设备或改时钟锚点必须给出 justification 依据"}
        cur = conn.execute("SELECT MAX(rev) FROM revisions WHERE project=?",
                           (pid,)).fetchone()[0]
        base = get_rev(conn, pid, cur)["payload"]
        if "payload" in body:                     # 整体替换
            new_payload = body["payload"]
        else:                                     # 顶层键补丁：aliases / sync_pulses / ...
            new_payload = dict(base)
            for k, v in (body.get("patch") or {}).items():
                if k == "aliases":
                    merged = dict(new_payload.get("aliases", {}))
                    merged.update(v)              # 重绑设备 = 覆盖别名映射
                    new_payload["aliases"] = merged
                else:
                    new_payload[k] = v
        result = evaluate(new_payload)
        conn.execute("INSERT INTO revisions VALUES(?,?,?,?,?,?)",
                     (pid, cur + 1, json.dumps(new_payload, ensure_ascii=False),
                      json.dumps(result, ensure_ascii=False), just, time.time()))
        conn.commit()
        return 201, {"project": pid, "rev": cur + 1, "status": result["status"],
                     "justification": just}

    def signoff(pid):
        row = conn.execute("SELECT signed_off FROM projects WHERE id=?", (pid,)).fetchone()
        if not row:
            return 404, {"error": "project not found"}
        conn.execute("UPDATE projects SET signed_off=1 WHERE id=?", (pid,))
        conn.commit()
        return 200, {"project": pid, "signed_off": True,
                     "frozen": ["matrix", "events", "aliases"]}

    def get_project(pid):
        row = conn.execute("SELECT name, created, signed_off FROM projects WHERE id=?",
                           (pid,)).fetchone()
        if not row:
            return 404, {"error": "project not found"}
        revs = conn.execute(
            "SELECT rev, justification, created, result FROM revisions"
            " WHERE project=? ORDER BY rev", (pid,)).fetchall()
        return 200, {
            "project": pid, "name": row[0], "created": row[1],
            "signed_off": bool(row[2]),
            "revisions": [{"rev": r[0], "justification": r[1], "created": r[2],
                           "status": json.loads(r[3])["status"]} for r in revs]}

    def get_revision(pid, rev):
        r = get_rev(conn, pid, int(rev))
        if not r:
            return 404, {"error": "revision not found"}
        return 200, {"project": pid, "rev": int(rev),
                     "justification": r["justification"], "replay": r["result"]}

    def get_diff(pid, query):
        m = dict(re.findall(r"([^=&?]+)=([^&]*)", query or ""))
        last = conn.execute("SELECT MAX(rev) FROM revisions WHERE project=?",
                            (pid,)).fetchone()[0]
        if last is None:
            return 404, {"error": "project not found"}
        a, b = int(m.get("from", last - 1)), int(m.get("to", last))
        ra, rb = get_rev(conn, pid, a), get_rev(conn, pid, b)
        if not ra or not rb:
            return 404, {"error": "revision not found"}
        return 200, {
            "project": pid, "from": a, "to": b,
            "payload_changes": diff_json(ra["payload"], rb["payload"]),
            "verdict_changes": verdict_changes(ra["result"], rb["result"]),
            "justification": rb["justification"]}

    def app(environ, start_response):
        with lock:
            return _dispatch(environ, start_response)

    def _dispatch(environ, start_response):
        method, path = environ["REQUEST_METHOD"], environ["PATH_INFO"].rstrip("/")
        query = environ.get("QUERY_STRING", "")
        body, err = (read_json(environ) if method == "POST" else ({}, None))
        if err:
            return respond(start_response, 400, {"error": err})
        m = re.fullmatch(r"/projects", path)
        if m and method == "POST":
            code, obj = create_project(body)
            return respond(start_response, code, obj)
        m = re.fullmatch(r"/projects/([0-9a-f]+)", path)
        if m and method == "GET":
            code, obj = get_project(m.group(1))
            return respond(start_response, code, obj)
        m = re.fullmatch(r"/projects/([0-9a-f]+)/revisions", path)
        if m and method == "POST":
            code, obj = add_revision(m.group(1), body)
            return respond(start_response, code, obj)
        m = re.fullmatch(r"/projects/([0-9a-f]+)/revisions/(\d+)", path)
        if m and method == "GET":
            code, obj = get_revision(m.group(1), m.group(2))
            return respond(start_response, code, obj)
        m = re.fullmatch(r"/projects/([0-9a-f]+)/signoff", path)
        if m and method == "POST":
            code, obj = signoff(m.group(1))
            return respond(start_response, code, obj)
        m = re.fullmatch(r"/projects/([0-9a-f]+)/diff", path)
        if m and method == "GET":
            code, obj = get_diff(m.group(1), query)
            return respond(start_response, code, obj)
        return respond(start_response, 404, {"error": "not found"})

    return app


def main():
    port, db = 8073, "firelink.db"
    args = sys.argv[1:]
    if "--port" in args:
        port = int(args[args.index("--port") + 1])
    if "--db" in args:
        db = args[args.index("--db") + 1]
    print(f"消防联动复核 API  listening on :{port}  db={db}")
    make_server("0.0.0.0", port, make_app(db)).serve_forever()


if __name__ == "__main__":
    main()
