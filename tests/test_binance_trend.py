"""추세 묶음 모의(관찰 전용, BINANCE_TREND_PAPER) 오프라인 시험

- 네트워크 없음: 고르기 결과·표시 가격을 직접 넣어 실제 엔진 코드(bot/binance_bot.py)를 돌림
- 실행: python -m unittest tests/test_binance_trend.py -v   (저장소 최상위에서)
"""
import os
import sys
import time
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bot"))
import binance_bot as bb  # noqa: E402


def engine():
    e = object.__new__(bb.Engine)
    e.state = {"positions": {}}
    return e


class Pick(unittest.TestCase):
    def test_pick_top_volume_then_rank(self):
        rows = [(f"S{i}USDT", 1000 - i, (i % 7) / 10) for i in range(30)]
        rows.append(("HUGEUSDT", 1, 9.9))                      # 거래대금 꼴찌 → 대상 밖
        longs, shorts = bb.trend_pick(rows, n=20, k=3)
        self.assertNotIn("HUGEUSDT", longs)
        self.assertEqual(len(longs), 3)
        self.assertEqual(len(shorts), 3)
        self.assertFalse(set(longs) & set(shorts))
        ret = {r[0]: r[2] for r in rows}
        self.assertTrue(min(ret[s] for s in longs) >= max(ret[s] for s in shorts))

    def test_pick_too_few(self):
        self.assertEqual(bb.trend_pick([("AUSDT", 1, 0.1)], n=10, k=2), ([], []))

    def test_close_math(self):
        tr = {"L": {"A": {"e": 100.0, "f": -0.05, "c": 0.0014}}, "S": {"B": {"e": 10.0, "f": 0.10, "c": 0.0, "lp": 9.0}}}
        r = bb.trend_close(tr, {"A": 110.0})                    # B 는 시세 없음 → 마지막 가격 9.0
        self.assertAlmostEqual(r["long"], 10.0)
        self.assertAlmostEqual(r["short"], 10.0)
        self.assertAlmostEqual(r["price"], 10.0)
        self.assertAlmostEqual(r["fund"], 0.025)
        self.assertAlmostEqual(r["cost"], -0.07)
        self.assertAlmostEqual(r["pct"], 10.0 + 0.025 - 0.07, places=3)


class Engine(unittest.TestCase):
    def setUp(self):
        self._keep = (bb.TREND_PAPER, bb.TREND_K, bb.log, bb.get)
        self.logs = []
        bb.log = lambda m: self.logs.append(m)
        bb.get = lambda *a, **k: (_ for _ in ()).throw(AssertionError("네트워크 호출 금지"))

    def tearDown(self):
        bb.TREND_PAPER, bb.TREND_K, bb.log, bb.get = self._keep

    def test_off_does_nothing(self):
        bb.TREND_PAPER = False
        e = engine()
        e.trend_tick(now=time.time())
        self.assertNotIn("trend", e.state)
        self.assertFalse(e.trend_view()["on"])

    def test_waits_until_0010_utc_and_starts_thread_once(self):
        bb.TREND_PAPER = True
        e = engine()
        started = []
        keep = bb.threading.Thread

        class T:
            def __init__(self, target, args, daemon):
                started.append(args)

            def start(self):
                pass

            def is_alive(self):
                return True
        bb.threading.Thread = T
        try:
            t0 = datetime(2026, 11, 30, 0, 5, tzinfo=timezone.utc).timestamp()
            e.trend_tick(now=t0)
            self.assertEqual(started, [])                       # 0시 10분 전 → 아무것도 안 함
            e.trend_tick(now=t0 + 600)
            e.trend_tick(now=t0 + 660)                          # 고르는 중 → 또 시작하지 않음
            self.assertEqual(started, [("2026-11-30",)])
        finally:
            bb.threading.Thread = keep

    def test_rotate_cycle_costs_funding_and_view(self):
        bb.TREND_PAPER, bb.TREND_K = True, 2
        e = engine()
        st = {"tr": {}, "hist": [], "day": "", "fn": {}}
        e.state["trend"] = st
        marks = {"A": 10.0, "B": 20.0, "C": 5.0, "D": 4.0, "E": 1.0}
        t = datetime(2026, 11, 30, 1, 0, tzinfo=timezone.utc).timestamp()  # 월요일
        e.trend_rotate(st, "2026-11-30", "0", ["A", "B"], ["C", "D"], marks, t)
        self.assertEqual(set(st["tr"]["0"]["L"]), {"A", "B"})
        self.assertAlmostEqual(st["tr"]["0"]["L"]["A"]["c"], bb.TREND_COST)
        self.assertEqual(st["hist"], [])
        # 펀딩: 다음 정산 시각이 묶음 시작 뒤 → 정산 지나면 롱은 냄, 숏은 받음
        nft = int(t * 1000) + 3600_000
        st["fn"] = {"A": [nft, 0.001], "C": [nft, 0.001]}
        data = [{"symbol": s, "markPrice": str(p), "lastFundingRate": "0.0005", "nextFundingTime": str(nft + 28800_000)}
                for s, p in marks.items()]
        bb.get = lambda *a, **k: data
        keep_time = bb.time.time
        bb.time.time = lambda: (nft + 1000) / 1000
        try:
            e.trend_fund()
            e.trend_fund()                                       # 같은 정산을 두 번 넣지 않음
        finally:
            bb.time.time = keep_time
        self.assertAlmostEqual(st["tr"]["0"]["L"]["A"]["f"], -0.1)
        self.assertAlmostEqual(st["tr"]["0"]["S"]["C"]["f"], 0.1)
        # 7일 뒤 같은 요일 교체: A 는 롱으로 이어짐(비용 0), E 는 새로 들어감
        m2 = {"A": 11.0, "B": 20.0, "C": 5.0, "D": 3.6, "E": 1.0}
        e.trend_rotate(st, "2026-12-07", "0", ["A", "E"], ["C", "B"], m2, t + 7 * 86400)
        self.assertEqual(len(st["hist"]), 1)
        h = st["hist"][0]
        self.assertAlmostEqual(h["price"], (10 + 0 + 0 + 10) / 4)
        self.assertAlmostEqual(h["fund"], 0.0)                   # -0.1 + 0.1
        self.assertAlmostEqual(h["cost"], -bb.TREND_COST * 100)
        self.assertEqual(st["tr"]["0"]["L"]["A"]["c"], 0.0)
        self.assertAlmostEqual(st["tr"]["0"]["L"]["E"]["c"], bb.TREND_COST)
        self.assertAlmostEqual(st["tr"]["0"]["S"]["B"]["c"], bb.TREND_COST)  # 롱 → 숏 으로 바뀌면 새로 들어감
        v = e.trend_view()
        self.assertTrue(v["on"])
        self.assertEqual(v["doneN"], 1)
        self.assertEqual(len(v["open"]), 1)
        self.assertAlmostEqual(v["avgPct"], h["pct"])

    def test_too_few_skips_day(self):
        bb.TREND_PAPER, bb.TREND_K = True, 10
        e = engine()
        st = {"tr": {}, "hist": [], "day": "", "fn": {}}
        e.trend_rotate(st, "2026-11-30", "0", ["A"], ["B"], {"A": 1, "B": 1}, time.time())
        self.assertEqual(st["day"], "2026-11-30")
        self.assertEqual(st["tr"], {})

    def test_setting_registered(self):
        self.assertEqual(bb.cb.APP_SETTINGS["BINANCE_TREND_PAPER"], ("choice", ("off", "on")))


if __name__ == "__main__":
    unittest.main()
