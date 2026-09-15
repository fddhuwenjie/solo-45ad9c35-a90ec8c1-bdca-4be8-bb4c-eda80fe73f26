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
- 恢复顺序校核（recovery）：火警解除后把 报警解除 clear、值班员复位 reset、
  设备回位 steps、锁存确认 ack 绑定回原触发实例；核对排烟阀/风机/电梯/广播
  的回位次序与最长返回时间；多分区并发时公共设备须等全部占用实例解除。
  * 设备提前回位 / 恢复超时 / 前置实例仍有效 / 同一确认多轮复用 -> fail；
    解除日志缺号 / 复位来源不明 / 校时不可信 / 窗口敞开 -> unknown。
  * 规则中不含 recovery 段的旧请求演算结果完全不变。
- 响应佐证（evidence）：离散反馈（风机运行触点、风阀到位开关）可能粘连或
  误报，仅凭“信号到了”就认定动作完成会漏掉“信号到了、设备没动”。每个
  respond 可声明 evidence 段，把反馈与电流、风压、阀位等模拟量采样绑定到
  同一触发实例：
  * sources 各声明 {device, signal, window_ms, range:{min,max,unit},
    duration_ms, missing_ms}；顶层 evidence 可给 combine(all/any/k_of_n)
    与各参数的共享缺省，事件可携带 value 与 unit。
  * 采样窗口相对反馈时刻；佐证须在窗口内持续落入有效范围至少 duration_ms，
    相邻样本间隔超出 missing_ms = 采样断档；同族单位自动换算（A/mA、
    Pa/hPa/kPa/MPa/mbar/bar、%/ratio）。
  * 汇总返回采用样本与最早分歧区间：佐证越界（或持续与反馈相斥）判 fail；
    单位无法换算、采样断档、日志缺号、窗口敞开/证据不足保持 unknown。
  * 未声明 evidence 的旧 respond 演算结果完全不变；归一化佐证规则、采用
    样本与分歧区间固定进重放 JSON，改佐证规则必须附 justification 另起修订。
- 主备切换复演（failover）：排烟风机、消防泵按主备组配置，主机收到联动
  命令后跳闸，控制器须在限定时间内转启备用机；主机只是反馈迟到时过早
  切换会造成双机并行或电源过载。respond 可声明 failover 段：
  * 矩阵字段：primary（缺省为本响应）、fault（主机故障信号）、standby
    （备用启动）、switch_wait_ms（故障后须等满的切换等待）、total_ms
    （总完成时限）、parallel_ms（允许并行时长）、group（共享备用组）。
  * 分析器把命令、主机运行佐证、故障信号与备用启动绑定到同一触发实例，
    区分主机成功 primary_success、合法切换 legal_switch、误切换
    spurious_switch、双机超时并行 parallel_overrun、备用超时
    standby_timeout；多分区争用同一备用机按触发时刻独占核对占用
    （先触发先得，后到者记 standby_occupied）。
  * 故障日志缺号、时钟不可信、主机佐证未决、敞开窗口证据不足或共享备用
    组备用归属多解时保持 unknown，不臆断；合法切换覆盖主机自身超时/
    佐证相斥的 fail 判定，选择链与采用事件固定进重放 JSON。
  * 规则中不含 failover 段的旧请求演算结果完全不变；规则改动派生修订，
    旧响应保持原判定，diff 给出 failover 规则与结论差异。
- 日志缺号 / 别名多解 / 校时残差越界 / 前置状态不明 -> 相应环节保持 unknown。
- 重绑设备或改时钟锚点、改触发规则必须附 justification，系统另起修订并
  保留旧演算（重放 JSON 固定当时的归一化复合规则、组成事件与实例判定）。
- 签结（signoff）后冻结矩阵、事件与别名映射，拒绝新修订；提供修订差异与重放 JSON。

运行:  python3 app.py [--port 8073] [--db firelink.db]

请求体（创建项目 / 新修订）示例见 simulate.py。字段约定:

  devices          {设备号: {type, zone, ...}}
  aliases          {别名: 设备号 或 [设备号, ...]}   # 多解即歧义
  matrix           [{id, trigger:{device,signal},                     # 旧写法，兼容
                     respond:[{device,signal,within_ms,after:["DEV:SIG"],
                               # 响应佐证（可选；不声明则维持离散反馈原判定）:
                               evidence:{
                                 combine: "all"|"any"|"k_of_n", k: 2,
                                 window_ms, range:{min,max,unit},  # 共享缺省
                                 duration_ms, missing_ms,
                                 sources: [
                                   {device, signal, window_ms?,
                                    range:{min,max,unit},      # 同族单位可换算
                                    duration_ms?, missing_ms?},
                                   "DEV:SIG"]},
                               # 主备切换复演（可选；不声明则维持原判定）:
                               failover:{
                                 primary?: "DEV:SIG",          # 缺省为本响应
                                 fault: "DEV:SIG", standby: "DEV:SIG",
                                 switch_wait_ms, total_ms, parallel_ms,
                                 group?: "共享备用组名"}}]}]}
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
  events           [{device, seq, signal, device_ts, value?, unit?}]

"""

import json
import numbers
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


# ---------------------------------------------------------------- 单位换算
# 同族单位 -> 换算到族基准单位的乘数；跨族（A/Pa/%）不可换算。
#   电流: A / mA；风压: Pa / hPa / kPa / MPa / mbar / bar；
#   阀位: % / ratio。未登记单位只与同符号单位比较。
UNIT_FACTORS = {
    "a": ("current", 1.0), "ma": ("current", 1e-3),
    "pa": ("pressure", 1.0), "hpa": ("pressure", 1e2),
    "kpa": ("pressure", 1e3), "mpa": ("pressure", 1e6),
    "mbar": ("pressure", 1e2), "bar": ("pressure", 1e5),
    "%": ("position", 0.01), "percent": ("position", 0.01),
    "pct": ("position", 0.01), "ratio": ("position", 1.0),
}


def convert_unit(value, unit, base_unit):
    """把 value 从 unit 换算到 base_unit；跨族/未知单位不可换算 -> None。"""
    u = (unit or base_unit or "").strip().lower()
    b = (base_unit or "").strip().lower()
    if not b:
        return value if not u or UNIT_FACTORS.get(u, (None, 1))[1] == 1.0 \
            else None
    if u == b:
        return value
    fu, fb = UNIT_FACTORS.get(u), UNIT_FACTORS.get(b)
    if fu is None:
        # 未登记单位：仅与同名单位比较（视为同物），不臆断量纲
        return None
    if fu[0] != fb[0]:
        return None              # 跨族（如电流通道报 Pa）不可换算
    return value * fu[1] / fb[1]


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

    # ---------------------------------------------------- 响应佐证引擎
    # 离散反馈只证明“触点动作了”，不证明设备真动。evidence 把反馈与电流、
    # 风压、阀位等模拟量采样绑定到同一触发实例：每路佐证在反馈后的采样窗口
    # 内取值，换算到有效范围单位，须持续在范围内至少 duration_ms；采样间隔
    # 超过 missing_ms 为断档。各源结论 support/contradict/unknown，再按
    # all/any/k_of_n 汇总，返回采用样本与最早分歧区间。
    def normalize_evidence(ev):
        """归一化 evidence 段；结构非法 -> ({'invalid': [...]}, None)。"""
        if not isinstance(ev, dict):
            return {"invalid": [{"member": "evidence",
                                 "reason": "invalid_evidence"}]}, None
        bad = []

        def int_field(d, key, tag, default=None, allow_none=True):
            if d.get(key) is None:
                if default is not None or allow_none:
                    return default
                bad.append({"member": tag, "reason": "invalid_evidence"})
                return None
            try:
                v = d[key]
                if isinstance(v, bool):
                    raise ValueError
                v = int(v)
                if v < 0:
                    raise ValueError
                return v
            except (TypeError, ValueError):
                bad.append({"member": tag, "reason": "invalid_evidence",
                            "detail": {key: d.get(key)}})
                return None

        def range_of(spec, tag):
            r = spec.get("range")
            if not isinstance(r, dict):
                bad.append({"member": tag, "reason": "invalid_evidence_range"})
                return None
            mn, mx = r.get("min"), r.get("max")
            for v0 in (mn, mx):
                if v0 is not None and (
                        isinstance(v0, bool)
                        or not isinstance(v0, numbers.Real)):
                    bad.append({"member": tag,
                                "reason": "invalid_evidence_range"})
                    return None
            if mn is not None and mx is not None and mn > mx:
                bad.append({"member": tag,
                            "reason": "invalid_evidence_range"})
                return None
            return {"min": mn, "max": mx,
                    "unit": (r.get("unit") or None)}

        def_window = int_field(ev, "window_ms", "evidence:window_ms",
                               allow_none=True)
        def_duration = int_field(ev, "duration_ms", "evidence:duration_ms",
                                 default=0)
        def_missing = int_field(ev, "missing_ms", "evidence:missing_ms",
                                allow_none=True)
        def_range = range_of(ev, "evidence:range") if ev.get("range") else None

        combine = ev.get("combine", "all")
        if combine not in ("all", "any", "k_of_n"):
            bad.append({"member": "evidence:combine",
                        "reason": "invalid_evidence",
                        "detail": {"combine": combine}})
        k = None
        if combine == "k_of_n":
            kv = ev.get("k")
            if isinstance(kv, bool) or not isinstance(kv, int):
                bad.append({"member": "evidence:k",
                            "reason": "invalid_evidence",
                            "detail": {"k": kv}})
            else:
                k = kv
            # k 范围要等源数确定后再核

        raw_sources = ev.get("sources")
        if not isinstance(raw_sources, list) or not raw_sources:
            bad.append({"member": "evidence:sources",
                        "reason": "invalid_evidence"})
            raw_sources = []
        sources = []
        tokens = set()
        for i, s0 in enumerate(raw_sources):
            if isinstance(s0, str):
                ds = s0.split(":", 1)
                if len(ds) != 2:
                    bad.append({"member": f"evidence:sources[{i}]",
                                "reason": "invalid_evidence"})
                    continue
                s0 = {"device": ds[0], "signal": ds[1]}
            if not isinstance(s0, dict) or not s0.get("signal") \
                    or not s0.get("device"):
                bad.append({"member": f"evidence:sources[{i}]",
                            "reason": "invalid_evidence"})
                continue
            tag = f"evidence:sources[{i}]"
            win = int_field(s0, "window_ms", f"{tag}:window_ms",
                            default=def_window, allow_none=True)
            dur = int_field(s0, "duration_ms", f"{tag}:duration_ms",
                            default=def_duration)
            mis = int_field(s0, "missing_ms", f"{tag}:missing_ms",
                            default=def_missing, allow_none=True)
            rng = range_of(s0, f"{tag}:range") if s0.get("range") else def_range
            if rng is None:
                bad.append({"member": tag, "reason": "invalid_evidence_range"})
                continue
            if win is None:
                bad.append({"member": tag, "reason": "invalid_evidence"})
                continue
            tok = f"{s0.get('device')}:{s0['signal']}"
            if tok in tokens:
                bad.append({"member": tag, "reason": "duplicate_evidence_source",
                            "detail": tok})
                continue
            tokens.add(tok)
            sources.append({"spec_device": s0.get("device"),
                            "signal": s0["signal"], "window_ms": win,
                            "duration_ms": dur, "missing_ms": mis,
                            "range": rng})
        if combine == "k_of_n":
            if not bad and not (isinstance(k, int)
                                and 1 <= k <= len(sources)):
                bad.append({"member": "evidence:k",
                            "reason": "k_out_of_range",
                            "detail": {"k": k, "n": len(sources)}})
        if bad:
            return {"invalid": bad}, None

        out_sources = []
        for s in sources:
            d, err = resolve(s["spec_device"])
            if err:
                bad.append({"member": f"{s['spec_device']}:{s['signal']}",
                            "reason": err})
                continue
            out_sources.append({**s, "device": d})
        if bad:
            return {"invalid": bad}, None
        return {"combine": combine, "k": k, "sources": out_sources}, True

    def eval_evidence(norm, fb_ts, cap):
        """反馈事件于 fb_ts 成立后，评估绑定本触发实例的佐证组合。
        返回 {status, combine, k, sources:[...], samples:[...]}；
        status ∈ support / contradict / unknown。"""
        def eval_source(s):
            dev, sig = s["device"], s["signal"]
            tag = f"{dev}:{sig}"
            ent = {"source": tag, "device": dev, "signal": sig,
                   "window_ms": s["window_ms"], "duration_ms": s["duration_ms"],
                   "missing_ms": s["missing_ms"], "range": s["range"],
                   "status": "unknown", "reason": None,
                   "adopted_samples": [], "divergence": None}
            d, err = resolve(dev)
            if err:
                ent["reason"] = err
                return ent
            if d in untrusted:
                ent["reason"] = "clock_residual_out_of_bounds"
                return ent
            win_close = fb_ts + s["window_ms"]
            hi = win_close
            window_open = True
            if cap is not None and fb_ts < cap < hi:
                hi = cap            # 全局复位截断佐证窗口，不得跨轮取证
                window_open = False
            g = gap_between(d, fb_ts, hi)
            if g:
                ent["reason"] = "log_gap"
                ent["gap"] = g
                return ent
            stream = sorted(by_dev.get(d, []),
                            key=lambda e: (e["ts"], e.get("seq") or 0))
            same = [e for e in stream if e["signal"] == sig]
            inwin = [e for e in same if fb_ts <= e["ts"] <= hi]
            base_unit = s["range"]["unit"]
            samples = []
            convert_fail = None
            for e in inwin:
                v = e.get("value")
                if isinstance(v, bool) or not isinstance(v, numbers.Real):
                    convert_fail = "evidence_value_not_numeric"
                    break
                cv = convert_unit(float(v), e.get("unit"), base_unit)
                if cv is None:
                    convert_fail = "unit_not_convertible"
                    break
                mn, mx = s["range"]["min"], s["range"]["max"]
                inside = (mn is None or cv >= mn) and (mx is None or cv <= mx)
                samples.append({"device": dev, "signal": sig,
                                "seq": e.get("seq"), "ts": e["ts"],
                                "value": v, "unit": e.get("unit"),
                                "converted_value": cv,
                                "range_unit": base_unit, "in_range": inside})
            # 单位无法换算 / 无数值：无法判定，保持 unknown（优先于其他嫌疑）
            if convert_fail:
                ent["reason"] = convert_fail
                ent["adopted_samples"] = samples
                return ent

            missing = s["missing_ms"]
            duration = s["duration_ms"]

            def too_long(a, b):
                return missing is not None and b - a > missing

            # ---- 缺测定位（均不得越过实例边界 cap）----
            # 起点空段：反馈到首样本间隔超容限；
            # 内部断档：相邻样本间隔超容限；
            # 末端断档：末样本到其后下一条同信号记录（不晚于 cap），或闭合
            #           窗口末端，间隔超容限。敞开窗口末端无记录不算断档。
            lead_seg = None
            if inwin and too_long(fb_ts, inwin[0]["ts"]):
                lead_seg = [fb_ts, inwin[0]["ts"]]
            internal_seg = None
            for a, b in zip(inwin, inwin[1:]):
                if too_long(a["ts"], b["ts"]):
                    internal_seg = [a["ts"], b["ts"]]
                    break
            tail_seg = None
            if inwin:
                last = inwin[-1]
                if cap is not None:
                    nxt = next((e for e in same
                                if last["ts"] < e["ts"] <= cap), None)
                    end_ref = nxt["ts"] if nxt is not None else hi
                else:
                    nxt = next((e for e in same
                                if e["ts"] > last["ts"]), None)
                    end_ref = nxt["ts"] if nxt is not None else None
                if end_ref is not None and too_long(last["ts"], end_ref):
                    tail_seg = [last["ts"], end_ref]
            elif too_long(fb_ts, hi):
                nxt = next((e for e in same if fb_ts < e["ts"] <= hi), None) \
                    if cap is not None else None
                if nxt is not None or cap is not None:
                    tail_seg = [fb_ts, nxt["ts"] if nxt is not None else hi]

            # ---- 缺测优先：起点空段、内部断档、末端断档任一成立即 unknown，
            #      不得先按越界/在范围下结论。缺口里可能藏反证：单个越界
            #      样本不能借末端插值满足 duration；越界段虽观测满 duration，
            #      但其后到下一条记录的间隔超容限同样不能先判 fail。----
            gap_seg = internal_seg or lead_seg or tail_seg
            if gap_seg is not None:
                ent["adopted_samples"] = samples
                ent["reason"] = "evidence_sample_gap"
                ent["gap_segment_ms"] = gap_seg
                return ent

            # ---- 无断档：按缺测容限把窗口内样本切成连续观测段；越界/在
            #      范围状态只能在段内沿用，跨断档不得外推。段内连续同态样本
            #      划分为 run，run 的可证覆盖时长算到：下一个异态样本（异态
            #      前一直同态）、或闭合窗口末端（能走到这里说明末端空段未超
            #      容限，允许插值）；敞开窗口只认已观测时长，绝不外推。----
            segments, cur = [], []
            for sm in samples:
                if cur and too_long(cur[-1]["ts"], sm["ts"]):
                    segments.append(cur)
                    cur = []
                cur.append(sm)
            if cur:
                segments.append(cur)

            def runs_of(seg, want):
                out, run = [], None
                for sm in seg:
                    if sm["in_range"] == want:
                        if run is None:
                            run = {"start": sm["ts"], "end": sm["ts"],
                                   "samples": [sm]}
                        else:
                            run["end"] = sm["ts"]
                            run["samples"].append(sm)
                    elif run is not None:
                        out.append(run)
                        run = None
                if run is not None:
                    out.append(run)
                return out

            def covered_span(seg, run):
                """run 可证覆盖：到段内异态样本，或闭合窗口末端（缺测已在
                上面拦过，此处插值安全）；敞开窗口只认已观测时长。"""
                flip = next((sm["ts"] for sm in seg
                             if sm["ts"] > run["end"]
                             and sm["in_range"] != run["samples"][0]["in_range"]),
                            None)
                if flip is not None:
                    return flip - run["start"], flip
                if not window_open:
                    return hi - run["start"], hi
                return run["end"] - run["start"], run["end"]

            contradiction = None
            support_run = None
            for seg in segments:
                for run in runs_of(seg, False):
                    span, end_bound = covered_span(seg, run)
                    if span >= duration and contradiction is None:
                        contradiction = (run, end_bound)
                if support_run is None:
                    for run in runs_of(seg, True):
                        span, _ = covered_span(seg, run)
                        if span >= duration:
                            support_run = run
                            break

            ent["adopted_samples"] = samples
            # 相斥只需一段已观测（或闭合窗口容限内）越界覆盖达到 duration，
            # 不依赖任何跨断档或向敞开末端的外推。
            if contradiction is not None:
                run, end_bound = contradiction
                ent["status"] = "contradict"
                ent["reason"] = "evidence_out_of_range"
                ent["divergence"] = {
                    "from_ts": run["start"], "to_ts": end_bound,
                    "range": s["range"], "samples": run["samples"]}
                return ent
            # 已观测在范围覆盖足够 -> 支持（同样不跨断档、不外推）
            if support_run is not None:
                ent["status"] = "support"
                return ent
            # 无缺证可指但覆盖不够：闭合窗口证据不足；敞开窗口后续可补样本
            ent["reason"] = ("evidence_window_open" if window_open
                             else "evidence_insufficient")
            return ent

        src_out = [eval_source(s) for s in norm["sources"]]
        supported = [s for s in src_out if s["status"] == "support"]
        contrad = [s for s in src_out if s["status"] == "contradict"]
        combine, k = norm["combine"], norm["k"]
        need = len(src_out) if combine == "all" else 1 if combine == "any" else k
        if contrad:
            status = "contradict"
        elif len(supported) >= need:
            status = "support"
        else:
            status = "unknown"
        # 最早分歧区间：相斥源中起始时刻最早者
        divs = [s["divergence"] for s in contrad if s.get("divergence")]
        divergence = min(divs, key=lambda d: d["from_ts"]) if divs else None
        samples_all = [sm for s in src_out for sm in s["adopted_samples"]]
        samples_all.sort(key=lambda x: (x["ts"], x["device"]))
        return {"status": status, "combine": combine, "k": k,
                "sources": src_out, "samples": samples_all,
                "divergence": divergence}

    _evidence_cache = {}

    def evidence_for(resp):
        """归一化（并缓存）某 respond 的佐证规则；未声明 -> None。"""
        if "evidence" not in resp:
            return None
        key = id(resp)
        if key not in _evidence_cache:
            _evidence_cache[key] = normalize_evidence(resp["evidence"])
        return _evidence_cache[key]

    def check_resp(t0, resp, rule, cap=None):
        name, sig = resp["device"], resp["signal"]
        head = (rule.get("trigger") or {}).get("device", "composite")
        chain = [head] + list(resp.get("after", [])) + [f"{name}:{sig}"]
        base = {"type": "response", "target": f"{name}:{sig}", "upstream": chain}
        ev_spec = evidence_for(resp)
        if ev_spec is not None and ev_spec[1] is None:
            return {**base, "status": "unknown",
                    "reason": "invalid_evidence",
                    "evidence": {"rule": ev_spec[0], "status": "unknown",
                                 "sources": [], "samples": []}}
        d, err = resolve(name)
        if err:
            return {**base, "status": "unknown", "reason": err}
        within = resp.get("within_ms")
        hi = t0 + within if within is not None else None
        if cap is not None and (hi is None or hi > cap):
            # 即使未声明时限，全局复位即关闭本轮：复位后的晚到反馈属于
            # 下一轮，不得再挂到旧实例（无 within_ms 时同样按 cap 截断）。
            hi = cap
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
        out = {**base, "status": "ok", "actual": ev["ts"],
               "elapsed_ms": ev["ts"] - t0}
        if ev_spec is not None:
            norm = ev_spec[0]
            er = eval_evidence(norm, ev["ts"], cap)
            block = {"status": er["status"], "rule": norm,
                     "combine": er["combine"],
                     "sources": er["sources"], "samples": er["samples"]}
            if er.get("divergence"):
                block["divergence"] = er["divergence"]
            out["evidence"] = block
            if er["status"] == "contradict":
                out.update(status="fail",
                           evidence_type="evidence_contradiction",
                           reason="evidence_out_of_range")
            elif er["status"] == "unknown":
                out.update(status="unknown", reason="evidence_inconclusive")
        return out

    # ---------------------------------------------------- 主备切换规则归一化
    # 排烟风机/消防泵按主备组配置：respond 可声明 failover 段，把主机命令
    # (primary，缺省为本响应)、主机故障信号 fault、备用启动 standby、切换
    # 等待 switch_wait_ms、总完成时限 total_ms、允许并行时长 parallel_ms
    # 与共享备用组 group 绑定到同一触发实例复演。规则改动派生修订，旧响应
    # 与旧演算保持原判定。
    _failover_cache = {}

    def parse_fo_endpoint(spec, tag, bad):
        if isinstance(spec, str):
            ds = spec.split(":", 1)
            if len(ds) != 2:
                bad.append({"member": tag, "reason": "invalid_failover"})
                return None
            spec = {"device": ds[0], "signal": ds[1]}
        if not isinstance(spec, dict) or not spec.get("device") \
                or not spec.get("signal"):
            bad.append({"member": tag, "reason": "invalid_failover"})
            return None
        d, err = resolve(spec["device"])
        if err:
            bad.append({"member": f"{tag}:{spec['device']}", "reason": err})
            return None
        return {"device": d, "signal": spec["signal"]}

    def normalize_failover(resp):
        """归一化 respond.failover；结构非法 -> ({'invalid': [...]}, None)。"""
        if "failover" not in resp:
            return None
        key = id(resp)
        if key in _failover_cache:
            return _failover_cache[key]

        def result(norm, valid):
            _failover_cache[key] = (norm, valid)
            return _failover_cache[key]

        fo = resp["failover"]
        if not isinstance(fo, dict):
            return result({"invalid": [{"member": "failover",
                                        "reason": "invalid_failover"}]}, None)
        bad = []
        ep_fault = parse_fo_endpoint(fo.get("fault"),
                                     "failover:fault", bad)
        ep_stand = parse_fo_endpoint(fo.get("standby"),
                                     "failover:standby", bad)
        td, terr = resolve(resp.get("device"))
        if terr:
            bad.append({"member": "failover:primary", "reason": terr})
            target_tok = f"{resp.get('device')}:{resp.get('signal')}"
        else:
            target_tok = f"{td}:{resp['signal']}"
        ep_primary = {"device": td, "signal": resp["signal"]} \
            if not terr else None
        if fo.get("primary") is not None:
            # 先按原始声明核对主机是否就是本响应（不依赖别名解析），
            # 解析失败另记别名错误，避免错误被 alias 问题吞掉
            spec_p = fo.get("primary")
            if isinstance(spec_p, str):
                raw_dev, raw_sig = (spec_p.split(":", 1) + [None])[:2] \
                    if ":" in spec_p else (spec_p, None)
                raw_tok = spec_p
            elif isinstance(spec_p, dict):
                raw_dev, raw_sig = spec_p.get("device"), spec_p.get("signal")
                raw_tok = f"{raw_dev}:{raw_sig}"
            else:
                raw_dev = raw_sig = raw_tok = None
            resp_tok = f"{resp.get('device')}:{resp.get('signal')}"
            if not isinstance(spec_p, (str, dict)) or not raw_dev or not raw_sig:
                bad.append({"member": "failover:primary",
                            "reason": "invalid_failover"})
            elif raw_tok != resp_tok:
                d0, _ = resolve(raw_dev)
                sig0 = raw_sig
                resolved = f"{d0}:{sig0}" if d0 else raw_tok
                if resolved != target_tok:
                    bad.append({"member": "failover:primary",
                                "reason": "failover_primary_mismatch",
                                "detail": raw_tok})
            ep_explicit = parse_fo_endpoint(fo.get("primary"),
                                            "failover:primary", bad)
            if ep_explicit is not None:
                if f"{ep_explicit['device']}:{ep_explicit['signal']}" \
                        == target_tok:
                    ep_primary = ep_explicit

        def ms_field(name):
            v = fo.get(name)
            if isinstance(v, bool) or not isinstance(v, numbers.Real) \
                    or int(v) < 0:
                bad.append({"member": f"failover:{name}",
                            "reason": "invalid_failover",
                            "detail": {name: v}})
                return None
            return int(v)

        wait = ms_field("switch_wait_ms")
        total = ms_field("total_ms")
        parallel = ms_field("parallel_ms")
        group = fo.get("group")
        if group is not None and (not isinstance(group, str) or not group):
            bad.append({"member": "failover:group",
                        "reason": "invalid_failover"})
            group = None
        if bad:
            dedup, seen = [], set()
            for b in bad:
                k = (b.get("member"), b.get("reason"))
                if k not in seen:
                    seen.add(k)
                    dedup.append(b)
            return result({"invalid": dedup}, None)
        return result({
            "primary": f"{ep_primary['device']}:{ep_primary['signal']}",
            "fault": f"{ep_fault['device']}:{ep_fault['signal']}",
            "standby": f"{ep_stand['device']}:{ep_stand['signal']}",
            "switch_wait_ms": wait, "total_ms": total,
            "parallel_ms": parallel, "group": group}, True)

    def failover_plan(rule, group_invalid):
        """固定本规则各 respond 的归一化主备规则进重放 JSON。
        group_invalid: 共享备用组跨规则备用归属多解时该组的非法说明。"""
        out = []
        for resp in rule.get("respond", []) or []:
            if "failover" not in resp:
                continue
            norm, valid = normalize_failover(resp)
            if valid and norm.get("group") in group_invalid:
                norm = {"invalid": group_invalid[norm["group"]]}
                valid = False
            target = (norm.get("primary") if valid
                      else f"{resp.get('device')}:{resp.get('signal')}")
            out.append({"target": target, "valid": bool(valid),
                        "rule": norm})
        return out

    def failover_group_conflicts(rules):
        """同一共享备用组在不同规则里指向不同备用端点 -> 备用归属多解。"""
        groups = {}
        for rule in rules:
            for resp in rule.get("respond", []) or []:
                nf = normalize_failover(resp)
                if not nf or not nf[1] or not nf[0].get("group"):
                    continue
                groups.setdefault(nf[0]["group"], set()).add(
                    nf[0]["standby"])
        return {g: [{"member": f"failover:group:{g}",
                     "reason": "standby_group_ambiguous",
                     "detail": sorted(toks)}]
                for g, toks in groups.items() if len(toks) > 1}

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

            def round_start_before(ts):
                """ts 之前最近一次全局复位时刻（本轮起点），无则负无穷。"""
                last = -(10**18)
                for r in reset_ts:
                    if r < ts:
                        last = r
                    else:
                        break
                return last

            def prior_gap_for(e):
                """本轮起点之后、本次置位之前的 seq 缺口：可能藏更早的
                置位/复位 -> 该轮 unknown。起点（复位）之前的旧缺口不算。"""
                lo = round_start_before(e["ts"])
                for g in gaps.get(dev, []):
                    if g["to_seq"] < e["seq"] and g["to_ts"] > lo:
                        return g
                return None

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
                        pg = prior_gap_for(e)
                        if pg is not None:
                            # 本轮起点后的 seq 缺口可能藏更早置位/复位
                            on["gap"] = pg
                            on["stable"] = False
                    else:
                        # 仍处置位（既无全局复位也无本机复位）就重复收到置位：
                        # 触点抖动/重复上报，并入同一轮，不得另算一次火警。
                        on["repeats"] += 1
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
                # 本轮起点之后、置位之后的缺口才可能藏本轮复位/置位；
                # 复位之前的旧缺口不得算到本轮头上。
                lo = round_start_before(on["set"]["ts"])
                tg = next((g for g in gaps.get(dev, [])
                           if g["from_seq"] > on["set"]["seq"]
                           and g["from_ts"] >= on["set"]["ts"]
                           and g["from_ts"] > lo), None)
                if tg:
                    on["gap"] = tg
                else:
                    # 保持证据只认本机参与信号事件（面板复位不算）：
                    # 后续同信号置位（含重复上报）或本机复位覆盖到保持
                    # 时长之外 => 置位坐实稳定；否则保持证据不完整
                    # （窗口敞开），保留 unknown 嫌疑。
                    sigs = {sig}
                    if local_reset_sig:
                        sigs.add(local_reset_sig)
                    tail_to = max((x["ts"] for x in stream
                                   if x["seq"] >= on["set"]["seq"]
                                   and x["signal"] in sigs), default=None)
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
                    # 的旧置位被滑窗再次确认；已复位轮次从本轮起点起可取，
                    # 锚点（恰在 lo 的置位）也算本轮候选。
                    def fresh(ep):
                        if ep["set"]["ts"] < lo:
                            return False
                        if ep["reset"] is None \
                                and ep["set"]["ts"] <= after_ts:
                            return False
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
        reported_gaps = set()   # 同一 seq 缺口在滑动窗口中只立一次 unknown

        def gap_key(g):
            gp = g.get("gap") or {}
            return (g.get("member"), gp.get("from_seq"), gp.get("to_seq"))

        def take_gaps(gs):
            """登记并返回尚未上报的缺口；同一缺口不重复立案。"""
            fresh = []
            for g in gs:
                if g.get("reason") == "log_gap":
                    k = gap_key(g)
                    if k in reported_gaps:
                        continue
                    reported_gaps.add(k)
                fresh.append(g)
            return fresh

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

        def hold_failure(chosen, t0v):
            """确认后复核保持时长：某路在 t0+hold 前复位（且无缺口嫌疑）
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

        def round_gap_suspects(a_ts, hi):
            """收集本轮相关的成员缺口：缺口在游标之后、且可能影响本轮确认
            （缺口与 [a_ts, hi] 重叠、位于本轮置位之前、或夹在本轮置位与
            确认点之间），尚未上报的逐项列出。"""
            out = []
            for pool in all_pools:
                for ep in pool:
                    if not ep.get("gap") or ep.get("gap_reported"):
                        continue
                    if ep["used"] or ep["banned"]:
                        continue
                    g = ep["gap"]
                    # 缺口在游标之后，且其时间区间不晚于本轮确认上界：
                    # 缺口里可能藏着更早置位/复位或窗口内的置位 -> 相关
                    relevant = (g["to_ts"] > cursor
                                and g["from_ts"] <= hi
                                and not (ep["used"] or ep["dead"]))
                    if not relevant:
                        continue
                    who = (f"{manual_ep['device']}:{manual_ep['signal']}"
                           if ep["manual"] else label(ep["mi"]))
                    out.append({"member": who, "reason": "log_gap", "gap": g})
                    ep["gap_reported"] = True
            return take_gaps(out)

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
                gapped = [ep for pool in all_pools for ep in pool
                          if ep.get("gap") and not (ep["used"] or ep["banned"])
                          and not ep.get("gap_reported")
                          and ep["set"]["ts"] > cursor]
                gs0 = []
                for ep in gapped:
                    who = (f"{manual_ep['device']}:{manual_ep['signal']}"
                           if ep["manual"] else label(ep["mi"]))
                    gs0 += take_gaps([{"member": who, "reason": "log_gap",
                                       "gap": ep["gap"]}])
                    ep["gap_reported"] = True
                if gs0:
                    raw.append(make_raw(ep0["set"]["ts"], gs0,
                                        "manual" if ep0["manual"] else "composite",
                                        next_reset_after(ep0["set"]["ts"])))
                    # 缺口轮保留（不判死），仅推进游标，避免滑窗重复立案；
                    # 其后若仍有置位可继续形成新轮。
                    cursor = ep0["set"]["ts"]
                    continue
                close0 = next_reset_after(ep0["set"]["ts"])
                if close0 is None:
                    who = (f"{manual_ep['device']}:{manual_ep['signal']}"
                           if ep0["manual"] else label(ep0["mi"]))
                    raw.append(make_raw(
                        ep0["set"]["ts"],
                        [{"member": who, "reason": "hold_evidence_incomplete"}],
                        "manual" if ep0["manual"] else "composite", close0))
                # 无缺口的非稳定置位（保持不足/偶发）：判死后窗口继续滑动
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
                    mgaps = take_gaps([{"member": f"{manual_ep['device']}:"
                                                   f"{manual_ep['signal']}",
                                        "reason": "log_gap",
                                        "gap": m_ep["gap"]}])
                    if mgaps:
                        raw.append(make_raw(m_ep["set"]["ts"], mgaps,
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
            round_gaps = round_gap_suspects(a_ts, hi)
            if sat and round_gaps:
                # 组合虽凑齐，但参与设备本轮相关日志有缺口（可能藏更早/
                # 复位事件）-> 该实例保持 unknown
                raw.append(make_raw(t0v, round_gaps, "composite", close))
                for ch in chosen:
                    ch["used"] = True
                if close is not None:
                    # 本轮（复位之前）其余置位一并封存，复位后另起一轮
                    kill_round(close)
                    cursor = close
                else:
                    cursor = t0v
                continue
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
                    hgaps = take_gaps(hgaps)
                    if hgaps:
                        raw.append(make_raw(t0v, hgaps, "composite", close,
                                            chosen=chosen))
                    else:
                        # 缺口已在更早窗口立案：本轮不再重复，置位判死
                        for ch in chosen:
                            ch["used"] = True
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

            # 未确认：仅在有缺口嫌疑/校时不可信/窗口仍敞开时立 unknown；
            # 窗口已在覆盖期内闭合 = 偶发未凑齐，不立案，锚点判死后窗口
            # 继续滑动（同一报警不得被滑窗重复计算）。
            gap_suspect = any(g["reason"] in ("log_gap",
                                              "clock_residual_out_of_bounds")
                              for g in gs)
            uncovered = [mi for mi in range(len(leaves))
                         if mi not in clock_members
                         and leaves[mi]["device"] not in by_dev]
            if round_gaps or gap_suspect or (not close and uncovered):
                gaps_out = list(round_gaps)
                gaps_out += take_gaps([g for g in gs
                                       if g["reason"] == "log_gap"])
                gaps_out += [g for g in gs
                             if g["reason"] == "clock_residual_out_of_bounds"]
                gaps_out += [{"member": label(mi), "reason": "window_open"}
                             for mi in uncovered]
                gaps_out = dedup_gaps(gaps_out)
                if gaps_out:
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
    def evidence_plan(rule):
        """固定本规则各 respond 的归一化佐证规则进重放 JSON。"""
        out = []
        for resp in rule.get("respond", []) or []:
            if "evidence" not in resp:
                continue
            norm, valid = evidence_for(resp)
            out.append({"target": f"{resp['device']}:{resp['signal']}",
                        "valid": bool(valid), "rule": norm})
        return out

    scenarios = []
    failover_group_invalid = failover_group_conflicts(
        p.get("matrix", []) or [])
    for rule in p.get("matrix", []) or []:
        plan = evidence_plan(rule)
        fo_plan = failover_plan(rule, failover_group_invalid)
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
                    if fo_plan:
                        inst["cap"] = r0["cap"]
                    instances.append(inst)
            sc_st = ("fail" if any(i["status"] == "fail" for i in instances)
                     else "unknown"
                     if any(i["status"] == "unknown" for i in instances)
                     else "pass")
            sc_obj = {"id": rule.get("id"), "status": sc_st,
                      "composite": normalized, "instances": instances}
            if plan:
                sc_obj["evidence_rules"] = plan
            if fo_plan:
                sc_obj["failover_rules"] = fo_plan
            scenarios.append(sc_obj)
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
        sc_obj = {"id": rule.get("id"), "status": sc_st, "instances": instances}
        if plan:
            sc_obj["evidence_rules"] = plan
        if fo_plan:
            sc_obj["failover_rules"] = fo_plan
        scenarios.append(sc_obj)


    # ---- 恢复顺序校核（火警解除 -> 人工复位 -> 设备回位 -> 锁存确认）----
    # 仅对声明了 recovery 段的规则生效；未声明 recovery 的旧请求演算结果不变。
    # 每个触发实例独立绑定一条恢复链：解除条件 clear、复位来源 reset、设备
    # 回位 steps、最长返回时间 max_return_ms 与锁存确认 ack 都挂回原实例；
    # 多分区并发时公共设备要等占用它的全部实例解除后方可回位。
    matrix_rules = p.get("matrix", []) or []

    def ev_ref(ev):
        if ev is None:
            return None
        ref = {"device": ev["device"], "seq": ev.get("seq"),
               "signal": ev["signal"], "ts": ev["ts"]}
        if ev.get("value") is not None:
            ref["value"] = ev.get("value")
        return ref

    def find_events(dev, sig, lo=None, hi=None, before_next=None):
        """窗口内同信号事件；before_next 为下一轮触发时刻（恢复窗口上界）。"""
        out = []
        for e in by_dev.get(dev, []):
            if e["signal"] != sig:
                continue
            if lo is not None and e["ts"] < lo:
                continue
            if hi is not None and e["ts"] > hi:
                continue
            if before_next is not None and e["ts"] >= before_next:
                continue
            out.append(e)
        return sorted(out, key=lambda e: (e["ts"], e["seq"]))

    def gap_overlaps(dev, lo, hi=None, before_next=None):
        upper = before_next if before_next is not None else hi
        for g in gaps.get(dev, []):
            if g["to_ts"] < lo:
                continue
            if upper is not None and g["from_ts"] > upper:
                continue
            return g
        return None

    def seq_gap_between(dev, from_seq, to_seq):
        """按 seq 次序：from_seq 与 to_seq 之间存在缺号即返回缺口。
        解除/复位阶段的时间戳可能晚于下一轮触发，但绑定仍按本设备 seq
        先后判断（时间戳倒序不影响 seq 次序）。"""
        if from_seq is None or to_seq is None:
            return None
        return next((g for g in gaps.get(dev, [])
                     if g["from_seq"] > from_seq and g["to_seq"] < to_seq),
                    None)

    def seq_gap_after(dev, from_seq, after_next=False):
        """from_seq 之后是否存在缺号（可能藏解除/复位/回位事件）。"""
        if from_seq is None:
            return None
        return next((g for g in gaps.get(dev, [])
                     if g["from_seq"] > from_seq), None)

    def ep_value_ok(ep, ev):
        """复位来源/解除来源可限定 value（按键号/来源码核对），不限定即全收。"""
        return ep.get("value") is None or ev.get("value") == ep.get("value")

    def parse_ep(spec, tag):
        if isinstance(spec, str):
            ds = spec.split(":", 1)
            if len(ds) != 2:
                return None, [{"member": f"{tag}:{spec}",
                               "reason": "invalid_recovery_endpoint"}]
            spec = {"device": ds[0], "signal": ds[1]}
        if not isinstance(spec, dict) or not spec.get("signal") \
                or not spec.get("device"):
            return None, [{"member": tag,
                           "reason": "invalid_recovery_endpoint"}]
        d, err = resolve(spec.get("device"))
        if err:
            return None, [{"member": f"{tag}:{spec['device']}", "reason": err}]
        return {"device": d, "signal": spec["signal"],
                "value": spec.get("value")}, None

    def parse_recovery(rule):
        """归一化 recovery 段；结构非法 -> ({'invalid': [...]}, None)。"""
        rec = rule.get("recovery")
        if not rec:
            return None, None
        bad = []

        def parse_eps(spec, tag):
            items = spec if isinstance(spec, list) else [spec]
            if not items:
                bad.append({"member": tag,
                            "reason": "invalid_recovery_endpoint"})
                return []
            eps = []
            for i, c in enumerate(items):
                ep, ge = parse_ep(c, f"{tag}[{i}]")
                if ge:
                    bad += ge
                else:
                    eps.append(ep)
            return eps

        clear_eps = parse_eps(rec.get("clear"), "clear") \
            if rec.get("clear") is not None else []
        if rec.get("clear") is None:
            bad.append({"member": "clear",
                        "reason": "invalid_recovery_endpoint"})
        reset_ep, ge = parse_ep(rec["reset"], "reset") if rec.get("reset") \
            else (None, [{"member": "reset",
                          "reason": "invalid_recovery_endpoint"}])
        if ge:
            bad += ge
        ack_ep = None
        if rec.get("ack"):
            ack_ep, ae = parse_ep(rec["ack"], "ack")
            if ae:
                bad += ae
        try:
            mr = rec.get("max_return_ms")
            max_return = int(mr) if mr is not None else None
            if max_return is not None and max_return < 0:
                raise ValueError
        except (TypeError, ValueError):
            bad.append({"member": "max_return_ms",
                        "reason": "invalid_recovery_endpoint"})
            max_return = None
        steps, tokens = [], set()
        for st0 in rec.get("steps", []) or []:
            if not isinstance(st0, dict) or not st0.get("device") \
                    or not st0.get("signal"):
                bad.append({"member": "steps",
                            "reason": "invalid_recovery_endpoint"})
                continue
            ep, ge = parse_ep(st0, "steps")
            if ge:
                bad += ge
                continue
            tok = f"{ep['device']}:{ep['signal']}"
            if tok in tokens:
                bad.append({"member": f"steps:{tok}",
                            "reason": "duplicate_recovery_step"})
                continue
            tokens.add(tok)
            try:
                w0 = st0.get("within_ms")
                within = int(w0) if w0 is not None else None
                if within is not None and within < 0:
                    raise ValueError
            except (TypeError, ValueError):
                bad.append({"member": f"steps:{tok}",
                            "reason": "invalid_recovery_endpoint",
                            "detail": {"within_ms": w0}})
                within = None
            after = []
            for dep in st0.get("after", []) or []:
                if not isinstance(dep, str) or ":" not in dep:
                    bad.append({"member": f"steps:{tok}",
                                "reason": "invalid_recovery_after",
                                "detail": dep})
                    continue
                dd = dep.split(":", 1)[0]
                rd, rerr = resolve(dd)
                if rerr:
                    bad.append({"member": f"steps:{tok}",
                                "reason": f"predecessor_{rerr}",
                                "detail": dep})
                elif rd in untrusted:
                    bad.append({"member": f"steps:{tok}",
                                "reason": "predecessor_clock_untrusted",
                                "detail": dep})
                else:
                    after.append(dep)
            steps.append({"device": ep["device"], "signal": ep["signal"],
                          "token": tok, "within_ms": within, "after": after,
                          "shared": bool(st0.get("shared"))})
        if not steps:
            bad.append({"member": "steps",
                        "reason": "invalid_recovery_endpoint"})
        # after 前置必须指向已声明回位步（同规则内），且不得成环
        for st1 in steps:
            for dep in st1["after"]:
                if not any(s["token"] == dep for s in steps):
                    bad.append({"member": f"steps:{st1['token']}",
                                "reason": "predecessor_not_a_step",
                                "detail": dep})
        graph = {s["token"]: list(s["after"]) for s in steps}
        for start in graph:
            stack, seen = [(start, [])], set()
            while stack:
                node, path = stack.pop()
                if node == start and path:
                    bad.append({"member": f"steps:{start}",
                                "reason": "recovery_after_cycle"})
                    break
                if node in seen:
                    continue
                seen.add(node)
                for nx in graph.get(node, []):
                    stack.append((nx, path + [node]))
        shared_devices = set()
        for sd0 in rec.get("shared_devices", []) or []:
            d, err = resolve(sd0)
            if err:
                bad.append({"member": f"shared:{sd0}", "reason": err})
            else:
                shared_devices.add(d)
        if bad:
            return {"invalid": bad}, None
        latch = bool(rec.get("latch", True)) if ack_ep else \
            bool(rec.get("latch", False))
        return {"clear": clear_eps, "reset": reset_ep, "ack": ack_ep,
                "max_return_ms": max_return, "steps": steps,
                "shared_devices": sorted(shared_devices),
                "latch": latch}, True

    rec_plan = [parse_recovery(rule) for rule in matrix_rules]

    def trigger_dev_of(rule):
        """定位实例原始触发事件用：旧单触发的设备/信号。"""
        tr = rule.get("trigger")
        if not tr:
            return None, None
        d, _ = resolve(tr.get("device"))
        return d, tr.get("signal")

    rule_trigger = [trigger_dev_of(rule) for rule in matrix_rules]

    # 公共设备：显式声明，或被两条及以上规则联动（respond）的设备。
    activated_devs = []
    for rule in matrix_rules:
        ds = set()
        for resp in rule.get("respond", []) or []:
            d, _ = resolve(resp.get("device"))
            if d:
                ds.add(d)
        activated_devs.append(ds)
    shared_global = set()
    for spec, valid in rec_plan:
        if valid:
            shared_global.update(spec["shared_devices"])
    for d in {d for ds in activated_devs for d in ds}:
        if sum(d in ds for ds in activated_devs) >= 2:
            shared_global.add(d)

    def anchor_event(idx, inst):
        """实例的原始定位事件：复合用组成叶子，旧触发用原始报警事件。"""
        for m in inst.get("members", []) or []:
            if m.get("seq") is not None:
                return {"device": m["device"], "seq": m["seq"],
                        "signal": m["signal"], "ts": m["set_ts"]}
        td, tsig = rule_trigger[idx]
        if td and tsig is not None and inst.get("trigger_ts") is not None:
            hit = next((e for e in by_dev.get(td, [])
                        if e["signal"] == tsig
                        and e["ts"] == inst["trigger_ts"]), None)
            if hit:
                return ev_ref(hit)
        return None



    # ---- 逐实例求解恢复链 ----
    def trigger_anchor_seq(idx, inst):
        """本实例在各触发/组成设备上的起始 seq（用于按 seq 找缺口）。"""
        out = {}
        for m in inst.get("members", []) or []:
            if m.get("seq") is not None:
                out.setdefault(m["device"], m["seq"])
        if not out:
            td, tsig = rule_trigger[idx]
            if td and inst.get("trigger_ts") is not None:
                hit = next((e for e in by_dev.get(td, [])
                            if e["signal"] == tsig
                            and e["ts"] == inst["trigger_ts"]), None)
                if hit:
                    out[td] = hit.get("seq")
        return out

    def solve_instance(idx, inst, next_ts):
        spec, _ = rec_plan[idx]
        t0 = inst.get("trigger_ts")
        tail_open = next_ts is None
        st = {"spec": spec, "inst": inst, "trigger_ts": t0,
              "confirmed": t0 is not None and inst.get("status") != "unknown",
              "clear": [], "reset": None, "ack": None, "steps": [],
              "pre_fail": None, "pre_unknown": None,
              "blocked": [], "unknown_occupants": []}
        if t0 is None:
            st["pre_unknown"] = "trigger_not_confirmed"
            return st
        anchor_seq = trigger_anchor_seq(idx, inst)

        def last_before(dev, hard_next):
            return max((e.get("seq") for e in by_dev.get(dev, [])
                        if e.get("seq") is not None
                        and (hard_next is None or e["ts"] < hard_next)),
                       default=None)

        def bind(dev, sig, lo_ts, lo_seq, want_value=None, hard_next=None):
            """在统一时轴 lo_ts 之后绑定本实例动作：
            * lo_seq：本设备上“已绑定给本实例（或其前序）的最后一个事件”
              的 seq；其后到候选（或窗口末尾）之间的 seq 缺口可能藏事件
              -> unknown，不臆断。
            * hard_next：下一轮触发时刻，其后的事件属下一轮，不得回扫。
            返回 (event, code, why, gap)，code ∈ ok/fail/unknown。"""
            if dev in untrusted:
                return None, "unknown", "clock_residual_out_of_bounds", None
            raw = find_events(dev, sig, lo=lo_ts)
            cands = ([e for e in raw if hard_next is None
                      or e["ts"] < hard_next])
            good = [e for e in cands
                    if want_value is None or e.get("value") == want_value]
            chosen = good[0] if good else None
            end_seq = (chosen.get("seq") if chosen
                       else last_before(dev, hard_next))
            g = None
            if lo_seq is not None and end_seq is not None:
                g = seq_gap_between(dev, lo_seq, end_seq)
            if chosen is None and lo_seq is not None and hard_next is None:
                g = g or seq_gap_after(dev, lo_seq)
            if chosen is not None and g:
                return chosen, "unknown", "log_gap", g
            if chosen is not None:
                return chosen, "ok", None, None
            if g:
                return None, "unknown", "log_gap", g
            if not tail_open or hard_next is not None:
                return None, "fail", "missing", None
            return None, "unknown", "recovery_window_open", None

        # 阶段 1：报警解除（clear 各端点均须观察到）
        clear_ts = t0
        for ep in spec["clear"]:
            d, tok = ep["device"], f"{ep['device']}:{ep['signal']}"
            ev0, code, why, g = bind(d, ep["signal"], t0,
                                     anchor_seq.get(d),
                                     want_value=ep.get("value"),
                                     hard_next=next_ts)
            ent = {"endpoint": tok, "status": "unknown",
                   "actual": None, "seq": None}
            if code == "ok":
                ent.update(status="ok", actual=ev0["ts"],
                           seq=ev0.get("seq"), event=ev_ref(ev0))
                clear_ts = max(clear_ts, ev0["ts"])
                st["clear"].append(ent)
                continue
            if ev0 is not None and ep.get("value") is not None:
                ent.update(reason="clear_source_unknown",
                           event=ev_ref(ev0))
            else:
                ent["reason"] = why
                if g:
                    ent["gap"] = g
            st["clear"].append(ent)
            st["pre_fail" if code == "fail" else "pre_unknown"] = \
                "clear_missing" if code == "fail" else "clear_not_observed"
        if st["pre_unknown"] or st["pre_fail"]:
            return st

        # 阶段 2：值班员复位（复位来源须可核对，且应晚于全部解除；
        # 逐实例独占申领，多分区并发时各绑定各自的复位记录）
        rep = spec["reset"]
        ev0, code, why, g = claim_event(
            rep["device"], rep["signal"], clear_ts, mode="exclusive",
            hard_next=next_ts, lo_seq0=anchor_seq.get(rep["device"]))
        # 来源核对在取到事件之后做：来源不符按“复位来源不明”判 unknown，
        # 不预过滤事件、也不另立 fail 前置项（避免 fail/unknown 自相矛盾）。
        source_unknown = ev0 is not None and rep.get("value") is not None \
            and ev0.get("value") != rep.get("value")
        if source_unknown:
            code, why = "unknown", "reset_source_unknown"
        rent = {"endpoint": f"{rep['device']}:{rep['signal']}",
                "status": "unknown", "actual": None, "seq": None}
        if code == "ok":
            rent.update(status="ok", actual=ev0["ts"], seq=ev0.get("seq"),
                        event=ev_ref(ev0))
            st["reset"] = rent
        elif source_unknown:
            rent.update(reason="reset_source_unknown", actual=ev0["ts"],
                        seq=ev0.get("seq"), event=ev_ref(ev0))
            st["reset"] = rent
            st["pre_unknown"] = "reset_source_unknown"
            return st
        else:
            rent["reason"] = ("reset_missing" if code == "fail" else why)
            if g:
                rent["gap"] = g
            st["reset"] = rent
            if code == "fail":
                st["pre_fail"] = "reset_source_unknown"
            else:
                st["pre_unknown"] = "reset_source_unknown"
            return st
        reset_ts, reset_seq = rent["actual"], rent["seq"]

        # 阶段 3：锁存确认（逐实例独占申领并绑定本实例；同一确认记录被
        # 多轮复用在汇总阶段判 fail）
        if spec["ack"]:
            aep = spec["ack"]
            ev0, code, why, g = claim_event(
                aep["device"], aep["signal"], reset_ts, mode="exclusive",
                hard_next=next_ts, lo_seq0=reset_seq)
            ack_source_unknown = ev0 is not None \
                and aep.get("value") is not None \
                and ev0.get("value") != aep.get("value")
            if ack_source_unknown:
                code, why = "unknown", "ack_source_unknown"
            aent = {"endpoint": f"{aep['device']}:{aep['signal']}",
                    "status": "unknown", "actual": None, "seq": None}
            if code == "ok":
                aent.update(status="ok", actual=ev0["ts"],
                            seq=ev0.get("seq"), event=ev_ref(ev0))
                st["ack"] = aent
            else:
                aent["reason"] = ("ack_missing" if code == "fail" else why)
                if g:
                    aent["gap"] = g
                if ev0 is not None:
                    aent.update(actual=ev0["ts"], seq=ev0.get("seq"),
                                event=ev_ref(ev0))
                st["ack"] = aent
                if code == "fail":
                    st["pre_fail"] = "ack_missing"
                else:
                    st["pre_unknown"] = "ack_not_observed"
                return st

        # 阶段 4：设备回位（提前回位 / 时限 / 回位次序）
        prev_seq = dict(anchor_seq)          # 每台设备已绑定最后 seq
        prev_seq[rep["device"]] = reset_seq
        last_ts_on = {rep["device"]: reset_ts}
        for sp in spec["steps"]:
            d, tok = sp["device"], sp["token"]
            is_shared = d in shared_global
            # 公共设备的回位上界不取本规则下一轮触发，而取跨规则下一次
            # 占用它的触发（多分区并发时，本分区复位后设备仍在为他区运行）。
            if is_shared:
                occ_triggers = []
                for oi, orule in enumerate(matrix_rules):
                    if d not in activated_devs[oi]:
                        continue
                    for oi2 in scenarios[oi]["instances"]:
                        tt = oi2.get("trigger_ts")
                        if tt is not None and tt > t0:
                            occ_triggers.append(tt)
                step_next = min(occ_triggers, default=None)
            else:
                step_next = next_ts
            chain = ["clear", rent["endpoint"]] + list(sp["after"]) + [tok]
            f = {"type": "recovery_step", "target": tok,
                 "upstream": chain, "status": "unknown",
                 "shared": is_shared}
            # 前置回位步：必须是已绑定、按时回位的事件
            lo_step, dep_bad = reset_ts, None
            for dep in sp["after"]:
                prior = next((x for x in st["steps"]
                              if x.get("target") == dep), None)
                pactual = prior.get("actual") if prior else None
                if pactual is None:
                    if prior is not None and prior["status"] == "unknown":
                        dep_bad = ("unknown",
                                   prior.get("reason")
                                   or "predecessor_not_returned", dep)
                    else:
                        dep_bad = ("fail",
                                   (prior or {}).get("reason")
                                   or "predecessor_not_returned", dep)
                    break
                if prior["status"] == "fail":
                    dep_bad = ("fail",
                               prior.get("reason")
                               or "predecessor_not_returned", dep)
                    break
                lo_step = max(lo_step, pactual)
            within = (sp["within_ms"] if sp["within_ms"] is not None
                      else spec["max_return_ms"])
            deadline = reset_ts + within if within is not None else None
            # 提前回位（非公共设备）：触发后、复位之前窗口内已回位
            early_list, early_gap = [], None
            if not is_shared:
                early_list = find_events(d, sp["signal"], lo=t0,
                                         hi=reset_ts,
                                         before_next=next_ts)
                if not early_list:
                    end_seq0 = last_before(d, next_ts)
                    gg = (seq_gap_between(d, anchor_seq.get(d), end_seq0)
                          if anchor_seq.get(d) is not None
                          and end_seq0 is not None
                          else seq_gap_after(d, anchor_seq.get(d)))
                    if gg and gg["from_ts"] <= reset_ts:
                        early_gap = gg
            if dep_bad:
                f.update(status=dep_bad[0], reason=dep_bad[1],
                         predecessor=dep_bad[2])
                st["steps"].append(f)
                continue
            if early_list:
                ev0 = early_list[0]
                f.update(status="fail", reason="returned_before_reset",
                         type="recovery_early_return", actual=ev0["ts"],
                         event=ev_ref(ev0))
                st["steps"].append(f)
                continue
            if early_gap:
                f.update(reason="log_gap", gap=early_gap)
                st["steps"].append(f)
                continue
            # 回位绑定：从“本实例可回位时刻”（复位与全部前置都完成）起取
            # 第一条回位；先 fan 后 valve 的次序也不会拿后一轮回位冒充。
            # 公共设备走跨实例申领：同一物理回位可被多个并发占用分区共用，
            # 之后的分区申领下一条；提前/占用是否合法在占用核查阶段判定。
            shared_status = None
            if is_shared:
                ev0, shared_status, why0, g0 = claim_event(
                    d, sp["signal"], lo_step, mode="shared")
                window_evs = [ev0] if ev0 else []
            else:
                # 非公共设备仍从复位时刻起取，以捕捉“先于前置回位”的倒序
                window_evs = [] if d in untrusted else [
                    e for e in find_events(d, sp["signal"], lo=reset_ts)
                    if step_next is None or e["ts"] < step_next]
                ev0, g0 = window_evs[0] if window_evs else None, None
            if ev0 is not None:
                if not is_shared:
                    gg = (seq_gap_between(d, prev_seq.get(d), ev0.get("seq"))
                          if prev_seq.get(d) is not None
                          and ev0.get("seq") is not None else None)
                else:
                    gg = g0 if shared_status == "unknown" else None
                if gg:
                    f.update(reason="log_gap", gap=gg)
                elif not is_shared and ev0["ts"] < lo_step:
                    # 非公共设备回位早于应先回位的前置：前置仍有效 -> 倒序
                    f.update(status="fail",
                             reason="predecessor_still_active",
                             type="recovery_out_of_order",
                             predecessor=(sp["after"][0]
                                          if sp["after"] else None),
                             actual=ev0["ts"], seq=ev0.get("seq"),
                             event=ev_ref(ev0))
                elif deadline is not None and ev0["ts"] > deadline:
                    f.update(status="fail", type="recovery_timeout",
                             reason="recovery_timeout", deadline=deadline,
                             actual=ev0["ts"], seq=ev0.get("seq"),
                             over_ms=ev0["ts"] - deadline,
                             event=ev_ref(ev0))
                else:
                    f.update(status="ok", actual=ev0["ts"],
                             seq=ev0.get("seq"),
                             elapsed_ms=ev0["ts"] - reset_ts,
                             event=ev_ref(ev0))
            else:
                if is_shared and shared_status == "unknown":
                    f.update(reason=why0, gap=g0)
                elif is_shared and shared_status == "fail":
                    if deadline is not None:
                        f.update(status="fail", type="recovery_timeout",
                                 reason="recovery_timeout",
                                 deadline=deadline)
                    else:
                        f.update(status="fail",
                                 reason="step_not_returned")
                else:
                    end_floor = last_before(d, step_next)
                    gg = (seq_gap_between(d, prev_seq.get(d), end_floor)
                          if prev_seq.get(d) is not None and end_floor is not None
                          else seq_gap_after(d, prev_seq.get(d))
                          if prev_seq.get(d) is not None else None)
                    if gg:
                        f.update(reason="log_gap", gap=gg)
                    elif step_next is not None or not tail_open:
                        late = find_events(d, sp["signal"], lo=lo_step,
                                           before_next=step_next)
                        if late and deadline is not None:
                            e2 = late[0]
                            f.update(status="fail", type="recovery_timeout",
                                     reason="recovery_timeout",
                                     deadline=deadline, actual=e2["ts"],
                                     seq=e2.get("seq"),
                                     over_ms=e2["ts"] - deadline,
                                     event=ev_ref(e2))
                        elif deadline is not None:
                            f.update(status="fail", type="recovery_timeout",
                                     reason="recovery_timeout",
                                     deadline=deadline)
                        else:
                            f.update(status="fail",
                                     reason="step_not_returned")
                    else:
                        f["reason"] = "recovery_window_open"
            st["steps"].append(f)
            if f.get("actual") is not None:
                prev_seq[d] = f.get("seq")
                last_ts_on[d] = f["actual"]
        return st
    rec_states = [None] * len(matrix_rules)
    # 公共设备回位事件的跨实例申领：同一回位动作可同时释放多个并发占用
    # 分区（同刻共用）；其后的实例从下一条回位申领。按全局触发时刻交错
    # 求解，使申领顺序与占用发生顺序一致。
    shared_claims = {}      # (dev,sig) -> 已被申领的公共设备回位事件
    # 复位 / 锁存确认走逐实例独占申领：同一条记录只能绑定一个触发实例，
    # 多分区并发时各取各的 seq，避免都取 seq=1 后误报确认复用。
    exclusive_claims = {}   # (dev,sig) -> 已被某实例占用的 reset/ack 事件

    def claim_event(dev, sig, lo_ts, mode="own", hard_next=None,
                    lo_seq0=None):
        """取本实例在 lo_ts 之后的动作。
        mode='shared'    公共设备回位：跨实例申领，同刻物理回位可被多个并发
                         占用分区共用，其后的实例从下一条回位继续。
        mode='exclusive' 复位/锁存确认：每条记录全局只绑定一个实例，同刻也
                         不共用，按触发顺序逐个占用。
        mode='own'（默认）每实例独立取事件，不占全局名额。
        来源（value）不在此预过滤：先取信号事件，由调用方核对来源，来源
        不符按“来源不明”判 unknown，而不是把事件过滤掉后误判 fail。
        返回 (event, code, why, gap)，code ∈ ok/fail/unknown。"""
        if dev in untrusted:
            return None, "unknown", "clock_residual_out_of_bounds", None
        if mode == "shared":
            used = shared_claims.setdefault((dev, sig), [])
            same_ts_share = True
        elif mode == "exclusive":
            used = exclusive_claims.setdefault((dev, sig), [])
            same_ts_share = False
        else:
            used, same_ts_share = [], False
        used_keys = {(u["ts"], u.get("seq")) for u in used}
        pool = sorted((e for e in by_dev.get(dev, [])
                       if e["signal"] == sig and e["ts"] >= lo_ts
                       and (hard_next is None or e["ts"] < hard_next)),
                      key=lambda e: (e["ts"], e["seq"]))
        cands = []
        for e in pool:
            if (e["ts"], e.get("seq")) in used_keys and not same_ts_share:
                continue   # 独占/独立：已被占用的记录不得再取
            if same_ts_share and (e["ts"], e.get("seq")) in used_keys \
                    and not any(u["ts"] == e["ts"] for u in used):
                continue   # 共享：同刻允许并发复用，否则跳过
            cands.append(e)
        ev0 = cands[0] if cands else None
        lo_seq = lo_seq0
        if mode in ("shared", "exclusive") and lo_seq is None:
            lo_seq = max((u.get("seq") for u in used
                          if u.get("seq") is not None), default=None)
        consume = mode in ("shared", "exclusive")
        if ev0 is not None:
            g = (seq_gap_between(dev, lo_seq, ev0.get("seq"))
                 if lo_seq is not None and ev0.get("seq") is not None
                 else None)
            if g:
                # 缺口记录也先占用，避免并发的后续实例重复绑定同一条
                if consume:
                    used.append(ev0)
                return ev0, "unknown", "log_gap", g
            if consume:
                used.append(ev0)
            return ev0, "ok", None, None
        # 无候选：本轮窗口内的末尾 seq（hard_next 之前），其后缺号可能藏事件
        if hard_next is not None:
            last_seq = max((e.get("seq") for e in by_dev.get(dev, [])
                            if e.get("seq") is not None
                            and e["ts"] < hard_next), default=None)
        else:
            last_seq = max((e.get("seq") for e in by_dev.get(dev, [])
                            if e.get("seq") is not None), default=None)
        g = (seq_gap_between(dev, lo_seq, last_seq)
             if lo_seq is not None and last_seq is not None
             else seq_gap_after(dev, lo_seq)
             if lo_seq is not None else None)
        if g:
            return None, "unknown", "log_gap", g
        # 本轮未闭合（无下一轮触发截断）：恢复窗口仍敞开，后续可能补来事件，
        # 证据不完整 -> 保持 unknown，不得在敞开窗口上直接判 fail；
        # 仅当本轮已被下一轮触发（hard_next）截断仍无事件时才判 fail。
        if hard_next is None:
            return None, "unknown", "recovery_window_open", None
        return None, "fail", "missing", None

    def claim_shared(dev, sig, lo_ts):
        return claim_event(dev, sig, lo_ts, mode="shared")

    def release_claim(dev, sig, ev0):
        """回退一次申领（事件早于本实例可用时刻 -> 让给并发的更早实例）。"""
        used = shared_claims.get((dev, sig))
        if not used:
            return
        for k in range(len(used) - 1, -1, -1):
            if used[k] is ev0:
                del used[k]
                return

    # ---- 求解所有实例（无效规则直接登记；有效规则按全局触发时刻交错，
    #      使公共设备申领顺序与占用发生顺序一致）----
    solve_order = sorted(((i["trigger_ts"], idx, j)
                          for idx, sc in enumerate(scenarios)
                          for j, i in enumerate(sc["instances"])
                          if i["trigger_ts"] is not None))
    for idx, (rule0, sc0) in enumerate(zip(matrix_rules, scenarios)):
        spec, valid = rec_plan[idx]
        if valid:
            continue
        rec_states[idx] = (None if spec is None
                           else [{"invalid": spec["invalid"], "inst": i}
                                 for i in sc0["instances"]]
                           or [{"invalid": spec["invalid"], "inst": None}])
    for _, idx, j in solve_order:
        spec, valid = rec_plan[idx]
        if not valid:
            continue
        sc = scenarios[idx]
        tlist = sorted(i["trigger_ts"] for i in sc["instances"]
                       if i["trigger_ts"] is not None)
        inst = sc["instances"][j]
        nxt = next((t for t in tlist if t > inst["trigger_ts"]), None)
        if rec_states[idx] is None:
            rec_states[idx] = []
        rec_states[idx].append(solve_instance(idx, inst, nxt))
    for idx, sc in enumerate(scenarios):
        spec, valid = rec_plan[idx]
        if not valid or rec_states[idx] is None:
            continue
        have = {id(s.get("inst")) for s in rec_states[idx]}
        tlist = sorted(i["trigger_ts"] for i in sc["instances"]
                       if i["trigger_ts"] is not None)
        for inst in sc["instances"]:
            if id(inst) in have:
                continue
            nxt = next((t for t in tlist if t > (inst["trigger_ts"] or -1)),
                       None)
            rec_states[idx].append(solve_instance(idx, inst, nxt))

    # ---- 公共设备占用核查（以回位事件时刻为准）----
    def occupant_clear(ost):
        """占用实例的解除时刻：已坐实 clear 给时刻；证据不明给 'unknown'；
        无 recovery 规则无法核对解除 -> 'unknown'；未解除 -> None。"""
        if "invalid" in ost:
            return "unknown"
        if ost.get("pre_unknown") in ("clear_not_observed",
                                      "trigger_not_confirmed"):
            return "unknown"
        clears = [c for c in ost.get("clear", []) if c.get("actual") is not None]
        if not clears:
            return None if ost.get("confirmed") else "unknown"
        return max(c["actual"] for c in clears)

    all_states = [(ri, st) for ri, sts in enumerate(rec_states)
                  if sts for st in sts]
    for idx, states in enumerate(rec_states):
        if not states:
            continue
        spec, valid = rec_plan[idx]
        if not valid:
            continue
        for st in states:
            if "spec" not in st:
                continue
            t0 = st["trigger_ts"]
            for sp in spec["steps"]:
                if sp["device"] not in shared_global:
                    continue
                sf = next((x for x in st["steps"]
                           if x["target"] == sp["token"]), None)
                ret_ts = sf.get("actual") if sf else None
                if ret_ts is None:
                    # 回位本身都没成立：占用核查不另立 finding
                    continue
                for oidx, ost in all_states:
                    if oidx == idx and ost is st:
                        continue
                    if "spec" not in ost and "invalid" not in ost:
                        continue   # 非恢复规则实例：无法核对解除，嫌疑另列
                    # 占用判定：对方规则联动过该公共设备
                    if sp["device"] not in activated_devs[oidx]:
                        continue
                    ot0 = ost.get("trigger_ts")
                    if ot0 is None or ot0 >= ret_ts:
                        continue
                    oc = occupant_clear(ost)
                    base = {"rule": matrix_rules[oidx].get("id"),
                            "trigger_ts": ot0,
                            "step": sp["token"],
                            "devices": [sp["device"]],
                            "event": anchor_event(oidx, ost.get("inst"))}
                    if oc == "unknown":
                        if not any(b["rule"] == base["rule"]
                                   and b["trigger_ts"] == ot0
                                   and b["step"] == sp["token"]
                                   for b in st["unknown_occupants"]):
                            st["unknown_occupants"].append(base)
                    elif oc is None or oc > ret_ts:
                        if not any(b["rule"] == base["rule"]
                                   and b["trigger_ts"] == ot0
                                   and b["step"] == sp["token"]
                                   for b in st["blocked"]):
                            st["blocked"].append(base)

    # ---- 锁存确认跨实例复用登记 ----
    ack_usage = {}
    for idx, states in enumerate(rec_states):
        if not states:
            continue
        for st in states:
            if "spec" not in st or not st.get("ack"):
                continue
            ev = st["ack"].get("event")
            if ev:
                ack_usage.setdefault((ev["device"], ev.get("seq")), []).append(
                    (idx, st))

    def build_block(st, spec):
        findings = []
        if "invalid" in st:
            for g in st["invalid"]:
                findings.append({"type": "recovery_precondition",
                                 "status": "unknown", "reason": g["reason"],
                                 "endpoint": g.get("member"),
                                 "detail": g.get("detail"),
                                 "upstream": ["recovery"]})
            return {"status": "unknown", "clear": [], "reset": None,
                    "ack": None, "steps": [], "findings": findings,
                    "blocked_by": [], "gaps": st["invalid"]}
        if st.get("pre_fail"):
            findings.append({"type": "recovery_precondition",
                             "status": "fail", "reason": st["pre_fail"],
                             "upstream": ["recovery"]})
        # 复位/确认“来源不明”已有对应的具体 recovery_reset/recovery_ack
        # finding（并定位原始事件），这里不再补泛化的前置 unknown，避免
        # 同一原因产生两条相互重复甚至矛盾的判定。
        _SPECIFIC_UNKNOWN = {"reset_source_unknown", "ack_source_unknown"}
        if st.get("pre_unknown") and \
                st["pre_unknown"] not in _SPECIFIC_UNKNOWN:
            findings.append({"type": "recovery_precondition",
                             "status": "unknown", "reason": st["pre_unknown"],
                             "upstream": ["recovery"]})
        for c in st.get("clear", []):
            if c["status"] == "unknown":
                f = {"type": "recovery_clear", "target": c["endpoint"],
                     "status": "unknown", "reason": c["reason"],
                     "upstream": [c["endpoint"]]}
                if c.get("gap"):
                    f["gap"] = c["gap"]
                findings.append(f)
        if st.get("reset") and st["reset"]["status"] == "unknown":
            f = {"type": "recovery_reset", "target": st["reset"]["endpoint"],
                 "status": "unknown", "reason": st["reset"]["reason"],
                 "upstream": [st["reset"]["endpoint"]]}
            if st["reset"].get("gap"):
                f["gap"] = st["reset"]["gap"]
            if st["reset"].get("event"):
                f["event"] = ev_ref(st["reset"]["event"])
            findings.append(f)
        if st.get("ack") and st["ack"]["status"] == "unknown":
            f = {"type": "recovery_ack", "target": st["ack"]["endpoint"],
                 "status": "unknown", "reason": st["ack"]["reason"],
                 "upstream": [st["ack"]["endpoint"]]}
            if st["ack"].get("gap"):
                f["gap"] = st["ack"]["gap"]
            if st["ack"].get("event"):
                f["event"] = ev_ref(st["ack"]["event"])
            findings.append(f)
        findings += [dict(x) for x in st.get("steps", [])]
        timeouts = [f for f in findings
                    if f.get("type") == "recovery_timeout"]
        if timeouts:
            min(timeouts, key=lambda f: f["deadline"])["first"] = True
        for b in st.get("blocked", []):
            findings.append({"type": "shared_occupancy", "status": "fail",
                             "reason": "shared_equipment_still_occupied",
                             "rule": b["rule"], "trigger_ts": b["trigger_ts"],
                             "devices": b["devices"], "event": b["event"],
                             "upstream": ["recovery",
                                          f"{b['rule']}@{b['trigger_ts']}"]})
        for b in st.get("unknown_occupants", []):
            findings.append({"type": "shared_occupancy", "status": "unknown",
                             "reason": "occupant_clear_unknown",
                             "rule": b["rule"], "trigger_ts": b["trigger_ts"],
                             "devices": b["devices"], "event": b["event"],
                             "upstream": ["recovery",
                                          f"{b['rule']}@{b['trigger_ts']}"]})
        if st.get("ack") and st["ack"].get("event") and spec["latch"]:
            ev = st["ack"]["event"]
            users = ack_usage.get((ev["device"], ev.get("seq")), [])
            if len(users) > 1:
                others = [{"rule": matrix_rules[ri].get("id"),
                           "trigger_ts": u["trigger_ts"]}
                          for ri, u in users if u is not st]
                findings.append({"type": "recovery_ack", "status": "fail",
                                 "reason": "ack_reused_across_rounds",
                                 "target": st["ack"]["endpoint"],
                                 "event": ev_ref(ev), "reused_by": others,
                                 "upstream": [st["ack"]["endpoint"]]})
        status = ("fail" if any(f["status"] == "fail" for f in findings)
                  else "unknown"
                  if any(f["status"] == "unknown" for f in findings)
                  else "ok")
        return {"status": status,
                "clear": [c for c in st.get("clear", [])],
                "reset": st.get("reset"),
                "ack": ({"endpoint": st["ack"]["endpoint"],
                         "event": ev_ref(st["ack"]["event"])}
                        if st.get("ack") and st["ack"].get("event")
                        else st.get("ack")),
                "steps": st.get("steps", []),
                "blocked_by": st.get("blocked", []),
                "findings": findings}

    def normalize_recovery_out(spec):
        return {"clear": [f"{e['device']}:{e['signal']}"
                          + (f"={e['value']}" if e.get("value") is not None
                             else "") for e in spec["clear"]],
                "reset": f"{spec['reset']['device']}:{spec['reset']['signal']}"
                + (f"={spec['reset']['value']}"
                   if spec["reset"].get("value") is not None else ""),
                "ack": (f"{spec['ack']['device']}:{spec['ack']['signal']}"
                        if spec["ack"] else None),
                "max_return_ms": spec["max_return_ms"],
                "latch": spec["latch"],
                "shared_devices": spec["shared_devices"],
                "steps": [{"device": s["device"], "signal": s["signal"],
                           "within_ms": s["within_ms"], "after": s["after"],
                           "shared": s["shared"]} for s in spec["steps"]]}

    # 汇总进场景：每实例挂 recovery 块，重算实例/场景状态；
    # 归一化恢复规则固定进重放 JSON（旧演算不重算，保持原结果）。
    for idx, sc in enumerate(scenarios):
        spec, valid = rec_plan[idx]
        if not valid and spec is None:
            continue
        norm = {"invalid": spec["invalid"]} if not valid \
            else normalize_recovery_out(spec)
        states = rec_states[idx]
        by_id = {id(s.get("inst")): s for s in states if s.get("inst")}
        new_instances = []
        for inst in sc["instances"]:
            st = by_id.get(id(inst))
            if st is None:
                new_instances.append(inst)
                continue
            block = build_block(st, spec)
            ni = dict(inst)
            ni["recovery"] = block
            ni["findings"] = inst.get("findings", []) + block["findings"]
            if block["status"] == "fail":
                ni["status"] = "fail"
            elif block["status"] == "unknown" and ni["status"] != "fail":
                ni["status"] = "unknown"
            new_instances.append(ni)
        for st in states:
            if st.get("inst") is None:
                block = build_block(st, spec)
                new_instances.append({"trigger_ts": None, "kind": "recovery",
                                      "status": "unknown", "members": [],
                                      "gaps": st["invalid"],
                                      "findings": block["findings"],
                                      "recovery": block})
        sc["instances"] = new_instances
        sc["recovery"] = {"rule": norm}
        sc["status"] = (
            "fail" if any(i["status"] == "fail" for i in new_instances)
            else "unknown" if any(i["status"] == "unknown"
                                  for i in new_instances)
            else "pass")

    # ---- 主备切换复演（主机跳闸后限定时间内转启备用机）----
    # 仅对声明了 failover 段的 respond 生效；未声明的演算结果完全不变。
    # 把主机命令/运行佐证/故障信号/备用启动绑定到同一触发实例，区分：
    #   primary_success  主机成功（无需切换）
    #   legal_switch     故障坐实后等满切换等待再转启，未造成超时长并行
    #   spurious_switch  故障前启备机（主机只是反馈迟到），误切换
    #   parallel_overrun 双机并行超过允许并行时长
    #   standby_timeout  故障坐实但总时限内未启备用（含备用被更早分区占用）
    # 多分区争用同一备用机按触发时刻核对占用：先触发先得，同一备用启动
    # 记录只绑定一个触发实例。故障日志缺号、时钟不可信、主机佐证未决、
    # 备用归属多解 -> 相应链路保持 unknown。
    def fo_find_primary_finding(inst, target):
        return next((f for f in inst.get("findings", [])
                     if f.get("type") == "response"
                     and f.get("target") == target), None)

    def fo_event_ref(e):
        return ev_ref(e) if e is not None else None

    def fo_gap(dev, lo, hi):
        return gap_between(dev, lo, hi)

    def solve_failover(plan_ent, inst, ctx):
        """逐触发实例复演一条主备链。
        ctx = {'standby': 申领到的备用启动事件或 None,
               'holder': 占用该事件的更早实例归属或 None,
               'busy':   窗口内被更早实例占用的备用事件列表,
               'closed': 备用窗口是否已闭合（cap 或窗口后有记录）}"""
        norm = plan_ent["rule"]
        target = plan_ent["target"]
        block = {"target": target, "rule": norm, "status": "unknown",
                 "outcome": None, "findings": []}
        t0 = inst.get("trigger_ts")
        cap = inst.get("cap")

        def add(status, ftype, reason, **extra):
            f = {"type": ftype, "target": target,
                 "upstream": [target], "status": status, "reason": reason}
            if norm.get("fault"):
                f["upstream"] += [norm["fault"], norm["standby"]]
            f.update(extra)
            block["findings"].append(f)
            return f

        def finish(status, outcome=None):
            block["status"] = status
            block["outcome"] = outcome
            return block

        if not plan_ent["valid"]:
            for g in norm["invalid"]:
                add("unknown", "failover_precondition", g["reason"],
                    endpoint=g.get("member"), detail=g.get("detail"))
            return finish("unknown")
        wait, total, parallel = (norm["switch_wait_ms"], norm["total_ms"],
                                 norm["parallel_ms"])
        pdev, psig = norm["primary"].split(":", 1)
        fdev, fsig = norm["fault"].split(":", 1)
        sdev, ssig = norm["standby"].split(":", 1)
        pf = fo_find_primary_finding(inst, target)
        deadline = t0 + total if t0 is not None else None

        # ---- 前置：触发未确认 ----
        if t0 is None:
            add("unknown", "failover_precondition", "trigger_not_confirmed")
            return finish("unknown")
        # ---- 前置：触发时标不可信（旧单触发在实例 findings 里已有标记）----
        if pf is not None and pf.get("reason") in (
                "trigger_clock_untrusted",
                "clock_residual_out_of_bounds"):
            add("unknown", "failover_precondition",
                "clock_residual_out_of_bounds")
            return finish("unknown")
        # ---- 前置：任一相关设备时标不可信 ----
        for dd in (pdev, fdev, sdev):
            if dd in untrusted:
                add("unknown", "failover_precondition",
                    "clock_residual_out_of_bounds", endpoint=dd)
                return finish("unknown")
        # ---- 主机佐证未决：不臆断主机成败，整条链路 unknown ----
        if pf is not None and pf.get("status") == "unknown":
            add("unknown", "failover_primary",
                pf.get("reason") or "primary_evidence_pending",
                actual=pf.get("actual"))
            return finish("unknown")

        def faults(lo, hi):
            return sorted((e for e in by_dev.get(fdev, [])
                           if e["signal"] == fsig and lo <= e["ts"]
                           and (hi is None or e["ts"] <= hi)),
                          key=lambda e: (e["ts"], e.get("seq") or 0))

        def running_after_switch(s_ts):
            """复用主机运行佐证的采用样本，判定 s_ts 之后主机是否仍持续
            运行超过允许并行时长。
            返回 'overrun' / 'ended' / 'inconclusive' / 'no_evidence'。
            结论只建立在已观测样本上：敞开窗口不外推，缺口优先由调用方拦。"""
            if pf is None or "evidence" not in pf:
                return "no_evidence"
            evb = pf["evidence"]
            if evb.get("status") == "contradict":
                # 佐证相斥自带分歧区间：区间在宽限内结束 = 主机已停
                div = evb.get("divergence") or {}
                to = div.get("to_ts")
                if to is not None and to <= s_ts + parallel:
                    return "ended"
                return "inconclusive"
            src_verdicts = []
            for src in evb.get("sources", []):
                post = [sm for sm in src.get("adopted_samples", [])
                        if sm["ts"] >= s_ts]
                inr = [sm for sm in post if sm["in_range"]]
                if not inr:
                    src_verdicts.append(
                        "ended" if post else "inconclusive")
                    continue
                start = inr[0]["ts"]
                stop = next((sm["ts"] for sm in post
                             if sm["ts"] >= start and not sm["in_range"]),
                            None)
                if stop is not None:
                    span = stop - start
                elif cap is not None:
                    span = cap - start        # 闭合窗口允许插值到实例边界
                else:
                    span = inr[-1]["ts"] - start   # 敞开窗口只认已观测时长
                src_verdicts.append("overrun" if span > parallel
                                    else "ended" if stop is not None
                                    else "inconclusive")
            if "overrun" in src_verdicts:
                return "overrun"
            if src_verdicts and all(v == "ended" for v in src_verdicts):
                return "ended"
            return "inconclusive"

        s_ev = ctx["standby"]
        s_ts = s_ev["ts"] if s_ev is not None else None
        within_window = (s_ts is not None and s_ts <= deadline
                         and (cap is None or s_ts < cap))

        # ---- 情形 A：总时限内拿到备用启动 ----
        if within_window:
            # 备用设备命令后到启动之间的日志缺口可能藏另一条启动/停止记录
            g_standby = fo_gap(sdev, t0, s_ts)
            if g_standby:
                add("unknown", "failover_standby", "log_gap", gap=g_standby,
                    standby=fo_event_ref(s_ev))
                return finish("unknown")
            # 争用：该启动记录已被更早触发的分区独占
            if ctx.get("holder"):
                fe = faults(t0, deadline)
                g = fo_gap(fdev, t0, deadline)
                if fe and not g:
                    add("fail", "standby_contention", "standby_occupied",
                        deadline=deadline, occupied_by=ctx["holder"],
                        standby=fo_event_ref(s_ev))
                    return finish("fail", "standby_timeout")
                if g:
                    add("unknown", "failover_fault", "log_gap", gap=g)
                    return finish("unknown")
                add("fail", "standby_contention", "standby_occupied",
                    deadline=deadline, occupied_by=ctx["holder"],
                    standby=fo_event_ref(s_ev))
                return finish("fail", "standby_timeout")
            # 故障信号：命令后整个总完成时限内搜索。切换前的故障用于
            # 判定合法/过早切换；切换后的故障（主机反馈迟到）只能解释
            # 并行何时结束，不能为切换授权。
            fe_all = faults(t0, deadline)
            fe = next((e for e in fe_all if e["ts"] <= s_ts), None)
            fe_late = None if fe else next(
                (e for e in fe_all if e["ts"] > s_ts), None)
            g_fault = fo_gap(fdev, t0, s_ts)
            if fe is not None and not g_fault:
                # ---- 切换前故障已坐实 ----
                if s_ts - fe["ts"] < wait:
                    add("fail", "failover_switch", "switch_before_wait",
                        fault=ev_ref(fe), standby=fo_event_ref(s_ev),
                        switch_wait_ms=wait, elapsed_ms=s_ts - fe["ts"],
                        deadline=fe["ts"] + wait)
                    return finish("fail", "spurious_switch")
                verdict = running_after_switch(fe["ts"])
                if verdict == "overrun":
                    add("fail", "failover_parallel", "parallel_overrun",
                        fault=ev_ref(fe), standby=fo_event_ref(s_ev),
                        parallel_ms=parallel,
                        parallel_from=fe["ts"], parallel_to=s_ts)
                    return finish("fail", "parallel_overrun")
                if verdict == "inconclusive":
                    add("unknown", "failover_primary",
                        "primary_evidence_pending",
                        fault=ev_ref(fe), standby=fo_event_ref(s_ev))
                    return finish("unknown")
                add("ok", "failover_switch", "legal_switch",
                    fault=ev_ref(fe), standby=fo_event_ref(s_ev),
                    switch_wait_ms=wait, elapsed_ms=s_ts - fe["ts"],
                    total_elapsed_ms=s_ts - t0, deadline=deadline)
                return finish("ok", "legal_switch")
            # ---- 故障前已启备用：主机只是反馈迟到（误切换）----
            if g_fault and fe_late is None:
                add("unknown", "failover_fault", "log_gap", gap=g_fault)
                return finish("unknown")
            if g_fault and fe_late is not None:
                add("unknown", "failover_fault", "log_gap", gap=g_fault)
                return finish("unknown")
            if pf is not None and pf.get("actual") is not None \
                    and pf["actual"] > s_ts:
                gp = fo_gap(pdev, t0, s_ts + parallel)
                if gp:
                    add("unknown", "failover_primary", "log_gap", gap=gp)
                    return finish("unknown")
            verdict = running_after_switch(s_ts)
            # 离散链（无佐证段）：主机反馈迟到时刻可直接量并行时长
            primary_late = (pf.get("actual") if pf is not None
                            and pf.get("status") == "ok"
                            and pf.get("actual") is not None
                            and pf["actual"] > s_ts else None)
            # 并行超时只能由“主机在备机启动后仍运行”的证据证明：
            #   1) 运行佐证显示在范围运行覆盖超过允许并行时长；
            #   2) 离散主机反馈迟到，且迟到时刻距备启超过允许并行时长。
            # 切换后才到的故障信号（fe_late）不能证明双机并行——没有主机
            # 运行反馈或佐证时，并行状态无法坐实，只能按误切换判。
            overlap_to = primary_late if primary_late is not None \
                else fe_late["ts"] if fe_late is not None \
                else s_ts + parallel
            overrun = False
            if verdict == "overrun":
                overrun = True
            elif primary_late is not None and verdict != "ended" \
                    and primary_late - s_ts > parallel:
                overrun = True
            if overrun:
                add("fail", "failover_parallel", "parallel_overrun",
                    standby=fo_event_ref(s_ev),
                    fault=ev_ref(fe_late) if fe_late else None,
                    parallel_ms=parallel,
                    parallel_from=s_ts, parallel_to=overlap_to,
                    over_ms=overlap_to - s_ts - parallel)
                return finish("fail", "parallel_overrun")
            if verdict == "inconclusive":
                add("unknown", "failover_primary",
                    "primary_evidence_pending",
                    standby=fo_event_ref(s_ev))
                return finish("unknown")
            if verdict == "no_evidence" and pf is None \
                    and primary_late is None and not ctx.get("closed"):
                # 无主机运行反馈/佐证，且采集窗口仍敞开：后续可能补来更早
                # 故障或主机跳闸记录，并行状态与切换合法性均待证，保持
                # unknown；已观察到的迟到故障（fe_late）同样不能授权
                add("unknown", "failover_fault", "failover_window_open",
                    standby=fo_event_ref(s_ev),
                    fault=ev_ref(fe_late) if fe_late else None,
                    deadline=deadline)
                return finish("unknown")
            # 离散链：主机反馈迟到时长决定并行是否超时；无故障即启备机
            # 本身即误切换，并行未超时记 spurious_switch。主机反馈已到但
            # 无故障时敞开窗口不影响该判定——故障须在切换前到达才合法，
            # 之后补来已无授权意义。
            add("fail", "failover_switch", "spurious_switch",
                standby=fo_event_ref(s_ev),
                fault=ev_ref(fe_late) if fe_late else None,
                parallel_ms=parallel,
                parallel_from=s_ts, parallel_to=overlap_to)
            return finish("fail", "spurious_switch")

        # ---- 情形 B：总时限内无备用启动 ----
        fe_list = faults(t0, deadline)
        g_fault = fo_gap(fdev, t0, deadline)
        g_standby = fo_gap(sdev, t0, deadline)
        if g_standby:
            add("unknown", "failover_standby", "log_gap", gap=g_standby)
            return finish("unknown")
        if g_fault:
            add("unknown", "failover_fault", "log_gap", gap=g_fault)
            return finish("unknown")
        # 争用：窗口内备用启动全部被更早触发的实例占用
        if ctx.get("busy"):
            who, bev = ctx["busy"][0]
            add("fail", "standby_contention", "standby_occupied",
                deadline=deadline, occupied_by=who,
                standby=fo_event_ref(bev))
            return finish("fail", "standby_timeout")
        closed = ctx.get("closed") or cap is not None
        if fe_list:
            add("fail", "failover_standby", "standby_timeout",
                fault=ev_ref(fe_list[0]), deadline=deadline)
            return finish("fail", "standby_timeout")
        if not closed:
            add("unknown", "failover_fault", "failover_window_open",
                deadline=deadline)
            return finish("unknown")
        # 窗口闭合且无故障、无备用启动：主机成功，无需切换（不立 fail）
        if pf is not None and pf.get("status") == "ok":
            return finish("ok", "primary_success")
        if pf is not None and pf.get("status") == "fail":
            if pf.get("evidence_type") == "evidence_contradiction":
                # 佐证已坐实主机跳闸却未切换
                add("fail", "failover_standby", "standby_timeout",
                    deadline=deadline)
                return finish("fail", "standby_timeout")
            add("fail", "failover_primary",
                pf.get("reason") or "primary_timeout",
                deadline=deadline, actual=pf.get("actual"))
            return finish("fail", "primary_failed_no_switch")
        add("unknown", "failover_primary", "primary_evidence_pending",
            deadline=deadline)
        return finish("unknown")

    # 预申领：按全局触发时刻交错，先触发先得（同一备用启动记录只绑一个
    # 触发实例）；同时记录窗口内被更早实例占用的备用事件与窗口闭合情况。
    fo_plans = []
    for idx, rule0 in enumerate(matrix_rules):
        fo_plans.append({ent["target"]: ent
                         for ent in scenarios[idx].get("failover_rules", [])})
    standby_claims = {}        # (dev,sig) -> 已占用事件
    standby_holders = {}       # (dev,sig,ts,seq) -> 归属引用
    fo_ctx = {}                # (idx,j,target) -> ctx
    fo_order = sorted(((i.get("trigger_ts"), idx, j)
                       for idx, sc in enumerate(scenarios)
                       for j, i in enumerate(sc["instances"])
                       if i.get("trigger_ts") is not None))
    for _, idx, j in fo_order:
        inst = scenarios[idx]["instances"][j]
        t0 = inst["trigger_ts"]
        cap = inst.get("cap")
        rid = matrix_rules[idx].get("id")
        for target, ent in fo_plans[idx].items():
            if not ent["valid"]:
                fo_ctx[(idx, j, target)] = None
                continue
            sdev, ssig = ent["rule"]["standby"].split(":", 1)
            pdev2 = ent["rule"]["primary"].split(":", 1)[0]
            fdev2 = ent["rule"]["fault"].split(":", 1)[0]
            deadline = t0 + ent["rule"]["total_ms"]
            hi = cap if cap is not None and cap < deadline else deadline
            used = standby_claims.setdefault((sdev, ssig), [])
            used_keys = {(u["ts"], u.get("seq")) for u in used}
            pool = sorted((e for e in by_dev.get(sdev, [])
                           if e["signal"] == ssig and t0 <= e["ts"] <= hi),
                          key=lambda e: (e["ts"], e.get("seq") or 0))
            free = next((e for e in pool
                         if (e["ts"], e.get("seq")) not in used_keys), None)
            holder = None
            if free is None and pool:
                he = pool[0]
                holder = standby_holders.get(
                    (sdev, ssig, he["ts"], he.get("seq")))
            if free is not None:
                used.append(free)
                ref = {"rule": rid, "trigger_ts": t0,
                       "event": ev_ref(free)}
                standby_holders[(sdev, ssig, free["ts"],
                                 free.get("seq"))] = ref
            busy = []
            for e in pool:
                who = standby_holders.get(
                    (sdev, ssig, e["ts"], e.get("seq")))
                if who is not None and who["trigger_ts"] < t0:
                    busy.append((who, e))
            # 窗口闭合：实例边界截断，或主/故/备任一设备在时限后还有日志
            # （证明采集已覆盖到窗口之后；敞开窗口不得直接判 fail）
            closed = cap is not None
            if not closed:
                for dd in (sdev, fdev2, pdev2):
                    if any(e["ts"] > hi for e in by_dev.get(dd, [])):
                        closed = True
                        break
            fo_ctx[(idx, j, target)] = {
                "standby": free, "holder": holder if free is None else None,
                "busy": busy, "closed": closed}
    # trigger_ts=None 的实例（触发未确认/兜底）：规则非法照常登记，
    # 合法规则只做前置 unknown，不参与备用申领。
    for idx, sc in enumerate(scenarios):
        for j, inst in enumerate(sc["instances"]):
            if inst.get("trigger_ts") is not None:
                continue
            for target in fo_plans[idx]:
                fo_ctx[(idx, j, target)] = {
                    "standby": None, "holder": None, "busy": [],
                    "closed": inst.get("cap") is not None}

    fo_blocks = {}
    for idx, sc in enumerate(scenarios):
        for j, inst in enumerate(sc["instances"]):
            for target, ent in fo_plans[idx].items():
                fo_blocks[(idx, j, target)] = solve_failover(
                    ent, inst, fo_ctx.get((idx, j, target)))

    # 汇总进场景：每实例挂 failover 块；合法切换覆盖主机超时/佐证相斥的
    # fail 判定，重算实例/场景状态。归一化规则固定进重放 JSON。
    for idx, sc in enumerate(scenarios):
        if not fo_plans[idx]:
            continue
        new_instances = []
        for j, inst in enumerate(sc["instances"]):
            ni = dict(inst)
            added = []
            block_by_target = {}
            for target in fo_plans[idx]:
                block = fo_blocks.get((idx, j, target))
                if block is None:
                    continue
                ni.setdefault("failover", []).append(block)
                added += block["findings"]
                block_by_target[target] = block
            merged = list(inst.get("findings", [])) + added
            superseded, shadowed = set(), set()
            for target, block in block_by_target.items():
                pf = next((f for f in merged
                           if f.get("target") == target
                           and f.get("status") == "fail"
                           and (f.get("type") == "timeout"
                                or f.get("evidence_type")
                                == "evidence_contradiction")), None)
                if pf is None:
                    continue
                if block["status"] == "ok" \
                        and block.get("outcome") == "legal_switch":
                    # 合法切换：主机自身的超时/佐证相斥判定被切换链覆盖
                    pf["superseded_by"] = "failover"
                    superseded.add(id(pf))
                elif block["status"] == "unknown":
                    # 主备链证据不足（缺号/窗口敞开/佐证未决）：主机超时
                    # 不再直接拖成 fail，随主备链保持 unknown
                    pf["shadowed_by"] = "failover"
                    shadowed.add(id(pf))
            for f in merged:
                if f.get("type") == "timeout":
                    f.pop("first", None)
            timeouts = [f for f in merged if f.get("type") == "timeout"
                        and id(f) not in superseded
                        and id(f) not in shadowed]
            if timeouts:
                min(timeouts, key=lambda f: f["deadline"])["first"] = True
            live = [f for f in merged
                    if id(f) not in superseded and id(f) not in shadowed]
            ni["findings"] = merged
            ni["status"] = (
                "fail" if any(f.get("status") == "fail" for f in live)
                else "unknown"
                if any(f.get("status") == "unknown" for f in live)
                else "pass")
            new_instances.append(ni)
        sc["instances"] = new_instances
        sc["status"] = (
            "fail" if any(i["status"] == "fail" for i in new_instances)
            else "unknown" if any(i["status"] == "unknown"
                                  for i in new_instances)
            else "pass")

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


def evidence_rule_canonical(rule):
    """归一化佐证规则的稳定投影：剔除解析期附带字段，仅留可比较的语义。"""
    if not isinstance(rule, dict):
        return rule
    if "invalid" in rule:
        return {"invalid": rule["invalid"]}
    return {"combine": rule.get("combine"), "k": rule.get("k"),
            "sources": [{"device": s.get("device", s.get("spec_device")),
                         "signal": s.get("signal"),
                         "window_ms": s.get("window_ms"),
                         "duration_ms": s.get("duration_ms"),
                         "missing_ms": s.get("missing_ms"),
                         "range": s.get("range")}
                        for s in rule.get("sources", [])]}


def evidence_changes(ra, rb):
    """对比两版结果中每个 respond 的佐证：规则版本与结论差异。
    旧版未声明 evidence 的 respond 不列出（保持原结果即可）。"""
    def index(result):
        out = {}
        for sc in result.get("scenarios", []):
            rules = {e["target"]: e for e in sc.get("evidence_rules", [])}
            for inst in sc.get("instances", []):
                for f in inst.get("findings", []):
                    if f.get("type") == "response" and "evidence" in f:
                        plan = rules.get(f["target"])
                        if plan is None:
                            # 旧版重放：规则固定在 finding 内
                            plan = {"target": f["target"], "valid": True,
                                    "rule": f["evidence"].get("rule")}
                        out[(sc["id"], inst.get("trigger_ts"),
                             f["target"])] = {
                            "rule": evidence_rule_canonical(
                                plan.get("rule")),
                            "status": f["evidence"]["status"],
                            "verdict": f["status"],
                            "reason": f.get("reason")}
        return out
    ia, ib = index(ra), index(rb)
    changes = []
    for k in sorted(set(ia) | set(ib)):
        a, b = ia.get(k), ib.get(k)
        if a == b:
            continue
        item = {"scenario": k[0], "trigger_ts": k[1], "target": k[2]}
        if a is None:
            item["op"] = "add"
            item["to"] = b
        elif b is None:
            item["op"] = "remove"
            item["from"] = a
        else:
            item["op"] = "replace"
            if a["rule"] != b["rule"]:
                item["rule_changed"] = True
                item["rule_from"] = a["rule"]
                item["rule_to"] = b["rule"]
            if a["status"] != b["status"] or a["verdict"] != b["verdict"]:
                item["verdict_from"] = {"evidence": a["status"],
                                        "response": a["verdict"]}
                item["verdict_to"] = {"evidence": b["status"],
                                      "response": b["verdict"]}
        changes.append(item)
    return changes


def failover_rule_canonical(rule):
    """归一化主备规则的稳定投影：非法规则仅留 invalid，合法规则全字段比较。"""
    if not isinstance(rule, dict):
        return rule
    if "invalid" in rule:
        return {"invalid": rule["invalid"]}
    return {k: rule.get(k) for k in
            ("primary", "fault", "standby", "switch_wait_ms", "total_ms",
             "parallel_ms", "group")}


def failover_changes(ra, rb):
    """对比两版结果中每个 respond 的主备切换复演：规则版本与结论差异。
    旧版未声明 failover 的 respond 不列出（保持原结果即可）。"""
    def index(result):
        out = {}
        for sc in result.get("scenarios", []):
            rules = {e["target"]: e for e in sc.get("failover_rules", [])}
            for inst in sc.get("instances", []):
                for block in inst.get("failover", []):
                    plan = rules.get(block["target"])
                    if plan is None:
                        plan = {"target": block["target"], "valid": True,
                                "rule": block.get("rule")}
                    out[(sc["id"], inst.get("trigger_ts"),
                         block["target"])] = {
                        "rule": failover_rule_canonical(plan.get("rule")),
                        "status": block.get("status"),
                        "outcome": block.get("outcome")}
        return out
    ia, ib = index(ra), index(rb)
    changes = []
    for k in sorted(set(ia) | set(ib)):
        a, b = ia.get(k), ib.get(k)
        if a == b:
            continue
        item = {"scenario": k[0], "trigger_ts": k[1], "target": k[2]}
        if a is None:
            item["op"] = "add"
            item["to"] = b
        elif b is None:
            item["op"] = "remove"
            item["from"] = a
        else:
            item["op"] = "replace"
            if a["rule"] != b["rule"]:
                item["rule_changed"] = True
                item["rule_from"] = a["rule"]
                item["rule_to"] = b["rule"]
            if a["status"] != b["status"] or a["outcome"] != b["outcome"]:
                item["verdict_from"] = {"status": a["status"],
                                        "outcome": a["outcome"]}
                item["verdict_to"] = {"status": b["status"],
                                      "outcome": b["outcome"]}
        changes.append(item)
    return changes


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
            return 400, {"error": "重绑设备、改时钟锚点、改触发/复合规则、"
                                 "改响应佐证或主备切换规则必须给出 "
                                 "justification 依据"}
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
            "evidence_changes": evidence_changes(ra["result"], rb["result"]),
            "failover_changes": failover_changes(ra["result"], rb["result"]),
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
