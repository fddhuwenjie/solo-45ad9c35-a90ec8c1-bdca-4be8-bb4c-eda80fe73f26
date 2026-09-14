# -*- coding: utf-8 -*-
"""
消防联动试验链复核 API
======================

- wsgiref 接 HTTP，json 解载荷，sqlite3 存修订。
- 统一时间轴：按校时脉冲拟合各设备时钟偏移，残差越界则该设备时标不可信。
- 逐场景回放：探测确认 -> 阀门到位 -> 风机启停 -> 电梯迫降 -> 广播切换，
  报告首个超时、倒序、互斥输出、未复位旁路，并给出上游链路。
- 复合触发规则（防排烟联动确认逻辑）：
  * 规则可定义 all / any / k_of_n 组合（支持嵌套与命名引用）、确认窗口
    window_ms、保持时长 hold_ms（去触点抖动）、全局复位信号 reset 与手报
    旁路 manual；单 trigger 写法保持兼容。
  * 分析器按校正时轴与设备 seq 生成独立触发实例：重复置位合并为同一轮，
    复位后再报警另起实例，窗口滑动不得跨轮串案；同一轮的响应链绑定到
    该实例（全局复位之后才发生的响应不挂到旧轮）。
  * 别名多解、参与设备日志缺号、校时不可信、组合引用循环或确认窗口证据
    不完整（窗口敞开/缺口可能藏事件）时，该实例保持 unknown 并在 gaps
    中逐项列出缺口。
- 日志缺号 / 别名多解 / 校时残差越界 / 前置状态不明 -> 相应环节保持 unknown。
- 重绑设备或改时钟锚点、改触发规则必须附 justification，系统另起修订并
  保留旧演算（重放 JSON 固定当时的归一化复合规则、组成事件与实例判定）。
- 签结（signoff）后冻结矩阵、事件与别名映射，拒绝新修订；提供修订差异与重放 JSON。

运行:  python3 app.py [--port 8073] [--db firelink.db]

请求体（创建项目 / 新修订）示例见 simulate.py。字段约定:

  devices          {设备号: {type, zone, ...}}
  aliases          {别名: 设备号 或 [设备号, ...]}   # 多解即歧义
  matrix           [{id, trigger:{device,signal},                     # 旧写法，兼容
                     respond:[{device,signal,within_ms,after:["DEV:SIG"]}]}
                    # 复合写法（trigger 旁并列 composite，或仅给 composite）:
                    {id, composite:{
                       all|any|k_of_n: [叶子, ...],
                       # 叶子 = {device, signal, reset?: "SIG"}
                       #      | {all|any|k_of_n:[...]}
                       #      | "#命名组合"
                       # k_of_n 简写: ["k_of_n", 2, 叶子...]
                       defs: {"命名": 组合},
                       window_ms: 确认窗口, hold_ms: 抖动保持时长,
                       reset: {device, signal}|"DEV:SIG",   # 全局复位
                       manual: {device, signal}             # 手报旁路：直接触发}}]
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

    def response_unknown(resp, chain, reason, **extra):
        name, sig = resp["device"], resp["signal"]
        out = {"type": "response", "target": f"{name}:{sig}",
               "upstream": chain, "status": "unknown", "reason": reason}
        out.update(extra)
        return out

    def check_resp(t0, resp, rule, cap=None):
        name, sig = resp["device"], resp["signal"]
        head = (rule.get("trigger") or {}).get("device", "composite")
        chain = [head] + list(resp.get("after", [])) + [f"{name}:{sig}"]
        base = {"type": "response", "target": f"{name}:{sig}", "upstream": chain}
        d, err = resolve(name)
        if err:
            return {**base, "status": "unknown", "reason": err}
        within = resp.get("within_ms")
        hi = t0 + within if within is not None else None
        if hi is not None and cap is not None and hi > cap:
            hi = cap   # 全局复位后（下一轮之前）的响应不挂到本轮
        if d in untrusted:
            return {**base, "status": "unknown", "reason": "clock_residual_out_of_bounds"}
        if hi is not None:
            g = gap_between(d, t0, hi)
            if g:
                return {**base, "status": "unknown", "reason": "log_gap", "gap": g}
        cands = [e for e in by_dev.get(d, [])
                 if e["signal"] == sig and e["ts"] >= t0 and (hi is None or e["ts"] <= hi)]
        if not cands:
            deadline = t0 + within if within is not None else None
            late_hi = cap if cap is not None and (deadline is None or cap < deadline) else deadline
            late = [e for e in by_dev.get(d, [])
                    if e["signal"] == sig and e["ts"] > t0
                    and (late_hi is None or e["ts"] <= late_hi)]
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
            if rerr:  # 前置别名多解/无解：前置状态不明，保持未知而非倒序
                return {**base, "status": "unknown",
                        "reason": f"predecessor_{rerr}", "predecessor": dep}
            if rd in untrusted:  # 前置设备时标不可信，无法判定先后
                return {**base, "status": "unknown",
                        "reason": "predecessor_clock_untrusted", "predecessor": dep}
            deps = [e for e in by_dev.get(rd, [])
                    if e["signal"] == ds and t0 <= e["ts"] <= ev["ts"]]
            if not deps:
                return {**base, "status": "fail", "type": "out_of_order",
                        "actual": ev["ts"], "missing_predecessor": dep}
        return {**base, "status": "ok", "actual": ev["ts"], "elapsed_ms": ev["ts"] - t0}

    # ---------------------------------------------------- 复合触发引擎
    # 一个“触发实例”= 一轮火警。确认 = 组合在 window_ms 窗口内凑齐（
    # 手报可旁路）；hold_ms 用于触点去抖（置位后在保持时长内复位 = 抖动，
    # 不立案）；reset 为全局复位，复位后再报警另起一轮。
    def eval_composite_rule(rule):
        """返回 (normalized, raw_instances)；结构非法时 raw 为 None。"""
        comp = rule["composite"]
        struct_gaps, leaves = [], []
        defs = comp.get("defs", {}) or {}

        def add_leaf(spec):
            leaf = {"device_spec": spec.get("device"),
                    "signal": spec.get("signal"),
                    "reset_signal": spec.get("reset")}
            d, err = resolve(spec.get("device"))
            if err:
                leaf.update(device=spec.get("device"), err=err)
                leaf["mi"] = len(leaves)
                leaves.append(leaf)
                return leaf["mi"]
            leaf["device"] = d
            key = (d, spec.get("signal"))
            for ex in leaves:  # 同一设备同一信号在组合里只算一路
                if "err" not in ex and (ex["device"], ex["signal"]) == key:
                    return ex["mi"]
            leaf["mi"] = len(leaves)
            leaves.append(leaf)
            return leaf["mi"]

        def parse_node(spec, stack):
            if isinstance(spec, str):
                ref = spec[1:] if spec.startswith("#") else spec
                if ref not in defs:
                    return {"bad": [{"member": spec, "reason": "ref_unresolved"}]}
                if ref in stack:
                    return {"bad": [{"member": f"#{ref}",
                                     "reason": "composite_cycle"}]}
                return parse_node(defs[ref], stack | {ref})
            if isinstance(spec, list):
                # 简写: ["k_of_n", 2, 叶子, ...]
                if len(spec) >= 3 and spec[0] == "k_of_n" \
                        and isinstance(spec[1], int) and not isinstance(spec[1], bool):
                    spec = {"k_of_n": spec[2:], "k": spec[1]}
                elif len(spec) >= 2 and spec[0] in ("all", "any"):
                    spec = {spec[0]: spec[1:]}
                else:
                    return {"bad": [{"member": None,
                                     "reason": "invalid_composite"}]}
            if not isinstance(spec, dict):
                return {"bad": [{"member": None, "reason": "invalid_composite"}]}
            if "device" in spec:
                if not spec.get("signal"):
                    return {"bad": [{"member": spec.get("device"),
                                     "reason": "invalid_composite"}]}
                mi = add_leaf(spec)
                if "err" in leaves[mi]:
                    return {"bad": [{"member": spec["device"],
                                     "reason": leaves[mi]["err"]}]}
                return {"leaf": mi}
            op = next((k for k in ("all", "any", "k_of_n") if k in spec), None)
            if op is None:
                return {"bad": [{"member": None, "reason": "invalid_composite"}]}
            children = spec[op]
            k = spec.get("k") if op == "k_of_n" else None
            if not isinstance(children, list) or not children:
                return {"bad": [{"member": None, "reason": "invalid_composite"}]}
            sub = [parse_node(c, stack) for c in children]
            bad = [b for n in sub for b in n.get("bad", [])]
            if bad:
                return {"bad": bad}
            if op == "all":
                return {"all": sub}
            if op == "any":
                return {"any": sub}
            if not (isinstance(k, int) and not isinstance(k, bool)
                    and 1 <= k <= len(sub)):
                return {"bad": [{"member": None, "reason": "k_out_of_range",
                                 "detail": {"k": k, "n": len(sub)}}]}
            return {"k_of_n": sub, "k": k}

        def parse_endpoint(ep, tag):
            if isinstance(ep, str):
                ds = ep.split(":", 1)
                if len(ds) != 2:
                    return None, [{"member": f"{tag}:{ep}",
                                   "reason": "invalid_composite"}]
                ep = {"device": ds[0], "signal": ds[1]}
            if not isinstance(ep, dict) or not ep.get("signal"):
                return None, [{"member": tag, "reason": "invalid_composite"}]
            d, err = resolve(ep.get("device"))
            if err:
                return None, [{"member": f"{tag}:{ep['device']}", "reason": err}]
            return {"device": d, "signal": ep["signal"]}, None

        root = parse_node(comp, set())
        if "bad" in root:
            struct_gaps = root["bad"]
        window = comp.get("window_ms", 0) or 0
        hold = comp.get("hold_ms", 0) or 0
        manual_ep, merr = (parse_endpoint(comp["manual"], "manual")
                           if comp.get("manual") else (None, None))
        reset_ep, rerr = (parse_endpoint(comp["reset"], "reset")
                          if comp.get("reset") else (None, None))
        if merr:
            struct_gaps += merr
        if rerr:
            struct_gaps += rerr

        def dump_node(n):
            if "leaf" in n:
                lf = leaves[n["leaf"]]
                out = {"device": lf["device"], "signal": lf["signal"]}
                if lf.get("reset_signal"):
                    out["reset"] = lf["reset_signal"]
                return out
            if "bad" in n:
                return {"invalid": n["bad"]}
            op = next(k for k in ("all", "any", "k_of_n") if k in n)
            out = {op: [dump_node(c) for c in n[op]]}
            if op == "k_of_n":
                out["k"] = n["k"]
            return out

        normalized = {
            "root": dump_node(root) if not struct_gaps
            else {"invalid": struct_gaps},
            "window_ms": window, "hold_ms": hold,
            "manual": (f"{manual_ep['device']}:{manual_ep['signal']}"
                       if manual_ep else None),
            "reset": (f"{reset_ep['device']}:{reset_ep['signal']}"
                      if reset_ep else None)}
        if struct_gaps:
            return normalized, None

        # ---- 全局复位时刻（复位设备不可信则只作未知嫌疑，不用于切轮） ----
        reset_ts = []
        if reset_ep:
            rd = reset_ep["device"]
            if rd not in untrusted:
                reset_ts = sorted(e["ts"] for e in by_dev.get(rd, [])
                                  if e["signal"] == reset_ep["signal"])

        def next_reset_after(ts):
            for r in reset_ts:
                if r > ts:
                    return r
            return None

        def gap_after_seq(dev, from_seq):
            for g in gaps.get(dev, []):
                if g["from_seq"] > from_seq:
                    return g
            return None

        # ---- 每路叶子（或手报）按 seq/ts 切分置位轮次：
        #      重复置位并入同一轮；保持时长内复位 = 触点抖动，丢弃；
        #      相关 seq 缺口可能藏复位/置位 -> 该轮 unknown。
        #      全局复位为每一轮关轮；手报为离散刻意动作，不做去抖。 ----
        def build_episodes(dev, sig, local_reset_sig, is_manual=False):
            if dev in untrusted:
                return None
            eff_hold = 0 if is_manual else hold
            stream = sorted(by_dev.get(dev, []), key=lambda e: (e["seq"], e["ts"]))

            def later_events(from_seq):
                return [x for x in stream if x["seq"] > from_seq]

            def settle(on, close_ts, synthetic):
                """在 close_ts 处关轮：先查缺口，再用保持时长判定稳定性。"""
                if on["stable"]:
                    on["reset"] = {"ts": close_ts, "seq": None,
                                   "synthetic": synthetic}
                    return "ok"
                g = gap_between(dev, on["set"]["ts"], close_ts)
                if g:
                    on["gap"] = g
                elif close_ts - on["set"]["ts"] >= eff_hold:
                    on["stable"] = True
                if on["gap"] or on["stable"]:
                    on["reset"] = {"ts": close_ts, "seq": None,
                                   "synthetic": synthetic}
                    return "ok"
                return "bounce"  # 保持时长内复位 => 触点抖动，丢弃该轮

            eps, on = [], None
            for e in stream:
                relevant = e["signal"] == sig or (
                    local_reset_sig and e["signal"] == local_reset_sig)
                if not relevant:
                    continue
                if e["signal"] == sig:
                    # 全局复位已在上一轮与本次置位之间发生：先关旧轮
                    if on is not None:
                        gr = next((r for r in reset_ts
                                   if on["set"]["ts"] < r <= e["ts"]), None)
                        if gr is not None:
                            if settle(on, gr, True) == "ok":
                                eps.append(on)
                            on = None
                    if on is None:
                        on = {"set": e, "reset": None, "repeats": 0,
                              "gap": None, "stable": is_manual, "mi": None,
                              "manual": is_manual,
                              "used": False, "dead": False, "banned": False}
                    elif local_reset_sig:
                        on["repeats"] += 1   # 有独立复位信号：重复置位=同轮
                    else:
                        # 无独立复位信号：上一置位轮在本点关轮，本次另起，
                        # 但记为前一轮的重复置位（触点抖动去重）
                        on["reset"] = {"ts": e["ts"], "seq": e.get("seq"),
                                       "synthetic": True}
                        eps.append(on)
                        on = {"set": e, "reset": None, "repeats": 0,
                              "gap": None, "stable": is_manual, "mi": None,
                              "manual": is_manual,
                              "used": False, "dead": False, "banned": False}
                    continue
                if on is not None:
                    if settle(on, e["ts"], False) == "ok":
                        on["reset"] = e
                        eps.append(on)
                    on = None
            if on is not None:
                gr = next((r for r in reset_ts if r > on["set"]["ts"]), None)
                if gr is not None and settle(on, gr, True) == "ok":
                    eps.append(on)
                    on = None
                elif gr is not None:
                    on = None
            if on is not None and not on["stable"]:
                tg = gap_after_seq(dev, on["set"]["seq"])
                if tg:
                    on["gap"] = tg
                else:
                    # 保持证据只认本机参与信号事件（面板复位不算）：
                    # 后续日志覆盖到保持时长之外 => 置位坐实稳定；
                    # 否则保持证据不完整（窗口敞开），保留 unknown 嫌疑。
                    sigs = {sig}
                    if local_reset_sig:
                        sigs.add(local_reset_sig)
                    tail_to = max((x["ts"] for x in later_events(on["set"]["seq"])
                                   if x["signal"] in sigs), default=None)
                    if tail_to is not None \
                            and tail_to - on["set"]["ts"] >= eff_hold:
                        on["stable"] = True
            if on is not None:
                eps.append(on)
            return eps

        pools, clock_members = [], set()
        for lf in leaves:
            if "err" in lf or lf["device"] in untrusted:
                pools.append(None)
                if "err" not in lf:
                    clock_members.add(lf["mi"])
            else:
                eps = build_episodes(lf["device"], lf["signal"],
                                     lf.get("reset_signal"))
                for ep in eps:
                    ep["mi"] = lf["mi"]
                pools.append(eps)
        manual_pool = []
        manual_clock = False
        if manual_ep:
            if manual_ep["device"] in untrusted:
                manual_clock = True
            else:
                manual_pool = build_episodes(manual_ep["device"],
                                             manual_ep["signal"], None,
                                             is_manual=True)
                for ep in manual_pool:
                    ep["mi"] = -1

        def label(mi):
            lf = leaves[mi]
            return f"{lf['device']}:{lf['signal']}"

        def dedup_gaps(gs):
            out, seen = [], set()
            for g in gs:
                k = (g.get("member"), g.get("reason"),
                     (g.get("gap") or {}).get("from_seq"))
                if k not in seen:
                    seen.add(k)
                    out.append(g)
            return out

        # ---- 在一个窗口上求解组合树 ----
        def solve_window(anchor_ts, lo, hi, after_ts):
            def leaf_gap(mi, reason, **extra):
                g = {"member": label(mi), "reason": reason}
                g.update(extra)
                return g

            def solve(n):
                if "leaf" in n:
                    mi = n["leaf"]
                    if mi in clock_members:
                        return False, [], None, \
                            [leaf_gap(mi, "clock_residual_out_of_bounds")]
                    # 仍处于置位（无复位）的轮次必须晚于游标，避免上一轮
                    # 的旧置位被滑窗再次确认；已复位轮次从本轮起点之后取
                    def fresh(ep):
                        if ep["set"]["ts"] <= lo:
                            return False
                        if ep["reset"] is None:
                            return ep["set"]["ts"] > after_ts
                        return True
                    cands = [ep for ep in (pools[mi] or [])
                             if not (ep["used"] or ep["dead"] or ep["banned"])
                             and ep["set"]["ts"] <= hi and fresh(ep)
                             and ep["stable"] and not ep["gap"]
                             and (ep["reset"] is None
                                  or ep["reset"]["ts"] >= anchor_ts)]
                    if cands:
                        chosen = min(cands, key=lambda ep: ep["set"]["ts"])
                        return True, [chosen], chosen["set"]["ts"], []
                    allc = [ep for ep in (pools[mi] or [])
                            if not (ep["used"] or ep["dead"] or ep["banned"])
                            and ep["set"]["ts"] <= hi
                            and ep["set"]["ts"] >= lo]
                    if any(ep.get("gap") for ep in allc):
                        return False, [], None, [leaf_gap(
                            mi, "log_gap",
                            gap=next(ep["gap"] for ep in allc if ep.get("gap")))]
                    if allc:  # 置位轮次未稳定：保持证据不足
                        return False, [], None, \
                            [leaf_gap(mi, "hold_evidence_incomplete")]
                    g = gap_between(leaves[mi]["device"], lo, hi)
                    if g:
                        return False, [], None, [leaf_gap(mi, "log_gap", gap=g)]
                    if leaves[mi]["device"] in by_dev:
                        return False, [], None, \
                            [leaf_gap(mi, "member_not_observed_in_window")]
                    return False, [], None, [leaf_gap(mi, "window_open")]

                def pack(c):
                    sat, chosen, t0v, gs = c
                    return {"sat": sat, "chosen": chosen, "t0": t0v, "gaps": gs}

                if "all" in n:
                    kids = [pack(solve(c)) for c in n["all"]]
                    if all(k["sat"] for k in kids):
                        return True, [ch for k in kids for ch in k["chosen"]], \
                            max(k["t0"] for k in kids), []
                    return False, [], None, [g for k in kids for g in k["gaps"]]
                if "any" in n:
                    kids = [pack(solve(c)) for c in n["any"]]
                    ok = [k for k in kids if k["sat"]]
                    if ok:
                        win = min(ok, key=lambda k: k["t0"])
                        return True, win["chosen"], win["t0"], []
                    return False, [], None, [g for k in kids for g in k["gaps"]]
                kids = [pack(solve(c)) for c in n["k_of_n"]]
                ok = sorted((k for k in kids if k["sat"]),
                            key=lambda k: k["t0"])
                if len(ok) >= n["k"]:
                    return True, [ch for k in ok[:n["k"]] for ch in k["chosen"]], \
                        ok[n["k"] - 1]["t0"], []
                return False, [], None, [g for k in kids for g in k["gaps"]]

            # 迭代复核：按实际 t0 剔除在 t0 前已局部复位的轮次
            for _ in range(len(leaves) + 1):
                sat, chosen, t0v, gs = solve(root)
                if not sat:
                    return False, [], None, dedup_gaps(gs)
                bad = [ch for ch in chosen
                       if ch["reset"] is not None and ch["reset"]["ts"] < t0v]
                if not bad:
                    return True, chosen, t0v, []
                for ch in bad:
                    ch["banned"] = True
            return False, [], None, dedup_gaps(gs)

        # ---- 逐轮扫描 ----
        cursor = -10**18
        raw = []
        all_pools = [p for p in pools if p] + [manual_pool]

        def member_view(chosen):
            out = []
            for ch in chosen:
                lf = leaves[ch["mi"]]
                out.append({"member": label(ch["mi"]),
                            "device": lf["device"], "signal": lf["signal"],
                            "seq": ch["set"].get("seq"),
                            "set_ts": ch["set"]["ts"],
                            "reset_ts": ch["reset"]["ts"] if ch["reset"] else None,
                            "repeat_sets": ch["repeats"]})
            return out

        def kill_round(close):
            # 只处置本轮（复位时刻之前）的置位；复位之后的属下一轮，不得串案
            for pool in all_pools:
                for ep in pool:
                    if not ep["used"] and ep["set"]["ts"] < close:
                        ep["dead"] = True

        def close_open_after(chosen_list, cutoff):
            """无全局复位确认后：把被消费各路仍敞开的置位在 cutoff 处关轮，
            这样同一信号后续再置位才能在 build 流里另起一轮（复位后再报警）。"""
            for ch in chosen_list:
                if ch["reset"] is not None:
                    continue
                dev = manual_ep["device"] if ch["manual"] else leaves[ch["mi"]]["device"]
                sig = manual_ep["signal"] if ch["manual"] else leaves[ch["mi"]]["signal"]
                later = [e for e in by_dev.get(dev, [])
                         if e["seq"] > ch["set"]["seq"] and e["signal"] == sig]
                if not later:
                    continue
                ch["reset"] = {"ts": later[0]["ts"], "seq": later[0]["seq"],
                               "synthetic": True}

        def hold_failure(chosen, t0v):            """确认后复核保持时长：某路在 t0+hold 前复位（且无缺口嫌疑）
            => 整轮按抖动废弃；有缺口 => 该轮 unknown。"""
            for ep in chosen:
                if ep["manual"]:
                    continue
                rset = ep["reset"]
                if rset is None:
                    continue
                if rset["ts"] >= t0v + hold:
                    continue
                if ep.get("gap"):
                    return "unknown", [{"member": label(ep["mi"]),
                                        "reason": "log_gap", "gap": ep["gap"]}]
                return "bounce", None
            return None, None

        def make_raw(t0v, gs, kind, cap, chosen=None, manual_ch=None):
            return {"trigger_ts": t0v, "gaps": dedup_gaps(gs), "kind": kind,
                    "members": member_view(chosen or []) if not manual_ch else [
                        {"member": f"{manual_ep['device']}:{manual_ep['signal']}",
                         "device": manual_ep["device"],
                         "signal": manual_ep["signal"],
                         "seq": manual_ch["set"].get("seq"),
                         "set_ts": manual_ch["set"]["ts"],
                         "reset_ts": (manual_ch["reset"]["ts"]
                                      if manual_ch["reset"] else None),
                         "repeat_sets": manual_ch["repeats"]}],
                    "cap": cap}

        while True:
            avail = [ep for pool in all_pools for ep in pool
                     if not (ep["used"] or ep["dead"] or ep["banned"])
                     and ep["stable"] and not ep["gap"]
                     and ep["set"]["ts"] > cursor]
            if not avail:
                # 残余的非稳定置位（保持证据不足/缺口）且无复位关窗：
                # 窗口敞开 -> 立一个 unknown；否则按已闭合未凑齐处理。
                pending = [ep for pool in all_pools for ep in pool
                           if not (ep["used"] or ep["dead"] or ep["banned"])
                           and ep["set"]["ts"] > cursor]
                if not pending:
                    break
                ep0 = min(pending, key=lambda e: e["set"]["ts"])
                gs0 = []
                for pool in all_pools:
                    for ep in pool:
                        if ep["gap"] and ep["set"]["ts"] > cursor:
                            who = (f"{manual_ep['device']}:{manual_ep['signal']}"
                                   if ep["manual"] else label(ep["mi"]))
                            gs0.append({"member": who, "reason": "log_gap",
                                        "gap": ep["gap"]})
                if not gs0:
                    close0 = next_reset_after(ep0["set"]["ts"])
                    if close0 is None:
                        who = (f"{manual_ep['device']}:{manual_ep['signal']}"
                               if ep0["manual"] else label(ep0["mi"]))
                        gs0 = [{"member": who,
                                "reason": "hold_evidence_incomplete"}]
                if gs0:
                    raw.append(make_raw(ep0["set"]["ts"], gs0,
                                        "manual" if ep0["manual"] else "composite",
                                        next_reset_after(ep0["set"]["ts"])))
                for ep in pending:
                    ep["dead"] = True
                cursor = ep0["set"]["ts"]
                continue
            anchor = min(avail, key=lambda ep: ep["set"]["ts"])
            a_ts = anchor["set"]["ts"]
            close = next_reset_after(a_ts)
            hi = a_ts + window
            if close is not None and close < hi:
                hi = close   # 窗口不得越过全局复位（同刻复位不截窗，防串案）

            # 手报旁路：本轮窗口内手报先直接确认，不等探测组合
            mans = [ep for ep in manual_pool
                    if not (ep["used"] or ep["dead"] or ep["banned"])
                    and a_ts <= ep["set"]["ts"] <= hi]
            if mans:
                m_ep = min(mans, key=lambda ep: ep["set"]["ts"])
                if m_ep.get("gap"):
                    raw.append(make_raw(m_ep["set"]["ts"],
                                        [{"member": f"{manual_ep['device']}:"
                                                    f"{manual_ep['signal']}",
                                          "reason": "log_gap",
                                          "gap": m_ep["gap"]}],
                                        "manual", close, manual_ch=m_ep))
                else:
                    m_ep["used"] = True
                    raw.append(make_raw(m_ep["set"]["ts"], [], "manual",
                                        close, manual_ch=m_ep))
                if close is not None:
                    kill_round(close)
                    cursor = close
                else:
                    cursor = m_ep["set"]["ts"]
                continue

            sat, chosen, t0v, gs = solve_window(a_ts, a_ts, hi, cursor)
            if sat:
                hfail, hgaps = hold_failure(chosen, t0v)
                if hfail == "bounce":
                    for ch in chosen:
                        ch["banned"] = True
                    anchor["dead"] = True
                    cursor = a_ts
                    continue
                for ch in chosen:
                    ch["used"] = True
                if hfail == "unknown":
                    raw.append(make_raw(t0v, hgaps, "composite", close,
                                        chosen=chosen))
                else:
                    raw.append(make_raw(t0v, [], "composite", close,
                                        chosen=chosen))
                if close is not None:
                    kill_round(close)
                    cursor = close
                else:
                    # 无全局复位：游标前进到本轮最早置位点；仍敞开的旧轮
                    # 已被 used，求解器只从游标之后再取（复位后再报警另起轮）
                    rtimes = [ch["reset"]["ts"] for ch in chosen
                              if ch.get("reset")
                              and not ch["reset"].get("synthetic")]
                    cursor = min(rtimes) if rtimes else min(
                        ch["set"]["ts"] for ch in chosen)
                continue

            # 未确认：仅在有缺口嫌疑或窗口仍敞开（参与设备毫无日志）时
            # 立 unknown；窗口已在覆盖期内闭合 = 偶发未凑齐，不立案，
            # 锚点判死后窗口继续滑动（同一报警不得被滑窗重复计算）。
            gap_suspect = any(g["reason"] in ("log_gap",
                                              "clock_residual_out_of_bounds")
                              for g in gs)
            uncovered = [mi for mi in range(len(leaves))
                         if mi not in clock_members
                         and leaves[mi]["device"] not in by_dev]
            if gap_suspect or (not close and uncovered):
                gaps_out = [g for g in gs
                            if g["reason"] in ("log_gap",
                                               "clock_residual_out_of_bounds")]
                gaps_out += [{"member": label(mi), "reason": "window_open"}
                             for mi in uncovered]
                raw.append(make_raw(a_ts, gaps_out, "composite", close))
            anchor["dead"] = True
            cursor = a_ts

        # ---- 全程零置位 / 手报设备不可信 的兜底实例 ----
        def ever_seen(d):
            return d in by_dev

        any_set = any(ep["set"]["ts"] > -10**17 for pool in all_pools for ep in pool)
        saw_candidate = bool(clock_members) or manual_clock or any(
            ever_seen(lf["device"]) for lf in leaves if "err" not in lf) \
            or (bool(manual_ep) and ever_seen(manual_ep["device"]))
        if not any_set and not raw:
            gs = [{"member": label(mi), "reason": "clock_residual_out_of_bounds"}
                  for mi in sorted(clock_members)]
            if not gs and not saw_candidate:
                gs = [{"member": None, "reason": "trigger_not_observed"}]
            if gs:
                raw.append(make_raw(None, gs, "composite", None))
        if manual_clock and not any(r["kind"] == "manual" for r in raw):
            raw.append(make_raw(None,
                                [{"member": f"{manual_ep['device']}:"
                                            f"{manual_ep['signal']}",
                                  "reason": "clock_residual_out_of_bounds"}],
                                "manual", None))

        raw.sort(key=lambda r: (r["trigger_ts"] is None, r["trigger_ts"] or 0))
        return normalized, raw

    # ---- 逐场景回放 ----
    scenarios = []
    for rule in p.get("matrix", []) or []:
        if "composite" in rule:
            normalized, raw = eval_composite_rule(rule)
            if raw is None:  # 结构非法（别名多解/循环/k 越界/端点非法）
                instances = [{"trigger_ts": None, "kind": "composite",
                              "status": "unknown", "members": [],
                              "gaps": normalized["root"]["invalid"],
                              "findings": []}]
            else:
                instances = []
                for r0 in raw:
                    findings = []
                    if r0["gaps"] or r0["trigger_ts"] is None:
                        st0 = "unknown"
                        for resp in rule.get("respond", []):
                            name, sig = resp["device"], resp["signal"]
                            chain = (["composite"] + list(resp.get("after", []))
                                     + [f"{name}:{sig}"])
                            findings.append(response_unknown(
                                resp, chain, "trigger_not_confirmed",
                                trigger_gaps=r0["gaps"]))
                    else:
                        for resp in rule.get("respond", []):
                            findings.append(check_resp(r0["trigger_ts"], resp,
                                                       rule, cap=r0["cap"]))
                        st0 = ("fail" if any(f["status"] == "fail"
                                             for f in findings)
                               else "unknown"
                               if any(f["status"] == "unknown" for f in findings)
                               else "pass")
                    timeouts = [f for f in findings if f.get("type") == "timeout"]
                    if timeouts:
                        min(timeouts, key=lambda f: f["deadline"])["first"] = True
                    inst = {"trigger_ts": r0["trigger_ts"], "kind": r0["kind"],
                            "status": st0, "members": r0["members"],
                            "gaps": r0["gaps"], "findings": findings}
                    instances.append(inst)
            sc_st = ("fail" if any(i["status"] == "fail" for i in instances)
                     else "unknown"
                     if any(i["status"] == "unknown" for i in instances)
                     else "pass")
            scenarios.append({"id": rule.get("id"), "status": sc_st,
                              "composite": normalized, "instances": instances})
            continue

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
                # 触发设备校时残差越界：t0 不可信，下游环节全部保持未知，
                # 不再用其时间计算 timeout / out_of_order
                findings.append({"type": "precondition_unknown", "status": "unknown",
                                 "reason": "clock_residual_out_of_bounds",
                                 "upstream": [trig["device"]]})
                for resp in rule.get("respond", []):
                    name, sig = resp["device"], resp["signal"]
                    chain = ([trig["device"]] + list(resp.get("after", []))
                             + [f"{name}:{sig}"])
                    findings.append({"type": "response",
                                     "target": f"{name}:{sig}", "upstream": chain,
                                     "status": "unknown",
                                     "reason": "trigger_clock_untrusted"})
            else:
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
                                       "reason": rerr, "member": m,
                                       "upstream": [m]})
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
                            "upstream": [ons[i][0], ons[j][0]],
                            "at": [ons[i][1], ons[j][1]]}
                    break
            if viol:
                break
        if viol:
            mutex_findings.append(viol)

    # ---- 旁路：许可窗口内且复位为合法；未复位/越窗为失败；
    #      日志缺号可能遗漏 bypass_off 时保持未知 ----
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
                    "type": None if ok else "bypass_outside_permit",
                    "upstream": [f"{d}:bypass_on", f"{d}:bypass_off"]})
                on = None
        if on:
            # 依据同一设备的 seq 先后判断：bypass_on 之后的缺号区间
            # 可能藏着 bypass_off（时间戳倒序不影响 seq 次序）
            hidden = [g for g in gaps.get(d, [])
                      if g["from_seq"] > on.get("seq", 0)]
            if hidden:  # 缺号区间可能藏着 bypass_off，不得直接判未复位
                bypass_findings.append({
                    "device": d, "on": on["ts"], "off": None,
                    "status": "unknown", "type": None,
                    "reason": "log_gap", "gap": hidden[0],
                    "upstream": [f"{d}:bypass_on"]})
            else:
                bypass_findings.append({
                    "device": d, "on": on["ts"], "off": None,
                    "status": "fail", "type": "bypass_not_reset",
                    "upstream": [f"{d}:bypass_on"]})

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
