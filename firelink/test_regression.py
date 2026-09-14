# -*- coding: utf-8 -*-
"""
回归测试：边界组合
  1. after 引用多解/无解别名 -> 已出现的响应保持 unknown，不判 out_of_order/fail
  2. 触发设备校时残差越界 -> 下游环节全部 unknown，不再计算 timeout
  3. seq 缺口可能遗漏 bypass_off -> 旁路 unknown，不判 bypass_not_reset/fail
  4. mutex / bypass finding 均带 upstream
  5. 既有三组模拟行为不变；修订/重放/差异/签结流程不变
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
