"""바이낸스 엔진 통째 돌려 보기 (네트워크 없이): 실제 run() 반복문을 가짜 거래소·가짜 웹페이지·가짜 시계로 몇 분 돌림

확인하는 것
- 새 코드로 시작·전략 선정·상태 올리기·보고서 쓰기가 오류 없이 도는지
- 대시보드 '테스트 실주문' → 실주문 진입 + 거래소 보호 손절
- 대응 명령: 수익 지키기(본전 손절) → 새 진입 멈춤 → 전부 정리, 결과가 화면용 상태(cmd)에 실리는지
- 승인·요청함이 보고서 파일(binance_report.json·report.json)에 적히는지
- 실행: python -m unittest tests/test_binance_smoke.py -v   (저장소 최상위에서)
"""
import json
import os
import random
import sys
import tempfile
import time as real_time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bot"))
import binance_bot as bb  # noqa: E402
import chamdom_bot as cb  # noqa: E402

SYMS = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
BAR = 900_000


class Done(Exception):
    pass


class Clock:
    """엔진이 보는 시계: sleep 하면 그만큼 앞으로 감. 정해 둔 시간이 지나면 반복문을 끝냄"""
    def __init__(self, limit):
        self.t0 = real_time.time()
        self.off = 0.0
        self.limit = limit
        self.hooks = []

    def time(self):
        return self.t0 + self.off

    def sleep(self, s):
        self.off += s
        for h in list(self.hooks):
            h(self.off)
        if self.off >= self.limit:
            raise Done()

    def __getattr__(self, name):
        return getattr(real_time, name)


class Resp:
    def __init__(self, data=None, status=200, text=""):
        self._d, self.status_code, self.text = data, status, text or json.dumps(data, ensure_ascii=False)[:200]
        self.content = self.text.encode()

    def json(self):
        return self._d


class World:
    """가짜 바이낸스 + 가짜 파이어베이스"""
    def __init__(self, clock):
        self.clock = clock
        rng = random.Random(3)
        self.px = {}
        self.closes = {}
        for s in SYMS + ["BTCUSDT"]:
            p, arr = 10.0 * (1 + rng.random()), []
            for _ in range(2600):
                p *= 1 + rng.gauss(0, 0.004)
                arr.append(p)
            self.closes[s] = arr
            if s in SYMS:
                self.px[s] = arr[-1]
        self.pos = {}                 # 거래소 실제 포지션 {sym: 수량(+롱/-숏)}
        self.entry = {}
        self.orders, self.algo, self.cancels = [], [], []
        self.docs = {"settings": {"values": {}, "updatedAt": 1}, "inbox": {}}
        self.patches = []

    # ----- 바이낸스 공개
    def public(self, path, params):
        now = int(self.clock.time() * 1000)
        if path == "/fapi/v1/time":
            return {"serverTime": now}
        if path == "/fapi/v1/exchangeInfo":
            return {"symbols": [{"symbol": s, "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING",
                                 "filters": [{"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                                             {"filterType": "MIN_NOTIONAL", "notional": "5"},
                                             {"filterType": "PRICE_FILTER", "tickSize": "0.0001"}]} for s in SYMS]}
        if path == "/fapi/v1/ticker/24hr":
            return [{"symbol": s, "quoteVolume": str(1000 - i)} for i, s in enumerate(SYMS)]
        if path == "/fapi/v1/ticker/price":
            return [{"symbol": s, "price": str(p)} for s, p in self.px.items()]
        if path == "/fapi/v1/premiumIndex":
            return [{"symbol": s, "lastFundingRate": "0.0001", "nextFundingTime": now + 3600_000} for s in SYMS]
        if path == "/fapi/v1/klines":
            arr = self.closes[params["symbol"]]
            last_open = now // BAR * BAR
            end = params.get("endTime")
            rows = []
            for k, c in enumerate(arr):
                t = last_open - (len(arr) - 1 - k) * BAR
                if end and t > end:
                    break
                o = arr[k - 1] if k else c
                rows.append([t, str(o), str(max(o, c) * 1.001), str(min(o, c) * 0.999), str(c), "1000", t + BAR - 1])
            return rows[-int(params.get("limit", 500)):]
        raise AssertionError("모르는 공개 경로 " + path)

    # ----- 바이낸스 서명 요청
    def signed(self, method, path, q):
        if path == "/fapi/v2/balance":
            return [{"asset": "USDT", "balance": "500", "availableBalance": "400", "crossUnPnl": "0"}]
        if path in ("/fapi/v2/positionRisk", "/fapi/v3/positionRisk"):
            return [{"symbol": s, "positionAmt": str(a), "entryPrice": str(self.entry[s]), "markPrice": str(self.px[s]),
                     "unRealizedProfit": str((self.px[s] - self.entry[s]) * a), "leverage": "1", "marginType": "isolated"}
                    for s, a in self.pos.items() if a]
        if path in ("/fapi/v1/leverage", "/fapi/v1/marginType"):
            return {}
        if path == "/fapi/v1/order" and method == "POST":
            sym, qty = q["symbol"], float(q["quantity"])
            sign = 1 if q["side"] == "BUY" else -1
            cur = self.pos.get(sym, 0.0)
            if q.get("reduceOnly") == "true" and (cur == 0 or (cur > 0) == (sign > 0)):
                return Resp({"code": -2022, "msg": "ReduceOnly Order is rejected."}, 400)
            self.pos[sym] = round(cur + sign * qty, 6)
            if cur == 0:
                self.entry[sym] = self.px[sym]
            self.orders.append((sym, q["side"], qty, q.get("reduceOnly") == "true"))
            return {"orderId": len(self.orders), "avgPrice": str(self.px[sym]), "executedQty": str(qty), "status": "FILLED"}
        if path == "/fapi/v1/algoOrder":
            self.algo.append((q["symbol"], q["side"], float(q["triggerPrice"])))
            return {"algoId": len(self.algo)}
        if path == "/fapi/v1/algoOpenOrders":
            self.cancels.append(q["symbol"])
            return {}
        if path == "/fapi/v1/userTrades":
            return []
        raise AssertionError(f"모르는 서명 경로 {method} {path}")

    # ----- requests 대신
    def get(self, url, params=None, headers=None, timeout=None):
        if url.startswith(bb.FAPI):
            return Resp(self.public(url[len(bb.FAPI):], params or {}))
        if "raw.githubusercontent.com" in url:
            return Resp(None, 404)
        if url.startswith(cb.FS_BASE):
            name = url.rsplit("/", 1)[-1]
            d = self.docs.get(name)
            if d is None:
                return Resp({}, 404)
            return Resp({"fields": {k: cb.to_fs(v) for k, v in d.items()}})
        raise AssertionError("모르는 주소 " + url)

    def request(self, method, url, headers=None, timeout=None):
        path, qs = url[len(bb.FAPI):].split("?", 1)
        q = dict(x.split("=", 1) for x in qs.split("&"))
        r = self.signed(method, path, q)
        return r if isinstance(r, Resp) else Resp(r)

    def post(self, url, data=None, json=None, headers=None, timeout=None):
        if "securetoken" in url:
            return Resp({"id_token": "t", "user_id": "u1", "refresh_token": "r", "expires_in": "3600"})
        if url.endswith(":runQuery"):
            return Resp([])
        raise AssertionError("모르는 주소 " + url)

    def patch(self, url, json=None, headers=None, timeout=None):
        self.patches.append({k: cb.from_fs(v) for k, v in json["fields"].items()})
        return Resp({})


class SmokeTest(unittest.TestCase):
    def test_engine_loop_end_to_end(self):
        tmp = tempfile.TemporaryDirectory()
        d = tmp.name
        clock = Clock(limit=420)
        w = World(clock)
        keep = {(m, k): getattr(m, k) for m, k in [
            (bb, "STATE_FILE"), (bb, "LOG_FILE"), (bb, "BASE_DIR"), (bb, "HERE"), (bb, "time"), (bb, "TOP_N"), (bb, "_OWNER"),
            (cb, "CONFIG_FILE"), (cb, "SETTINGS_FILE"), (cb, "STATE_FILE"), (cb, "LOG_FILE"), (cb, "REPORT_DIR"), (cb, "time"),
            (cb, "BT_TRIALS"), (bb.requests, "get"), (bb.requests, "post"), (bb.requests, "patch"), (bb.requests, "request")]}
        try:
            bb.STATE_FILE, bb.LOG_FILE = os.path.join(d, "binance_state.json"), os.path.join(d, "binance.log")
            bb.BASE_DIR = bb.HERE = d
            cb.CONFIG_FILE, cb.SETTINGS_FILE = os.path.join(d, "bot_config.env"), os.path.join(d, "bot_settings.json")
            cb.STATE_FILE, cb.LOG_FILE = os.path.join(d, "bot_state.json"), os.path.join(d, "bot.log")
            with open(cb.CONFIG_FILE, "w", encoding="utf-8") as f:
                f.write("UPBIT_ACCESS_KEY=x\nUPBIT_SECRET_KEY=x\nFIREBASE_LINK_CODE=x\nBINANCE_API_KEY=k\nBINANCE_SECRET_KEY=s\n"
                        "BINANCE_TRADE_MODE=live\nBINANCE_ORDER_USDT=20\nBINANCE_DAILY_LOSS_USDT=50\nBINANCE_MAX_LIVE=5\n")
            with open(cb.STATE_FILE, "w", encoding="utf-8") as f:          # 업비트 봇이 남긴 장부(넘겨받을 기록)
                json.dump({"aiDecisions": {"ai-old": {"decision": "approve", "at": 1}}, "inboxSeen": ["seen-1"],
                           "userRequests": [{"id": "req-old", "text": "예전 요청", "at": 1}]}, f)
            bb.time = cb.time = clock
            bb.TOP_N, cb.BT_TRIALS, bb._OWNER = 3, 15, None
            bb.requests.get, bb.requests.post, bb.requests.patch, bb.requests.request = w.get, w.post, w.patch, w.request
            eng = bb.Engine()
            cb.REPORT_DIR = os.path.join(d, "report")                        # load_config 가 기본 위치로 되돌리므로 그 뒤에 지정
            now_ms = lambda: int(clock.time() * 1000)
            w.docs["settings"]["binanceTestOrderAt"] = now_ms()               # 화면에서 '테스트 실주문'을 누름
            w.docs["inbox"] = {"seen-1": {"kind": "approve", "target": "ai-should-not", "at": 5},
                               "in-1": {"kind": "approve", "target": "ai-20261124-new", "at": now_ms()},
                               "in-2zzzzzzzzzzzz": {"kind": "request", "text": "숏 쏠림 알려줘", "at": now_ms()}}
            steps = {"be": False, "pause": False, "close": False}
            sym = SYMS[0]

            def script(off):
                pos = eng.state["positions"].get(sym)
                cmds = w.docs["settings"].setdefault("binanceCmds", {})
                if pos and pos.get("live") and not steps["be"] and off > 60:
                    w.px[sym] = pos["entry"] * 0.993                           # 숏이 0.7% 이익 (익절 +1% 전)
                    cmds["c-be"] = {"type": "breakeven", "arg": "all", "at": now_ms()}
                    steps["be"] = True
                elif steps["be"] and not steps["pause"] and off > 150:
                    cmds["c-pause"] = {"type": "pause", "arg": "", "at": now_ms()}
                    steps["pause"] = True
                elif steps["pause"] and not steps["close"] and off > 240:
                    cmds["c-all"] = {"type": "closeAll", "arg": "pause", "at": now_ms()}
                    steps["close"] = True
            clock.hooks.append(script)
            with self.assertRaises(Done):
                eng.run()

            st = eng.state
            with open(bb.LOG_FILE, encoding="utf-8") as f:
                log = f.read()
            self.assertNotIn("Traceback", log)
            self.assertNotIn("Error:", log.replace("RuntimeError: 바이낸스", ""), log[-3000:])
            # 테스트 실주문이 들어갔고 보호 손절이 걸렸음
            self.assertEqual(st["testOrder"]["status"], "opened", st.get("testOrder"))
            self.assertEqual(w.orders[0][:2], (sym, "SELL"))
            self.assertGreaterEqual(len(w.algo), 2)                           # 처음 건 손절 + 본전으로 다시 건 손절
            self.assertEqual(w.algo[0][1], "BUY")
            # 명령 세 개가 차례로 실행됨
            done = {c["id"]: c for c in st["cmdLog"]}
            self.assertEqual(done["c-be"]["status"], "done", done.get("c-be"))
            self.assertEqual(done["c-pause"]["status"], "done")
            self.assertEqual(done["c-all"]["status"], "done", done.get("c-all"))
            self.assertIn(sym, w.cancels)
            self.assertFalse([s for s, p in st["positions"].items() if p.get("live")])   # 실보유 없음
            self.assertEqual(w.pos.get(sym), 0.0)                             # 거래소에도 없음
            self.assertTrue(st["guard"]["paused"])
            ll = st["liveLedger"]
            self.assertEqual(ll["trades"], 1)
            self.assertGreater(ll["pnlUsdt"], 0)                              # 0.7% 이익에서 정리
            self.assertEqual(st["tradeLog"][-1]["exit"], bb.MANUAL_CLOSE)
            # 화면용 상태에 결과가 실림
            last = w.patches[-1]
            self.assertEqual(last["cmd"]["id"], "c-all")
            self.assertTrue(last["guard"]["paused"])
            self.assertEqual(last["exposure"]["net"], 0)
            self.assertEqual(last["cmdPollSec"], bb.CMD_POLL_SEC)
            # 승인·요청이 장부와 보고서 파일에 적힘 (예전에 본 항목은 다시 안 봄)
            self.assertEqual(st["aiDecisions"]["ai-20261124-new"]["decision"], "approve")
            self.assertNotIn("ai-should-not", st["aiDecisions"])
            self.assertEqual([r["id"] for r in st["userRequests"]], ["req-old", "req-zzzzzzzzzzzz"])
            with open(os.path.join(cb.REPORT_DIR, "binance_report.json"), encoding="utf-8") as f:
                rep = json.load(f)
            self.assertIn("ai-20261124-new", rep["aiDecisions"])
            self.assertIn("ai-old", rep["aiDecisions"])
            self.assertTrue(rep["savedAt"])
            with open(os.path.join(cb.REPORT_DIR, "report.json"), encoding="utf-8") as f:
                stub = json.load(f)
            self.assertTrue(stub["upbitRetired"])
            self.assertEqual(stub["userRequests"][-1]["text"], "숏 쏠림 알려줘")
        finally:
            for (m, k), v in keep.items():
                setattr(m, k, v)
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
