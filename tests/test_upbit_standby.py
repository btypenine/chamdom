"""업비트 봇 대기 모드(UPBIT_MODE=standby) 오프라인 시험

- 네트워크는 전부 막음(업비트·Firebase 호출이 생기면 시험 실패) — 실제 봇 코드(bot/chamdom_bot.py)를 그대로 불러 시험
- 시각은 가짜 시계로 바꿔 하루 동안의 명령·요청함 확인 횟수와 AI 정기 분석 횟수를 셈
- 실행: python -m unittest tests/test_upbit_standby.py -v   (저장소 최상위에서)
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
    now_value = datetime(2026, 11, 17, 10, 0, tzinfo=KST)

    @classmethod
    def ts(cls):
        return cls.now_value.timestamp()


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
        self.calls = []

    def prices(self, markets):
        return {m: self.price_map[m] for m in markets if m in self.price_map}

    def minute_candles(self, market, total):            # normal 모드 매수 탐색 확인용: 호출만 기록하고 봉 없음
        self.calls.append(("minute_candles", market))
        return []

    def __getattr__(self, name):                        # 그 밖의 업비트 호출(주문·잔고)은 생기면 안 됨
        def _f(*a, **k):
            raise AssertionError(f"시험 중 업비트 {name} 호출이 생김")
        return _f


class FakeCloud:
    """Firebase 대신: 읽기 호출 횟수만 셈"""

    def __init__(self, *a):
        self.reads = {"cmd": 0, "inbox": 0, "sug": 0}
        self.commands = []
        self.finished = []

    def pending_commands(self):
        self.reads["cmd"] += 1
        out, self.commands = self.commands, []
        return out

    def inbox_items(self):
        self.reads["inbox"] += 1
        return {}

    def suggestions(self):
        self.reads["sug"] += 1
        return []

    def finish_command(self, name, status, result):
        self.finished.append((name, status, result))


class StandbyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg_path = os.path.join(self.tmp, "bot_config.env")
        self.patches = [
            mock.patch.object(cb, "CONFIG_FILE", self.cfg_path),
            mock.patch.object(cb, "SETTINGS_FILE", os.path.join(self.tmp, "bot_settings.json")),
            mock.patch.object(cb, "STATE_FILE", os.path.join(self.tmp, "bot_state.json")),
            mock.patch.object(cb, "LOG_FILE", os.path.join(self.tmp, "bot.log")),
            mock.patch.object(cb, "datetime", FakeDatetime),
            mock.patch.object(cb.time, "time", FakeClock.ts),
            mock.patch.object(cb.time, "sleep", lambda s: None),
            mock.patch.object(cb, "Upbit", FakeUpbit),
            mock.patch.object(cb, "Cloud", FakeCloud),
            mock.patch.object(cb.requests, "get", no_network),
            mock.patch.object(cb.requests, "post", no_network),
            mock.patch.object(cb.requests, "request", no_network),
            mock.patch.object(cb.requests.Session, "request", no_network),
        ]
        for p in self.patches:
            p.start()
        self.set_now(2026, 11, 17, 10, 0)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    # ---------- 도우미 ----------
    def set_now(self, *ymdhm):
        FakeClock.now_value = datetime(*ymdhm, tzinfo=KST)

    def make_bot(self, mode=None, ai_key=False):
        lines = ["UPBIT_ACCESS_KEY=test-access", "UPBIT_SECRET_KEY=test-secret", "FIREBASE_LINK_CODE=test-link",
                 "TRADE_MODE=paper", "DAILY_LOSS_LIMIT=0"]
        if mode is not None:
            lines.append(f"UPBIT_MODE={mode}")
        if ai_key:
            lines.append("ANTHROPIC_API_KEY=test-ai")
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        bot = cb.Bot(cb.load_config())
        bot.roll_day()
        bot.ledger["seed"] = 200000.0
        bot.upload_status = lambda: None                 # 상태 올리기(쓰기)는 이 시험 대상 아님
        return bot

    def run_day(self, bot, step_s=5):
        """보유 중 루프(5초)처럼 24시간 동안 명령·요청함·친구 제안함 확인을 돌림 → 읽기 호출 횟수"""
        start = FakeClock.now_value
        t = 0
        while t < 24 * 3600:
            FakeClock.now_value = start + timedelta(seconds=t)
            bot.handle_commands()
            bot.handle_inbox()
            bot.handle_suggestions()
            t += step_s
        return dict(bot.cloud.reads)

    def held_positions(self):
        now_ms = FakeClock.ts() * 1000
        return {
            "KRW-AAA": {"tp": 110, "sl": 90, "deadline": 9e12, "at": now_ms, "cost": 10000, "volume": 100},
            "KRW-BBB": {"tp": 110, "sl": 90, "deadline": 9e12, "at": now_ms, "cost": 10000, "volume": 100},
            "KRW-CCC": {"tp": 110, "sl": 90, "deadline": 9e12, "at": now_ms, "cost": 10000, "volume": 100},
            "KRW-DDD": {"tp": 110, "sl": 90, "deadline": FakeClock.ts() - 1, "at": now_ms, "cost": 10000, "volume": 100},
        }

    # ---------- 1. 기본값 normal = 기존 동작 ----------
    def test_default_is_normal(self):
        bot = self.make_bot()                                        # 설정 파일에 UPBIT_MODE 없음
        self.assertEqual(bot.cfg["UPBIT_MODE"], "normal")
        self.assertFalse(bot.standby)
        self.assertEqual(bot.can_enter(), (True, ""))

    def test_unknown_value_falls_back_to_normal(self):
        for v in ("", "STANDBY ", "pause", "stop", " OFF"):
            bot = self.make_bot(mode=v)
            want = v.strip().lower() if v.strip().lower() in ("standby", "off") else "normal"
            self.assertEqual(bot.cfg["UPBIT_MODE"], want, v)

    # ---------- 1-B. off(꺼짐) = 대기와 같은 동작 + 상태에 'off' 표시 ----------
    def test_off_behaves_like_standby(self):
        bot = self.make_bot(mode="off")
        self.assertEqual(bot.cfg["UPBIT_MODE"], "off")
        self.assertTrue(bot.standby)
        ok, why = bot.can_enter()
        self.assertFalse(ok)
        self.assertIn("대기 모드", why)
        self.assertEqual(bot.cloud_every("cmd"), cb.STANDBY_CMD_EVERY)
        self.assertEqual(bot.cloud_every("inbox"), cb.STANDBY_INBOX_EVERY)
        self.assertIn("off", cb.APP_SETTINGS["UPBIT_MODE"][1])

    def test_off_scan_makes_no_buys(self):
        bot = self.make_bot(mode="off")
        bot.state["strategies"] = {f"KRW-T{i}": {"hold": 10, "sl": -1} for i in range(5)}
        with mock.patch.object(bot, "enter", side_effect=AssertionError("꺼짐 모드에서 매수가 생김")):
            for _ in range(20):
                bot.scan_entries()

    def test_normal_intervals_unchanged(self):
        bot = self.make_bot(mode="normal")
        self.assertEqual(bot.cloud_every("cmd"), cb.CLOUD_CMD_EVERY)
        self.assertEqual(bot.cloud_every("inbox"), cb.CLOUD_INBOX_EVERY)
        self.assertEqual(bot.cloud_every("sug"), cb.CLOUD_SUG_EVERY)
        self.assertEqual((cb.CLOUD_CMD_EVERY, cb.CLOUD_INBOX_EVERY, cb.CLOUD_SUG_EVERY), (15, 60, 600))

    def test_normal_day_read_counts(self):
        reads = self.run_day(self.make_bot())
        self.assertEqual(reads, {"cmd": 24 * 3600 // 15, "inbox": 24 * 60, "sug": 24 * 6})   # 5760 · 1440 · 144

    def test_normal_scan_still_looks_for_entries(self):
        bot = self.make_bot()
        bot.state["strategies"] = {"KRW-TST": {"hold": 10, "sl": -1}}
        bot.scan_entries()
        self.assertIn(("minute_candles", "KRW-TST"), bot.upbit.calls)   # normal 은 평소처럼 매수 신호를 찾음

    def test_normal_ai_schedule_unchanged(self):
        self.assertEqual(cb.AI_SCHEDULE_HOURS, [3, 9, 15, 21])
        for h, want in ((2, "2026-11-16 21시"), (3, "2026-11-17 3시"), (14, "2026-11-17 9시"), (23, "2026-11-17 21시")):
            self.assertEqual(cb.Bot.ai_slot(datetime(2026, 11, 17, h, 0, tzinfo=KST)), want)
        self.assertEqual(self.count_ai_runs(self.make_bot(ai_key=True)), 4)

    # ---------- 2. standby: 신규 매수 0 ----------
    def test_standby_blocks_new_buys(self):
        bot = self.make_bot(mode="standby")
        self.assertTrue(bot.standby)
        ok, why = bot.can_enter()
        self.assertFalse(ok)
        self.assertIn("대기 모드", why)
        self.assertIn("신규 진입 중지: 업비트 대기 모드", bot.summary())

    def test_standby_scan_makes_no_upbit_calls_and_no_buys(self):
        bot = self.make_bot(mode="standby")
        bot.state["strategies"] = {f"KRW-T{i}": {"hold": 10, "sl": -1} for i in range(10)}
        with mock.patch.object(bot, "enter", side_effect=AssertionError("대기 모드에서 매수가 생김")):
            for _ in range(100):
                bot.scan_entries()
        self.assertEqual(bot.upbit.calls, [])
        self.assertEqual(bot.state["positions"], {})

    def test_pause_still_wins_over_standby(self):
        bot = self.make_bot(mode="standby")
        bot.state["paused"] = True
        self.assertEqual(bot.can_enter(), (False, "일시정지"))

    # ---------- 3. standby: 보유분은 기존 규칙대로 정리, 강제 매도 없음 ----------
    def test_standby_keeps_managing_positions(self):
        bot = self.make_bot(mode="standby")
        bot.state["positions"] = self.held_positions()
        bot.upbit.price_map = {"KRW-AAA": 85, "KRW-BBB": 120, "KRW-CCC": 100, "KRW-DDD": 100}
        calls = []
        with mock.patch.object(bot, "exit", lambda mk, price, reason: calls.append((mk, reason))):
            bot.manage_positions()
        got = dict(calls)
        self.assertEqual(got.get("KRW-AAA"), "손절")
        self.assertEqual(got.get("KRW-BBB"), "익절")
        self.assertIn("KRW-DDD", got)                              # 보유시간 초과 정리
        self.assertNotIn("KRW-CCC", got)                           # 익절·손절선 안쪽은 그대로 보유(강제 매도 없음)

    def test_standby_and_normal_manage_positions_identically(self):
        results = []
        for mode in ("normal", "standby"):
            bot = self.make_bot(mode=mode)
            bot.state["positions"] = self.held_positions()
            bot.upbit.price_map = {"KRW-AAA": 85, "KRW-BBB": 120, "KRW-CCC": 100, "KRW-DDD": 100}
            calls = []
            with mock.patch.object(bot, "exit", lambda mk, price, reason: calls.append((mk, reason))):
                bot.manage_positions()
            results.append(sorted(calls))
        self.assertEqual(results[0], results[1])

    # ---------- 4. standby: 확인 주기 늘림 ----------
    def test_standby_intervals(self):
        bot = self.make_bot(mode="standby")
        self.assertEqual(bot.cloud_every("cmd"), 120)
        self.assertEqual(bot.cloud_every("inbox"), 600)
        self.assertEqual(bot.cloud_every("sug"), 3600)
        self.assertLess(bot.cloud_every("cmd"), cb.COMMAND_MAX_AGE)  # 10분 지난 명령 거절 기준보다 짧아야 비상 정리가 안 버려짐

    def test_standby_day_read_counts(self):
        reads = self.run_day(self.make_bot(mode="standby"))
        self.assertEqual(reads, {"cmd": 720, "inbox": 144, "sug": 24})

    def test_standby_emergency_commands_still_work(self):
        bot = self.make_bot(mode="standby")
        bot.state["positions"] = {"KRW-AAA": {"tp": 110, "sl": 90, "deadline": 9e12, "at": 0, "cost": 10000,
                                              "volume": 100, "unit": 100}}
        bot.upbit.price_map = {"KRW-AAA": 100}
        bot.handle_commands()                                      # 첫 확인
        created = (FakeClock.now_value + timedelta(seconds=5)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        bot.cloud.commands = [{"type": "close_bot_positions", "createdAt": created, "_name": "users/u/commands/c1"}]
        sold = []
        self.set_now(2026, 11, 17, 10, 1)                          # 60초 뒤: 아직 확인 안 함
        bot.handle_commands()
        self.assertEqual(bot.cloud.finished, [])
        self.set_now(2026, 11, 17, 10, 2)                          # 120초 뒤: 처리
        with mock.patch.object(bot, "exit", lambda mk, price, reason: sold.append((mk, reason))):
            bot.handle_commands()
        self.assertEqual(sold, [("KRW-AAA", "비상 정리")])
        self.assertEqual(bot.cloud.finished[0][1], "done")
        self.assertTrue(bot.state["paused"])

    # ---------- 5. standby: AI 정기 분석 하루 1번 ----------
    def count_ai_runs(self, bot, hours=24):
        bot.state["tradeLog"] = [{"pnl": 1}] * 5
        bot.state["aiSlotDone"] = bot.ai_slot(hours=cb.STANDBY_AI_HOURS if bot.standby else None)   # 시작 시각 몫은 이미 한 것으로
        runs = 0
        start = FakeClock.now_value
        for m in range(0, hours * 60, 10):
            FakeClock.now_value = start + timedelta(minutes=m)
            if bot.ai_should_run():
                runs += 1
        return runs

    def test_standby_ai_once_a_day(self):
        bot = self.make_bot(mode="standby", ai_key=True)
        self.assertEqual(self.count_ai_runs(bot, hours=72), 3)

    def test_switch_to_standby_does_not_trigger_extra_ai(self):
        self.set_now(2026, 11, 17, 16, 0)
        bot = self.make_bot(mode="normal", ai_key=True)
        bot.state["tradeLog"] = [{"pnl": 1}] * 5
        self.assertTrue(bot.ai_should_run())                       # normal: 15시 정기 분석
        bot2 = self.make_bot(mode="standby", ai_key=True)          # 대기 모드로 바꿔 재시작(장부 이어받음)
        bot2.state["aiSlotDone"] = bot.state["aiSlotDone"]
        bot2.state["tradeLog"] = [{"pnl": 1}] * 5
        self.assertFalse(bot2.ai_should_run())                     # 이미 15시에 했으니 오늘 09시 몫은 다시 안 함
        self.set_now(2026, 11, 18, 9, 0)
        self.assertTrue(bot2.ai_should_run())                      # 다음 날 09시에 1번

    # ---------- 6. 대시보드 설정 ----------
    def test_dashboard_setting_values(self):
        self.assertEqual(cb.clean_app_settings({"UPBIT_MODE": "standby"}), {"UPBIT_MODE": "standby"})
        self.assertEqual(cb.clean_app_settings({"UPBIT_MODE": "Normal"}), {"UPBIT_MODE": "normal"})
        self.assertEqual(cb.clean_app_settings({"UPBIT_MODE": "sell_all"}), {})

    def test_dashboard_value_overrides_file(self):
        cb.write_app_settings_file({"UPBIT_MODE": "standby"}, 0)
        bot = self.make_bot(mode="normal")
        self.assertTrue(bot.standby)
        self.assertEqual(cb.settings_view(bot.cfg)["UPBIT_MODE"], {"value": "standby", "src": "app"})

    def test_setting_belongs_to_upbit_bot_only(self):
        bot = self.make_bot()
        self.assertEqual(cb.settings_view(bot.cfg)["UPBIT_MODE"], {"value": "normal", "src": "default"})
        self.assertNotIn("UPBIT_MODE", cb.settings_view(bot.cfg, binance=True))
        new = {"UPBIT_MODE": "standby"}
        self.assertTrue(cb.app_settings_changed(bot.cfg, new, False))    # 업비트 봇만 다시 시작
        self.assertFalse(cb.app_settings_changed(bot.cfg, new, True))    # 바이낸스 엔진은 그대로

    def test_settings_check_interval_unchanged_for_switch_back(self):
        self.assertEqual(cb.APP_SETTINGS_EVERY, 600)              # 대기 모드에서도 설정은 10분마다 확인 → normal 로 되돌리기 가능
        self.assertEqual(cb.STATUS_EVERY, 30)                     # 상태 올리기 그대로 → 대시보드 '연결 끊김'(3분) 안 생김


if __name__ == "__main__":
    unittest.main()
