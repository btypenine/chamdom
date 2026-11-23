"""바이낸스 엔진 '대응 명령'(대시보드 버튼)·승인/요청 연결 오프라인 시험

- 네트워크 없음: 거래소(Live)·시세·웹페이지(Cloud)를 가짜로 바꿔 실제 엔진 코드(bot/binance_bot.py)를 그대로 돌림
- 실행: python -m unittest tests/test_binance_cockpit.py -v   (저장소 최상위에서)
"""
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bot"))
import binance_bot as bb  # noqa: E402
import chamdom_bot as cb  # noqa: E402


class FakeLive:
    def __init__(self, px):
        self.px, self.offset, self.filters = px, 0, {"X": {}}
        self.orders, self.cancelled, self.stops = [], [], []
        self.fail_market, self.fail_stop = set(), False

    def market(self, sym, side, qty, reduce=False):
        if sym in self.fail_market:
            raise RuntimeError("바이낸스 /fapi/v1/order 실패 400 코드 -1001: 일시 오류")
        self.orders.append((sym, side, qty, reduce))
        return self.px[sym], qty

    def cancel_stops(self, sym):
        self.cancelled.append(sym)

    def stop_order(self, sym, side, trig):
        if self.fail_stop:
            raise RuntimeError("stop 실패")
        self.stops.append((sym, side, trig))
        return len(self.stops)

    def round_price(self, sym, px, up):
        return px

    def positions(self):
        return {}


class FakeCloud:
    uid = "u1"

    def __init__(self):
        self.inbox, self.sugs = {}, []

    def _auth(self):
        return {}

    def inbox_items(self):
        return dict(self.inbox)

    def suggestions(self):
        return list(self.sugs)


def jload(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def pos(side, entry, usdt=50.0, rid="r1", **kw):
    long_ = side == "long"
    p = {"side": side, "entry": entry, "tp": entry * (1.031 if long_ else 0.969), "sl": entry * (0.95 if long_ else 1.05),
         "usdt": usdt, "qty": usdt / entry, "live": True, "at": int(time.time() * 1000) - 3600_000,
         "deadline": int(time.time()) + 36000, "rid": rid, "desc": "시험", "maxFav": 0.0, "p": 0.6, "stopId": 7,
         "stopPx": entry * (0.945 if long_ else 1.055)}
    p.update(kw)
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self._keep = (bb.STATE_FILE, bb.LOG_FILE, bb.prices, bb.BASE_DIR, cb.REPORT_DIR)
        bb.STATE_FILE, bb.LOG_FILE, bb.BASE_DIR = os.path.join(d, "binance_state.json"), os.path.join(d, "binance.log"), d
        cb.REPORT_DIR = os.path.join(d, "report")
        self.px = {"AAAUSDT": 1.02, "BBBUSDT": 2.0, "CCCUSDT": 0.97}
        bb.prices = lambda syms: {s: self.px[s] for s in syms if s in self.px}
        e = object.__new__(bb.Engine)
        e.state = bb.load_state()
        e.state.setdefault("liveLedger", {"trades": 0, "wins": 0, "pnlUsdt": 0.0, "day": "", "dayPnl": 0.0, "dayTrades": 0})
        e.cfg = {"ROLE": "owner"}
        e.live, e.live_new, e.cloud = FakeLive(self.px), True, FakeCloud()
        e.order_usdt, e.daily_loss, e.month_loss, e.max_live, e.live_basis = 50.0, 100.0, 100.0, 10, "all"
        e.last_status, e._last_exchange_check = time.time(), time.time()
        e.uploads = 0

        def upload(force=False):
            e.uploads += 1
        e.upload = upload
        self.cmds = {}
        e.dash_fields = lambda: {"cmds": dict(self.cmds)}
        e.state["ruleBook"]["r1"] = {"id": "r1", "symbol": "AAAUSDT", "side": "long", "desc": "시험", "btEdge": 1.0, "n": 0,
                                     "wins": 0, "sumPct": 0.0, "sumSq": 0.0, "status": "testing"}
        e.state["positions"] = {"AAAUSDT": pos("long", 1.0), "BBBUSDT": pos("short", 2.1, usdt=60.0),
                                "CCCUSDT": pos("short", 0.95)}
        self.e = e

    def tearDown(self):
        bb.STATE_FILE, bb.LOG_FILE, bb.prices, bb.BASE_DIR, cb.REPORT_DIR = self._keep
        self.tmp.cleanup()

    def send(self, typ, arg="", age=0, cid=None):
        cid = cid or f"c{len(self.cmds) + 1}"
        self.cmds[cid] = {"type": typ, "arg": arg, "at": int((time.time() - age) * 1000)}
        self.e.check_commands()
        return self.e.state.get("cmd") or {}


class CommandTest(Base):
    def test_pause_blocks_new_live_and_resume_clears_rest(self):
        e = self.e
        strat = {"rid": "r1", "side": "long"}
        self.assertTrue(e.live_ok(strat, "ZZZUSDT"))
        r = self.send("pause")
        self.assertEqual(r["status"], "done")
        self.assertTrue(e.state["guard"]["paused"])
        self.assertFalse(e.live_ok(strat, "ZZZUSDT"))
        self.assertIn("일시정지(대시보드)", json.dumps(e.state.get("liveSkip"), ensure_ascii=False))
        self.assertEqual(len(e.state["positions"]), 3)                      # 들고 있는 것은 그대로
        e.state["guard"].update({"restUntil": time.time() + 3600, "streak": 0, "restStart": 1})
        r = self.send("resume")
        self.assertEqual(r["status"], "done")
        self.assertFalse(e.state["guard"]["paused"])
        self.assertEqual(e.state["guard"]["restUntil"], 0)
        self.assertIn("연속 손실 쉼도 해제", r["msg"])
        self.assertTrue(e.live_ok(strat, "ZZZUSDT"))

    def test_close_one_symbol(self):
        e = self.e
        e.state["guard"].update({"streak": 2})
        r = self.send("close", "AAAUSDT")
        self.assertEqual(r["status"], "done", r)
        self.assertNotIn("AAAUSDT", e.state["positions"])
        self.assertEqual(e.live.orders, [("AAAUSDT", "SELL", 50.0, True)])   # 롱 정리 = reduceOnly 매도
        self.assertIn("AAAUSDT", e.live.cancelled)                           # 거래소 보호 손절 취소
        ll = e.state["liveLedger"]
        self.assertEqual(ll["trades"], 1)
        self.assertAlmostEqual(ll["pnlUsdt"], 50 * (0.02 - 2 * bb.FEE), places=3)
        self.assertEqual(e.state["ruleBook"]["r1"]["n"], 0)                  # 규칙 성적에는 안 넣음
        self.assertEqual(e.state["guard"]["streak"], 2)                      # 연속 손실 계산도 그대로
        t = e.state["tradeLog"][-1]
        self.assertEqual((t["symbol"], t["exit"], t["live"]), ("AAAUSDT", bb.MANUAL_CLOSE, True))
        self.assertEqual(len(e.state["positions"]), 2)

    def test_close_missing_symbol_rejected(self):
        r = self.send("close", "NOPEUSDT")
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(self.e.live.orders, [])

    def test_close_side_short_only(self):
        e = self.e
        r = self.send("closeSide", "short")
        self.assertEqual(r["status"], "done", r)
        self.assertEqual(sorted(e.state["positions"]), ["AAAUSDT"])
        self.assertEqual(sorted(o[0] for o in e.live.orders), ["BBBUSDT", "CCCUSDT"])
        self.assertTrue(all(o[1] == "BUY" and o[3] for o in e.live.orders))  # 숏 정리 = reduceOnly 매수
        # BBB 2.1→2.0 숏 이익, CCC 0.95→0.97 숏 손실
        want = 60 * (2.1 / 2.0 - 1 - 2 * bb.FEE) + 50 * (0.95 / 0.97 - 1 - 2 * bb.FEE)
        self.assertAlmostEqual(e.state["liveLedger"]["pnlUsdt"], want, places=3)
        self.assertFalse(e.state["guard"].get("restUntil"))

    def test_close_all_with_pause_and_partial_failure(self):
        e = self.e
        e.live.fail_market.add("BBBUSDT")
        r = self.send("closeAll", "pause")
        self.assertEqual(r["status"], "partial", r)
        self.assertEqual(sorted(e.state["positions"]), ["BBBUSDT"])          # 실패한 것은 그대로(보호 손절 유지)
        self.assertNotIn("BBBUSDT", e.live.cancelled)
        self.assertTrue(e.state["guard"]["paused"])
        self.assertIn("실패 1건", r["msg"])

    def test_manual_losses_do_not_trigger_streak_rest(self):
        e = self.e
        self.px.update({"AAAUSDT": 0.97, "BBBUSDT": 2.2, "CCCUSDT": 0.99})   # 셋 다 손실
        r = self.send("closeAll")
        self.assertEqual(r["status"], "done", r)
        self.assertEqual(e.state["positions"], {})
        g = e.state["guard"]
        self.assertFalse(g.get("restUntil", 0) > time.time())
        self.assertEqual(g.get("streak", 0), 0)
        self.assertFalse(g.get("paused"))
        self.assertLess(e.state["liveLedger"]["dayPnl"], 0)                  # 손익 자체는 장부·하루 한도에 그대로 반영

    def test_old_duplicate_unknown(self):
        e = self.e
        r = self.send("closeAll", age=bb.CMD_MAX_AGE_SEC + 5)
        self.assertEqual(r["status"], "expired")
        self.assertEqual(len(e.state["positions"]), 3)
        self.assertEqual(e.live.orders, [])
        r = self.send("close", "AAAUSDT", cid="same")
        self.assertEqual(r["status"], "done")
        n = len(e.live.orders)
        e.state["positions"]["AAAUSDT"] = pos("long", 1.0)                   # 같은 명령 번호는 다시 와도 실행 안 함
        e.check_commands()
        self.assertEqual(len(e.live.orders), n)
        self.assertIn("AAAUSDT", e.state["positions"])
        r = self.send("sellEverythingNow")
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(len(e.state["cmdLog"]), 3)

    def test_no_live_connection(self):
        self.e.live = None
        r = self.send("closeAll")
        self.assertEqual(r["status"], "failed")
        self.assertEqual(len(self.e.state["positions"]), 3)

    def test_breakeven_moves_only_profitable(self):
        e = self.e
        r = self.send("breakeven", "all")
        self.assertEqual(r["status"], "done", r)
        a, b, c = (e.state["positions"][k] for k in ("AAAUSDT", "BBBUSDT", "CCCUSDT"))
        lock = 2 * bb.FEE + bb.BE_LOCK_PCT / 100
        self.assertAlmostEqual(a["sl"], 1.0 * (1 + lock))                    # 롱 +2% → 본전 위
        self.assertAlmostEqual(b["sl"], 2.1 * (1 - lock))                    # 숏 +4.8% → 본전 위(아래쪽)
        self.assertAlmostEqual(c["sl"], 0.95 * 1.05)                         # 숏 손실 중 → 그대로
        self.assertTrue(a["beMoved"] and b["beMoved"] and not c.get("beMoved"))
        self.assertEqual(sorted(e.live.cancelled), ["AAAUSDT", "BBBUSDT"])
        self.assertEqual(len(e.live.stops), 2)
        # 거래소 보호 손절은 본전에서 여유폭만큼 손실 쪽
        trig = dict((s[0], s[2]) for s in e.live.stops)
        self.assertLess(trig["AAAUSDT"], 1.0)
        self.assertGreater(trig["BBBUSDT"], 2.1)
        # 다시 눌러도 더 올리지 않음
        r = self.send("breakeven", "all")
        self.assertEqual(r["status"], "rejected")
        # 본전 손절에 걸리면 '본전 정리'로 기록
        self.px["AAAUSDT"] = 1.0005
        e.manage()
        self.assertNotIn("AAAUSDT", e.state["positions"])
        self.assertEqual([t["exit"] for t in e.state["tradeLog"] if t["symbol"] == "AAAUSDT"], ["본전 정리"])

    def test_breakeven_restores_when_exchange_stop_fails(self):
        e = self.e
        e.live.fail_stop = True
        old = e.state["positions"]["AAAUSDT"]["sl"]
        r = self.send("breakeven", "AAAUSDT")
        self.assertEqual(r["status"], "rejected", r)
        self.assertEqual(e.state["positions"]["AAAUSDT"]["sl"], old)
        self.assertFalse(e.state["positions"]["AAAUSDT"].get("beMoved"))

    def test_side_cap_setting(self):
        e = self.e
        keep = bb.SIDE_CAP_LIVE
        try:
            bb.SIDE_CAP_LIVE = 0                                             # 기본: 제한 없음(기존 동작)
            self.assertTrue(e.live_ok({"rid": "r1", "side": "short"}, "ZZZUSDT"))
            bb.SIDE_CAP_LIVE = 2                                             # 숏은 이미 2건 → 숏 신호는 모의로, 롱(1건)은 실주문
            self.assertFalse(e.live_ok({"rid": "r1", "side": "short"}, "ZZZUSDT"))
            self.assertIn("같은 방향 실보유 최대(설정)", json.dumps(e.state.get("liveSkip"), ensure_ascii=False))
            self.assertTrue(e.live_ok({"rid": "r1", "side": "long"}, "ZZZUSDT"))
            bb.SIDE_CAP_LIVE = 1
            self.assertFalse(e.live_ok({"rid": "r1", "side": "long"}, "ZZZUSDT"))
        finally:
            bb.SIDE_CAP_LIVE = keep
        self.assertIn("BINANCE_SIDE_CAP", cb.APP_SETTINGS)
        self.assertEqual(cb.clean_app_settings({"BINANCE_SIDE_CAP": "3", "X": "1"}), {"BINANCE_SIDE_CAP": "3"})
        self.assertEqual(cb.clean_app_settings({"BINANCE_SIDE_CAP": "11"}), {})

    def test_view_has_cockpit_fields(self):
        e = self.e
        self.send("pause")
        ev = e.exposure_view()
        self.assertEqual(ev, {"long": 50.0, "short": 110.0, "nLong": 1, "nShort": 2, "net": -60.0})


class WireTest(Base):
    """웹페이지 문서 → 엔진 명령 읽기, 엔진 상태 → 화면용 값"""
    def test_dash_fields_reads_commands_from_settings_doc(self):
        e = self.e
        del e.dash_fields                                            # 실제 읽기 함수를 씀(HTTP만 가짜)
        now = int(time.time() * 1000)
        doc = {"fields": {
            "values": cb.to_fs({"BINANCE_ORDER_USDT": "50"}), "updatedAt": cb.to_fs(now),
            "binanceTestOrderAt": cb.to_fs(123), "binanceResumeAt": cb.to_fs(456.0),
            "binanceCmds": cb.to_fs({"c1": {"type": "pause", "arg": "", "at": now}, "bad": "x"})}}

        class R:
            status_code = 200

            def json(self):
                return doc
        keep = bb.requests.get
        calls = []
        bb.requests.get = lambda url, headers=None, timeout=None: calls.append(url) or R()
        try:
            f = e.dash_fields()
            self.assertEqual(f["binanceTestOrderAt"], 123)
            self.assertEqual(f["binanceResumeAt"], 456)
            self.assertEqual(list(f["cmds"]), ["c1"])                # 모양이 틀린 항목은 버림
            n = len(calls)
            e.dash_fields()                                          # 몇 초 안에는 다시 읽지 않음
            self.assertEqual(len(calls), n)
            e.check_commands()
            self.assertTrue(e.state["guard"]["paused"])
            self.assertEqual(e.state["cmd"]["id"], "c1")
        finally:
            bb.requests.get = keep

    def test_read_limit_backs_off(self):
        e = self.e
        del e.dash_fields

        class R:
            status_code = 429

            def json(self):
                return {}
        keep = bb.requests.get
        bb.requests.get = lambda url, headers=None, timeout=None: R()
        try:
            self.assertIsNone(e.dash_fields())
            e.check_commands()                                       # 한도 초과여도 오류 없이 넘어감
            self.assertGreater(e._dash_backoff, time.time())
        finally:
            bb.requests.get = keep

    def test_view_for_dashboard(self):
        e = self.e
        e.cfg.update({"_appSettings": {"BINANCE_SIDE_CAP": "3"}, "BINANCE_SIDE_CAP": "3"})
        e.state["aiDecisions"] = {f"ai-{i}": {"decision": "approve", "at": i} for i in range(60)}
        e.state["userRequests"] = [{"id": "req-1", "text": "t", "at": 1}]
        e.state["bridgeInit"] = 1
        self.send("pause")
        v = e.view()
        self.assertTrue(v["guard"]["paused"])
        self.assertEqual(v["exposure"]["net"], -60.0)
        self.assertEqual(v["cmd"]["type"], "pause")
        self.assertEqual(len(v["aiDecisions"]), 40)                  # 화면용은 최근 40건만
        self.assertIn("ai-59", v["aiDecisions"])
        self.assertEqual(v["userRequests"][0]["id"], "req-1")
        self.assertEqual(v["cmdPollSec"], bb.CMD_POLL_SEC)
        self.assertEqual(v["settings"]["BINANCE_SIDE_CAP"], {"value": "3", "src": "app"})
        p = [q for q in v["positions"] if q["symbol"] == "BBBUSDT"][0]
        self.assertEqual((p["usdt"], p["beMoved"], p["live"]), (60.0, False, True))
        json.dumps({k: cb.to_fs(x) for k, x in v.items()})           # 웹페이지에 올릴 수 있는 모양인지


class BridgeTest(Base):
    def test_takeover_and_inbox(self):
        e = self.e
        with open(os.path.join(bb.BASE_DIR, "bot_state.json"), "w", encoding="utf-8") as f:
            json.dump({"aiDecisions": {"ai-old": {"decision": "approve", "at": 1}}, "inboxSeen": ["i-old"],
                       "userRequests": [{"id": "req-old", "text": "예전", "at": 1}], "sugSeen": ["s-old"]}, f)
        now = int(time.time() * 1000)
        e.cloud.inbox = {"i-old": {"kind": "approve", "target": "ai-zzz", "at": 5},
                         "i-1": {"kind": "approve", "target": "ai-20261124-x", "at": now - 5000},
                         "i-2": {"kind": "reject", "target": "ai-20261124-y", "at": now - 4000},
                         "i-3aaaaaaaaaaaa": {"kind": "request", "text": "숏을 줄여줘", "at": now - 3000},
                         "i-4": {"kind": "approve", "target": "bot-gate-x-1", "at": now}}
        e.cloud.sugs = [{"_id": "s-old", "text": "예전 제안", "at": 1}, {"_id": "s-new-123", "text": "새 제안", "nick": "친구A", "at": now}]
        e.bridge_poll()
        st = e.state
        self.assertEqual(st["aiDecisions"]["ai-old"]["decision"], "approve")           # 넘겨받음
        self.assertNotIn("ai-zzz", st["aiDecisions"])                                   # 이미 본 항목은 다시 안 봄
        self.assertEqual(st["aiDecisions"]["ai-20261124-x"]["decision"], "approve")
        self.assertEqual(st["aiDecisions"]["ai-20261124-y"]["decision"], "reject")
        self.assertNotIn("bot-gate-x-1", st["aiDecisions"])                             # 업비트 봇 제안은 대상 아님
        ids = [r["id"] for r in st["userRequests"]]
        self.assertEqual(ids, ["req-old", "req-aaaaaaaaaaaa", "sug-s-new-123"])
        self.assertTrue(st["userRequests"][-1]["friend"])
        n = e.uploads
        e._inbox_at = e._sug_at = 0
        e.bridge_poll()                                                                  # 두 번째는 새 것 없음
        self.assertEqual(e.uploads, n)
        self.assertEqual(len(st["userRequests"]), 3)

    def test_fresh_start_ignores_old_items(self):
        e = self.e
        now = int(time.time() * 1000)
        e.cloud.inbox = {"a": {"kind": "request", "text": "아주 예전", "at": now - 7 * 3600 * 1000},
                         "b": {"kind": "request", "text": "방금", "at": now - 60000}}
        e.bridge_poll()
        self.assertEqual([r["text"] for r in e.state["userRequests"]], ["방금"])

    def test_friend_pc_does_nothing(self):
        e = self.e
        e.cfg["ROLE"] = "friend"
        e.cloud.inbox = {"b": {"kind": "request", "text": "방금", "at": int(time.time() * 1000)}}
        e.bridge_poll()
        self.assertFalse(e.state.get("userRequests"))
        self.assertFalse(e.state.get("bridgeInit"))

    def test_report_stub_rules(self):
        e = self.e
        os.makedirs(cb.REPORT_DIR)
        path = os.path.join(cb.REPORT_DIR, "report.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"botState": "running", "tradeLog": [1, 2, 3]}, f)
        e.bridge_init()
        e.state["aiDecisions"] = {"ai-1": {"decision": "approve", "at": 1}}
        e.bridge_report()                                                   # 업비트 봇이 방금 쓴 파일 → 건드리지 않음
        self.assertEqual(jload(path)["botState"], "running")
        old = time.time() - bb.BRIDGE_STALE_SEC - 60
        os.utime(path, (old, old))
        e.bridge_report()                                                   # 멈춘 파일 → 보관하고 장부를 적음
        rep = jload(path)
        self.assertTrue(rep["upbitRetired"])
        self.assertEqual(rep["aiDecisions"], {"ai-1": {"decision": "approve", "at": 1}})
        keep = jload(os.path.join(cb.REPORT_DIR, "upbit_last_report.json"))
        self.assertEqual(keep["tradeLog"], [1, 2, 3])
        e.state["aiDecisions"]["ai-2"] = {"decision": "reject", "at": 2}
        e.bridge_report()                                                   # 이미 엔진이 쓰는 파일 → 계속 갱신
        self.assertIn("ai-2", jload(path)["aiDecisions"])
        self.assertEqual(keep, jload(os.path.join(cb.REPORT_DIR, "upbit_last_report.json")))


class UpdateTest(unittest.TestCase):
    def test_upbit_quiet_and_shared_file_update(self):
        with tempfile.TemporaryDirectory() as d:
            keep = (bb.HERE, bb._OWNER, bb._update_file, bb.CB_MTIME)
            calls = []
            try:
                bb.HERE = d
                bb._update_file = lambda url, path, marker, name: calls.append(name) or False
                open(os.path.join(d, "start_bot.bat"), "w").close()
                open(os.path.join(d, "chamdom_bot.py"), "w").close()
                bb.CB_MTIME = bb.cb_mtime()
                open(os.path.join(d, "bot.log"), "w").close()
                bb._OWNER = True
                self.assertFalse(bb.upbit_quiet())
                self.assertFalse(bb.check_update())
                self.assertEqual(calls, ["binance_bot.py"])                  # 업비트 봇이 살아 있으면 공용 파일은 안 건드림
                old = time.time() - 31 * 60
                os.utime(os.path.join(d, "bot.log"), (old, old))
                self.assertTrue(bb.upbit_quiet())
                calls.clear()
                bb.check_update()
                self.assertEqual(calls, ["binance_bot.py", "chamdom_bot.py"])  # 조용하면 엔진이 대신 받음
                bb._OWNER = False
                calls.clear()
                bb.check_update()
                self.assertEqual(calls, ["binance_bot.py"])                  # 지인 PC(같은 폴더)에서는 기존대로
                os.utime(os.path.join(d, "chamdom_bot.py"), (old, old))
                self.assertTrue(bb.check_update())                           # 공용 파일이 바뀌어 있으면 다시 시작
            finally:
                bb.HERE, bb._OWNER, bb._update_file, bb.CB_MTIME = keep


class UpbitRetireTest(unittest.TestCase):
    def test_owner_only(self):
        self.assertTrue(cb.upbit_retired({"ROLE": "owner"}))
        self.assertTrue(cb.upbit_retired({}))
        self.assertFalse(cb.upbit_retired({"ROLE": "friend"}))

    def test_holdings_count_real_only(self):
        with tempfile.TemporaryDirectory() as d:
            keep = cb.STATE_FILE
            try:
                cb.STATE_FILE = os.path.join(d, "bot_state.json")
                self.assertEqual(cb.bot_holdings(), 0)                       # 장부 없음
                with open(cb.STATE_FILE, "w", encoding="utf-8") as f:
                    json.dump({"positions": {"KRW-A": {"paper": True}, "KRW-B": {"cost": 6000}}}, f)
                self.assertEqual(cb.bot_holdings(), 1)
            finally:
                cb.STATE_FILE = keep


if __name__ == "__main__":
    unittest.main()
