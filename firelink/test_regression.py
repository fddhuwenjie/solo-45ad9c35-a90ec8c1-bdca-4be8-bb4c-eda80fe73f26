# -*- coding: utf-8 -*-
"""
回归测试：边界组合
  1. after 引用多解/无解别名 -> 已出现的响应保持 unknown，不判 out_of_order/fail
  2. 触发设备校时残差越界 -> 下游环节全部 unknown，不再计算 timeout
  3. seq 缺口可能遗漏 bypass_off -> 旁路 unknown，不判 bypass_not_reset/fail
  4. mutex / bypass finding 均带 upstream
  5. 既有三组模拟行为不变；修订/重放/差异/签结流程不变
  6. 响应佐证：粘连触点（离散反馈到、电流为 0）判 fail 并给分歧区间；
     单位同族换算、跨族不可换算；采样断档/日志缺号/窗口证据不足 unknown；
     all/any/k_of_n 汇总；旧 respond 与修订固化（规则/样本/结论可还原）
运行:  python3 test_regression.py
"""
import json
import threading
import unittest
import urllib.request
from wsgiref.simple_server import make_server

from app import evaluate, make_app
from simulate import CLOCK_DRIFT, MISSING_EVENTS, LEGAL_BYPASS, PORT


def base_payload(**kw):
    p = {
        "sync_tolerance_ms": 150,
        "devices": {"TD": {"type": "smoke"}, "R1": {"type": "valve"},
                    "R2": {"type": "fan"}},
        "aliases": {},
        "matrix": [],
        "sync_pulses": [{"device": "TD", "device_ts": 0, "master_ts": 0},
                        {"device": "R1", "device_ts": 0, "master_ts": 0},
                        {"device": "R2", "device_ts": 0, "master_ts": 0}],
        "events": [{"device": "TD", "seq": 1, "signal": "alarm", "device_ts": 1000}],
    }
    p.update(kw)
    return p


def only_finding(result, target):
    return [f for sc in result["scenarios"] for i in sc["instances"]
            for f in i["findings"] if f.get("target") == target]


class AfterAliasTest(unittest.TestCase):
    """after 引用多解/无解别名：前置状态不明 -> unknown，而非 out_of_order。"""

    def _run(self, aliases):
        p = base_payload(
            aliases=aliases,
            matrix=[{"id": "S", "trigger": {"device": "TD", "signal": "alarm"},
                     "respond": [{"device": "R2", "signal": "start",
                                  "within_ms": 60000,
                                  "after": ["阀门:open"]}]}],
            events=[{"device": "TD", "seq": 1, "signal": "alarm", "device_ts": 1000},
                    {"device": "R2", "seq": 1, "signal": "start", "device_ts": 5000}])
        return evaluate(p)

    def test_ambiguous_after_alias_keeps_unknown(self):
        r = self._run({"阀门": ["R1", "R2"]})       # 多解
        f = only_finding(r, "R2:start")[0]
        self.assertEqual(f["status"], "unknown")
        self.assertEqual(f["reason"], "predecessor_alias_ambiguous")
        self.assertNotEqual(f.get("type"), "out_of_order")
        self.assertEqual(r["status"], "unknown")    # 不得 fail

    def test_unresolved_after_alias_keeps_unknown(self):
        r = self._run({})                            # 无解
        f = only_finding(r, "R2:start")[0]
        self.assertEqual(f["status"], "unknown")
        self.assertEqual(f["reason"], "predecessor_alias_unresolved")
        self.assertEqual(r["status"], "unknown")

    def test_resolvable_after_alias_still_enforced(self):
        # 唯一解析的前置缺失时，out_of_order 判定保留
        r = self._run({"阀门": "R1"})
        f = only_finding(r, "R2:start")[0]
        self.assertEqual(f["status"], "fail")
        self.assertEqual(f["type"], "out_of_order")


class TriggerClockTest(unittest.TestCase):
    """触发设备残差越界：下游不再计算 timeout，全部 unknown。"""

    def setUp(self):
        self.payload = base_payload(
            sync_pulses=[{"device": "TD", "device_ts": 0, "master_ts": 0},
                         {"device": "TD", "device_ts": 100000,
                          "master_ts": 100500},          # 残差 500 > 150
                         {"device": "R1", "device_ts": 0, "master_ts": 0}],
            matrix=[{"id": "S", "trigger": {"device": "TD", "signal": "alarm"},
                     "respond": [{"device": "R1", "signal": "open",
                                  "within_ms": 30000}]}],
            events=[{"device": "TD", "seq": 1, "signal": "alarm",
                     "device_ts": 1000}])                # R1 无事件：原本会 timeout

    def test_no_timeout_from_untrusted_trigger(self):
        r = evaluate(self.payload)
        findings = [f for sc in r["scenarios"] for i in sc["instances"]
                    for f in i["findings"]]
        self.assertIn("TD", r["clock"]["untrusted"])
        self.assertFalse(any(f.get("type") == "timeout" for f in findings))
        resp = only_finding(r, "R1:open")[0]
        self.assertEqual(resp["status"], "unknown")
        self.assertEqual(resp["reason"], "trigger_clock_untrusted")
        self.assertEqual(r["status"], "unknown")        # 不得 fail

    def test_trusted_trigger_still_times_out(self):
        self.payload["sync_pulses"][1]["master_ts"] = 100100  # 残差回到容限内
        r = evaluate(self.payload)
        resp = only_finding(r, "R1:open")[0]
        self.assertEqual(resp["status"], "fail")
        self.assertEqual(resp["type"], "timeout")
        self.assertTrue(resp["first"])


class BypassGapTest(unittest.TestCase):
    """seq 缺口可能遗漏 bypass_off -> unknown；无缺口仍判 bypass_not_reset。"""

    def _run(self, events):
        return evaluate(base_payload(
            matrix=[], events=events,
            bypass_permits=[{"device": "R1", "start": 0, "end": 10**9,
                             "reason": "年检"}]))

    def test_gap_after_bypass_on_keeps_unknown(self):
        r = self._run([
            {"device": "R1", "seq": 1, "signal": "bypass_on", "device_ts": 100},
            {"device": "R1", "seq": 2, "signal": "run", "device_ts": 200},
            # 缺 seq=3，bypass_off 可能藏在缺口里
            {"device": "R1", "seq": 4, "signal": "run", "device_ts": 400}])
        b = r["bypass"][0]
        self.assertEqual(b["status"], "unknown")
        self.assertEqual(b["reason"], "log_gap")
        self.assertNotEqual(b.get("type"), "bypass_not_reset")
        self.assertEqual(r["status"], "unknown")        # 不得 fail

    def test_no_gap_still_bypass_not_reset(self):
        r = self._run([
            {"device": "R1", "seq": 1, "signal": "bypass_on", "device_ts": 100},
            {"device": "R1", "seq": 2, "signal": "run", "device_ts": 200}])
        b = r["bypass"][0]
        self.assertEqual(b["status"], "fail")
        self.assertEqual(b["type"], "bypass_not_reset")

    def test_inverted_timestamp_gap_after_on_keeps_unknown(self):
        # bypass_on seq=1 ts=300，后续 seq=3 ts=200（时间戳倒序）：
        # 缺失的 seq=2 可能记录 bypass_off -> unknown，不得判 fail
        r = self._run([
            {"device": "R1", "seq": 1, "signal": "bypass_on", "device_ts": 300},
            {"device": "R1", "seq": 3, "signal": "run", "device_ts": 200}])
        b = r["bypass"][0]
        self.assertEqual(b["status"], "unknown")
        self.assertEqual(b["reason"], "log_gap")
        self.assertEqual(b["gap"]["from_seq"], 2)
        self.assertEqual(r["status"], "unknown")

    def test_preceding_gap_does_not_mask_unreset(self):
        # 缺口(seq=2)位于 bypass_on(seq=3) 之前，不影响未复位判定 -> fail
        r = self._run([
            {"device": "R1", "seq": 1, "signal": "run", "device_ts": 100},
            {"device": "R1", "seq": 3, "signal": "bypass_on", "device_ts": 200},
            {"device": "R1", "seq": 4, "signal": "run", "device_ts": 300}])
        b = r["bypass"][0]
        self.assertEqual(b["status"], "fail")
        self.assertEqual(b["type"], "bypass_not_reset")

    def test_reset_inside_gap_free_log_stays_ok(self):
        r = self._run([
            {"device": "R1", "seq": 1, "signal": "bypass_on", "device_ts": 100},
            {"device": "R1", "seq": 2, "signal": "bypass_off", "device_ts": 300}])
        self.assertEqual(r["bypass"][0]["status"], "ok")


class UpstreamTest(unittest.TestCase):
    """mutex / bypass finding 都必须带 upstream。"""

    def test_mutex_violation_has_upstream(self):
        r = evaluate(base_payload(
            matrix=[], mutex=[["R1:start", "R2:start"]],
            events=[{"device": "R1", "seq": 1, "signal": "start", "device_ts": 100},
                    {"device": "R2", "seq": 1, "signal": "start", "device_ts": 200}]))
        m = r["mutex"][0]
        self.assertEqual(m["status"], "fail")
        self.assertEqual(m["upstream"], ["R1:start", "R2:start"])

    def test_mutex_unknown_member_has_upstream(self):
        r = evaluate(base_payload(matrix=[], mutex=[["不存在的设备:start", "R2:start"]]))
        self.assertEqual(r["mutex"][0]["upstream"], ["不存在的设备:start"])

    def test_bypass_findings_have_upstream(self):
        r = evaluate(base_payload(
            matrix=[],
            events=[{"device": "R1", "seq": 1, "signal": "bypass_on",
                     "device_ts": 100}]))
        self.assertEqual(r["bypass"][0]["upstream"], ["R1:bypass_on"])


class CombinedBoundaryTest(unittest.TestCase):
    """组合：触发不可信 + 响应缺失 + 多解 after + 缺口旁路，全链路 unknown。"""

    def test_all_boundaries_combined(self):
        p = base_payload(
            aliases={"阀": ["R1", "R2"]},
            sync_pulses=[{"device": "TD", "device_ts": 0, "master_ts": 0},
                         {"device": "TD", "device_ts": 100000, "master_ts": 100500},
                         {"device": "R1", "device_ts": 0, "master_ts": 0},
                         {"device": "R2", "device_ts": 0, "master_ts": 0}],
            matrix=[{"id": "S", "trigger": {"device": "TD", "signal": "alarm"},
                     "respond": [{"device": "R2", "signal": "start",
                                  "within_ms": 60000, "after": ["阀:open"]}]}],
            bypass_permits=[{"device": "R1", "start": 0, "end": 10**9,
                             "reason": "年检"}],
            events=[{"device": "TD", "seq": 1, "signal": "alarm", "device_ts": 1000},
                    {"device": "R2", "seq": 1, "signal": "start", "device_ts": 5000},
                    {"device": "R1", "seq": 1, "signal": "bypass_on",
                     "device_ts": 100},
                    {"device": "R1", "seq": 2, "signal": "run", "device_ts": 200},
                    {"device": "R1", "seq": 4, "signal": "run", "device_ts": 400}])
        r = evaluate(p)
        self.assertEqual(r["status"], "unknown")
        self.assertFalse(any(f.get("type") == "timeout"
                             for sc in r["scenarios"] for i in sc["instances"]
                             for f in i["findings"]))
        self.assertEqual(r["bypass"][0]["status"], "unknown")
        # 触发可信后：after 多解仍 unknown，旁路仍 unknown，整体仍不得 fail
        p["sync_pulses"][1]["master_ts"] = 100100
        r2 = evaluate(p)
        self.assertEqual(r2["status"], "unknown")
        f = only_finding(r2, "R2:start")[0]
        self.assertEqual(f["reason"], "predecessor_alias_ambiguous")


class ExistingBehaviorTest(unittest.TestCase):
    """既有三组模拟行为不变。"""

    def test_clock_drift_case(self):
        r = evaluate(CLOCK_DRIFT)
        self.assertEqual(r["status"], "unknown")
        self.assertIn("F1", r["clock"]["untrusted"])
        f = only_finding(r, "F1:start")[0]
        self.assertEqual(f["reason"], "clock_residual_out_of_bounds")

    def test_missing_events_case(self):
        r = evaluate(MISSING_EVENTS)
        self.assertEqual(r["status"], "fail")
        f = only_finding(r, "PA1:switch")[0]
        self.assertEqual(f["type"], "timeout")
        self.assertTrue(f["first"])
        g = only_finding(r, "LIFT1:homed")[0]
        self.assertEqual(g["status"], "unknown")
        self.assertEqual(g["reason"], "log_gap")

    def test_legal_bypass_case(self):
        r = evaluate(LEGAL_BYPASS)
        self.assertEqual(r["status"], "pass")
        self.assertEqual(r["bypass"][0]["status"], "ok")


class LifecycleHttpTest(unittest.TestCase):
    """修订 / 重放 / 差异 / 签结 流程不变。"""

    @classmethod
    def setUpClass(cls):
        cls.httpd = make_server("127.0.0.1", PORT + 1, make_app(":memory:"))
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def call(self, method, path, body=None):
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{PORT + 1}{path}",
                                     data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_revision_replay_diff_signoff(self):
        code, o = self.call("POST", "/projects", CLOCK_DRIFT)
        self.assertEqual(code, 201)
        pid = o["project"]
        # 缺 justification -> 400
        code, _ = self.call("POST", f"/projects/{pid}/revisions",
                            {"patch": {"sync_pulses": []}})
        self.assertEqual(code, 400)
        # 附依据改时钟锚点 -> rev2，判定 unknown -> pass
        patch = dict(CLOCK_DRIFT)
        patch["sync_pulses"] = [dict(x) for x in CLOCK_DRIFT["sync_pulses"]]
        patch["sync_pulses"][-1]["master_ts"] = 100120
        code, o = self.call("POST", f"/projects/{pid}/revisions",
                            {"justification": "以 NTP 重校 F1 锚点",
                             "patch": {"sync_pulses": patch["sync_pulses"]}})
        self.assertEqual((code, o["rev"], o["status"]), (201, 2, "pass"))
        # 重放：rev1 旧演算保留
        code, o = self.call("GET", f"/projects/{pid}/revisions/1")
        self.assertEqual(o["replay"]["status"], "unknown")
        # 差异
        code, o = self.call("GET", f"/projects/{pid}/diff?from=1&to=2")
        self.assertEqual(o["verdict_changes"],
                         [{"scenario": "S1-防烟", "from": "unknown", "to": "pass"}])
        # 签结 -> 冻结 -> 新修订 409
        code, o = self.call("POST", f"/projects/{pid}/signoff")
        self.assertEqual(code, 200)
        code, _ = self.call("POST", f"/projects/{pid}/revisions",
                            {"justification": "签结后再改", "patch": {}})
        self.assertEqual(code, 409)


    def test_evidence_revision_pinned_and_diff(self):
        # rev1：触点粘连 -> fail；修订佐证规则+重采样本 -> pass；
        # 旧版重放仍 fail（规则/样本/分歧区间固定），diff 列出规则变化。
        from simulate import CONTACT_STUCK, CONTACT_HEALTHY
        code, o = self.call("POST", "/projects", CONTACT_STUCK)
        self.assertEqual((code, o["status"]), (201, "fail"))
        pid = o["project"]
        # 缺 justification -> 400
        code, _ = self.call("POST", f"/projects/{pid}/revisions",
                            {"payload": CONTACT_HEALTHY})
        self.assertEqual(code, 400)
        # 附依据修订佐证规则与样本 -> rev2 pass
        code, o = self.call("POST", f"/projects/{pid}/revisions",
                            {"justification": "换表并加装风压测点，佐证改 any",
                             "payload": CONTACT_HEALTHY})
        self.assertEqual((code, o["rev"], o["status"]), (201, 2, "pass"))
        # 旧版重放：规则/样本/结论原样还原
        code, o = self.call("GET", f"/projects/{pid}/revisions/1")
        f = (o["replay"]["scenarios"][0]["instances"][0]
             ["findings"][0])
        self.assertEqual(f["status"], "fail")
        self.assertEqual(f["evidence"]["combine"], "all")
        self.assertEqual(f["evidence"]["divergence"]["from_ts"], 2200)
        # 新版
        code, o = self.call("GET", f"/projects/{pid}/revisions/2")
        f = (o["replay"]["scenarios"][0]["instances"][0]
             ["findings"][0])
        self.assertEqual(f["status"], "ok")
        self.assertEqual(f["evidence"]["combine"], "any")
        # diff：结论翻转 + 佐证规则变化
        code, o = self.call("GET", f"/projects/{pid}/diff?from=1&to=2")
        self.assertEqual(o["verdict_changes"],
                         [{"scenario": "S4-排烟风机",
                           "from": "fail", "to": "pass"}])
        ch = o["evidence_changes"][0]
        self.assertTrue(ch["rule_changed"])
        self.assertEqual(ch["verdict_from"],
                         {"evidence": "contradict", "response": "fail"})
        self.assertEqual(ch["verdict_to"],
                         {"evidence": "support", "response": "ok"})


def composite_payload(**kw):
    """复合触发现代化载荷：两只烟感 k_of_n(2) + 面板复位 + 手报旁路。"""
    devs = {"D1": {"type": "smoke"}, "D2": {"type": "smoke"},
            "M1": {"type": "manual"}, "P1": {"type": "panel"},
            "V1": {"type": "valve"}}
    comp = kw.pop("composite", {
        "k_of_n": [{"device": "D1", "signal": "alarm"},
                   {"device": "D2", "signal": "alarm"}], "k": 2,
        "window_ms": 30000, "hold_ms": 500,
        "reset": {"device": "P1", "signal": "reset"},
        "manual": {"device": "M1", "signal": "alarm"}})
    p = {
        "sync_tolerance_ms": 150,
        "devices": devs,
        "aliases": {},
        "matrix": [{"id": "C", "composite": comp,
                    "respond": kw.pop("respond", [])}],
        "sync_pulses": [{"device": d, "device_ts": 0, "master_ts": 0}
                        for d in devs],
        "events": kw.pop("events", []),
    }
    p.update(kw)
    return p


def cev(d, sig, ts, seq=1):
    return {"device": d, "seq": seq, "signal": sig, "device_ts": ts}


class CompositeConfirmTest(unittest.TestCase):
    """all / any / k_of_n 在确认窗口内凑齐 -> 生成实例并 pass。"""

    def _rule(self, op, k=None):
        children = [{"device": "D1", "signal": "alarm"},
                    {"device": "D2", "signal": "alarm"}]
        comp = {op: children, "window_ms": 30000, "hold_ms": 500,
                "reset": {"device": "P1", "signal": "reset"}}
        if k is not None:
            comp["k"] = k
        return comp

    def test_all_confirmed(self):
        p = composite_payload(composite=self._rule("all"), events=[
            cev("D1", "alarm", 1000), cev("D2", "alarm", 2000),
            cev("P1", "reset", 90000)])
        r = evaluate(p)
        insts = r["scenarios"][0]["instances"]
        self.assertEqual(r["status"], "pass")
        self.assertEqual(len(insts), 1)
        self.assertEqual(insts[0]["trigger_ts"], 2000)  # t0=最晚置位
        self.assertEqual({m["device"] for m in insts[0]["members"]},
                         {"D1", "D2"})

    def test_any_confirmed_uses_earliest(self):
        p = composite_payload(composite=self._rule("any"), events=[
            cev("D1", "alarm", 2000), cev("D2", "alarm", 1000),
            cev("P1", "reset", 90000)])
        insts = evaluate(p)["scenarios"][0]["instances"]
        self.assertEqual(len(insts), 1)
        self.assertEqual(insts[0]["trigger_ts"], 1000)  # 最早满足者
        self.assertEqual([m["device"] for m in insts[0]["members"]], ["D2"])

    def test_k_of_n_confirmed(self):
        p = composite_payload(events=[
            cev("D1", "alarm", 1000), cev("D2", "alarm", 2500),
            cev("P1", "reset", 90000)])
        insts = evaluate(p)["scenarios"][0]["instances"]
        self.assertEqual(len(insts), 1)
        self.assertEqual(insts[0]["status"], "pass")

    def test_all_single_detector_no_instance(self):
        # 偶发单烟、窗口在面板复位前闭合：不足以启动，不立案 -> 不算火警
        p = composite_payload(composite=self._rule("all"), events=[
            cev("D1", "alarm", 1000), cev("P1", "reset", 90000)])
        sc = evaluate(p)["scenarios"][0]
        self.assertEqual(len(sc["instances"]), 0)
        self.assertEqual(sc["status"], "pass")

    def test_out_of_window_no_instance(self):
        p = composite_payload(events=[
            cev("D1", "alarm", 1000), cev("D2", "alarm", 41000),
            cev("P1", "reset", 90000)])
        sc = evaluate(p)["scenarios"][0]
        self.assertEqual(len(sc["instances"]), 0)

    def test_list_shorthand(self):
        # 嵌套 ["k_of_n", 2, 叶子...] 简写等价于 {"k_of_n":[...],"k":2}
        comp = {"all": [["k_of_n", 2,
                         {"device": "D1", "signal": "alarm"},
                         {"device": "D2", "signal": "alarm"}]],
                "window_ms": 30000, "hold_ms": 500,
                "reset": {"device": "P1", "signal": "reset"}}
        p = composite_payload(composite=comp, events=[
            cev("D1", "alarm", 1000), cev("D2", "alarm", 2000),
            cev("P1", "reset", 90000)])
        self.assertEqual(evaluate(p)["status"], "pass")
        root = evaluate(p)["scenarios"][0]["composite"]["root"]
        self.assertIn("all", root)
        self.assertEqual(root["all"][0]["k"], 2)


class CompositeRoundTest(unittest.TestCase):
    """复位前后分轮、重复置位去抖、窗口滑动不串案。"""

    EV_TWO_ROUNDS = [
        cev("D1", "alarm", 1000), cev("D2", "alarm", 2000),
        cev("P1", "reset", 5000),
        cev("D1", "alarm", 20000, 2), cev("D2", "alarm", 21000, 2),
        cev("P1", "reset", 90000)]

    def test_reset_splits_rounds(self):
        p = composite_payload(events=self.EV_TWO_ROUNDS)
        insts = evaluate(p)["scenarios"][0]["instances"]
        self.assertEqual([i["trigger_ts"] for i in insts], [2000, 21000])
        self.assertTrue(all(i["status"] == "pass" for i in insts))

    def test_repeat_set_counts_one_round(self):
        p = composite_payload(events=[
            cev("D1", "alarm", 1000), cev("D1", "alarm", 1100, 2),
            cev("D2", "alarm", 2000),
            cev("D1", "alarm", 3000, 3),   # 同一轮第三次上报
            cev("P1", "reset", 90000)])
        insts = evaluate(p)["scenarios"][0]["instances"]
        self.assertEqual(len(insts), 1)
        rep = {m["device"]: m["repeat_sets"]
               for m in insts[0]["members"]}
        self.assertEqual(rep["D1"], 2)
        self.assertEqual(rep["D2"], 0)

    def test_bounce_discarded(self):
        comp = {"k_of_n": [
                    {"device": "D1", "signal": "alarm"},
                    {"device": "D2", "signal": "alarm",
                     "reset": "restore"}], "k": 2,
                "window_ms": 30000, "hold_ms": 500,
                "reset": {"device": "P1", "signal": "reset"}}
        p = composite_payload(composite=comp, events=[
            cev("D1", "alarm", 1000), cev("D2", "alarm", 2000),
            cev("D2", "restore", 2200, 2),       # 200ms 抖动
            cev("P1", "reset", 90000)])
        sc = evaluate(p)["scenarios"][0]
        self.assertEqual(len(sc["instances"]), 0)

    def test_bounce_then_real_alarm(self):
        comp = {"k_of_n": [
                    {"device": "D1", "signal": "alarm"},
                    {"device": "D2", "signal": "alarm",
                     "reset": "restore"}], "k": 2,
                "window_ms": 30000, "hold_ms": 500,
                "reset": {"device": "P1", "signal": "reset"}}
        p = composite_payload(composite=comp, events=[
            cev("D1", "alarm", 1000), cev("D2", "alarm", 2000),
            cev("D2", "restore", 2200, 2),
            cev("D2", "alarm", 3000, 3),            # 抖动后再真报
            cev("P1", "reset", 90000)])
        insts = evaluate(p)["scenarios"][0]["instances"]
        self.assertEqual(len(insts), 1)
        d2 = next(m for m in insts[0]["members"] if m["device"] == "D2")
        self.assertEqual(d2["seq"], 3)

    def test_response_chain_bound_to_round(self):
        p = composite_payload(
            respond=[{"device": "V1", "signal": "open",
                      "within_ms": 30000}],
            events=self.EV_TWO_ROUNDS[:5] + [
                cev("V1", "open", 3000),
                cev("P1", "reset", 90000)])
        # 重排：第一轮 V1 到位，第二轮无 V1 -> 第二轮 timeout
        p["events"] = [
            cev("D1", "alarm", 1000), cev("D2", "alarm", 2000),
            cev("V1", "open", 3000), cev("P1", "reset", 5000),
            cev("D1", "alarm", 20000, 2), cev("D2", "alarm", 21000, 2),
            cev("P1", "reset", 90000)]
        insts = evaluate(p)["scenarios"][0]["instances"]
        self.assertEqual([i["trigger_ts"] for i in insts], [2000, 21000])
        self.assertEqual(insts[0]["findings"][0]["status"], "ok")
        self.assertEqual(insts[1]["findings"][0]["status"], "fail")
        self.assertEqual(insts[1]["findings"][0]["type"], "timeout")
        self.assertEqual(evaluate(p)["status"], "fail")


class CompositeGapTest(unittest.TestCase):
    """参与设备日志缺号 -> 相关实例 unknown 并列出缺口；不得汇总成空 pass。"""

    def test_missing_seq_marks_unknown_with_gap(self):
        p = composite_payload(events=[
            cev("D1", "alarm", 1000),
            cev("D2", "run", 500),
            {"device": "D2", "seq": 3, "signal": "alarm",
             "device_ts": 2000},                      # D2 缺 seq=2
            cev("P1", "reset", 90000)])
        sc = evaluate(p)["scenarios"][0]
        self.assertEqual(sc["status"], "unknown")
        # 同一个缺口只立一个实例（窗口滑动不得重复计数）
        self.assertEqual(len(sc["instances"]), 1)
        inst = sc["instances"][0]
        self.assertEqual(inst["status"], "unknown")
        self.assertTrue(any(g["member"] == "D2:alarm"
                            and g["reason"] == "log_gap"
                            for g in inst["gaps"]))

    def test_gap_might_hide_member_is_unknown(self):
        p = composite_payload(events=[
            cev("D1", "alarm", 1000),
            cev("D2", "run", 1500),
            {"device": "D2", "seq": 3, "signal": "run",
             "device_ts": 40000},                     # 缺口跨确认窗口
            cev("P1", "reset", 90000)])
        sc = evaluate(p)["scenarios"][0]
        self.assertEqual(sc["status"], "unknown")
        self.assertEqual(len(sc["instances"]), 1)
        self.assertTrue(any(g["reason"] == "log_gap"
                            for g in sc["instances"][0]["gaps"]))

    def test_gap_instance_keeps_responses_unknown(self):
        p = composite_payload(
            respond=[{"device": "V1", "signal": "open",
                      "within_ms": 30000}],
            events=[
            cev("D1", "alarm", 1000),
            cev("D2", "run", 500),
            {"device": "D2", "seq": 3, "signal": "alarm",
             "device_ts": 2000},
            cev("P1", "reset", 90000)])
        r = evaluate(p)
        self.assertEqual(r["status"], "unknown")      # 不得因 V1 而变 fail/pass
        inst = r["scenarios"][0]["instances"][0]
        self.assertEqual(inst["findings"][0]["status"], "unknown")
        self.assertEqual(inst["findings"][0]["reason"],
                         "trigger_not_confirmed")

    def test_gap_before_members_set_is_unknown(self):
        # 置位事件之前的 seq 缺口可能藏更早报警：组合虽凑齐仍 unknown
        p = composite_payload(events=[
            cev("D1", "run", 100),
            {"device": "D1", "seq": 3, "signal": "alarm",
             "device_ts": 1000},                       # D1 缺 seq=2
            cev("D2", "alarm", 2000),
            cev("P1", "reset", 90000)])
        sc = evaluate(p)["scenarios"][0]
        self.assertEqual(sc["status"], "unknown")
        self.assertEqual(len(sc["instances"]), 1)
        self.assertTrue(any(g["member"] == "D1:alarm"
                            for g in sc["instances"][0]["gaps"]))

    def test_gap_round_then_clean_round(self):
        # 第一轮证据有缺口(unknown)，复位后第二轮日志干净 -> 仍应分出第二轮
        p = composite_payload(events=[
            cev("D1", "run", 100),
            {"device": "D1", "seq": 3, "signal": "alarm",
             "device_ts": 1000},
            cev("D2", "alarm", 2000), cev("P1", "reset", 5000),
            cev("D1", "alarm", 20000, 4),
            cev("D2", "alarm", 21000, 2),
            cev("P1", "reset", 90000)])
        insts = evaluate(p)["scenarios"][0]["instances"]
        statuses = [(i["trigger_ts"], i["status"]) for i in insts]
        self.assertIn((2000, "unknown"), statuses)
        self.assertIn((21000, "pass"), statuses)

    def test_untrusted_member_clock_unknown(self):
        p = composite_payload(events=[cev("D1", "alarm", 1000)])
        p["sync_pulses"] = [
            {"device": "D1", "device_ts": 0, "master_ts": 0},
            {"device": "D1", "device_ts": 100000, "master_ts": 100500},
            {"device": "D2", "device_ts": 0, "master_ts": 0},
            {"device": "M1", "device_ts": 0, "master_ts": 0},
            {"device": "P1", "device_ts": 0, "master_ts": 0},
            {"device": "V1", "device_ts": 0, "master_ts": 0}]
        r = evaluate(p)
        sc = r["scenarios"][0]
        self.assertEqual(sc["status"], "unknown")
        self.assertTrue(any(
            g["reason"] == "clock_residual_out_of_bounds"
            for i in sc["instances"] for g in i["gaps"]))


class CompositeManualTest(unittest.TestCase):
    """手报旁路可直接触发；窗口外不抢探测组合。"""

    def test_manual_directly_confirms(self):
        p = composite_payload(events=[
            cev("M1", "alarm", 1500), cev("P1", "reset", 90000)])
        insts = evaluate(p)["scenarios"][0]["instances"]
        self.assertEqual(len(insts), 1)
        self.assertEqual(insts[0]["kind"], "manual")
        self.assertEqual(insts[0]["trigger_ts"], 1500)

    def test_manual_with_log_gap_unknown(self):
        p = composite_payload(events=[
            cev("M1", "run", 100),
            {"device": "M1", "seq": 3, "signal": "alarm",
             "device_ts": 1500},                      # 手报前缺 seq=2
            cev("P1", "reset", 90000)])
        insts = evaluate(p)["scenarios"][0]["instances"]
        self.assertEqual(len(insts), 1)
        self.assertEqual(insts[0]["kind"], "manual")
        self.assertEqual(insts[0]["status"], "unknown")
        self.assertTrue(any(g["reason"] == "log_gap"
                            for g in insts[0]["gaps"]))

    def test_manual_and_composite_in_same_window(self):
        p = composite_payload(events=[
            cev("D1", "alarm", 1000), cev("M1", "alarm", 1500),
            cev("D2", "alarm", 2000), cev("P1", "reset", 90000)])
        insts = evaluate(p)["scenarios"][0]["instances"]
        # 同轮：手报先直接确认，只立一个实例
        self.assertEqual([i["kind"] for i in insts], ["manual"])


class CompositeStructureTest(unittest.TestCase):
    """结构非法 / 循环 / 别名多解 / k 越界 -> unknown 并列缺口。"""

    def _comp(self, root, **extra):
        c = dict(root)
        c.update(window_ms=30000, hold_ms=500,
                 reset={"device": "P1", "signal": "reset"})
        c.update(extra)
        return c

    def test_cycle_unknown(self):
        comp = self._comp(
            {"all": ["#a"],
             "defs": {"a": {"any": ["#b"]},
                      "b": {"all": ["#a"]}}})
        p = composite_payload(composite=comp)
        inst = evaluate(p)["scenarios"][0]["instances"][0]
        self.assertEqual(inst["status"], "unknown")
        self.assertTrue(any(g["reason"] == "composite_cycle"
                            for g in inst["gaps"]))

    def test_alias_ambiguous_unknown(self):
        comp = self._comp({"all": [
            {"device": "烟感", "signal": "alarm"},
            {"device": "D2", "signal": "alarm"}]})
        p = composite_payload(composite=comp, aliases={"烟感": ["D1", "D2"]})
        inst = evaluate(p)["scenarios"][0]["instances"][0]
        self.assertEqual(inst["status"], "unknown")
        self.assertTrue(any(g["reason"] == "alias_ambiguous"
                            for g in inst["gaps"]))

    def test_k_out_of_range_unknown(self):
        comp = self._comp({"k_of_n": [
            {"device": "D1", "signal": "alarm"},
            {"device": "D2", "signal": "alarm"}], "k": 3})
        p = composite_payload(composite=comp)
        inst = evaluate(p)["scenarios"][0]["instances"][0]
        self.assertEqual(inst["status"], "unknown")
        self.assertTrue(any(g["reason"] == "k_out_of_range"
                            for g in inst["gaps"]))

    def test_normalized_rule_pinned_in_replay(self):
        p = composite_payload(events=[
            cev("D1", "alarm", 1000), cev("D2", "alarm", 2000),
            cev("P1", "reset", 90000)])
        comp = evaluate(p)["scenarios"][0]["composite"]
        self.assertEqual(comp["window_ms"], 30000)
        self.assertEqual(comp["reset"], "P1:reset")
        self.assertEqual(comp["manual"], "M1:alarm")
        self.assertEqual(comp["root"]["k"], 2)
        self.assertEqual(
            sorted((n["device"], n["signal"]) for n in comp["root"]["k_of_n"]),
            [("D1", "alarm"), ("D2", "alarm")])


def evidence_payload(events, evidence, **kw):
    """佐证载荷：D1 报警 -> F1 start 反馈；I1 电流 / P1 风压 / V1 阀位。"""
    p = base_payload(
        devices={"D1": {"type": "smoke"}, "F1": {"type": "fan"},
                 "I1": {"type": "ammeter"}, "P1": {"type": "pressure"},
                 "V1": {"type": "valve"}, "P9": {"type": "panel"}},
        matrix=[{"id": "S",
                 "trigger": {"device": "D1", "signal": "alarm"},
                 "respond": [{"device": "F1", "signal": "start",
                              "within_ms": 60000, "evidence": evidence}]}],
        sync_pulses=[{"device": d, "device_ts": 0, "master_ts": 0}
                     for d in ("D1", "F1", "I1", "P1", "V1", "P9")],
        events=events)
    p.update(kw)
    return p


def smp(dev, sig, ts, seq, value, unit=None):
    e = {"device": dev, "seq": seq, "signal": sig, "device_ts": ts,
         "value": value}
    if unit:
        e["unit"] = unit
    return e


FB_EVENTS = [
    {"device": "D1", "seq": 1, "signal": "alarm", "device_ts": 1000},
    {"device": "F1", "seq": 1, "signal": "start", "device_ts": 2000}]
CUR_RANGE = {"min": 5, "max": 20, "unit": "A"}


def cur_evidence(**over):
    ev = {"window_ms": 5000, "range": CUR_RANGE, "duration_ms": 1000,
          "missing_ms": 2000,
          "sources": [{"device": "I1", "signal": "I"}]}
    ev.update(over)
    return ev


def evidence_finding(result):
    return result["scenarios"][0]["instances"][0]["findings"][0]


class ResponseEvidenceTest(unittest.TestCase):
    """响应佐证：离散反馈须与电流/风压/阀位等模拟量证据一致。"""

    def test_stuck_contact_zero_current_fails(self):
        # 触点粘连：start 已上报且电流密集持续 0A（无断档）-> fail，分歧
        # 区间止于最后一个实测越界样本，不外推到敞开窗口末端
        r = evaluate(evidence_payload(FB_EVENTS + [
            smp("I1", "I", 2200, 1, 0.1, "A"),
            smp("I1", "I", 3500, 2, 0.0, "A"),
            smp("I1", "I", 4800, 3, 0.0, "A")], cur_evidence()))
        f = evidence_finding(r)
        self.assertEqual(f["status"], "fail")
        self.assertEqual(f["type"], "response")
        self.assertEqual(f["evidence_type"], "evidence_contradiction")
        self.assertEqual(f["evidence"]["status"], "contradict")
        self.assertEqual(f["evidence"]["sources"][0]["status"], "contradict")
        d = f["evidence"]["divergence"]
        self.assertEqual(d["from_ts"], 2200)
        # 敞开窗口：分歧止于最后一个实测越界样本 4800，不外推到窗口末端 7000
        self.assertEqual(d["to_ts"], 4800)
        self.assertEqual(r["status"], "fail")

    def test_closed_window_single_sample_not_extrapolated_to_cap(self):
        # 闭合窗口在 4000 复位：只有 2200 一条 0A 样本，观测覆盖为 0，
        # 且末样本到复位点 1800ms 超过缺测容限 1500ms -> unknown，
        # 不得把该单点越界外推到复位点来满足 1500ms duration 判 fail。
        devs = {"D1": {"type": "smoke"}, "P9": {"type": "panel"},
                "F1": {"type": "fan"}, "I1": {"type": "ammeter"}}
        comp = {"all": [{"device": "D1", "signal": "alarm"}],
                "window_ms": 30000, "hold_ms": 0,
                "reset": {"device": "P9", "signal": "reset"}}
        ev = cur_evidence(window_ms=9000, duration_ms=1500, missing_ms=1500)
        p = {
            "devices": devs, "aliases": {},
            "matrix": [{"id": "C", "composite": comp,
                        "respond": [{"device": "F1", "signal": "start",
                                     "within_ms": 60000, "evidence": ev}]}],
            "sync_pulses": [{"device": d, "device_ts": 0, "master_ts": 0}
                            for d in devs],
            "events": [
                cev("D1", "alarm", 1000), cev("F1", "start", 2000),
                smp("I1", "I", 2200, 1, 0.0, "A"),
                cev("P9", "reset", 4000)]}
        r = evaluate(p)
        f = r["scenarios"][0]["instances"][0]["findings"][0]
        self.assertEqual(f["status"], "unknown")
        self.assertNotEqual(f.get("evidence_type"), "evidence_contradiction")
        src = f["evidence"]["sources"][0]
        self.assertEqual(src["status"], "unknown")
        self.assertEqual(src["reason"], "evidence_sample_gap")
        self.assertEqual(src["gap_segment_ms"], [2200, 4000])
        self.assertIsNone(f["evidence"].get("divergence"))

    def test_internal_gap_takes_precedence_over_fail(self):
        # 2200、3200 两条 0A 刚好覆盖 1000ms duration，但到 4800 下一条
        # 样本间隔 1600ms > 缺测容限 1000ms -> 因内部断档 unknown，
        # 缺口可能藏在范围样本，不得先按越界判 fail。
        r = evaluate(evidence_payload(FB_EVENTS + [
            smp("I1", "I", 2200, 1, 0.0, "A"),
            smp("I1", "I", 3200, 2, 0.0, "A"),
            smp("I1", "I", 4800, 3, 0.0, "A")],
            cur_evidence(duration_ms=1000, missing_ms=1000)))
        f = evidence_finding(r)
        self.assertEqual(f["status"], "unknown")
        self.assertNotEqual(f.get("evidence_type"), "evidence_contradiction")
        src = f["evidence"]["sources"][0]
        self.assertEqual(src["status"], "unknown")
        self.assertEqual(src["reason"], "evidence_sample_gap")
        self.assertEqual(src["gap_segment_ms"], [3200, 4800])
        self.assertIsNone(f["evidence"].get("divergence"))
        self.assertEqual(r["status"], "unknown")

    def test_healthy_current_passes_with_samples(self):
        r = evaluate(evidence_payload(FB_EVENTS + [
            smp("I1", "I", 2200, 1, 0.2, "A"),    # 启动爬升，短暂越界不判
            smp("I1", "I", 2600, 2, 8.0, "A"),
            smp("I1", "I", 3200, 3, 8.1, "A"),
            smp("I1", "I", 4000, 4, 7.9, "A"),
            smp("I1", "I", 5000, 5, 8.0, "A")], cur_evidence()))
        f = evidence_finding(r)
        self.assertEqual(f["status"], "ok")
        self.assertEqual(f["evidence"]["status"], "support")
        self.assertTrue(all({"ts", "value", "converted_value", "in_range"}
                            <= set(s) for s in f["evidence"]["samples"]))
        self.assertEqual(r["status"], "pass")

    def test_unit_conversion_ma_to_a(self):
        # 电流以 mA 上报、范围以 A 声明：同族换算后判定
        r = evaluate(evidence_payload(FB_EVENTS + [
            smp("I1", "I", 2100, 1, 200, "mA"),    # 0.2 A，越界
            smp("I1", "I", 3000, 2, 8000, "mA"),
            smp("I1", "I", 4000, 3, 8000, "mA")], cur_evidence()))
        f = evidence_finding(r)
        self.assertEqual(f["status"], "ok")
        first = f["evidence"]["samples"][0]
        self.assertAlmostEqual(first["converted_value"], 0.2)
        self.assertFalse(first["in_range"])

    def test_pressure_units_pa_kpa_bar(self):
        ev = cur_evidence(range={"min": 300, "max": 800, "unit": "Pa"},
                          sources=[{"device": "P1", "signal": "W"}])
        r = evaluate(evidence_payload(FB_EVENTS + [
            smp("P1", "W", 2500, 1, 0.5, "kPa"),   # 500 Pa
            smp("P1", "W", 3500, 2, 5.0, "mbar"),  # 500 Pa
            smp("P1", "W", 4500, 3, 500, "Pa")], ev))
        f = evidence_finding(r)
        self.assertEqual(f["status"], "ok")
        self.assertTrue(all(s["in_range"]
                            for s in f["evidence"]["samples"]))

    def test_cross_family_unit_inconclusive(self):
        # 电流通道上报 Pa：跨族无法换算 -> unknown，不判 fail
        r = evaluate(evidence_payload(FB_EVENTS + [
            smp("I1", "I", 2500, 1, 8.0, "Pa"),
            smp("I1", "I", 4000, 2, 8.0, "Pa")], cur_evidence()))
        f = evidence_finding(r)
        self.assertEqual(f["status"], "unknown")
        self.assertEqual(f["reason"], "evidence_inconclusive")
        self.assertEqual(f["evidence"]["sources"][0]["reason"],
                         "unit_not_convertible")
        self.assertEqual(r["status"], "unknown")

    def test_sample_gap_unknown(self):
        # 两点间隔 4000ms > 缺测容限 2000ms -> 采样断档 unknown
        r = evaluate(evidence_payload(FB_EVENTS + [
            smp("I1", "I", 2500, 1, 8.0, "A"),
            smp("I1", "I", 6500, 2, 8.0, "A")],
            cur_evidence(window_ms=6000)))
        f = evidence_finding(r)
        self.assertEqual(f["status"], "unknown")
        self.assertEqual(f["evidence"]["sources"][0]["reason"],
                         "evidence_sample_gap")
        self.assertEqual(
            f["evidence"]["sources"][0]["gap_segment_ms"], [2500, 6500])

    def test_log_gap_unknown(self):
        r = evaluate(evidence_payload(FB_EVENTS + [
            smp("I1", "I", 2500, 1, 8.0, "A"),
            smp("I1", "I", 4000, 4, 8.0, "A")], cur_evidence()))  # 缺 seq 2,3
        f = evidence_finding(r)
        self.assertEqual(f["status"], "unknown")
        self.assertEqual(f["evidence"]["sources"][0]["reason"], "log_gap")

    def test_open_window_insufficient_unknown(self):
        # 敞开窗口内只覆盖 200ms，达不到 duration -> unknown(window_open)
        r = evaluate(evidence_payload(FB_EVENTS + [
            smp("I1", "I", 2200, 1, 8.0, "A"),
            smp("I1", "I", 2400, 2, 8.0, "A")],
            cur_evidence(missing_ms=3000)))
        f = evidence_finding(r)
        self.assertEqual(f["status"], "unknown")
        self.assertEqual(f["evidence"]["sources"][0]["reason"],
                         "evidence_window_open")

    def test_all_any_k_of_n_combines(self):
        src = [
            {"device": "I1", "signal": "I", "range": CUR_RANGE},
            {"device": "P1", "signal": "W",
             "range": {"min": 300, "max": 800, "unit": "Pa"}},
            {"device": "V1", "signal": "POS",
             "range": {"min": 90, "max": 100, "unit": "%"}}]
        events = FB_EVENTS + [
            smp("I1", "I", 2200, 1, 8.0, "A"),
            smp("I1", "I", 4000, 2, 8.0, "A"),
            smp("P1", "W", 2500, 1, 500, "Pa"),
            smp("P1", "W", 4000, 2, 500, "Pa"),
            smp("V1", "POS", 2500, 1, 10, "%"),   # 阀位相斥
            smp("V1", "POS", 4000, 2, 10, "%")]
        # all：任一相斥即 fail
        r = evaluate(evidence_payload(
            events, cur_evidence(combine="all", sources=src)))
        self.assertEqual(evidence_finding(r)["status"], "fail")
        # k_of_n(2)：两支持一相斥，相斥仍优先 -> fail
        r = evaluate(evidence_payload(
            events, cur_evidence(combine="k_of_n", k=2, sources=src)))
        self.assertEqual(evidence_finding(r)["status"], "fail")
        # any + 去掉阀位源：一支持即 pass（风压未知设备删除）
        r = evaluate(evidence_payload(
            FB_EVENTS + [
                smp("I1", "I", 2200, 1, 8.0, "A"),
                smp("I1", "I", 4000, 2, 8.0, "A")],
            cur_evidence(combine="any", sources=[src[0]])))
        self.assertEqual(evidence_finding(r)["status"], "ok")

    def test_k_of_n_two_support_one_unknown(self):
        src = [
            {"device": "I1", "signal": "I", "range": CUR_RANGE},
            {"device": "P1", "signal": "W",
             "range": {"min": 300, "max": 800, "unit": "Pa"}},
            {"device": "V1", "signal": "POS",
             "range": {"min": 90, "max": 100, "unit": "%"}}]
        events = FB_EVENTS + [
            smp("I1", "I", 2200, 1, 8.0, "A"),
            smp("I1", "I", 4000, 2, 8.0, "A"),
            smp("P1", "W", 2500, 1, 500, "Pa"),
            smp("P1", "W", 4000, 2, 500, "Pa")]   # V1 无样本 -> unknown
        r = evaluate(evidence_payload(
            events, cur_evidence(combine="k_of_n", k=2,
                                 missing_ms=3000, sources=src)))
        self.assertEqual(evidence_finding(r)["status"], "ok")

    def test_per_source_overrides_defaults(self):
        # 顶层缺省 + 源级覆盖窗口：源窗口 2200..4200 被复位 3000 截断为
        # 2200..3000，唯一样本 2500 距起点仅 300ms -> 闭合窗口证据不足
        comp = {"all": [{"device": "D1", "signal": "alarm"}],
                "window_ms": 30000, "hold_ms": 0,
                "reset": {"device": "P9", "signal": "reset"}}
        ev = {"window_ms": 5000, "duration_ms": 1000, "missing_ms": 2000,
              "range": CUR_RANGE,
              "sources": [{"device": "I1", "signal": "I",
                           "window_ms": 2200}]}
        p = evidence_payload(FB_EVENTS + [
            cev("P9", "reset", 3000),
            smp("I1", "I", 2500, 1, 8.0, "A"),
            smp("I1", "I", 4500, 2, 8.0, "A")], ev)
        p["matrix"][0]["composite"] = comp
        del p["matrix"][0]["trigger"]
        r = evaluate(p)
        f = r["scenarios"][0]["instances"][0]["findings"][0]
        self.assertEqual(f["status"], "unknown")
        self.assertEqual(f["evidence"]["sources"][0]["reason"],
                         "evidence_insufficient")

    def test_shorthand_string_source(self):
        ev = cur_evidence(sources=["I1:I"])
        r = evaluate(evidence_payload(FB_EVENTS + [
            smp("I1", "I", 2500, 1, 8.0, "A"),
            smp("I1", "I", 4000, 2, 8.0, "A")], ev))
        self.assertEqual(evidence_finding(r)["status"], "ok")

    def test_invalid_evidence_structure_unknown(self):
        r = evaluate(evidence_payload(FB_EVENTS, {"sources": []}))
        f = evidence_finding(r)
        self.assertEqual(f["status"], "unknown")
        self.assertEqual(f["reason"], "invalid_evidence")
        self.assertTrue(f["evidence"]["rule"]["invalid"])

    def test_k_out_of_range_unknown(self):
        ev = cur_evidence(combine="k_of_n", k=3,
                          sources=[{"device": "I1", "signal": "I"}])
        r = evaluate(evidence_payload(FB_EVENTS, ev))
        f = evidence_finding(r)
        self.assertEqual(f["status"], "unknown")
        self.assertTrue(any(g["reason"] == "k_out_of_range"
                            for g in f["evidence"]["rule"]["invalid"]))

    def test_untrusted_evidence_clock_unknown(self):
        p = evidence_payload(FB_EVENTS + [
            smp("I1", "I", 2500, 1, 8.0, "A"),
            smp("I1", "I", 4000, 2, 8.0, "A")], cur_evidence())
        p["sync_pulses"] = [
            {"device": "D1", "device_ts": 0, "master_ts": 0},
            {"device": "F1", "device_ts": 0, "master_ts": 0},
            {"device": "I1", "device_ts": 0, "master_ts": 0},
            {"device": "I1", "device_ts": 100000, "master_ts": 100500}]
        r = evaluate(p)
        f = evidence_finding(r)
        self.assertEqual(f["status"], "unknown")
        self.assertEqual(f["evidence"]["sources"][0]["reason"],
                         "clock_residual_out_of_bounds")

    def test_normalized_rule_pinned_in_replay(self):
        r = evaluate(evidence_payload(FB_EVENTS + [
            smp("I1", "I", 2500, 1, 8.0, "A"),
            smp("I1", "I", 4000, 2, 8.0, "A")],
            cur_evidence(sources=["I1:I"])))
        plan = r["scenarios"][0]["evidence_rules"][0]
        self.assertEqual(plan["target"], "F1:start")
        self.assertTrue(plan["valid"])
        self.assertEqual(plan["rule"]["sources"][0]["device"], "I1")

    def test_evidence_bound_to_trigger_round(self):
        # 复合触发两轮：佐证窗口被全局复位截断，不把下一轮样本挂到本轮
        devs = {"D1": {"type": "smoke"}, "P9": {"type": "panel"},
                "F1": {"type": "fan"}, "I1": {"type": "ammeter"}}
        comp = {"all": [{"device": "D1", "signal": "alarm"}],
                "window_ms": 30000, "hold_ms": 0,
                "reset": {"device": "P9", "signal": "reset"}}
        ev = cur_evidence(window_ms=8000)
        p = {
            "devices": devs, "aliases": {},
            "matrix": [{"id": "C", "composite": comp,
                        "respond": [{"device": "F1", "signal": "start",
                                     "within_ms": 60000, "evidence": ev}]}],
            "sync_pulses": [{"device": d, "device_ts": 0, "master_ts": 0}
                            for d in devs],
            "events": [
                cev("D1", "alarm", 1000), cev("F1", "start", 2000),
                smp("I1", "I", 2200, 1, 8.0, "A"),
                cev("P9", "reset", 3000),                # 截断本轮佐证窗口
                cev("D1", "alarm", 20000, 2),
                cev("F1", "start", 21000, 2),
                smp("I1", "I", 21500, 2, 8.0, "A"),
                smp("I1", "I", 23000, 3, 8.0, "A")]}
        r = evaluate(p)
        insts = r["scenarios"][0]["instances"]
        self.assertEqual([i["trigger_ts"] for i in insts], [1000, 20000])
        f1, f2 = (i["findings"][0] for i in insts)
        # 第一轮窗口在 3000 截断：2200 一个在范围样本，覆盖到复位仅 800ms
        # < duration，证据不足 unknown；下一轮 21500 的样本位于 cap 之后，
        # 不得越过实例边界拿来给本轮算断档/作证；第二轮样本齐 -> ok
        self.assertEqual(f1["status"], "unknown")
        self.assertEqual(f1["evidence"]["sources"][0]["reason"],
                         "evidence_insufficient")
        self.assertEqual(f2["status"], "ok")

    def test_single_out_of_range_sample_not_extrapolated_to_fail(self):
        # 反例一(a)：单个越界样本无法覆盖要求的持续时间 -> unknown，
        # 不得把越界状态外推到窗口末端判 fail（触点只报了一次 0A，
        # 其后电流可能已正常建立）。
        ev = cur_evidence(duration_ms=2000, missing_ms=3000)
        r = evaluate(evidence_payload(FB_EVENTS + [
            smp("I1", "I", 2500, 1, 0.0, "A")], ev))   # 仅一条越界样本
        f = evidence_finding(r)
        self.assertEqual(f["status"], "unknown")
        self.assertEqual(f["reason"], "evidence_inconclusive")
        self.assertEqual(f["evidence"]["status"], "unknown")
        self.assertNotEqual(f.get("evidence_type"), "evidence_contradiction")
        src = f["evidence"]["sources"][0]
        self.assertEqual(src["status"], "unknown")
        self.assertEqual(src["reason"], "evidence_window_open")
        self.assertIsNone(f["evidence"].get("divergence"))
        self.assertEqual(r["status"], "unknown")

    def test_sample_gap_not_extrapolated_to_fail(self):
        # 反例一(b)：样本间隔超过 missing_ms，即便每个越界样本都在范围外，
        # 缺口里可能藏在范围样本 -> unknown，不得跨断档外推判 fail。
        ev = cur_evidence(duration_ms=500, missing_ms=1000)
        r = evaluate(evidence_payload(FB_EVENTS + [
            smp("I1", "I", 2500, 1, 0.0, "A"),
            smp("I1", "I", 4000, 2, 0.0, "A")], ev))  # 间隔 1500 > 容限
        f = evidence_finding(r)
        self.assertEqual(f["status"], "unknown")
        src = f["evidence"]["sources"][0]
        self.assertEqual(src["reason"], "evidence_sample_gap")
        self.assertEqual(src["gap_segment_ms"], [2500, 4000])
        self.assertNotEqual(f.get("evidence_type"), "evidence_contradiction")
        self.assertEqual(r["status"], "unknown")

    def test_post_reset_feedback_not_bound_to_old_round(self):
        # 反例二：反馈（respond 未声明 within_ms）在全局复位之后才到，
        # 不得挂到旧触发实例；应只属于复位后的下一轮。
        devs = {"D1": {"type": "smoke"}, "P9": {"type": "panel"},
                "F1": {"type": "fan"}, "I1": {"type": "ammeter"}}
        comp = {"all": [{"device": "D1", "signal": "alarm"}],
                "window_ms": 30000, "hold_ms": 0,
                "reset": {"device": "P9", "signal": "reset"}}
        # 不给 within_ms：旧实现只按触发时限取反馈，复位后的 start 会被
        # 误挂到第一轮；修复后第一轮应 timeout（复位前无反馈）。
        resp = {"device": "F1", "signal": "start",
                "evidence": cur_evidence(window_ms=8000)}
        p = {
            "devices": devs, "aliases": {},
            "matrix": [{"id": "C", "composite": comp, "respond": [resp]}],
            "sync_pulses": [{"device": d, "device_ts": 0, "master_ts": 0}
                            for d in devs],
            "events": [
                cev("D1", "alarm", 1000),
                cev("P9", "reset", 5000),            # 第一轮结束
                cev("F1", "start", 21000, 1),        # 复位后的反馈
                cev("D1", "alarm", 20000, 2),
                smp("I1", "I", 21500, 1, 8.0, "A"),
                smp("I1", "I", 23000, 2, 8.0, "A")]}
        r = evaluate(p)
        insts = r["scenarios"][0]["instances"]
        self.assertEqual([i["trigger_ts"] for i in insts], [1000, 20000])
        f1, f2 = (i["findings"][0] for i in insts)
        self.assertEqual(f1["status"], "fail")
        self.assertEqual(f1["type"], "timeout")
        self.assertNotIn("evidence", f1)
        self.assertEqual(f2["status"], "ok")
        self.assertEqual(f2["actual"], 21000)


class LegacySingleTriggerCompatTest(unittest.TestCase):
    """旧单 trigger 请求在复合引擎上线后行为不变。"""

    def test_single_trigger_still_works(self):
        p = base_payload(
            matrix=[{"id": "S",
                     "trigger": {"device": "TD", "signal": "alarm"},
                     "respond": [{"device": "R1", "signal": "open",
                                  "within_ms": 30000}]}],
            events=[{"device": "TD", "seq": 1, "signal": "alarm",
                     "device_ts": 1000},
                    {"device": "R1", "seq": 1, "signal": "open",
                     "device_ts": 2000}])
        r = evaluate(p)
        sc = r["scenarios"][0]
        self.assertNotIn("composite", sc)       # 旧形态不带 composite
        self.assertEqual(len(sc["instances"]), 1)
        self.assertEqual(sc["instances"][0]["status"], "pass")


# ---------------------------------------------------------------- 主备切换

FO_DEVS = {"D1": {"type": "smoke"}, "F1": {"type": "fan"},
           "FB": {"type": "fan"}, "C1": {"type": "controller"}}


def fo_pulses():
    return [{"device": d, "device_ts": 0, "master_ts": 0}
            for d in FO_DEVS]


def fo_ev(d, sig, ts, seq=1):
    return {"device": d, "seq": seq, "signal": sig, "device_ts": ts}


FO_RULE = {"fault": "C1:trip", "standby": "FB:start",
           "switch_wait_ms": 5000, "total_ms": 30000,
           "parallel_ms": 2000}


def fo_payload(events, rule=None, composite=False, **kw):
    """主备链载荷：D1 报警 -> F1 start(60s) 声明 failover。"""
    fo = dict(rule or FO_RULE)
    respond = [{"device": "F1", "signal": "start",
                "within_ms": 60000, "failover": fo}]
    if composite:
        trigger = {"composite": {
            "all": [{"device": "D1", "signal": "alarm"}],
            "window_ms": 30000, "hold_ms": 0,
            "reset": {"device": "P9", "signal": "reset"}}}
    else:
        trigger = {"trigger": {"device": "D1", "signal": "alarm"}}
    p = {"sync_tolerance_ms": 150, "devices": dict(FO_DEVS),
         "aliases": {},
         "matrix": [dict(id="S", respond=respond, **trigger)],
         "sync_pulses": fo_pulses(), "events": events}
    p.update(kw)
    return p


def fo_block(result):
    return result["scenarios"][0]["instances"][0]["failover"][0]


class FailoverPrimarySuccessTest(unittest.TestCase):
    """主机成功：无故障、无切换，闭合窗口后判 primary_success/pass。"""

    def test_primary_success_closed_window(self):
        # C1 在时限后仍有日志 -> 窗口闭合，主机正常启动，无 trip、无备机
        r = evaluate(fo_payload([
            fo_ev("D1", "alarm", 1000), fo_ev("F1", "start", 2000),
            fo_ev("C1", "run", 35000)]))
        b = fo_block(r)
        self.assertEqual(b["outcome"], "primary_success")
        self.assertEqual(b["status"], "ok")
        self.assertEqual(r["status"], "pass")

    def test_open_window_no_fault_keeps_unknown(self):
        # 敞开窗口：日志末端在时限内，后续可能补来跳闸 -> 不臆断成功
        r = evaluate(fo_payload([
            fo_ev("D1", "alarm", 1000), fo_ev("F1", "start", 2000)]))
        b = fo_block(r)
        self.assertIsNone(b["outcome"])
        self.assertEqual(b["status"], "unknown")
        self.assertEqual(b["findings"][-1]["reason"],
                         "failover_window_open")
        self.assertEqual(r["status"], "unknown")


class FailoverLegalSwitchTest(unittest.TestCase):
    """合法切换：故障坐实 -> 等满切换等待 -> 总时限内启备机。"""

    EVENTS = [fo_ev("D1", "alarm", 1000), fo_ev("C1", "trip", 4000),
              fo_ev("FB", "start", 9000)]

    def test_legal_switch_overrides_primary_timeout(self):
        r = evaluate(fo_payload(self.EVENTS))
        b = fo_block(r)
        self.assertEqual(b["outcome"], "legal_switch")
        self.assertEqual(b["status"], "ok")
        # 主机自身的 timeout fail 被合法切换覆盖，整体 pass
        self.assertEqual(r["status"], "pass")
        to = [f for sc in r["scenarios"] for i in sc["instances"]
              for f in i["findings"] if f.get("type") == "timeout"][0]
        self.assertEqual(to.get("superseded_by"), "failover")
        self.assertNotIn("first", to)
        f = [f for f in b["findings"]
             if f["reason"] == "legal_switch"][0]
        self.assertEqual(f["elapsed_ms"], 5000)      # 故障->备启恰等满
        self.assertEqual(f["total_elapsed_ms"], 8000)
        self.assertEqual(f["fault"]["device"], "C1")
        self.assertEqual(f["standby"]["device"], "FB")

    def test_legal_switch_under_composite_trigger(self):
        r = evaluate(fo_payload(
            self.EVENTS + [fo_ev("P9", "reset", 90000)], composite=True))
        b = fo_block(r)
        self.assertEqual(b["outcome"], "legal_switch")
        self.assertEqual(r["status"], "pass")

    def test_switch_before_wait_is_spurious(self):
        # 故障 4000，等待 5000，备机 6000（仅 2s）-> 过早切换
        r = evaluate(fo_payload([
            fo_ev("D1", "alarm", 1000), fo_ev("C1", "trip", 4000),
            fo_ev("FB", "start", 6000)]))
        b = fo_block(r)
        self.assertEqual(b["outcome"], "spurious_switch")
        self.assertEqual(b["findings"][-1]["reason"],
                         "switch_before_wait")
        self.assertEqual(r["status"], "fail")

    def test_fault_after_standby_does_not_authorize(self):
        # 备机 9s 先启，故障 12s 才到：切换时故障未坐实，不构成合法切换
        r = evaluate(fo_payload([
            fo_ev("D1", "alarm", 1000), fo_ev("FB", "start", 9000),
            fo_ev("C1", "trip", 12000), fo_ev("C1", "run", 40000)]))
        b = fo_block(r)
        self.assertEqual(b["outcome"], "spurious_switch")
        self.assertEqual(r["status"], "fail")


class FailoverStandbyTimeoutTest(unittest.TestCase):
    """故障坐实但总时限内未启备用 -> standby_timeout/fail。"""

    def test_standby_timeout_after_fault(self):
        r = evaluate(fo_payload([
            fo_ev("D1", "alarm", 1000), fo_ev("C1", "trip", 4000),
            fo_ev("FB", "start", 40000)]))             # 超过 30s 总时限
        b = fo_block(r)
        self.assertEqual(b["outcome"], "standby_timeout")
        self.assertEqual(b["status"], "fail")
        self.assertEqual(b["findings"][-1]["deadline"], 31000)
        self.assertEqual(r["status"], "fail")

    def test_open_window_no_fault_no_standby_unknown(self):
        r = evaluate(fo_payload([fo_ev("D1", "alarm", 1000)]))
        b = fo_block(r)
        self.assertEqual(b["status"], "unknown")
        self.assertEqual(b["findings"][-1]["reason"],
                         "failover_window_open")
        self.assertEqual(r["status"], "unknown")

    def test_contradiction_primary_dead_closed_without_switch(self):
        # 主机反馈到但佐证相斥（跳闸坐实），窗口闭合仍无备机 -> 未切换 fail
        ev = {"combine": "all", "window_ms": 8000,
              "duration_ms": 1000, "missing_ms": 3000,
              "sources": [{"device": "I1", "signal": "I",
                           "range": {"min": 5, "max": 20, "unit": "A"}}]}
        p = fo_payload([
            fo_ev("D1", "alarm", 1000), fo_ev("F1", "start", 2000),
            {"device": "I1", "seq": 1, "signal": "I",
             "device_ts": 2200, "value": 0.0, "unit": "A"},
            {"device": "I1", "seq": 2, "signal": "I",
             "device_ts": 4000, "value": 0.0, "unit": "A"},
            fo_ev("C1", "run", 40000)], rule=FO_RULE)
        p["devices"]["I1"] = {"type": "ammeter"}
        p["sync_pulses"] = [{"device": d, "device_ts": 0, "master_ts": 0}
                            for d in p["devices"]]
        p["matrix"][0]["respond"][0]["evidence"] = ev
        r = evaluate(p)
        b = fo_block(r)
        self.assertEqual(b["status"], "fail")
        self.assertEqual(b["outcome"], "standby_timeout")


class FailoverSpuriousParallelTest(unittest.TestCase):
    """误切换（主机只是反馈迟到）与双机超时并行。"""

    def test_short_parallel_is_spurious_switch(self):
        # 备机 3s 启（无故障），主机 4s 迟到，并行 1s <= 2s 宽限
        r = evaluate(fo_payload([
            fo_ev("D1", "alarm", 1000), fo_ev("FB", "start", 3000),
            fo_ev("F1", "start", 4000), fo_ev("C1", "run", 35000)]))
        b = fo_block(r)
        self.assertEqual(b["outcome"], "spurious_switch")
        self.assertEqual(b["findings"][-1]["reason"], "spurious_switch")
        self.assertEqual(r["status"], "fail")

    def test_long_parallel_is_overrun(self):
        # 备机 3s 启，主机 12s 才到，并行 9s > 2s -> 双机超时并行
        r = evaluate(fo_payload([
            fo_ev("D1", "alarm", 1000), fo_ev("FB", "start", 3000),
            fo_ev("F1", "start", 12000), fo_ev("C1", "run", 35000)]))
        b = fo_block(r)
        self.assertEqual(b["outcome"], "parallel_overrun")
        f = b["findings"][-1]
        self.assertEqual(f["reason"], "parallel_overrun")
        self.assertEqual(f["parallel_from"], 3000)
        self.assertEqual(f["parallel_to"], 12000)
        self.assertEqual(f["over_ms"], 7000)
        self.assertEqual(r["status"], "fail")

    def test_late_fault_overrun(self):
        # 备机 3s 启，故障 10s 才报：并行持续 7s > 2s -> 超时并行
        r = evaluate(fo_payload([
            fo_ev("D1", "alarm", 1000), fo_ev("FB", "start", 3000),
            fo_ev("C1", "trip", 10000), fo_ev("C1", "run", 35000)]))
        b = fo_block(r)
        self.assertEqual(b["outcome"], "parallel_overrun")


class FailoverEvidenceTest(unittest.TestCase):
    """主机运行佐证参与并行判定；佐证未决保持 unknown。"""

    EVIDENCE = {"combine": "all", "window_ms": 15000,
                "duration_ms": 1000, "missing_ms": 3000,
                "sources": [{"device": "I1", "signal": "I",
                             "range": {"min": 5, "max": 20, "unit": "A"}}]}

    def _p(self, events, composite=True):
        devs = dict(FO_DEVS)
        devs["I1"] = {"type": "ammeter"}
        if composite:
            devs["P9"] = {"type": "panel"}
        fo = dict(FO_RULE)
        respond = [{"device": "F1", "signal": "start",
                    "within_ms": 60000, "failover": fo,
                    "evidence": self.EVIDENCE}]
        trig = ({"composite": {"all": [{"device": "D1", "signal": "alarm"}],
                               "window_ms": 30000, "hold_ms": 0,
                               "reset": {"device": "P9",
                                         "signal": "reset"}}}
                if composite else
                {"trigger": {"device": "D1", "signal": "alarm"}})
        return {"devices": devs, "aliases": {},
                "matrix": [dict(id="S", respond=respond, **trig)],
                "sync_pulses": [{"device": d, "device_ts": 0,
                                 "master_ts": 0} for d in devs],
                "events": events}

    def smp(self, ts, seq, v):
        return {"device": "I1", "seq": seq, "signal": "I",
                "device_ts": ts, "value": v, "unit": "A"}

    def test_running_evidence_overrun_open_window(self):
        # 备机 3s 启（无故障），主机反馈 3.5s，电流 8A 持续到 8s，
        # 敞开窗口下只认已观测：并行 3.5..8 > 2s -> 超时并行
        r = evaluate(self._p([
            fo_ev("D1", "alarm", 1000), fo_ev("FB", "start", 3000),
            fo_ev("F1", "start", 3500),
            self.smp(3600, 1, 8.0), self.smp(5000, 2, 8.0),
            self.smp(8000, 3, 8.0), fo_ev("C1", "run", 40000)]))
        b = fo_block(r)
        self.assertEqual(b["outcome"], "parallel_overrun")
        self.assertEqual(r["status"], "fail")

    def test_running_evidence_overrun_closed_cap(self):
        # 闭合窗口（P9 10s 复位）：样本持续在范围到 9s，允许插值到 cap
        r = evaluate(self._p([
            fo_ev("D1", "alarm", 1000), fo_ev("FB", "start", 3000),
            fo_ev("F1", "start", 3500),
            self.smp(3600, 1, 8.0), self.smp(6000, 2, 8.0),
            self.smp(9000, 3, 8.0), fo_ev("P9", "reset", 10000)]))
        b = fo_block(r)
        self.assertEqual(b["outcome"], "parallel_overrun")

    def test_evidence_pending_keeps_unknown(self):
        # 备机 3s 启，主机反馈 3.5s，但佐证窗口内只有两条样本且敞开、
        # 覆盖不足 -> 主机佐证未决，不臆断并行 -> unknown
        r = evaluate(self._p([
            fo_ev("D1", "alarm", 1000), fo_ev("FB", "start", 3000),
            fo_ev("F1", "start", 3500),
            self.smp(3600, 1, 8.0), self.smp(4000, 2, 8.0),
            fo_ev("C1", "run", 40000)]))
        b = fo_block(r)
        self.assertEqual(b["status"], "unknown")
        self.assertIsNone(b["outcome"])
        self.assertEqual(b["findings"][-1]["reason"],
                         "primary_evidence_pending")
        self.assertEqual(r["status"], "unknown")

    def test_dead_primary_legal_switch_ignores_contradiction_window(self):
        # 故障 4s 坐实，佐证相斥区间在宽限内结束，备机 9s 合法切换
        r = evaluate(self._p([
            fo_ev("D1", "alarm", 1000), fo_ev("F1", "start", 2000),
            self.smp(2200, 1, 0.0), self.smp(3000, 2, 0.0),
            fo_ev("C1", "trip", 4000), fo_ev("FB", "start", 9000),
            fo_ev("P9", "reset", 90000)], composite=True))
        b = fo_block(r)
        self.assertEqual(b["outcome"], "legal_switch")
        self.assertEqual(r["status"], "pass")


class FailoverGapClockTest(unittest.TestCase):
    """故障日志缺号 / 时钟不可信 / 备用缺号 -> unknown。"""

    def test_fault_log_gap_unknown(self):
        r = evaluate(fo_payload([
            fo_ev("D1", "alarm", 1000), fo_ev("C1", "run", 3000),
            {"device": "C1", "seq": 3, "signal": "trip",
             "device_ts": 4000},                        # C1 缺 seq=2
            fo_ev("FB", "start", 10000)]))
        b = fo_block(r)
        self.assertEqual(b["status"], "unknown")
        self.assertEqual(b["findings"][-1]["reason"], "log_gap")
        self.assertEqual(r["status"], "unknown")        # 主机 timeout 被遮蔽

    def test_fault_clock_untrusted_unknown(self):
        p = fo_payload([
            fo_ev("D1", "alarm", 1000), fo_ev("C1", "trip", 4000),
            fo_ev("FB", "start", 10000)])
        p["sync_pulses"].append(
            {"device": "C1", "device_ts": 100000, "master_ts": 100500})
        r = evaluate(p)
        b = fo_block(r)
        self.assertEqual(b["status"], "unknown")
        self.assertEqual(b["findings"][-1]["reason"],
                         "clock_residual_out_of_bounds")
        self.assertEqual(r["status"], "unknown")

    def test_standby_log_gap_unknown(self):
        r = evaluate(fo_payload([
            fo_ev("D1", "alarm", 1000), fo_ev("C1", "trip", 4000),
            {"device": "FB", "seq": 3, "signal": "start",
             "device_ts": 10000}]))                      # FB 缺 seq=1,2
        b = fo_block(r)
        self.assertEqual(b["status"], "unknown")
        self.assertEqual(b["findings"][-1]["reason"], "log_gap")

    def test_trigger_unconfirmed_unknown(self):
        # 复合触发未确认：实例 trigger_ts=None -> 主备链 unknown
        p = fo_payload([fo_ev("D1", "run", 1000)], composite=True)
        r = evaluate(p)
        insts = r["scenarios"][0]["instances"]
        self.assertTrue(any(
            any(b["status"] == "unknown"
                and b["findings"][-1]["reason"] == "trigger_not_confirmed"
                for b in i.get("failover", []))
            for i in insts))


class FailoverStructureTest(unittest.TestCase):
    """结构非法 / primary 不一致 / 备用归属多解 -> unknown 并列非法说明。"""

    def test_missing_fields_invalid(self):
        r = evaluate(fo_payload(
            [fo_ev("D1", "alarm", 1000)],
            rule={"fault": "C1:trip", "standby": "FB:start"}))
        plan = r["scenarios"][0]["failover_rules"][0]
        self.assertFalse(plan["valid"])
        reasons = {g["reason"] for g in plan["rule"]["invalid"]}
        self.assertIn("invalid_failover", reasons)
        b = fo_block(r)
        self.assertEqual(b["status"], "unknown")

    def test_primary_mismatch_invalid(self):
        r = evaluate(fo_payload(
            [fo_ev("D1", "alarm", 1000)],
            rule={"primary": "OTHER:start", "fault": "C1:trip",
                  "standby": "FB:start", "switch_wait_ms": 5000,
                  "total_ms": 30000, "parallel_ms": 2000}))
        plan = r["scenarios"][0]["failover_rules"][0]
        self.assertFalse(plan["valid"])
        self.assertTrue(any(
            g["reason"] == "failover_primary_mismatch"
            for g in plan["rule"]["invalid"]))

    def test_ambiguous_fault_alias_invalid(self):
        p = fo_payload([fo_ev("D1", "alarm", 1000)],
                       rule={"fault": "控制器:trip", "standby": "FB:start",
                             "switch_wait_ms": 5000, "total_ms": 30000,
                             "parallel_ms": 2000})
        p["aliases"] = {"控制器": ["C1", "FB"]}
        r = evaluate(p)
        plan = r["scenarios"][0]["failover_rules"][0]
        self.assertFalse(plan["valid"])
        self.assertTrue(any(g["reason"] == "alias_ambiguous"
                            for g in plan["rule"]["invalid"]))

    def test_shared_group_ambiguous_standby(self):
        # 同组两条规则 standby 端点不同 -> 备用归属多解，两边都 unknown
        devs = dict(FO_DEVS)
        devs["D2"] = {"type": "smoke"}
        pulses = [{"device": d, "device_ts": 0, "master_ts": 0}
                  for d in devs]

        def resp(standby):
            return {"device": "F1", "signal": "start", "within_ms": 60000,
                    "failover": {"fault": "C1:trip", "standby": standby,
                                 "group": "G1", "switch_wait_ms": 5000,
                                 "total_ms": 30000, "parallel_ms": 2000}}
        p = {"devices": devs, "aliases": {}, "sync_pulses": pulses,
             "matrix": [
                 {"id": "A", "trigger": {"device": "D1", "signal": "alarm"},
                  "respond": [resp("FB:start")]},
                 {"id": "B", "trigger": {"device": "D2", "signal": "alarm"},
                  "respond": [resp("F1:start")]}],
             "events": [fo_ev("D1", "alarm", 1000),
                        fo_ev("D2", "alarm", 2000)]}
        r = evaluate(p)
        for sc in r["scenarios"]:
            plan = sc["failover_rules"][0]
            self.assertFalse(plan["valid"])
            self.assertTrue(any(
                g["reason"] == "standby_group_ambiguous"
                for g in plan["rule"]["invalid"]))


class FailoverContentionTest(unittest.TestCase):
    """多分区争用同一备用机：按触发时刻独占，先触发先得。"""

    DEVS = {"DA": {}, "DB": {}, "FA": {}, "FB": {}, "CA": {}, "CB": {}}

    def _payload(self):
        def fo(fault):
            return {"fault": fault, "standby": "FB:start", "group": "G1",
                    "switch_wait_ms": 5000, "total_ms": 30000,
                    "parallel_ms": 2000}
        return {"devices": dict(self.DEVS), "aliases": {},
                "sync_pulses": [{"device": d, "device_ts": 0,
                                 "master_ts": 0} for d in self.DEVS],
                "matrix": [
                    {"id": "A", "trigger": {"device": "DA",
                                            "signal": "alarm"},
                     "respond": [{"device": "FA", "signal": "start",
                                  "within_ms": 60000,
                                  "failover": fo("CA:trip")}]},
                    {"id": "B", "trigger": {"device": "DB",
                                            "signal": "alarm"},
                     "respond": [{"device": "FA", "signal": "start",
                                  "within_ms": 60000,
                                  "failover": fo("CB:trip")}]}]}

    def test_earlier_trigger_occupies_standby(self):
        p = self._payload()
        p["events"] = [
            fo_ev("DA", "alarm", 1000), fo_ev("CA", "trip", 3000),
            fo_ev("DB", "alarm", 5000), fo_ev("CB", "trip", 7000),
            fo_ev("FB", "start", 9000),                     # 归 A
            fo_ev("CA", "run", 40000), fo_ev("CB", "run", 40000),
            fo_ev("FB", "start", 45000, 2)]                 # B 窗口外
        r = evaluate(p)
        blocks = {}
        for sc in r["scenarios"]:
            for inst in sc["instances"]:
                blocks[sc["id"]] = inst["failover"][0]
        self.assertEqual(blocks["A"]["outcome"], "legal_switch")
        self.assertEqual(blocks["B"]["status"], "fail")
        self.assertEqual(blocks["B"]["outcome"], "standby_timeout")
        f = blocks["B"]["findings"][-1]
        self.assertEqual(f["reason"], "standby_occupied")
        self.assertEqual(f["occupied_by"]["rule"], "A")
        self.assertEqual(f["occupied_by"]["trigger_ts"], 1000)

    def test_distinct_standby_events_serve_both(self):
        # B 的窗口内还有第二条空闲备用启动 -> B 也能合法切换
        p = self._payload()
        p["events"] = [
            fo_ev("DA", "alarm", 1000), fo_ev("CA", "trip", 3000),
            fo_ev("DB", "alarm", 5000), fo_ev("CB", "trip", 13000),
            fo_ev("FB", "start", 9000),                      # 归 A
            fo_ev("FB", "start", 20000, 2),                  # 归 B（<35s）
            fo_ev("CA", "run", 40000), fo_ev("CB", "run", 40000)]
        r = evaluate(p)
        blocks = {}
        for sc in r["scenarios"]:
            for inst in sc["instances"]:
                blocks[sc["id"]] = inst["failover"][0]
        self.assertEqual(blocks["A"]["outcome"], "legal_switch")
        self.assertEqual(blocks["B"]["outcome"], "legal_switch")
        self.assertNotEqual(
            blocks["A"]["findings"][-1]["standby"]["seq"],
            blocks["B"]["findings"][-1]["standby"]["seq"])


class FailoverRevisionHttpTest(unittest.TestCase):
    """主备规则纳入修订/重放/差异/签结；旧版判定保持不变。"""

    @classmethod
    def setUpClass(cls):
        cls.httpd = make_server("127.0.0.1", PORT + 2, make_app(":memory:"))
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def call(self, method, path, body=None):
        data = json.dumps(body, ensure_ascii=False).encode() \
            if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT + 2}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_revision_pins_choice_chain_and_diff(self):
        # rev1：故障后等待参数过严（等待 8s），备机 9s 启 -> 过早切换 fail
        p1 = fo_payload([
            fo_ev("D1", "alarm", 1000), fo_ev("C1", "trip", 4000),
            fo_ev("FB", "start", 9000)],
            rule={"fault": "C1:trip", "standby": "FB:start",
                  "switch_wait_ms": 8000, "total_ms": 30000,
                  "parallel_ms": 2000})
        code, o = self.call("POST", "/projects", p1)
        self.assertEqual((code, o["status"]), (201, "fail"))
        pid = o["project"]
        # rev2：等待改回 5s（附依据）-> 合法切换 pass
        p2 = json.loads(json.dumps(p1))
        p2["matrix"][0]["respond"][0]["failover"]["switch_wait_ms"] = 5000
        code, _ = self.call("POST", f"/projects/{pid}/revisions",
                            {"payload": p2})
        self.assertEqual(code, 400)                    # 缺 justification
        code, o = self.call("POST", f"/projects/{pid}/revisions",
                            {"justification": "按验收现场整定单把切换等待"
                                              "由 8s 改为 5s",
                             "payload": p2})
        self.assertEqual((code, o["rev"], o["status"]), (201, 2, "pass"))
        # 旧版重放：选择链与采用事件固定，仍为 fail
        code, o = self.call("GET", f"/projects/{pid}/revisions/1")
        b = (o["replay"]["scenarios"][0]["instances"][0]
             ["failover"][0])
        self.assertEqual(b["rule"]["switch_wait_ms"], 8000)
        self.assertEqual(b["outcome"], "spurious_switch")
        # 新版
        code, o = self.call("GET", f"/projects/{pid}/revisions/2")
        b = (o["replay"]["scenarios"][0]["instances"][0]
             ["failover"][0])
        self.assertEqual(b["outcome"], "legal_switch")
        # diff：主备规则与结论差异
        code, o = self.call("GET", f"/projects/{pid}/diff?from=1&to=2")
        ch = o["failover_changes"][0]
        self.assertTrue(ch["rule_changed"])
        self.assertEqual(ch["rule_from"]["switch_wait_ms"], 8000)
        self.assertEqual(ch["rule_to"]["switch_wait_ms"], 5000)
        self.assertEqual(ch["verdict_from"]["outcome"], "spurious_switch")
        self.assertEqual(ch["verdict_to"]["outcome"], "legal_switch")
        # 签结后冻结
        code, _ = self.call("POST", f"/projects/{pid}/signoff")
        self.assertEqual(code, 200)
        code, _ = self.call("POST", f"/projects/{pid}/revisions",
                            {"justification": "签结后改主备参数",
                             "payload": p2})
        self.assertEqual(code, 409)


class FailoverBackwardCompatTest(unittest.TestCase):
    """未声明 failover 的旧请求演算结果完全不变。"""

    def test_no_failover_key_in_replay(self):
        p = base_payload(
            matrix=[{"id": "S",
                     "trigger": {"device": "TD", "signal": "alarm"},
                     "respond": [{"device": "R2", "signal": "start",
                                  "within_ms": 60000}]}],
            events=[{"device": "TD", "seq": 1, "signal": "alarm",
                     "device_ts": 1000},
                    {"device": "R2", "seq": 1, "signal": "start",
                     "device_ts": 2000}])
        r = evaluate(p)
        sc = r["scenarios"][0]
        self.assertNotIn("failover_rules", sc)
        self.assertNotIn("failover", sc["instances"][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
