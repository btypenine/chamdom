"""바이낸스 '미끄러운 종목' 관찰 오프라인 시험 (매매 판단에는 안 쓰는 표시·집계 기능)

- 네트워크 없음 — 실제 엔진 코드(bot/binance_bot.py)의 관찰 함수만 가짜 상태로 불러 시험
- 실행: python -m unittest tests/test_binance_slipsym.py -v   (저장소 최상위에서)
"""
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bot"))
import binance_bot as bb  # noqa: E402

E = bb.Engine
NAMES = ["slipsym_seed", "slipsym_mark", "slipsym_record", "slipsym_view", "stop_gap_record"]


def fake(state):
    o = types.SimpleNamespace(state=state, SLIPSYM_HI=E.SLIPSYM_HI)
    for n in NAMES:
        setattr(o, n, types.MethodType(getattr(E, n), o))
    return o


def pos(side="short", live=True):
    return {"side": side, "live": live, "entry": 1.0, "sl": 1.05 if side == "short" else 0.95, "usdt": 20}


class SlipSymTest(unittest.TestCase):
    def test_seed_from_recent(self):
        o = fake({"positions": {}, "stopGapRecent": [
            {"symbol": "MMTUSDT", "who": "exchange", "over": -4.5},
            {"symbol": "MMTUSDT", "who": "exchange", "over": -0.5},
            {"symbol": "ADAUSDT", "who": "bot", "over": -3.0}]})        # 봇 손절은 제외
        o.slipsym_seed()
        self.assertEqual(o.state["slipSym"], {"MMTUSDT": -4.5})
        o.state["slipSym"]["X"] = -9                                   # 두 번째 호출은 덮어쓰지 않음
        o.slipsym_seed()
        self.assertIn("X", o.state["slipSym"])

    def test_mark_record_view(self):
        o = fake({"positions": {"MMTUSDT": pos(), "ADAUSDT": pos("long")}, "slipSym": {"MMTUSDT": -4.5}})
        o.slipsym_mark("MMTUSDT")
        o.slipsym_mark("ADAUSDT")
        o.slipsym_record("live", o.state["positions"]["MMTUSDT"], -5.0)
        o.slipsym_record("paper", o.state["positions"]["ADAUSDT"], 3.0)
        o.slipsym_record("live", {"side": "short"}, 1.0)               # 표시 없는 옛 포지션은 건너뜀
        v = o.slipsym_view()
        flags = {(r["kind"], r["flag"], r["side"]): r["n"] for r in v["rows"]}
        self.assertEqual(flags, {("live", "slip", "short"): 1, ("paper", "normal", "long"): 1})
        self.assertEqual(v["open"], 1)
        self.assertEqual(v["symbols"][0]["symbol"], "MMTUSDT")

    def test_exchange_stop_updates_symbol(self):
        o = fake({"positions": {}})
        p = pos()
        o.stop_gap_record("KAVAUSDT", p, -7.0, "거래소 보호 손절(실제 체결가)")
        o.stop_gap_record("KAVAUSDT", p, -5.2, "손절")                 # 봇 손절은 종목 기록에 안 씀
        self.assertLess(o.state["slipSym"]["KAVAUSDT"], -1.5)
        self.assertEqual(len(o.state["slipSym"]), 1)


if __name__ == "__main__":
    unittest.main()
