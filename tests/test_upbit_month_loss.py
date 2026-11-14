"""업비트 봇 월 손실 한도(MONTH_LOSS_LIMIT) 오프라인 시험

- 네트워크는 전부 막음(업비트·Firebase 호출이 생기면 시험 실패) — 실제 봇 코드(bot/chamdom_bot.py)를 그대로 불러 시험
- 시각은 가짜 시계로 바꿔 월 경계(매월 1일 09:00 KST)를 흉내 냄
- 실행: python -m unittest tests/test_upbit_month_loss.py -v   (저장소 최상위에서)
- bot_config.env 는 쓰지 않음 — 임시 폴더에 가짜 값으로 만든 설정 파일만 씀
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bot"))
import chamdom_bot as cb  # noqa: E402

KST = timezone(timedelta(hours=9))


class FakeClock:
    now_value = datetime(2026, 11, 14, 15, 0, tzinfo=KST)


class FakeDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        v = FakeClock.now_value
        return v.astimezone(tz) if tz else v.replace(tzinfo=None)


def no_network(*a, **k):
    raise AssertionError("시험 중 네트워크 호출이 생김: " + str(a[:1]))


class FakeUpbit:
    def __init__(self, *a):
        self.price_map = {}

    def prices(self, markets):
        return {m: self.price_map[m] for m in markets if m in self.price_map}

    def __getattr__(self, name):                      # 그 밖의 업비트 호출(주문·잔고)은 생기면 안 됨
        def _f(*a, **k):
            raise AssertionError(f"시험 중 업비트 {name} 호출이 생김")
        return _f


class FakeCloud:
    def __init__(self, *a):
        pass


class MonthLossTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg_path = os.path.join(self.tmp, "bot_config.env")
        self.patches = [
            mock.patch.object(cb, "CONFIG_FILE", self.cfg_path),
            mock.patch.object(cb, "SETTINGS_FILE", os.path.join(self.tmp, "bot_settings.json")),
            mock.patch.object(cb, "STATE_FILE", os.path.join(self.tmp, "bot_state.json")),
            mock.patch.object(cb, "LOG_FILE", os.path.join(self.tmp, "bot.log")),
            mock.patch.object(cb, "datetime", FakeDatetime),
            mock.patch.object(cb, "Upbit", FakeUpbit),
            mock.patch.object(cb, "Cloud", FakeCloud),
            mock.patch.object(cb.requests, "get", no_network),
            mock.patch.object(cb.requests, "post", no_network),
            mock.patch.object(cb.requests, "request", no_network),
            mock.patch.object(cb.requests.Session, "request", no_network),
        ]
        for p in self.patches:
            p.start()
        self.set_now(2026, 11, 14, 15, 0)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    # ---------- 도우미 ----------
    def set_now(self, *ymdhm):
        FakeClock.now_value = datetime(*ymdhm, tzinfo=KST)

    def make_bot(self, month_limit=None, daily_limit=0, mode="paper"):
        lines = ["UPBIT_ACCESS_KEY=test-access", "UPBIT_SECRET_KEY=test-secret", "FIREBASE_LINK_CODE=test-link",
                 f"TRADE_MODE={mode}", f"DAILY_LOSS_LIMIT={daily_limit}"]
        if month_limit is not None:
            lines.append(f"MONTH_LOSS_LIMIT={month_limit}")
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        bot = cb.Bot(cb.load_config())
        bot.roll_day()
        return bot

    @staticmethod
    def trade(bot, pnl):
        """실제 매도와 같이 종목을 붙여 장부에 넣음. 월 한도만 보려고 시드는 다시 채움(시드 부족 판정과 섞이지 않게)"""
        bot.book(pnl, "KRW-TST")
        bot.ledger["seed"] = max(bot.ledger["seed"], 200000.0)

    # ---------- 1. 기본값 0 = 기존 동작 ----------
    def test_default_is_zero_and_unlimited(self):
        bot = self.make_bot()                                         # 설정 파일에 MONTH_LOSS_LIMIT 없음
        self.assertEqual(bot.cfg["MONTH_LOSS_LIMIT"], 0)
        for _ in range(50):
            self.trade(bot, -100000)                                         # 한 달에 -500만 원
        self.assertEqual(bot.ledger["monthPnl"], -5000000)
        self.assertEqual(bot.can_enter(), (True, ""))

    def test_zero_explicit_is_unlimited(self):
        bot = self.make_bot(month_limit=0)
        self.trade(bot, -9999999)
        self.assertTrue(bot.can_enter()[0])
        self.assertFalse(bot.month_blocked())

    # ---------- 2. 한도 전 / 도달 ----------
    def test_before_limit_allows_buy(self):
        bot = self.make_bot(month_limit=50000)
        self.trade(bot, -30000)
        self.trade(bot, +5000)
        self.trade(bot, -24000)                                              # 합계 -49,000 (한도 -50,000 전)
        self.assertEqual(round(bot.ledger["monthPnl"]), -49000)
        self.assertEqual(bot.ledger["monthTrades"], 3)
        self.assertEqual(bot.can_enter(), (True, ""))

    def test_reaching_limit_blocks_buy(self):
        bot = self.make_bot(month_limit=50000)
        self.trade(bot, -30000)
        self.trade(bot, -20000)                                              # 정확히 -50,000 → 멈춤
        ok, why = bot.can_enter()
        self.assertFalse(ok)
        self.assertIn("이번 달 손실 한도", why)
        bot.scan_entries()                                            # 매수 탐색이 업비트를 부르지 않고 바로 끝나야 함

    def test_losses_over_several_days_add_up(self):
        bot = self.make_bot(month_limit=50000, daily_limit=40000)
        self.set_now(2026, 11, 3, 12, 0); bot.roll_day()
        self.trade(bot, -30000)                                              # 하루 한도(-40,000) 안
        self.set_now(2026, 11, 10, 12, 0); bot.roll_day()
        self.assertEqual(bot.ledger["dayPnl"], 0.0)                   # 날짜 바뀌면 하루 손익은 0부터
        self.assertTrue(bot.can_enter()[0])
        self.trade(bot, -25000)                                              # 하루 -25,000, 월 -55,000
        ok, why = bot.can_enter()
        self.assertFalse(ok)
        self.assertIn("이번 달 손실 한도", why)

    def test_profit_reduces_month_loss(self):
        bot = self.make_bot(month_limit=50000)
        self.trade(bot, -50000)
        self.assertFalse(bot.can_enter()[0])
        self.trade(bot, +10000)                                              # 보유분 매도 수익이 나면 다시 한도 안
        self.assertTrue(bot.can_enter()[0])

    # ---------- 3. 다음 달 초기화 (09:00 KST 경계) ----------
    def test_resets_on_first_day_0900_kst(self):
        bot = self.make_bot(month_limit=50000)
        self.set_now(2026, 11, 30, 20, 0); bot.roll_day()
        self.trade(bot, -60000)
        self.assertFalse(bot.can_enter()[0])
        self.set_now(2026, 12, 1, 8, 59); bot.roll_day()              # 12/1 08:59 = 아직 11월 거래월
        self.assertEqual(bot.ledger["month"], "2026-11")
        self.assertFalse(bot.can_enter()[0])
        self.set_now(2026, 12, 1, 9, 0); bot.roll_day()               # 12/1 09:00 = 12월 시작
        self.assertEqual(bot.ledger["month"], "2026-12")
        self.assertEqual(bot.ledger["monthPnl"], 0.0)
        self.assertEqual(bot.ledger["monthTrades"], 0)
        self.assertEqual(bot.can_enter(), (True, ""))

    def test_reset_without_roll_day_call(self):
        """roll_day 를 거치지 않아도 판정 시점에 달이 바뀌었으면 풀림"""
        bot = self.make_bot(month_limit=50000)
        self.trade(bot, -50000)
        self.set_now(2026, 12, 1, 9, 1)
        self.assertTrue(bot.can_enter()[0])
        self.assertEqual(bot.ledger["month"], "2026-12")

    def test_month_key_matches_today_key(self):
        cases = [((2026, 12, 1, 8, 59), "2026-11"), ((2026, 12, 1, 9, 0), "2026-12"),
                 ((2027, 1, 1, 8, 0), "2026-12"), ((2027, 1, 1, 9, 0), "2027-01")]
        for ymdhm, want in cases:
            now = datetime(*ymdhm, tzinfo=KST)
            self.assertEqual(cb.month_key(now), want, ymdhm)
            self.assertEqual(cb.month_key(now), cb.today_key(now)[:7])

    def test_state_survives_restart(self):
        bot = self.make_bot(month_limit=50000)
        self.trade(bot, -50000)
        bot2 = cb.Bot(bot.cfg)                                       # 재시작(장부 파일 다시 읽음)
        self.assertEqual(bot2.ledger["monthPnl"], -50000)
        self.assertFalse(bot2.can_enter()[0])

    # ---------- 4. 보유분 매도·손절은 평소대로 ----------
    def test_sell_and_stoploss_still_run_when_blocked(self):
        bot = self.make_bot(month_limit=50000)
        self.trade(bot, -50000)
        self.assertFalse(bot.can_enter()[0])
        now_ms = FakeClock.now_value.timestamp() * 1000
        bot.state["positions"] = {
            "KRW-AAA": {"tp": 110, "sl": 90, "deadline": 9e12, "at": now_ms, "cost": 10000, "volume": 100},
            "KRW-BBB": {"tp": 110, "sl": 90, "deadline": 9e12, "at": now_ms, "cost": 10000, "volume": 100},
        }
        bot.upbit.price_map = {"KRW-AAA": 85, "KRW-BBB": 120}         # AAA 손절선 아래, BBB 익절선 위
        calls = []
        with mock.patch.object(bot, "exit", lambda mk, price, reason: calls.append((mk, reason))):
            bot.manage_positions()
        self.assertIn(("KRW-AAA", "손절"), calls)
        self.assertIn(("KRW-BBB", "익절"), calls)

    # ---------- 5. 처음 켜질 때(기존 장부에 월 기록 없음) ----------
    def test_first_run_takes_over_today_only(self):
        bot = self.make_bot(month_limit=50000)
        lg = bot.ledger
        for k in ("month", "monthPnl", "monthTrades"):
            lg.pop(k, None)
        lg.update({"day": cb.today_key(), "dayPnl": -7000.0, "dayTrades": 4})
        bot.roll_month()
        self.assertEqual((lg["month"], lg["monthPnl"], lg["monthTrades"]), ("2026-11", -7000.0, 4))

    def test_first_run_ignores_last_month_day(self):
        bot = self.make_bot(month_limit=50000)
        lg = bot.ledger
        for k in ("month", "monthPnl", "monthTrades"):
            lg.pop(k, None)
        lg.update({"day": "2026-10-31", "dayPnl": -7000.0, "dayTrades": 4})
        bot.roll_month()
        self.assertEqual((lg["monthPnl"], lg["monthTrades"]), (0.0, 0))

    def test_old_state_keeps_other_ledger_values(self):
        bot = self.make_bot(month_limit=50000)
        lg = bot.ledger
        lg.pop("month", None)
        lg.update({"seed": 123456.0, "locked": 789.0, "realizedTotal": -8858.0, "tradesTotal": 256})
        bot.roll_month()
        self.assertEqual((lg["seed"], lg["locked"], lg["realizedTotal"], lg["tradesTotal"]), (123456.0, 789.0, -8858.0, 256))

    # ---------- 6. 대시보드 설정 ----------
    def test_dashboard_setting_range(self):
        out = cb.clean_app_settings({"MONTH_LOSS_LIMIT": "50,000"})
        self.assertEqual(out, {"MONTH_LOSS_LIMIT": "50000"})
        self.assertEqual(cb.clean_app_settings({"MONTH_LOSS_LIMIT": "0"}), {"MONTH_LOSS_LIMIT": "0"})
        self.assertEqual(cb.clean_app_settings({"MONTH_LOSS_LIMIT": "-1"}), {})
        self.assertEqual(cb.clean_app_settings({"MONTH_LOSS_LIMIT": "20000000"}), {})

    def test_dashboard_value_overrides_file(self):
        cb.write_app_settings_file({"MONTH_LOSS_LIMIT": "30000"}, 0)
        bot = self.make_bot(month_limit=0)
        self.assertEqual(bot.cfg["MONTH_LOSS_LIMIT"], 30000)
        view = cb.settings_view(bot.cfg)
        self.assertEqual(view["MONTH_LOSS_LIMIT"], {"value": "30000", "src": "app"})
        self.trade(bot, -30000)
        self.assertFalse(bot.can_enter()[0])

    def test_setting_belongs_to_upbit_bot_only(self):
        bot = self.make_bot()
        self.assertIn("MONTH_LOSS_LIMIT", cb.settings_view(bot.cfg))
        self.assertEqual(cb.settings_view(bot.cfg)["MONTH_LOSS_LIMIT"]["src"], "default")
        self.assertNotIn("MONTH_LOSS_LIMIT", cb.settings_view(bot.cfg, binance=True))
        new = {"MONTH_LOSS_LIMIT": "50000"}
        self.assertTrue(cb.app_settings_changed(bot.cfg, new, False))    # 업비트 봇만 다시 시작
        self.assertFalse(cb.app_settings_changed(bot.cfg, new, True))    # 바이낸스 엔진은 그대로


if __name__ == "__main__":
    unittest.main(verbosity=2)
