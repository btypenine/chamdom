"""업비트 봇 진화 전략 생성(UPBIT_EVO_SHARE) 오프라인 시험

- 네트워크 없음 — 실제 봇 코드(bot/chamdom_bot.py)의 진화 함수만 불러 시험
- 실행: python -m unittest tests/test_upbit_evo.py -v   (저장소 최상위에서)
"""
import os
import random
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bot"))
import chamdom_bot as cb  # noqa: E402


def rule(market, spec, trades, sum_pct, status="testing"):
    return {"id": cb.rule_id(market, spec), "market": market, "spec": spec, "trades": trades,
            "sumPct": sum_pct, "status": status}


SPEC_A = {"conds": [["ema_cross", {"f": 9, "s": 50}], ["breakout", {"lb": 30}]], "tp": 3.0, "sl": -5.0, "hold": 240}
SPEC_B = {"conds": [["green", {}], ["rsi_below", {"n": 7, "t": 30}]], "tp": 3.0, "sl": -3.0, "hold": 60}


class EvoTest(unittest.TestCase):
    def fake_bot(self, share=30):
        rb = {}
        for r in (rule("KRW-G", SPEC_A, 6, 6.6), rule("KRW-XPL", SPEC_B, 4, 5.1),
                  rule("KRW-SOL", SPEC_A, 15, -9.0),                    # 평균 마이너스 → 제외
                  rule("KRW-UNI", SPEC_B, 15, 3.0, "retired"),          # 퇴출 → 제외
                  rule("KRW-ARK", SPEC_B, 2, 4.0)):                     # 거래 2회 → 제외
            rb[r["id"] + r["market"]] = r
        return types.SimpleNamespace(state={"ruleBook": rb, "strategies": {}}, cfg={"UPBIT_EVO_SHARE": share})

    def test_pool_only_good_rules(self):
        genes = cb.Bot.evo_pool(self.fake_bot())
        self.assertEqual(sorted(g["coin"] for g in genes), ["G", "XPL"])
        self.assertGreater(genes[0]["fit"], genes[1]["fit"])

    def test_children_are_valid_specs(self):
        genes = cb.Bot.evo_pool(self.fake_bot())
        rng = random.Random(7)
        for _ in range(500):
            sp, gen, parent = cb.evo_breed(genes, rng)
            names = [c[0] for c in sp["conds"]]
            self.assertTrue(1 <= len(names) <= 3)
            self.assertEqual(len(names), len(set(names)))
            self.assertIn(sp["tp"], cb.TP_CHOICES)
            self.assertIn(sp["sl"], cb.SL_CHOICES)
            self.assertIn(sp["hold"], cb.HOLD_CHOICES)
            self.assertEqual(gen, 1)
            self.assertTrue(parent)
            cb.describe(sp)
            cb.rule_id("KRW-X", sp)
        # 부모 spec 은 바뀌지 않아야 함 (깊은 복사)
        self.assertEqual(genes[0]["spec"], SPEC_A)

    def test_stats_split_by_origin_and_kind(self):
        b = self.fake_bot()
        cb.Bot.evo_record(b, {"origin": "evo"}, {"paper": True}, 0.5)
        cb.Bot.evo_record(b, {}, {}, -0.2)
        v = cb.Bot.evo_view(b)
        self.assertEqual(v["share"], 30)
        self.assertEqual({r["kind"]: r["n"] for r in v["rows"]}, {"evo:paper": 1, "rand:live": 1})

    def test_default_is_off(self):
        self.assertIn("UPBIT_EVO_SHARE", cb.APP_SETTINGS)
        self.assertEqual(cb.APP_SETTINGS["UPBIT_EVO_SHARE"], ("num", (0, 75)))


if __name__ == "__main__":
    unittest.main()
