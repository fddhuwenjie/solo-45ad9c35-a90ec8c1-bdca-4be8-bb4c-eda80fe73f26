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


if __name__ == "__main__":
    unittest.main(verbosity=2)
