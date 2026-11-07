# -*- coding: utf-8 -*-
"""
참돔 트레이드 PC 봇 v2 — 소액 반복 자동매매

동작 요약
- 매일 09:00(KST)에 업비트 원화마켓 중 최근 1년 거래대금 상위 10종목 선정 (보호 코인·유의 종목 제외)
- 종목마다 무작위 전략 조합을 1분봉으로 백테스트 → 승률 90% 이상 + 최근 구간 무손실인 것만 채택
- 1시간마다 새 데이터로 전략을 다시 고름
- 신호가 나오면 기본 1만원 시장가 매수 → 수수료 빼고 +0.5% 이상이면 매도(익절)
- 손절/최대 보유시간은 전략별로 백테스트에서 고른 값 사용
- 손실로 끝나면 30분간 전체 거래 정지, 마틴게일(손실 후 2배, 최대 단계 제한) 선택 가능
- 시드 20만원(설정값)에서 시작, 수익의 50%는 잠금(거래 불가), 50%는 시드에 편입, 손실은 시드에서 차감
- 보호 코인(XRP)은 절대 주문하지 않고, 웹페이지에도 표시하지 않음
- TRADE_MODE=live 일 때만 실제 주문. 없거나 paper 면 모의매매

실행: python chamdom_bot.py   (종료: Ctrl+C)
필요 패키지: python -m pip install requests pyjwt
"""
import hashlib
import json
import math
import os
import random
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode, unquote

try:
    import jwt
    import requests
except ImportError:
    print("필요 패키지가 없습니다. 먼저 실행하세요:\n  python -m pip install requests pyjwt")
    sys.exit(1)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ======================= 설정 =======================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "bot_config.env")
SETTINGS_FILE = os.path.join(BASE_DIR, "bot_settings.json")   # 대시보드 '봇 설정'에서 바꾼 값 (bot_config.env 보다 우선)
# 대시보드에서 바꿀 수 있는 항목만 (키·연결 코드는 절대 웹에 올리지 않고 bot_config.env 에만 둠)
APP_SETTINGS = {
    "TRADE_MODE": ("choice", ("paper", "live")),
    "DAILY_LOSS_LIMIT": ("num", (0, 1000000)),
    "BINANCE_TRADE_MODE": ("choice", ("paper", "live")),
    "BINANCE_ORDER_USDT": ("num", (5, 200)),
    "BINANCE_DAILY_LOSS_USDT": ("num", (1, 500)),
    "BINANCE_MAX_LIVE": ("num", (1, 10)),
    "BINANCE_LIVE_BASIS": ("choice", ("rule", "side", "all")),
    "BINANCE_STYLE": ("choice", ("swing", "wide")),
    "BINANCE_MONTH_LOSS_USDT": ("num", (5, 2000)),
}
APP_SETTINGS_EVERY = 600                                       # 대시보드 설정 확인 주기(초) — 읽기 한도(429) 아끼려고 10분


def clean_app_settings(raw):
    """웹에서 온 값 검사: 정해진 항목·범위만 통과, 나머지는 버림"""
    out = {}
    for k, v in (raw or {}).items():
        rule = APP_SETTINGS.get(k)
        if not rule or v is None or str(v).strip() == "":
            continue
        kind, arg = rule
        v = str(v).strip().lower()
        if kind == "choice":
            if v in arg:
                out[k] = v
        else:
            try:
                x = float(v.replace(",", ""))
            except ValueError:
                continue
            if arg[0] <= x <= arg[1]:
                out[k] = str(int(x)) if x == int(x) else str(x)
    return out


def read_app_settings_file():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return clean_app_settings(json.load(f).get("values"))
    except Exception:
        return {}


def write_app_settings_file(values, updated_at):
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"values": values, "updatedAt": updated_at}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_FILE)


def app_settings_changed(cfg, new_values, prefix_binance):
    """이 프로그램(업비트 봇 / 바이낸스 엔진)과 관련된 값이 지금 실행 중인 값과 다른지"""
    old = cfg.get("_appSettings", {})
    for k in APP_SETTINGS:
        if k.startswith("BINANCE_") != prefix_binance:
            continue
        if old.get(k) != new_values.get(k):
            return True
    return False
STATE_FILE = os.path.join(BASE_DIR, "bot_state.json")
LOG_FILE = os.path.join(BASE_DIR, "bot.log")
# 분석용 보고서 (키·연결 코드 없음). Claude 예약 분석이 이 폴더만 읽음
REPORT_DIR = os.path.join(os.path.expanduser("~"), "Documents", "chamdom_report")

FIREBASE_API_KEY = "AIzaSyAriivkle0ogGBauc-VyyMm6YDgMhc8ccw"
FIREBASE_PROJECT = "chamdom-trade"
FS_BASE = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}/databases/(default)/documents"
UPBIT = "https://api.upbit.com"
KST = timezone(timedelta(hours=9))

PROTECTED_COINS = {"XRP"}        # 절대 거래 금지 + 화면 표시 안 함 (bot_config.env 의 PROTECTED_COINS 가 있으면 그 값으로 바뀜)
STABLE_COINS = {"USDT", "USDC", "DAI", "USDE", "USD1", "PYUSD", "TUSD"}  # 가격 고정 코인은 거래 대상 제외
MIN_ORDER_KRW = 5000
FEE_RATE = 0.0005                # 업비트 원화마켓 0.05%

# 탐험 모드 (2026-10-28 사용자 결정): 큰 수익보다 '많은 종목·많은 시도'를 소액으로 계속 돌리며 실전 기록으로 규칙을 걸러냄
EXPLORE_MODE = True
EXPLORE_ORDER_KRW = 6000         # 1회 주문 고정 (업비트 최소 5,000원 + 하락 여유) · bot_config.env 의 ORDER_KRW 보다 우선
EXPLORE_MARTINGALE = 0           # 마틴게일 끔 · bot_config.env 보다 우선

# 매매 규칙 (bot_config.env 에서 바꿀 수 있는 값은 load_config 참고)
TARGET_NET_PCT = 0.5             # 기본 익절(순수익 %) — 전략마다 백테스트로 따로 정함
LOSS_COOLDOWN_MIN = 7            # 손실(손절·보유시간 초과 손실) 후 신규 매수 정지(분) — 2026-10-28 사용자 요청 30→7
COOLDOWN_PER_MARKET = EXPLORE_MODE   # True: 손실 난 그 종목만 정지 / False: 전체 정지
PROFIT_LOCK_RATIO = 0.5          # 수익 중 잠그는 비율
TOP_N = 50 if EXPLORE_MODE else 10   # 거래대금 상위 종목 수
MAX_OPEN_POSITIONS = 30 if EXPLORE_MODE else 3   # 동시에 들고 있을 최대 종목 수

# 백테스트
STRATEGY_VERSION = 4             # 바뀌면 재시작 직후 전략을 새로 고름
BT_CANDLES = 4320                # 1분봉 개수(3일) — 20시간은 우연히 맞는 전략이 너무 많이 통과했음
BT_TRIALS = 200                  # 종목당 시험할 무작위 전략 수 (많을수록 우연히 맞는 전략이 늘어남)
BT_MIN_TRADES = 20               # 채택 최소 거래 수 (6회는 우연과 구분 불가)
BT_HOLDOUT_MIN_TRADES = 5        # 최근 검증 구간 최소 거래 수
BT_HOLDOUT_MIN_WINRATE = 0.80    # 최근 검증 구간 최소 승률
PROVEN_ONLY_LIVE = True          # 실전 통과(proven) 규칙만 실제 돈으로 주문, 검증 중 규칙은 모의 체결로 계속 시험·학습
SHADOW_SLIPPAGE = 0.0015         # 모의 체결 미끄러짐(매수·매도 각각) — 실전 평균보다 약간 보수적으로
MONKEY_MAX_OPEN = 40             # 원숭이 기준선(무작위 진입 비교) 동시 추적 최대 수 — 분석 전용
MONKEY_RECENT = 50               # 최근 성적은 마지막 50건으로 따로 봄
MOVE_RISE_PCT = 2.0              # 놓친 큰 움직임: 최근 60분 저점 대비 이만큼(%) 오르면 '큰 움직임'으로 기록 — 분석 전용
MOVE_WINDOW_MIN = 60             # 저점을 찾는 구간(분)
MOVE_FOLLOW_MIN = 60             # 감지 시점에 따라 샀다면 몇 분 뒤 성적을 볼지
MOVE_CHECK_MIN = (15, 30)        # 따라샀다면: 중간 시점 손익도 기록 (보유시간을 얼마로 잡을지 판단용) — 분석 전용
MOVE_STOP_PCT = -1.5             # 따라샀다면: 이 손절선을 걸었다면 어땠을지 가정 계산 — 분석 전용
BT_SLIPPAGE = 0.001              # (고정값 쓸 때) 시장가 체결 미끄러짐 0.1% (매수·매도 각각)
BT_SLIP_BASE = 0.0003            # 종목별 미끄러짐 = 0.03% + 호가 한 칸/가격 (BTC≈0.03%, 100원대 코인≈0.5~1%)
EXPLORE_MIN_TRADES = 6           # 탐험 모드: 학습 구간 최소 거래 수
EXPLORE_MIN_EDGE = 0.20          # 탐험 모드: 학습 구간 거래당 순익(비용 포함) 최소 % (bt-lab 10-28: 0%→-0.09%/건, 0.2%→+0.22%/건, 거래 수 약 절반)
EXPLORE_HOLDOUT_FLOOR = -0.10    # 탐험 모드: 검증 구간 거래당 순익이 이보다 나쁘면 탈락
COND_LEARN_MIN = 10              # 조건별 실전 기록이 이만큼 쌓이면 무작위 전략 생성 확률에 반영
SHAPE_LEARN_MIN = 20             # 익절폭·보유시간 칸별 실전 기록이 이만큼(+규칙 3개 이상) 쌓이면 생성 확률에 반영
SHAPE_LEARN_RANGE = (0.5, 2.0)   # 익절폭·보유시간 학습 가중치 범위 (조건 학습 0.3~3배보다 완만하게)
BT_MIN_WINRATE = 0.90            # (참고용) 이전 고정 기준
BT_MIN_EDGE_PCT = 0.10           # 거래당 평균 순수익 최소 % (학습·검증 구간 모두)
BT_MIN_PROFIT_FACTOR = 1.5       # 총이익 / 총손실 최소 (학습 구간)
BT_HOLDOUT = 0.25                # 마지막 25% 구간은 따로 검증(손실이면 탈락)
REOPTIMIZE_MIN = 60              # 전략 재선정 주기(분)

# 실전 학습: 그날 실제 거래에서 이 기준보다 못하면 그 종목은 다음 날 9시까지 제외
LIVE_MIN_TRADES = 3              # 판단에 필요한 최소 거래 수
LIVE_MIN_WINRATE = 2 / 3         # 이보다 낮으면 제외 (3전 2승은 통과 — 0.67로 두면 2/3=0.666… 이 걸려 버렸음)
LIVE_MAX_LOSS_STREAK = 2         # 같은 종목 연속 손실 횟수 한도
LIVE_DAY_MIN_TRADES = 8          # 하루 거래가 이만큼 쌓였는데
LIVE_DAY_MIN_WINRATE = 0.50      # 승률이 이보다 낮으면 다음 날 9시까지 신규 매수 중지
# 규칙 장부: 종목+규칙마다 실전 성적을 날짜와 상관없이 계속 기록
RULE_EVAL_TRADES = 5             # 이 횟수를 채울 때까지 규칙을 바꾸지 않음, 이후 판정
RULE_RECHECK = (10, 20, 40)      # 이 횟수마다 다시 판정 (5회는 표본이 작아 운 영향 큼)
RULE_PROMOTE_MIN_AVG = 0.15      # 실주문 자격(통과): 거래당 +0.15% 이상 — 0% 근처는 체결 비용·운 차이로 실전에서 뒤집히기 쉬움
RULE_POST_CHECK = 5              # (참고용) 통과 '이후' 성적 집계 단위
RULE_OUT_WINDOW = 5              # 3아웃제: 규칙마다 5회 묶음으로 성적을 봄 → 그 5회 거래당 0% 이하면 '1아웃'
RULE_OUT_LIMIT = 3               # 3아웃이면 퇴출(다시 채택 안 함). 1·2아웃은 실주문 자격만 잃고 모의로 계속 시험
RULE_BOOK_MAX = 300              # 장부에 남길 최대 규칙 수 (퇴출 규칙부터 정리)
UNIVERSE_DAYS = 30               # 거래대금 순위 기간(최근 30일)
UNIVERSE_MIN_LISTED_DAYS = 90    # 상장 90일 미만 종목 제외

STATUS_EVERY = 30
LOOP_IDLE = 15                   # 보유 없을 때 확인 간격(초)
LOOP_ACTIVE = 5                  # 보유 중 확인 간격(초)
COMMAND_MAX_AGE = 10 * 60
CLOUD_CMD_EVERY = 15             # 대시보드 명령(일시정지·승인 등) 확인 간격(초) — 읽기 한도 절약
CLOUD_INBOX_EVERY = 60           # 요청함·승인함 확인 간격(초) — 읽기 한도(하루 5만) 아끼려고 1분
CLOUD_INBOX_FALLBACK_EVERY = 5   # 예비 요청함(사용자 문서 inbox 필드)은 5번에 1번만 같이 읽음(대시보드가 status/inbox 저장에 실패했을 때만 쓰는 곳)
CLOUD_SUG_EVERY = 10 * 60        # 친구 제안함 확인 간격(초)

# AI 분석가 (bot_config.env 에 ANTHROPIC_API_KEY 가 있을 때만 동작)
AI_MODEL_DEFAULT = "claude-sonnet-5-5"
AI_EVERY_LOSSES = 5              # 손실 거래가 이만큼 새로 쌓이면 분석
AI_MIN_INTERVAL_MIN = 60         # 손실 누적으로 인한 분석 최소 간격
AI_SCHEDULE_HOURS = [3, 9, 15, 21]  # 정기 분석 시각(KST) — 하루 4번
AI_TRADE_LOG = 60                # AI에게 보여줄 최근 거래 수

# 자동 업데이트: GitHub에 올라간 최신 코드를 받아 스스로 재시작 (start_bot.bat 이 다시 띄워줌)
UPDATE_URL = "https://raw.githubusercontent.com/btypenine/chamdom/main/bot/chamdom_bot.py"
UPDATE_URL_TMPL = "https://raw.githubusercontent.com/btypenine/chamdom/{branch}/bot/chamdom_bot.py"
UPDATE_EVERY = 10 * 60
RESTART_CODE = 3


class RestartForUpdate(Exception):
    pass


def check_update():
    """새 버전이 있으면 파일을 바꾸고 True"""
    me = os.path.abspath(__file__)
    try:
        res = requests.get(UPDATE_URL, params={"t": int(time.time())}, timeout=15)
        if res.status_code != 200:
            return False
        new = res.content.decode("utf-8").replace("\r\n", "\n")
        with open(me, encoding="utf-8") as f:
            cur = f.read().replace("\r\n", "\n")
        if new == cur:
            return False
        if "참돔 트레이드 PC 봇" not in new:
            return False
        compile(new, "chamdom_bot.py", "exec")          # 문법 오류가 있는 코드는 받지 않음
        with open(me + ".prev", "w", encoding="utf-8") as f:
            f.write(cur)
        tmp = me + ".new"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(new)
        os.replace(tmp, me)
        return True
    except Exception as e:
        print(f"[업데이트 확인 실패] {e}", flush=True)
        return False


ACTIVITY = deque(maxlen=60)       # 웹페이지 '실시간 기록'용 최근 로그


def log(msg):
    line = f"[{datetime.now(KST).strftime('%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    ACTIVITY.append(f"{datetime.now(KST).strftime('%H:%M:%S')} {msg}")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"[오류] 설정 파일이 없습니다: {CONFIG_FILE}")
        sys.exit(1)
    cfg = {}
    with open(CONFIG_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    app = read_app_settings_file()                                   # 대시보드에서 바꾼 값이 있으면 그 값을 씀
    cfg["_fileSettings"] = {k: cfg[k] for k in APP_SETTINGS if k in cfg}
    cfg.update(app)
    cfg["_appSettings"] = app
    missing = [k for k in ("UPBIT_ACCESS_KEY", "UPBIT_SECRET_KEY", "FIREBASE_LINK_CODE")
               if not cfg.get(k) or cfg[k].startswith("여기에")]
    if missing:
        print("[오류] bot_config.env 에 값이 비어 있습니다: " + ", ".join(missing))
        sys.exit(1)

    def num(key, default):
        try:
            return float(str(cfg.get(key, default)).replace(",", ""))
        except ValueError:
            return float(default)

    cfg["BOT_BUDGET_KRW"] = int(num("BOT_BUDGET_KRW", 200000))
    cfg["ORDER_KRW"] = int(num("ORDER_KRW", 10000))
    cfg["MARTINGALE_MAX_LEVEL"] = int(num("MARTINGALE_MAX_LEVEL", 3))     # 0이면 마틴게일 끔
    cfg["DAILY_LOSS_LIMIT"] = int(num("DAILY_LOSS_LIMIT", 100000))       # 0이면 제한 없음
    cfg["TRADE_MODE"] = cfg.get("TRADE_MODE", "paper").lower()
    cfg["DEVICE_NAME"] = cfg.get("DEVICE_NAME") or "PC"
    key = cfg.get("ANTHROPIC_API_KEY", "")
    cfg["ANTHROPIC_API_KEY"] = "" if not key or key.startswith("여기에") else key
    cfg["AI_MODEL"] = cfg.get("AI_MODEL") or AI_MODEL_DEFAULT
    # 사용자별 설정 (친구용): 없으면 기존 값 그대로
    cfg["ROLE"] = (cfg.get("ROLE") or "owner").lower()             # owner | friend
    if "PROTECTED_COINS" in cfg:                                     # 예: XRP,DOGE  (빈 값이면 보호 코인 없음)
        PROTECTED_COINS.clear()
        PROTECTED_COINS.update(c.strip().upper() for c in cfg["PROTECTED_COINS"].split(",") if c.strip())
    channel = (cfg.get("UPDATE_CHANNEL") or ("stable" if cfg["ROLE"] == "friend" else "main")).lower()
    if channel not in ("main", "stable"):
        channel = "stable"
    cfg["UPDATE_CHANNEL"] = channel
    global UPDATE_URL, REPORT_DIR
    UPDATE_URL = UPDATE_URL_TMPL.format(branch=channel)
    if cfg["ROLE"] == "friend":
        REPORT_DIR = ""                                              # 성민님 PC 분석용 보고서는 친구 PC에서 만들지 않음
    if EXPLORE_MODE:
        cfg["ORDER_KRW"] = EXPLORE_ORDER_KRW
        cfg["MARTINGALE_MAX_LEVEL"] = EXPLORE_MARTINGALE
    return cfg


def sync_app_settings(cloud, cfg, binance=False):
    """대시보드 설정을 받아 bot_settings.json 에 저장. 이 프로그램 관련 값이 바뀌었으면 True(→ 다시 시작해서 적용)"""
    doc = cloud.app_settings()
    if doc is None:
        return False
    values = clean_app_settings(doc.get("values"))
    if values != read_app_settings_file():
        write_app_settings_file(values, doc.get("updatedAt") or 0)
    return app_settings_changed(cfg, values, binance)


def settings_view(cfg, binance=False):
    """대시보드에 보여줄 '지금 적용 중인 값'과 출처(app=대시보드, file=bot_config.env, default=기본값)"""
    out = {}
    for k in APP_SETTINGS:
        if k.startswith("BINANCE_") != binance:
            continue
        src = "app" if k in cfg.get("_appSettings", {}) else ("file" if k in cfg.get("_fileSettings", {}) else "default")
        out[k] = {"value": str(cfg.get(k, "")), "src": src}
    return out


def today_key(now=None):
    """09:00 KST 기준 '거래일' (09:00 이전은 전날로 침)"""
    now = now or datetime.now(KST)
    return (now - timedelta(hours=9)).strftime("%Y-%m-%d")


def load_state(budget):
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("paused", False)
    s.setdefault("positions", {})      # 봇이 실제로(또는 모의로) 산 코인: {market: {...}}
    lg = s.setdefault("ledger", {})
    lg.setdefault("seed", float(budget))       # 거래 가능한 시드
    lg.setdefault("locked", 0.0)               # 잠긴 수익(거래 불가)
    lg.setdefault("realizedTotal", 0.0)
    lg.setdefault("tradesTotal", 0)
    lg.setdefault("winsTotal", 0)
    lg.setdefault("day", "")
    lg.setdefault("dayPnl", 0.0)
    lg.setdefault("dayTrades", 0)
    lg.setdefault("dayWins", 0)
    lg.setdefault("cooldownUntil", 0)
    lg.setdefault("martLevel", 0)
    lg.setdefault("gateTrades", 0)             # 현재 전략 기준으로 센 오늘 거래 수 (부진 판단용)
    lg.setdefault("gateWins", 0)
    s.setdefault("universe", {"day": "", "markets": []})
    s.setdefault("strategies", {})     # {market: spec}
    s.setdefault("lastOptimize", 0)
    s.setdefault("lastCandle", {})
    s.setdefault("marketStats", {})    # 오늘 종목별 실전 성적
    s.setdefault("blocked", [])        # 오늘 제외된 종목
    s.setdefault("lastReport", None)   # 전날 결산
    s.setdefault("ruleBook", {})       # 규칙 장부 {rid: {...}} — 매일 초기화하지 않음
    s.pop("paper", None)               # v1 모의매매 기록 정리
    return s


def save_state(s):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


# ======================= 업비트 =======================
class UpbitError(Exception):
    def __init__(self, name, message):
        super().__init__(f"{name}: {message}")
        self.name = name


class Upbit:
    def __init__(self, access, secret):
        self.access, self.secret = access, secret

    def _headers(self, params=None):
        payload = {"access_key": self.access, "nonce": str(uuid.uuid4())}
        if params:
            q = unquote(urlencode(params, doseq=True)).encode("utf-8")
            payload["query_hash"] = hashlib.sha512(q).hexdigest()
            payload["query_hash_alg"] = "SHA512"
        token = jwt.encode(payload, self.secret, algorithm="HS256")
        if isinstance(token, bytes):
            token = token.decode()
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def _check(res):
        if res.status_code in (200, 201):
            return res.json()
        try:
            err = res.json().get("error", {})
            raise UpbitError(err.get("name", str(res.status_code)), err.get("message", res.text))
        except ValueError:
            raise UpbitError(str(res.status_code), res.text[:200])

    def _public(self, path, params):
        for attempt in range(5):
            res = requests.get(f"{UPBIT}{path}", params=params, timeout=10)
            if res.status_code == 429:
                time.sleep(1 + attempt)
                continue
            return self._check(res)
        raise UpbitError("429", "요청이 너무 많습니다")

    # --- 시세 ---
    def markets_detail(self):
        return self._public("/v1/market/all", {"isDetails": "true"})

    def prices(self, markets):
        if not markets:
            return {}
        data = self._public("/v1/ticker", {"markets": ",".join(markets)})
        return {d["market"]: float(d["trade_price"]) for d in data}

    def day_candles(self, market, count=200, to=None):
        params = {"market": market, "count": count}
        if to:
            params["to"] = to
        return self._public("/v1/candles/days", params)

    def minute_candles(self, market, total):
        """1분봉 total개, 오래된 것 → 최신 순"""
        out, to = [], None
        while len(out) < total:
            params = {"market": market, "count": min(200, total - len(out))}
            if to:
                params["to"] = to
            data = self._public("/v1/candles/minutes/1", params)
            if not data:
                break
            out.extend(data)
            to = data[-1]["candle_date_time_utc"] + "Z"
            time.sleep(0.12)
        out.reverse()
        return out

    # --- 계정/주문 ---
    def accounts(self):
        return self._check(requests.get(f"{UPBIT}/v1/accounts", headers=self._headers(), timeout=10))

    def _guard(self, market):
        if market.split("-")[1] in PROTECTED_COINS:
            raise UpbitError("protected", f"{market}는 거래 금지 종목입니다")

    def buy_market(self, market, krw):
        self._guard(market)
        params = {"market": market, "side": "bid", "ord_type": "price", "price": str(int(krw))}
        return self._check(requests.post(f"{UPBIT}/v1/orders", json=params, headers=self._headers(params), timeout=10))

    def sell_market(self, market, volume):
        self._guard(market)
        params = {"market": market, "side": "ask", "ord_type": "market", "volume": f"{volume:.8f}"}
        return self._check(requests.post(f"{UPBIT}/v1/orders", json=params, headers=self._headers(params), timeout=10))

    def order(self, order_uuid):
        params = {"uuid": order_uuid}
        return self._check(requests.get(f"{UPBIT}/v1/order", params=params, headers=self._headers(params), timeout=10))

    def wait_fill(self, order_uuid, tries=10):
        """체결 결과: (체결수량, 체결금액 합, 수수료)"""
        info = None
        for _ in range(tries):
            time.sleep(0.6)
            info = self.order(order_uuid)
            if info.get("state") in ("done", "cancel") and float(info.get("executed_volume", 0)) > 0:
                break
        if not info:
            raise UpbitError("fill", "주문 조회 실패")
        vol = float(info.get("executed_volume", 0))
        funds = sum(float(t["funds"]) for t in info.get("trades", []))
        fee = float(info.get("paid_fee", 0))
        return vol, funds, fee


# ======================= Firebase =======================
def to_fs(v):
    if v is None:
        return {"nullValue": None}
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"integerValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, str):
        return {"stringValue": v}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {"values": [to_fs(x) for x in v]}}
    if isinstance(v, dict):
        return {"mapValue": {"fields": {k: to_fs(x) for k, x in v.items()}}}
    return {"stringValue": str(v)}


def from_fs(v):
    if "stringValue" in v:
        return v["stringValue"]
    if "integerValue" in v:
        return int(v["integerValue"])
    if "doubleValue" in v:
        return v["doubleValue"]
    if "booleanValue" in v:
        return v["booleanValue"]
    if "timestampValue" in v:
        return v["timestampValue"]
    if "mapValue" in v:
        return {k: from_fs(x) for k, x in v["mapValue"].get("fields", {}).items()}
    if "arrayValue" in v:
        return [from_fs(x) for x in v["arrayValue"].get("values", [])]
    return None


class Cloud:
    def __init__(self, link_code):
        self.refresh_token = link_code
        self.id_token = None
        self.uid = None
        self.expires_at = 0

    def _auth(self):
        if self.id_token and time.time() < self.expires_at - 300:
            return {"Authorization": f"Bearer {self.id_token}"}
        res = requests.post(
            f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}",
            data={"grant_type": "refresh_token", "refresh_token": self.refresh_token}, timeout=10)
        if res.status_code != 200:
            raise RuntimeError("웹페이지 연결 코드가 맞지 않거나 만료되었습니다. 웹페이지에서 연결 코드를 다시 복사해 bot_config.env 에 넣으세요.")
        d = res.json()
        self.id_token, self.uid = d["id_token"], d["user_id"]
        self.refresh_token = d.get("refresh_token", self.refresh_token)
        self.expires_at = time.time() + int(d.get("expires_in", 3600))
        return {"Authorization": f"Bearer {self.id_token}"}

    def connect(self):
        self._auth()
        return self.uid

    def inbox_items(self):
        """대시보드가 남긴 요청·승인 (status/inbox 문서, 없으면 사용자 문서의 inbox 필드). 명령 컬렉션은 보안 규칙상 정해진 명령만 허용돼서 따로 둠"""
        h = self._auth()
        out = {}
        self._inbox_n = getattr(self, "_inbox_n", 0) + 1
        urls = [(f"{FS_BASE}/users/{self.uid}/status/inbox", None)]
        if self._inbox_n % CLOUD_INBOX_FALLBACK_EVERY == 1:
            urls.append((f"{FS_BASE}/users/{self.uid}", "inbox"))
        for url, key in urls:
            try:
                res = requests.get(url, headers=h, timeout=10)
                if res.status_code == 429:
                    raise RuntimeError("429 읽기 한도 초과")
                if res.status_code != 200:
                    continue
                fields = {k: from_fs(v) for k, v in res.json().get("fields", {}).items()}
                if key:
                    fields = fields.get(key) or {}
                for k, v in fields.items():
                    if isinstance(v, dict) and v.get("kind"):
                        out[k] = v
            except Exception as e:
                if "429" in str(e):
                    raise                                       # 한도 초과는 위에서 10분 쉬게 알림
                continue
        return out

    def app_settings(self):
        """대시보드 '봇 설정' 값 (status/settings 문서, 안 되면 사용자 문서의 botSettings 필드). 없으면 None"""
        h = self._auth()
        best = None
        for url, key in ((f"{FS_BASE}/users/{self.uid}/status/settings", None), (f"{FS_BASE}/users/{self.uid}", "botSettings")):
            res = requests.get(url, headers=h, timeout=10)
            if res.status_code == 429:
                raise RuntimeError("429 읽기 한도 초과")
            if res.status_code != 200:
                continue
            f = {k: from_fs(v) for k, v in res.json().get("fields", {}).items()}
            if key:
                f = f.get(key) or {}
            if isinstance(f, dict) and isinstance(f.get("values"), dict):
                if not best or (f.get("updatedAt") or 0) > (best.get("updatedAt") or 0):
                    best = f
            if best and not key:
                break                                                   # status/settings 가 있으면 사용자 문서는 안 읽음
        return best

    def suggestions(self, limit=50):
        """친구들이 대시보드 '요청하기'로 보낸 제안 (공용 suggestions 컬렉션, 운영자만 전부 읽을 수 있음)"""
        h = self._auth()
        body = {"structuredQuery": {"from": [{"collectionId": "suggestions"}],
                                    "orderBy": [{"field": {"fieldPath": "at"}, "direction": "DESCENDING"}],
                                    "limit": limit}}
        res = requests.post(f"{FS_BASE}:runQuery", json=body, headers=h, timeout=10)
        if res.status_code != 200:
            raise RuntimeError(f"친구 제안 확인 실패 {res.status_code}: {res.text[:150]}")
        out = []
        for row in res.json():
            d = row.get("document")
            if d:
                f = {k: from_fs(v) for k, v in d.get("fields", {}).items()}
                f["_id"] = d["name"].rsplit("/", 1)[-1]
                out.append(f)
        return out

    def put_status(self, data, partial=False):
        h = self._auth()
        url = f"{FS_BASE}/users/{self.uid}/status/current"
        if partial:
            url += "?" + "&".join(f"updateMask.fieldPaths={k}" for k in data)
        res = requests.patch(url, json={"fields": {k: to_fs(v) for k, v in data.items()}}, headers=h, timeout=10)
        if res.status_code != 200:
            raise RuntimeError(f"상태 업로드 실패 {res.status_code}: {res.text[:200]}")

    def add_trade(self, data):
        h = self._auth()
        res = requests.post(f"{FS_BASE}/users/{self.uid}/trades",
                            json={"fields": {k: to_fs(v) for k, v in data.items()}}, headers=h, timeout=10)
        if res.status_code != 200:
            raise RuntimeError(f"거래 기록 실패 {res.status_code}: {res.text[:200]}")

    def pending_commands(self):
        h = self._auth()
        body = {"structuredQuery": {
            "from": [{"collectionId": "commands"}],
            "where": {"fieldFilter": {"field": {"fieldPath": "status"}, "op": "EQUAL", "value": {"stringValue": "pending"}}},
            "limit": 20}}
        res = requests.post(f"{FS_BASE}/users/{self.uid}:runQuery", json=body, headers=h, timeout=10)
        if res.status_code != 200:
            raise RuntimeError(f"명령 확인 실패 {res.status_code}: {res.text[:200]}")
        out = []
        for row in res.json():
            d = row.get("document")
            if not d:
                continue
            fields = {k: from_fs(v) for k, v in d.get("fields", {}).items()}
            fields["_name"] = d["name"]
            out.append(fields)
        out.sort(key=lambda c: c.get("createdAt") or "")
        return out

    def finish_command(self, name, status, result):
        h = self._auth()
        fields = {"status": status, "result": result, "doneAt": int(time.time() * 1000)}
        mask = "&".join(f"updateMask.fieldPaths={k}" for k in fields)
        res = requests.patch(f"https://firestore.googleapis.com/v1/{name}?{mask}",
                             json={"fields": {k: to_fs(v) for k, v in fields.items()}}, headers=h, timeout=10)
        if res.status_code != 200:
            raise RuntimeError(f"명령 결과 기록 실패 {res.status_code}: {res.text[:200]}")


# ======================= 지표와 전략 =======================
def ema(values, n):
    k = 2 / (n + 1)
    out, e = [], values[0]
    for v in values:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def sma(values, n):
    out, s = [], 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        out.append(s / min(i + 1, n))
    return out


def rsi(closes, n):
    out = [50.0] * len(closes)
    gain = loss = 0.0
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        g, l = max(d, 0), max(-d, 0)
        if i <= n:
            gain += g / n
            loss += l / n
        else:
            gain = (gain * (n - 1) + g) / n
            loss = (loss * (n - 1) + l) / n
        out[i] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


def stdev(values, n):
    out = []
    for i in range(len(values)):
        w = values[max(0, i - n + 1): i + 1]
        m = sum(w) / len(w)
        out.append(math.sqrt(sum((x - m) ** 2 for x in w) / len(w)))
    return out


CONDITION_POOL = [
    ("rsi_below", [{"n": n, "t": t} for n in (7, 14) for t in (20, 25, 30, 35)]),
    ("bb_below", [{"n": 20, "k": k} for k in (1.5, 2.0, 2.5)]),
    ("ema_up", [{"f": f, "s": s} for f in (5, 9) for s in (20, 50)]),
    ("ema_cross", [{"f": f, "s": s} for f in (5, 9) for s in (20, 50)]),
    ("drop", [{"lb": lb, "p": p} for lb in (5, 10, 20) for p in (0.5, 1.0, 1.5, 2.0)]),
    ("breakout", [{"lb": lb} for lb in (10, 20, 30)]),
    ("vol_spike", [{"n": 20, "m": m} for m in (1.5, 2.0, 3.0)]),
    ("green", [{}]),
]
TP_CHOICES = [0.3, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0]     # 익절: 수수료 뺀 순수익 %
SL_CHOICES = [-0.5, -0.8, -1.0, -1.5, -2.0, -3.0, -5.0]  # 손절: 매수 단가 대비 %
HOLD_CHOICES = [30, 60, 120, 240]


def tick_size(price):
    """업비트 원화마켓 호가 단위"""
    # 2026-10-28 업비트 /v1/orderbook/instruments 실제 값과 54종목 대조해 맞춤 (기존 표는 100~1,000원대를 0.1원으로 봐서
    # 미끄러짐을 1/10로 과소 추정 → 비용에 지는 규칙이 대거 채택됐음)
    for floor, tick in ((2000000, 1000), (1000000, 1000), (500000, 500), (100000, 100), (50000, 50), (10000, 10),
                        (5000, 5), (1000, 1), (100, 1), (10, 0.1), (1, 0.01), (0.1, 0.001), (0.01, 0.0001),
                        (0.001, 0.00001), (0.0001, 0.000001)):
        if price >= floor:
            return tick
    return 0.0000001


def slip_for(price):
    return BT_SLIP_BASE + (tick_size(price) / price if price else 0.001)


def weighted_pick(rng, choices, wmap=None):
    """choices 중 하나를 wmap(값→가중치, 없으면 1) 비율로 뽑음. wmap 이 비면 rng.choice 와 같음"""
    if not wmap:
        return rng.choice(choices)
    w = [max(0.05, wmap.get(c, 1.0)) for c in choices]
    x, acc = rng.random() * sum(w), 0.0
    for c, wi in zip(choices, w):
        acc += wi
        if x <= acc:
            return c
    return choices[-1]


def random_spec(rng, weights=None, shape=None):
    """weights: 조건 이름별 가중치(실전 성적으로 학습). 없으면 균등
    shape: {"tp": {익절폭: 가중치}, "hold": {보유분: 가중치}} — 실전에서 덜 손해 본 익절폭·보유시간을 더 자주 시도"""
    k = rng.choice([1, 2, 2, 3])
    pool = list(range(len(CONDITION_POOL)))
    w = [max(0.05, (weights or {}).get(CONDITION_POOL[i][0], 1.0)) for i in pool]
    names = []
    for _ in range(k):
        x, acc = rng.random() * sum(w), 0.0
        for idx, wi in enumerate(w):
            acc += wi
            if x <= acc:
                break
        names.append(pool.pop(idx))
        w.pop(idx)
    conds = []
    for i in names:
        name, variants = CONDITION_POOL[i]
        conds.append([name, rng.choice(variants)])
    shape = shape or {}
    return {"conds": conds, "tp": weighted_pick(rng, TP_CHOICES, shape.get("tp")), "sl": rng.choice(SL_CHOICES),
            "hold": weighted_pick(rng, HOLD_CHOICES, shape.get("hold"))}


def describe(spec):
    names = {"rsi_below": "RSI과매도", "bb_below": "볼린저하단", "ema_up": "상승추세", "ema_cross": "골든크로스",
             "drop": "단기급락", "breakout": "고점돌파", "vol_spike": "거래량급증", "green": "양봉"}
    return "+".join(names[c[0]] for c in spec["conds"]) + f" / 익절+{spec.get('tp', TARGET_NET_PCT)}% / 손절{spec['sl']}% / 최대{spec['hold']}분"


def rule_id(market, spec):
    """종목 + 조건 + 익절·손절·보유시간 → 고유번호 (같은 규칙이면 날이 바뀌어도 같은 번호)"""
    key = json.dumps([market, sorted([c[0], sorted(c[1].items())] for c in spec["conds"]),
                      spec.get("tp", TARGET_NET_PCT), spec["sl"], spec["hold"]], ensure_ascii=False)
    return market.split("-")[1] + "-" + hashlib.sha1(key.encode()).hexdigest()[:8]


SPEC_KEYS = ("conds", "tp", "sl", "hold")
ENTRY_CHART_BARS = 60            # 대시보드 '진입 근거' 팝업에 보여줄 진입 직전 1분봉 수


def _enc(vals, ref):
    """가격 목록 → 기준가 대비 0.01% 단위 정수를 쉼표로 이은 문자열 (Firestore 용량 절약, 대시보드에서 되돌림)"""
    return ",".join(str(round((x / ref - 1) * 10000)) for x in vals)


def entry_snapshot(series, spec):
    """진입 순간 근거: 조건별 실제 값 + 진입 직전 1분봉·지표선 (대시보드 팝업용, 매매에는 영향 없음)"""
    n, c, last = series.n, series.c, series.n - 1
    a = max(0, n - ENTRY_CHART_BARS)
    ref = c[last]
    reasons, lines = [], []
    for name, p in spec["conds"]:
        if name == "rsi_below":
            v = rsi(c, p["n"])[last]
            reasons.append(f"RSI({p['n']}) {v:.1f} < {p['t']} → 과매도")
        elif name == "bb_below":
            m, sd = sma(c, p["n"]), stdev(c, p["n"])
            low = [m[i] - p["k"] * sd[i] for i in range(n)]
            reasons.append(f"종가 {ref:.6g} < 볼린저 하단({p['n']}, {p['k']}σ) {low[last]:.6g}")
            lines.append({"name": f"볼린저 하단 {p['k']}σ", "kind": "bb", "vals": _enc(low[a:], ref)})
            lines.append({"name": f"볼린저 중심 {p['n']}", "kind": "mid", "vals": _enc(m[a:], ref)})
        elif name in ("ema_up", "ema_cross"):
            f, s = ema(c, p["f"]), ema(c, p["s"])
            if name == "ema_up":
                reasons.append(f"EMA{p['f']} {f[last]:.6g} > EMA{p['s']} {s[last]:.6g} → 상승추세")
            else:
                reasons.append(f"EMA{p['f']} 선이 EMA{p['s']} 선을 아래에서 위로 뚫음 (골든크로스)")
            names = [l["name"] for l in lines]
            if f"EMA{p['f']}" not in names:
                lines.append({"name": f"EMA{p['f']}", "kind": "fast", "vals": _enc(f[a:], ref)})
            if f"EMA{p['s']}" not in names:
                lines.append({"name": f"EMA{p['s']}", "kind": "slow", "vals": _enc(s[a:], ref)})
        elif name == "drop":
            hi = max(c[last - p["lb"]:last])
            reasons.append(f"최근 {p['lb']}분 고점 {hi:.6g} 대비 {(ref / hi - 1) * 100:+.2f}% (기준 -{p['p']}%) → 단기급락")
        elif name == "breakout":
            hi = max(series.h[last - p["lb"]:last])
            reasons.append(f"종가 {ref:.6g} > 직전 {p['lb']}분 고가 {hi:.6g} → 고점돌파")
            lines.append({"name": f"{p['lb']}분 고점", "kind": "level", "vals": _enc([hi] * (n - a), ref)})
        elif name == "vol_spike":
            avg = sma(series.v, p["n"])[last - 1]
            reasons.append(f"거래량 평균의 {series.v[last] / avg:.1f}배 (기준 {p['m']}배) → 거래량급증" if avg else "거래량급증")
        elif name == "green":
            reasons.append(f"양봉 (시가 {series.o[last]:.6g} → 종가 {ref:.6g})")
    vmax = max(series.v[a:]) or 1
    return {"reasons": reasons, "lines": lines, "ref": ref,
            "o": _enc(series.o[a:], ref), "h": _enc(series.h[a:], ref), "l": _enc(series.l[a:], ref), "c": _enc(c[a:], ref),
            "v": ",".join(str(round(x / vmax * 100)) for x in series.v[a:]),
            "endAt": int((time.time() // 60 - 1) * 60 * 1000)}   # 마지막(신호) 봉 시작 시각


class Series:
    """캔들 묶음 + 지표 캐시"""
    def __init__(self, candles):
        self.o = [c["opening_price"] for c in candles]
        self.h = [c["high_price"] for c in candles]
        self.l = [c["low_price"] for c in candles]
        self.c = [c["trade_price"] for c in candles]
        self.v = [c["candle_acc_trade_volume"] for c in candles]
        self.n = len(self.c)
        self.slip = slip_for(self.c[-1]) if self.c else BT_SLIPPAGE
        self._cache = {}

    def cond(self, name, p):
        key = (name, tuple(sorted(p.items())))
        if key in self._cache:
            return self._cache[key]
        c, n = self.c, self.n
        if name == "rsi_below":
            r = rsi(c, p["n"])
            out = [r[i] < p["t"] for i in range(n)]
        elif name == "bb_below":
            m, sd = sma(c, p["n"]), stdev(c, p["n"])
            out = [c[i] < m[i] - p["k"] * sd[i] for i in range(n)]
        elif name == "ema_up":
            f, s = ema(c, p["f"]), ema(c, p["s"])
            out = [f[i] > s[i] for i in range(n)]
        elif name == "ema_cross":
            f, s = ema(c, p["f"]), ema(c, p["s"])
            out = [i > 0 and f[i - 1] <= s[i - 1] and f[i] > s[i] for i in range(n)]
        elif name == "drop":
            lb = p["lb"]
            out = [i >= lb and c[i] <= max(c[i - lb:i]) * (1 - p["p"] / 100) for i in range(n)]
        elif name == "breakout":
            lb = p["lb"]
            out = [i >= lb and c[i] > max(self.h[i - lb:i]) for i in range(n)]
        elif name == "vol_spike":
            a = sma(self.v, p["n"])
            out = [i > 0 and self.v[i] > p["m"] * a[i - 1] for i in range(n)]
        elif name == "green":
            out = [c[i] > self.o[i] for i in range(n)]
        else:
            out = [False] * n
        self._cache[key] = out
        return out

    def signal(self, spec):
        arrs = [self.cond(name, p) for name, p in spec["conds"]]
        return [all(a[i] for a in arrs) for i in range(self.n)]


def avg_range_pct(series, last=60):
    """최근 1분봉 평균 변동폭 % (고가-저가)/종가"""
    n = series.n
    idx = range(max(0, n - last), n)
    vals = [(series.h[i] - series.l[i]) / series.c[i] * 100 for i in idx if series.c[i]]
    return sum(vals) / len(vals) if vals else 0.0


FAIL_LABELS = {
    "market_drop": "시장 전체 하락",
    "vol_spike": "변동성 급증(손절폭 대비 출렁임)",
    "late_entry": "신호 늦게 따라감(진입가가 신호가보다 높음)",
    "near_miss": "익절 직전까지 갔다가 반락(익절폭 과대)",
    "instant_drop": "진입 직후 바로 하락(신호 오판)",
    "timeout": "보유시간 안에 목표 미도달(힘 부족)",
    "slow_bleed": "서서히 하락(추세 반대 진입)",
}


def diagnose(pos, exit_price, reason, btc_change_pct, now_vol):
    """손해로 끝난 거래가 왜 실패했는지 추정. 여러 원인 중 가장 강한 것 순서로 반환"""
    unit, tp, sl = pos["unit"], pos["tp"], pos["sl"]
    up_room = tp - unit
    mfe = (pos.get("maxP", unit) - unit) / up_room * 100 if up_room > 0 else 0   # 익절까지 몇 % 갔었나
    held_min = (time.time() * 1000 - pos["at"]) / 60000
    causes = []
    if btc_change_pct is not None and btc_change_pct <= -0.5:
        causes.append(("market_drop", f"보유 중 BTC {btc_change_pct:+.2f}%"))
    bt_vol, entry_vol = pos.get("btVol") or 0, pos.get("entryVol") or 0
    if bt_vol and max(entry_vol, now_vol) > bt_vol * 1.8:
        causes.append(("vol_spike", f"1분 변동폭 {max(entry_vol, now_vol):.2f}% (백테스트 때 {bt_vol:.2f}%)"))
    sig = pos.get("signalPrice")
    if sig and unit > sig * 1.003:
        causes.append(("late_entry", f"신호가 대비 {(unit / sig - 1) * 100:+.2f}% 비싸게 체결"))
    if mfe >= 70:
        causes.append(("near_miss", f"익절까지 {mfe:.0f}% 도달 후 반락"))
    if reason == "보유시간 초과":
        causes.append(("timeout", f"{held_min:.0f}분 보유, 최고 익절 진행률 {max(mfe, 0):.0f}%"))
    elif mfe < 10 and held_min <= 5:
        causes.append(("instant_drop", f"{held_min:.1f}분 만에 손절, 한 번도 위로 못 감"))
    elif mfe < 30:
        causes.append(("slow_bleed", f"{held_min:.0f}분 동안 위로 {max(mfe, 0):.0f}%만 갔다가 하락"))
    if not causes:
        causes.append(("slow_bleed", f"{held_min:.0f}분 보유 후 손실"))
    return {"primary": causes[0][0], "label": FAIL_LABELS[causes[0][0]],
            "causes": [{"code": c, "label": FAIL_LABELS[c], "detail": d} for c, d in causes],
            "mfePct": round(mfe, 1), "heldMin": round(held_min, 1), "btcPct": btc_change_pct}


def target_price(entry_cost_per_unit, net_pct=TARGET_NET_PCT):
    """수수료 빼고 순수익 net_pct % 가 되는 매도가"""
    return entry_cost_per_unit * (1 + net_pct / 100) / (1 - FEE_RATE)


def backtest(series, spec, start=60, end=None):
    """1분봉 백테스트 → 거래별 순수익률(%) 리스트"""
    sig = series.signal(spec)
    end = end or series.n
    results, i, cooldown_until = [], start, -1
    while i < end - 1:
        if i < cooldown_until or not sig[i]:
            i += 1
            continue
        j = i + 1
        slip = getattr(series, "slip", BT_SLIPPAGE)
        fill = series.o[j] * (1 + slip)                # 시장가 매수 미끄러짐 (종목별 호가 단위 반영)
        entry = fill * (1 + FEE_RATE)                  # 매수 수수료 포함 단가
        tp, sl = target_price(entry, spec.get("tp", TARGET_NET_PCT)), entry * (1 + spec["sl"] / 100)
        exit_price, k = None, j
        while k < end:
            if series.l[k] <= sl:                       # 같은 봉에서 둘 다 닿으면 손절로 가정(보수적)
                exit_price = sl
                break
            if series.h[k] >= tp:
                exit_price = tp
                break
            if k - j >= spec["hold"]:
                exit_price = series.c[k]
                break
            k += 1
        if exit_price is None:
            break
        pnl = (exit_price * (1 - slip) * (1 - FEE_RATE) / entry - 1) * 100
        results.append(pnl)
        i = k + 1
        if pnl < 0:
            cooldown_until = i + LOSS_COOLDOWN_MIN
    return results


def stats(results):
    n = len(results)
    wins = [x for x in results if x > 0]
    losses = [-x for x in results if x <= 0]
    gross_l = sum(losses)
    return {"n": n, "winrate": len(wins) / n if n else 0, "edge": sum(results) / n if n else 0,
            "total": sum(results), "pf": (sum(wins) / gross_l) if gross_l > 0 else 99.0}


def evaluate(series, spec):
    """익절·손절·보유시간까지 포함한 조합을 앞 75%로 고르고, 보지 않은 뒤 25%에서 다시 통과해야 채택.
    기준은 '승률'이 아니라 '거래당 평균 순수익'(익절 폭이 달라서 승률만으로는 비교 불가)"""
    split = int(series.n * (1 - BT_HOLDOUT))
    tr = stats(backtest(series, spec, end=split))
    if EXPLORE_MODE:
        # 탐험: 백테스트에서 비용 빼고 지지 않는 규칙이면 후보 → 진짜 판정은 실전 5회(규칙 장부)
        if tr["n"] < EXPLORE_MIN_TRADES or tr["edge"] < EXPLORE_MIN_EDGE:
            return None
        ho = stats(backtest(series, spec, start=split))
        if ho["n"] and ho["edge"] < EXPLORE_HOLDOUT_FLOOR:
            return None
        score = tr["edge"] if not ho["n"] else (tr["edge"] * tr["n"] + ho["edge"] * ho["n"]) / (tr["n"] + ho["n"])
        return {"winrate": round(tr["winrate"], 3), "trades": tr["n"], "totalPct": round(tr["total"], 2),
                "edgePct": round(tr["edge"], 3), "pf": round(min(tr["pf"], 99), 2),
                "holdoutPct": round(ho["total"], 2), "holdoutTrades": ho["n"], "holdoutWinrate": round(ho["winrate"], 3),
                "holdoutEdgePct": round(ho["edge"], 3), "score": round(score, 4)}
    if tr["n"] < BT_MIN_TRADES or tr["edge"] < BT_MIN_EDGE_PCT or tr["pf"] < BT_MIN_PROFIT_FACTOR:
        return None
    # 손익분기 승률보다 여유 있게 높아야 함
    tp, sl = spec.get("tp", TARGET_NET_PCT), abs(spec["sl"]) + 0.2
    breakeven = sl / (sl + tp)
    if tr["winrate"] < breakeven + 0.05:
        return None
    ho = stats(backtest(series, spec, start=split))
    if ho["n"] < BT_HOLDOUT_MIN_TRADES or ho["edge"] < BT_MIN_EDGE_PCT or ho["winrate"] < breakeven:
        return None
    return {"winrate": round(tr["winrate"], 3), "trades": tr["n"], "totalPct": round(tr["total"], 2),
            "edgePct": round(tr["edge"], 3), "pf": round(min(tr["pf"], 99), 2),
            "holdoutPct": round(ho["total"], 2), "holdoutTrades": ho["n"], "holdoutWinrate": round(ho["winrate"], 3),
            "holdoutEdgePct": round(ho["edge"], 3), "score": round(min(tr["edge"], ho["edge"]), 4)}


# ======================= 시장 판단 관문 =======================
# 종목별 전략(조건·익절·손절)과 별개로 '지금 시장 전체가 새로 사도 되는 상황인가'를 보는 층.
# 원칙: 새 관문은 항상 '관찰'로 시작함 → 실전에서 '이 관문이 막았을 거래'와 '통과시킨 거래'의 거래당 순익을 비교
#       → 막았을 거래가 확실히 더 나쁘면 스스로 '적용'으로 올라가고, 효과가 사라지면 '관찰'로 돌아감.
#       적용 중에 막은 매수는 '가상 거래'로 익절·손절·시간까지 끝까지 따라가서 막은 게 맞았는지 계속 잼.
# 새 관문 추가: gate_xxx(bot) -> (막을지 bool, 지금 상태 설명 str) 함수를 만들고 MARKET_GATES 에 한 줄 추가.
GATE_EVAL_MIN = 20               # 판정에 필요한 '막았을 거래' 수
GATE_EVAL_EPISODES = 3           # 그 거래들이 나온 서로 다른 시간대(1시간 단위) 수 — 급락 한 번만 보고 판단하지 않음
GATE_PASS_MIN = 20               # 비교 대상 '통과 거래' 최소 수
GATE_MARGIN = 0.15               # 막았을 거래가 통과 거래보다 거래당 이만큼(%p) 이상 나쁘고, 그 자체도 손실이어야 적용
GATE_RECHECK = 20                # 이후 막았을 거래가 이만큼 더 쌓일 때마다 재판정
BTC_DROP_MIN = 30                # BTC 급락 관문: 최근 몇 분
BTC_DROP_PCT = -0.5              # 이 % 이하로 빠졌으면 막음
BTC_TREND_MIN = 120              # BTC 하락 추세 관문: 최근 몇 분 (급락 관문보다 느린 흐름을 봄)
BTC_TREND_PCT = -1.0             # 이 % 이하로 밀려 있으면 막음
EVENTS_URL = "https://raw.githubusercontent.com/btypenine/chamdom/main/bot/events.json"
EVENTS_REFRESH = 30 * 60         # 일정표 다시 받는 간격(초) — 코드 재시작 없이 일정만 갱신 가능


def gate_btc_drop(bot):
    c = bot.btc_closes()
    if len(c) < BTC_DROP_MIN + 1:
        return False, "BTC 시세 확인 중"
    chg = (c[-1] / c[-1 - BTC_DROP_MIN] - 1) * 100
    return chg <= BTC_DROP_PCT, f"BTC {BTC_DROP_MIN}분 {chg:+.2f}% (기준 {BTC_DROP_PCT}%)"


def gate_btc_trend(bot):
    """10-29 10~11시처럼 BTC가 2시간에 걸쳐 서서히 밀릴 때 — 30분 급락 관문은 기준선 근처에서 켜졌다 꺼졌다 함"""
    c = bot.btc_closes()
    if len(c) < BTC_TREND_MIN + 1:
        return False, "BTC 시세 확인 중"
    chg = (c[-1] / c[-1 - BTC_TREND_MIN] - 1) * 100
    return chg <= BTC_TREND_PCT, f"BTC {BTC_TREND_MIN}분 {chg:+.2f}% (기준 {BTC_TREND_PCT}%)"


BTC_PUMP_MIN = 30               # BTC 급등 추격 관문: 최근 몇 분
BTC_PUMP_PCT = 0.5              # BTC가 이 % 이상 올라 있으면 막음 (11-03 실전·검증: BTC 급락 관문이 '막았을' 29건 거래당 +0.20% vs 통과 317건 -0.48%
                                # → 급락 직후 매수가 오히려 나았음. 반대로 BTC가 막 뛴 직후 알트 추격 매수가 나쁜지 관찰로 잼)


def gate_btc_pump(bot):
    """BTC가 30분 새 급히 오른 직후의 매수를 따로 잼 — 실패 원인 '신호 늦게 따라감'·'서서히 하락'과 겹치는지 확인용. 관찰로 시작"""
    c = bot.btc_closes()
    if len(c) < BTC_PUMP_MIN + 1:
        return False, "BTC 시세 확인 중"
    chg = (c[-1] / c[-1 - BTC_PUMP_MIN] - 1) * 100
    return chg >= BTC_PUMP_PCT, f"BTC {BTC_PUMP_MIN}분 {chg:+.2f}% (기준 +{BTC_PUMP_PCT}%)"


BTC_QUIET_MIN = 60              # BTC 잠잠함 관문: 최근 몇 분 1분봉 고저폭을 볼지
BTC_QUIET_PCT = 0.4             # 그 고저폭이 이 % 미만이면 막음 (11-04 실전·검증: BTC 급락 관문이 '막았을' 30건 거래당 +0.31% vs 통과 329건 -0.47%,
                                # 변동 큰 시간대 43건 +0.41% vs 316건 -0.52%, 실패 원인 1위 '힘 부족 시간 초과' 144건
                                # → 시장이 움직일 때는 오히려 낫고, 잠잠할 때 목표까지 못 가는지 관찰로 잼)


def gate_btc_quiet(bot):
    """실패 원인 1위 '보유시간 안에 목표 미도달(힘 부족)' 대응 — BTC가 1시간 동안 거의 안 움직인 잠잠한 장의 매수를 따로 잼. 관찰로 시작"""
    c = bot.btc_closes()
    if len(c) < BTC_QUIET_MIN + 1:
        return False, "BTC 시세 확인 중"
    w = c[-1 - BTC_QUIET_MIN:]
    rng = (max(w) / min(w) - 1) * 100
    return rng < BTC_QUIET_PCT, f"BTC {BTC_QUIET_MIN}분 고저폭 {rng:.2f}% (기준 {BTC_QUIET_PCT}% 미만)"


BTC_DAY_HOURS = 24              # BTC 하루 약세 관문: 최근 몇 시간 변화를 볼지(60분봉)
BTC_DAY_PCT = -3.0              # 이 % 이하로 빠져 있으면 막음
BTC_DAY_REFRESH = 600           # 60분봉은 10분마다만 다시 받음


def gate_btc_day(bot):
    """10-30 바이낸스 해킹 뒤 BTC 24시간 -5%: 30분·120분 관문은 반등 때마다 꺼지는데, 하루 내내 밀리는 장에서는 모의 거래가 '시장 전체 하락'으로 연달아 짐"""
    now = time.time()
    if now - bot.__dict__.get("_btc_day_at", 0) >= BTC_DAY_REFRESH:
        try:
            data = bot.upbit._public("/v1/candles/minutes/60", {"market": "KRW-BTC", "count": BTC_DAY_HOURS + 1})
            if data and len(data) >= BTC_DAY_HOURS + 1:
                bot._btc_day = (data[0]["trade_price"] / data[BTC_DAY_HOURS]["trade_price"] - 1) * 100
                bot._btc_day_at = now
        except Exception:
            pass
    chg = bot.__dict__.get("_btc_day")
    if chg is None:
        return False, "BTC 하루 시세 확인 중"
    return chg <= BTC_DAY_PCT, f"BTC {BTC_DAY_HOURS}시간 {chg:+.2f}% (기준 {BTC_DAY_PCT}%)"


def gate_event(bot):
    now = time.time()
    nxt = None
    for e in bot.events():
        try:
            at = datetime.fromisoformat(e["at"]).timestamp()
        except Exception:
            continue
        lo, hi = at - float(e.get("before_h", 2)) * 3600, at + float(e.get("after_h", 2)) * 3600
        if lo <= now <= hi:
            return True, f"{e['name']} {datetime.fromtimestamp(at, KST).strftime('%m-%d %H:%M')} 전후 (앞 {e.get('before_h', 2)}시간·뒤 {e.get('after_h', 2)}시간)"
        if lo > now and (nxt is None or lo < nxt[0]):
            nxt = (lo, e, at)
    if nxt:
        return False, f"다음: {nxt[1]['name']} {datetime.fromtimestamp(nxt[2], KST).strftime('%m-%d %H:%M')}"
    return False, "등록된 일정 없음"


VOLATILE_HOURS = (23, 0, 17)     # 변동 큰 시간대(한국시간): 미국장 시작 23~00시, 유럽장 17시 (bt-lab 10-29: 평소의 1.4~1.8배)


def gate_volatile_hours(bot):
    h = datetime.now(KST).hour
    return h in VOLATILE_HOURS, f"지금 {h:02d}시 · 변동 큰 시간대 {', '.join(f'{x:02d}시' for x in VOLATILE_HOURS)}"


DAWN_HOURS = range(3, 9)        # 새벽 시간대 관문(한국시간 03:00~08:59): 실전 201건 중 이 시간 매수 58건 거래당 -0.87% vs 나머지 143건 -0.27% (10-27~10-30)


def gate_dawn_hours(bot):
    """아시아 새벽(미국장 마감 후~한국장 전) 거래가 얇은 시간대의 매수를 따로 잼 — 관찰로 시작"""
    h = datetime.now(KST).hour
    return h in DAWN_HOURS, f"지금 {h:02d}시 · 새벽 시간대 {DAWN_HOURS.start:02d}~{DAWN_HOURS.stop - 1:02d}시"


BAD_HOUR_MIN_N = 10             # 나쁜 시간대(자기 학습) 관문: 그 시각(매수 시각 기준) 실전 표본이 이만큼 이상이고
BAD_HOUR_PCT = -0.8             # 거래당 순익이 이 % 이하이면 막음 — 고정 시간표 대신 hourStats 로 스스로 고름
                                # 근거(11-06): 00~02시 41건 거래당 -1.12%, 21~22시 35건 -1.05% vs 전체 -0.4% 안팎.
                                # 단, 고정 '새벽 시간대'(03~08시)는 만든 뒤 -0.87% → -0.2% 안팎으로 회귀함 → 관찰로 시작해 판정에 맡김


def gate_bad_hour(bot):
    """지금 시각(한국시간)의 누적 실전 성적이 나쁘면 매수를 따로 잼 — 관찰로 시작"""
    h = datetime.now(KST).hour
    st = (bot.state.get("hourStats") or {}).get("%02d" % h) or {}
    n = st.get("n", 0)
    if n < BAD_HOUR_MIN_N:
        return False, f"지금 {h:02d}시 · 표본 {n}건(최소 {BAD_HOUR_MIN_N}건 필요)"
    avg = st.get("sumPct", 0.0) / n
    return avg <= BAD_HOUR_PCT, f"지금 {h:02d}시 실전 {n}건 거래당 {avg:+.2f}% (기준 {BAD_HOUR_PCT:+.1f}% 이하)"


CHASE_PCT = 0.3                 # 추격 매수 관문: 매수 직전 현재가가 신호가(신호 봉 종가)보다 이 % 이상 비싸면 막음


def gate_chase(bot, mk, price, ctx):
    """실패 원인 2위 '신호 늦게 따라감' 대응 — 매수할 때마다(종목별) 확인하는 관문"""
    sig = (ctx or {}).get("signalPrice")
    if not sig or not price:
        return False, "신호가 없음"
    gap = (price / sig - 1) * 100
    return gap >= CHASE_PCT, f"{mk.split('-')[1]} 현재가가 신호가보다 {gap:+.2f}% (기준 +{CHASE_PCT}%)"


WEAK_HO_MIN_TRADES = 2          # 검증 근거 약함 관문: 검증구간(보지 않은 뒤 25%) 거래가 이보다 적거나 (bt-lab 10-30: 2회·+0.1%가 차이 가장 큼)
WEAK_HO_MIN_EDGE = 0.1          # 검증구간 거래당 순익이 이 % 미만이면 막음


def gate_weak_holdout(bot, mk, price, ctx):
    """백테스트 예상(거래당 +0.3%)과 실전(-0.4%) 차이 대응 — 검증구간 근거가 얇은 전략의 매수를 따로 잼"""
    sp = (bot.state.get("strategies") or {}).get(mk) or {}
    n, edge = sp.get("holdoutTrades"), sp.get("holdoutEdgePct")
    if n is None:
        return False, "검증 기록 없음"
    edge = edge if n else 0.0
    return (n < WEAK_HO_MIN_TRADES or edge < WEAK_HO_MIN_EDGE), \
        f"{mk.split('-')[1]} 검증구간 {n}회 거래당 {edge:+.2f}% (기준 {WEAK_HO_MIN_TRADES}회·{WEAK_HO_MIN_EDGE:+.1f}% 이상)"


BREADTH_MIN = 30                # 시장 폭 약세 관문: 종목별 최근 몇 분 변화를 볼지
BREADTH_DOWN_FRAC = 0.7         # 대상 종목 중 이 비율 이상이 하락 중이면 막음
BREADTH_MIN_MARKETS = 10        # 최근 확인한 종목이 이보다 적으면 판단 안 함
BREADTH_FRESH = 300             # 이 초 안에 확인한 종목만 셈


def gate_breadth(bot, mk, price, ctx):
    """실패 원인 2위 '시장 전체 하락' 대응 — BTC는 버티는데 알트 전체가 같이 밀리는 때를 따로 잼 (BTC 관문과 별개)"""
    now = time.time()
    chg = [c for c, at in (bot.__dict__.get("_mk_chg") or {}).values() if now - at <= BREADTH_FRESH]
    if len(chg) < BREADTH_MIN_MARKETS:
        return False, f"확인한 종목 {len(chg)}개(기준 {BREADTH_MIN_MARKETS}개 이상)"
    down = sum(c < 0 for c in chg) / len(chg)
    avg = sum(chg) / len(chg)
    return down >= BREADTH_DOWN_FRAC, f"대상 {len(chg)}종목 중 {down * 100:.0f}% 하락 · 평균 {avg:+.2f}% ({BREADTH_MIN}분, 기준 {BREADTH_DOWN_FRAC * 100:.0f}% 이상)"


COIN_TREND_MIN = 120             # 종목 자체 하락 추세 관문: 그 종목의 최근 몇 분 변화를 볼지
COIN_TREND_PCT = -2.0            # 이 % 이하로 밀려 있는 종목은 막음 (BTC·시장 폭과 별개로 '그 코인만' 흘러내리는 때)


def gate_coin_trend(bot, mk, price, ctx):
    """실패 원인 '서서히 하락(추세 반대 진입)' 대응 — 눌림 매수 조건(볼린저하단·RSI과매도·단기급락)이 실전 성적 최하위라
    그 종목 자체가 최근 2시간 계속 밀리는 중이면 따로 잼"""
    chg = (ctx or {}).get("coinChg")
    if chg is None:
        return False, "종목 변화 기록 없음"
    return chg <= COIN_TREND_PCT, f"{mk.split('-')[1]} 최근 {COIN_TREND_MIN}분 {chg:+.2f}% (기준 {COIN_TREND_PCT}%)"


REACH_MIN = 60                   # 힘 부족 관문: 최근 몇 분 1분봉 움직임으로 기대 움직임을 잴지
REACH_X = 1.5                    # 익절폭이 '기대 움직임(평균|1분 변화| × √보유분)'의 이 배수보다 크면 막음 (bt-lab 10-31: 1.5배 초과 16건 거래당 -0.36% vs 나머지 83건 -0.04%)


def gate_reach(bot, mk, price, ctx):
    """실패 원인 1위 '보유시간 안에 목표 미도달(힘 부족)' 대응 — 그 종목의 요즘 움직임으로는 보유시간 안에
    익절폭까지 가기 어려운 매수(조용한 코인에 큰 익절폭)를 따로 잼"""
    reach, tp = (ctx or {}).get("reachPct"), (ctx or {}).get("tpPct")
    if not reach or tp is None:
        return False, "움직임 기록 없음"
    return tp > reach * REACH_X, f"{mk.split('-')[1]} 익절 +{tp:.1f}% · 보유시간 기대 움직임 {reach:.2f}% (익절폭이 {tp / reach:.1f}배, 기준 {REACH_X:.1f}배 초과)"


BIG_TP_PCT = 1.5                 # 큰 익절폭 관문: 전략 익절폭이 이 % 이상이면 막음 (11-02 실전·검증 374건: 익절 +1.5% 이상 253건 거래당 -0.47% vs 미만 121건 -0.25%,
                                 # 한 규칙 쏠림 빼도 -0.64%대 vs -0.08~-0.48% → 같은 방향. 최근 60건도 -1.00% vs -0.56%)


def gate_big_tp(bot, mk, price, ctx):
    """실패 원인 1위 '보유시간 안에 목표 미도달' 대응 — 힘 부족(종목 움직임 대비)과 달리 익절폭 자체만 봄. 관찰로 시작"""
    tp = (ctx or {}).get("tpPct")
    if tp is None:
        return False, "익절폭 기록 없음"
    return tp >= BIG_TP_PCT, f"{mk.split('-')[1]} 익절 +{tp:g}% (기준 +{BIG_TP_PCT:g}% 이상)"


VOL_X = 1.8                      # 변동성 급증 관문: 매수 순간 최근 60분 1분봉 평균 변동폭이 백테스트 때의 이 배수보다 크면 막음
                                 # (실패 분석 'vol_spike' 판정 기준과 같은 1.8배. 11-05 00~03시 손실 8건 중 7건이 이 원인)


def gate_vol_spike(bot, mk, price, ctx):
    """실패 원인 '변동성 급증(손절폭 대비 출렁임)' 대응 — 백테스트 때보다 훨씬 출렁이는 종목은
    손절폭이 상대적으로 좁아져 잡음에 털리기 쉬우므로 매수를 따로 잼. 관찰로 시작"""
    ev, bv = (ctx or {}).get("entryVol"), (ctx or {}).get("btVol")
    if not ev or not bv:
        return False, "변동폭 기록 없음"
    return ev > bv * VOL_X, f"{mk.split('-')[1]} 1분 변동폭 {ev:.2f}% · 백테스트 때 {bv:.2f}% ({ev / bv:.1f}배, 기준 {VOL_X:.1f}배 초과)"


# entry=True 관문은 1분마다가 아니라 매수 신호가 날 때마다 그 종목 기준으로 확인: fn(bot, 종목, 현재가, ctx)
MARKET_GATES = [
    {"id": "btc_drop", "name": "BTC 급락", "desc": f"BTC가 최근 {BTC_DROP_MIN}분 {BTC_DROP_PCT}% 이하로 빠지면 새로 사지 않음", "fn": gate_btc_drop},
    {"id": "btc_trend", "name": "BTC 하락 추세", "desc": f"BTC가 최근 {BTC_TREND_MIN}분 동안 {BTC_TREND_PCT}% 이하로 밀려 있으면 새로 사지 않음", "fn": gate_btc_trend},
    {"id": "btc_day", "name": "BTC 하루 약세", "desc": f"BTC가 최근 {BTC_DAY_HOURS}시간 {BTC_DAY_PCT}% 이하로 빠져 있으면 새로 사지 않음(악재로 하루 내내 밀리는 장)", "fn": gate_btc_day},
    {"id": "btc_pump", "name": "BTC 급등 추격", "desc": f"BTC가 최근 {BTC_PUMP_MIN}분 +{BTC_PUMP_PCT}% 이상 뛰어 있으면 새로 사지 않음(급등 직후 추격 방지)", "fn": gate_btc_pump},
    {"id": "btc_quiet", "name": "BTC 잠잠함", "desc": f"BTC가 최근 {BTC_QUIET_MIN}분 고저폭 {BTC_QUIET_PCT}% 미만으로 거의 안 움직이면 새로 사지 않음(목표 미도달 방지)", "fn": gate_btc_quiet},
    {"id": "event", "name": "경제 일정", "desc": "FOMC·미국 물가(CPI) 발표 등 정해진 일정 앞뒤로 새로 사지 않음 (일정표: bot/events.json)", "fn": gate_event},
    {"id": "volatile_hours", "name": "변동 큰 시간대", "desc": "미국장 시작(23~00시)·유럽장 시작(17시)에는 새로 사지 않음", "fn": gate_volatile_hours},
    {"id": "dawn_hours", "name": "새벽 시간대", "desc": f"새벽 {DAWN_HOURS.start:02d}~{DAWN_HOURS.stop - 1:02d}시(한국시간)에는 새로 사지 않음", "fn": gate_dawn_hours},
    {"id": "bad_hour", "name": "나쁜 시간대(학습)", "desc": f"지금 시각의 실전 성적이 {BAD_HOUR_MIN_N}건 이상·거래당 {BAD_HOUR_PCT:+.1f}% 이하인 시간대에는 새로 사지 않음(시간표를 스스로 고름)", "fn": gate_bad_hour},
    {"id": "chase", "name": "추격 매수", "desc": f"매수 직전 현재가가 신호가보다 {CHASE_PCT}% 이상 올라 있으면 사지 않음(늦게 따라붙기 방지)", "fn": gate_chase, "entry": True},
    {"id": "weak_holdout", "name": "검증 근거 약함", "desc": f"전략의 검증구간 거래가 {WEAK_HO_MIN_TRADES}회 미만이거나 거래당 {WEAK_HO_MIN_EDGE:+.1f}% 미만이면 사지 않음", "fn": gate_weak_holdout, "entry": True},
    {"id": "breadth", "name": "시장 폭 약세", "desc": f"거래 대상 종목 중 {BREADTH_DOWN_FRAC * 100:.0f}% 이상이 최근 {BREADTH_MIN}분 하락 중이면 사지 않음(알트 동반 하락)", "fn": gate_breadth, "entry": True},
    {"id": "coin_trend", "name": "종목 하락 추세", "desc": f"그 종목이 최근 {COIN_TREND_MIN}분 {COIN_TREND_PCT}% 이하로 밀려 있으면 사지 않음(혼자 흘러내리는 코인 눌림 매수 방지)", "fn": gate_coin_trend, "entry": True},
    {"id": "reach", "name": "힘 부족", "desc": f"그 종목 최근 {REACH_MIN}분 움직임으로 본 보유시간 기대 움직임보다 익절폭이 {REACH_X:.1f}배 넘게 크면 사지 않음(목표 미도달 방지)", "fn": gate_reach, "entry": True},
    {"id": "big_tp", "name": "큰 익절폭", "desc": f"전략의 익절폭이 +{BIG_TP_PCT:g}% 이상이면 사지 않음(작은 목표가 실전에서 덜 짐)", "fn": gate_big_tp, "entry": True},
    {"id": "vol_spike", "name": "변동성 급증", "desc": f"그 종목 최근 60분 1분봉 변동폭이 백테스트 때의 {VOL_X:.1f}배를 넘으면 사지 않음(손절폭 대비 출렁임에 털리기 방지)", "fn": gate_vol_spike, "entry": True},
]

# ---- 청산 판단 (보유 중) — 관문과 같은 '관찰 → 승인 → 적용' 구조 ----
# 관찰: 조건이 걸린 순간 '지금 정리했다면 몇 %'를 기록해 두고, 실제로 끝난 결과와 비교(조기 정리가 나았는지)
# 적용(승인 후): 그 순간 실제로 정리하고, 원래 규칙대로 들고 있었다면 어땠을지를 가상으로 끝까지 추적
EXIT_EVAL_MIN = 20               # 판정에 필요한 표본 수
EXIT_MARGIN = 0.15               # 조기 정리가 평균 이만큼(%p) 이상 나아야 적용 제안
BTC_EXIT_PCT = -0.5              # 매수 후 BTC가 이만큼 빠졌고 포지션이 손실 중이면 조기 정리 대상


TP_LIMIT_MAX_MIN = 400  # 지정가 익절 관찰: 보유가 이보다 길면 1분봉 조회량이 커서 건너뜀(분석 전용)
# 물타기 관찰(분석 전용, 실제 주문 없음): '손실 나면 4배로 물타고 평단+익절폭에 지정가 매도'였다면 결과가 어땠을지
MARTIN_STEP_PCT = 1.5   # 매수가 대비 이만큼씩 내려갈 때마다 한 번 더 삼(-1.5%, -3%, -4.5%)
MARTIN_MULT = 4         # 직전 금액의 몇 배로 물타나
MARTIN_MAX_ADDS = 3     # 최대 물타기 횟수(6천 → 2.4만 → 9.6만 → 38.4만원)
EXIT_RECENT_N = 20     # 청산 규칙 '최근 N건' 흐름 표시용(판정 기준은 누적 그대로)
OVERLAP_RECENT = 30    # 관문 겹침 성적 '최근 N건' 흐름 표시용(분석 전용)


def exit_btc_drop(bot, pos, price):
    c = bot.btc_closes()
    if not c or not pos.get("btcAtEntry"):
        return False, ""
    chg = (c[-1] / pos["btcAtEntry"] - 1) * 100
    pnl = (price * (1 - FEE_RATE) / pos["unit"] - 1) * 100
    return (chg <= BTC_EXIT_PCT and pnl < 0), f"매수 후 BTC {chg:+.2f}% · 포지션 {pnl:+.2f}%"


STALE_EXIT_FRAC = 0.5            # 힘 빠진 보유: 보유시간의 이 비율이 지났는데
STALE_EXIT_PROG = 20             # 익절까지 진행률(최고가 기준)이 이 % 미만이고 손실 중이면 조기 정리 대상


def exit_stale(bot, pos, price):
    """실패 원인 1위 '보유시간 안에 목표 미도달(힘 부족)' 대응 — 끝까지 들고 있어 봐야 대개 시간 초과로 끝나는 거래를 일찍 정리"""
    start, end = pos["at"] / 1000, pos["deadline"]
    if end <= start:
        return False, ""
    frac = (time.time() - start) / (end - start)
    up_room = pos["tp"] - pos["unit"]
    prog = (pos.get("maxP", pos["unit"]) - pos["unit"]) / up_room * 100 if up_room > 0 else 0
    pnl = (price * (1 - FEE_RATE) / pos["unit"] - 1) * 100
    return (frac >= STALE_EXIT_FRAC and prog < STALE_EXIT_PROG and pnl < 0), \
        f"보유시간 {frac * 100:.0f}% 경과 · 익절 진행률 최고 {max(prog, 0):.0f}% · 포지션 {pnl:+.2f}%"


GIVEBACK_PROG = 70              # 익절 근처 되밀림: 익절까지 진행률(최고가 기준)이 이 % 이상 갔었는데
GIVEBACK_PNL = 0.0               # 지금 순손익(수수료 포함)이 이 % 이하로 되밀렸으면 조기 정리 대상


def exit_giveback(bot, pos, price):
    """실패 원인 '익절 직전까지 갔다가 반락' 대응 — 거의 다 간 수익을 손실로 바꾸기 전에 본전 근처에서 정리"""
    up_room = pos["tp"] - pos["unit"]
    prog = (pos.get("maxP", pos["unit"]) - pos["unit"]) / up_room * 100 if up_room > 0 else 0
    pnl = (price * (1 - FEE_RATE) / pos["unit"] - 1) * 100
    return (prog >= GIVEBACK_PROG and pnl <= GIVEBACK_PNL), f"익절 진행률 최고 {prog:.0f}% · 지금 포지션 {pnl:+.2f}%"


MID_FADE_PROG = 40              # 중간 진행 후 되밀림: 익절까지 진행률(최고가 기준)이 이 % 이상 갔었고(되밀림 정리 기준 70% 미만 구간 포함)
MID_FADE_FRAC = 0.5             # 보유시간의 이 비율이 지났는데
MID_FADE_PNL = 0.0              # 지금 순손익(수수료 포함)이 이 % 이하이면 조기 정리 대상


def exit_mid_fade(bot, pos, price):
    """실패 원인 1위 '보유시간 안에 목표 미도달' 중, 익절까지 40~70% 정도 갔다가 되밀려 결국 시간 초과 손실로 끝나는 거래 대응.
    힘 빠진 보유(진행률 20% 미만)·익절 근처 되밀림(70% 이상) 사이의 빈 구간을 잼 — 관찰로 시작"""
    start, end = pos["at"] / 1000, pos["deadline"]
    if end <= start:
        return False, ""
    frac = (time.time() - start) / (end - start)
    up_room = pos["tp"] - pos["unit"]
    prog = (pos.get("maxP", pos["unit"]) - pos["unit"]) / up_room * 100 if up_room > 0 else 0
    pnl = (price * (1 - FEE_RATE) / pos["unit"] - 1) * 100
    return (frac >= MID_FADE_FRAC and prog >= MID_FADE_PROG and pnl <= MID_FADE_PNL), \
        f"보유시간 {frac * 100:.0f}% 경과 · 익절 진행률 최고 {prog:.0f}% · 포지션 {pnl:+.2f}%"


DEEP_CUT_SL = -3.0              # 넓은 손절 중간 차단: 규칙 손절폭이 이 % 이하(-3%·-5% 처럼 넓은 규칙)인데
DEEP_CUT_PNL = -2.0             # 지금 순손익(수수료 포함)이 이 % 이하로 빠졌으면 조기 정리 대상


def exit_deep_cut(bot, pos, price):
    """손절폭이 넓은 규칙(-3%·-5%)의 한 번 큰 손실이 앞선 여러 번 익절을 지우는 문제 대응.
    끝까지 손절선까지 가는 것보다 -2%에서 끊는 게 나은지 관찰로 시작해 실전 결과와 비교"""
    if not pos.get("unit") or not pos.get("sl"):
        return False, ""
    sl_pct = (pos["sl"] / pos["unit"] - 1) * 100
    pnl = (price * (1 - FEE_RATE) / pos["unit"] - 1) * 100
    return (sl_pct <= DEEP_CUT_SL and pnl <= DEEP_CUT_PNL), f"규칙 손절 {sl_pct:.1f}% · 지금 포지션 {pnl:+.2f}%"


EXIT_RULES = [
    {"id": "btc_exit", "name": "보유 중 BTC 하락 정리", "desc": f"들고 있는 동안 BTC가 {BTC_EXIT_PCT}% 이상 빠지고 손실 중이면 일찍 정리", "fn": exit_btc_drop},
    {"id": "stale_exit", "name": "힘 빠진 보유 정리", "desc": f"보유시간 {STALE_EXIT_FRAC * 100:.0f}%가 지나도 익절까지 {STALE_EXIT_PROG}%도 못 갔고 손실 중이면 일찍 정리", "fn": exit_stale},
    {"id": "giveback_exit", "name": "익절 근처 되밀림 정리", "desc": f"익절까지 {GIVEBACK_PROG}% 이상 갔다가 순손익 {GIVEBACK_PNL:+.1f}% 이하로 되밀리면 일찍 정리", "fn": exit_giveback},
    {"id": "mid_fade_exit", "name": "중간 진행 후 되밀림 정리", "desc": f"보유시간 {MID_FADE_FRAC * 100:.0f}%가 지났고 익절까지 {MID_FADE_PROG}% 이상 갔었는데 순손익 {MID_FADE_PNL:+.1f}% 이하로 되밀렸으면 일찍 정리", "fn": exit_mid_fade},
    {"id": "deep_cut_exit", "name": "넓은 손절 중간 차단", "desc": f"손절폭 {DEEP_CUT_SL:.0f}% 이하로 넓은 규칙이 순손익 {DEEP_CUT_PNL:.0f}% 이하로 빠지면 손절선까지 기다리지 않고 일찍 정리", "fn": exit_deep_cut},
]


# ======================= 봇 =======================
class Bot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.live = cfg["TRADE_MODE"] == "live"
        self.upbit = Upbit(cfg["UPBIT_ACCESS_KEY"], cfg["UPBIT_SECRET_KEY"])
        self.cloud = Cloud(cfg["FIREBASE_LINK_CODE"])
        self.state = load_state(cfg["BOT_BUDGET_KRW"])
        self.error = ""
        self.rng = random.Random()
        self.last_status = 0

    # ---------- 장부 ----------
    @property
    def ledger(self):
        return self.state["ledger"]

    def in_use(self):
        return sum(p["cost"] for p in self.state["positions"].values() if not p.get("paper"))

    def is_shadow(self, market, spec):
        """실매매 중이라도 아직 실전 통과 못 한 규칙이면 모의 체결로만 시험"""
        if not (self.live and PROVEN_ONLY_LIVE):
            return False
        rb = self.state["ruleBook"].get(spec.get("rid") or rule_id(market, spec))
        return not (rb and rb.get("status") == "proven")

    def shadow_book(self, pnl, market=None):
        """모의 체결 결과: 시드·실현손익에는 넣지 않고 따로 집계 (손실 종목 정지는 실전과 같게)"""
        lg = self.ledger
        lg["shadowTrades"] = lg.get("shadowTrades", 0) + 1
        lg["shadowWins"] = lg.get("shadowWins", 0) + (1 if pnl > 0 else 0)
        lg["shadowPnl"] = lg.get("shadowPnl", 0.0) + pnl
        lg["dayShadowTrades"] = lg.get("dayShadowTrades", 0) + 1
        lg["dayShadowPnl"] = lg.get("dayShadowPnl", 0.0) + pnl
        if pnl <= 0 and COOLDOWN_PER_MARKET and market:
            self.state.setdefault("coolMarkets", {})[market] = int(time.time()) + LOSS_COOLDOWN_MIN * 60
        save_state(self.state)

    def available(self):
        return max(0.0, self.ledger["seed"] - self.in_use())

    def roll_day(self):
        day = today_key()
        lg = self.ledger
        if lg["day"] != day:
            if lg["day"]:
                log(f"거래일 마감 {lg['day']}: {lg['dayTrades']}회(익절 {lg['dayWins']}) 손익 {lg['dayPnl']:+,.0f}원")
                by = [{"market": m, **v} for m, v in self.state["marketStats"].items()]
                by.sort(key=lambda x: x["pnl"])
                self.state["aiDue"] = True
                self.state["lastReport"] = {
                    "day": lg["day"], "trades": lg["dayTrades"], "wins": lg["dayWins"], "pnl": round(lg["dayPnl"]),
                    "seed": round(lg["seed"]), "locked": round(lg["locked"]), "byMarket": by,
                    "blocked": list(self.state["blocked"])}
            self.state["marketStats"] = {}
            self.state["blocked"] = []
            lg.update({"day": day, "dayPnl": 0.0, "dayTrades": 0, "dayWins": 0, "gateTrades": 0, "gateWins": 0,
                       "dayShadowTrades": 0, "dayShadowPnl": 0.0})
            save_state(self.state)
            log(f"새 거래일 {day} 시작 · 시드 {lg['seed']:,.0f}원 · 잠긴 수익 {lg['locked']:,.0f}원")

    def book(self, pnl, market=None):
        lg = self.ledger
        lg["realizedTotal"] += pnl
        lg["dayPnl"] += pnl
        lg["tradesTotal"] += 1
        lg["dayTrades"] += 1
        lg["gateTrades"] += 1
        if pnl > 0:
            lg["gateWins"] += 1
            lg["winsTotal"] += 1
            lg["dayWins"] += 1
            lg["locked"] += pnl * PROFIT_LOCK_RATIO
            lg["seed"] += pnl * (1 - PROFIT_LOCK_RATIO)
            lg["martLevel"] = 0
        else:
            lg["seed"] += pnl
            until = int(time.time()) + LOSS_COOLDOWN_MIN * 60
            if COOLDOWN_PER_MARKET and market:
                self.state.setdefault("coolMarkets", {})[market] = until
            else:
                lg["cooldownUntil"] = until
            lg["martLevel"] = min(lg["martLevel"] + 1, self.cfg["MARTINGALE_MAX_LEVEL"])
        save_state(self.state)

    def learn(self, market, pnl, reason):
        st = self.state["marketStats"].setdefault(
            market, {"trades": 0, "wins": 0, "pnl": 0.0, "lossStreak": 0, "reasons": {}})
        st["trades"] += 1
        st["pnl"] = round(st["pnl"] + pnl)
        st["reasons"][reason] = st["reasons"].get(reason, 0) + 1
        if pnl > 0:
            st["wins"] += 1
            st["lossStreak"] = 0
        else:
            st["lossStreak"] += 1
        # 종목 하루 제외는 하지 않음: 규칙마다 실전 5회를 채운 뒤 규칙 장부에서 판정 (rule_record)
        save_state(self.state)

    def rule_record(self, pos, pnl):
        """규칙 장부에 실전 결과 기록 → 5회부터 판정"""
        rid = pos.get("rid")
        rb = self.state["ruleBook"].get(rid) if rid else None
        if not rb:
            return
        pct = pnl / pos["cost"] * 100 if pos.get("cost") else 0.0
        if rb["status"] == "proven":
            # 예측력 점검(분석 전용, 매매 영향 없음): 이 거래 '직전' 규칙의 누적 성적·횟수 구간별로 다음 거래 결과를 모음
            # → 과거 성적이 좋은 규칙일수록 정말 다음 거래도 좋은지(장부 선별이 실제로 통하는지) 확인
            if rb.get("trades"):
                pavg = rb["sumPct"] / rb["trades"]
                pp = self.state.setdefault("passPredict", {})
                for key in ("avg:" + ("<0.3" if pavg < 0.3 else ("0.3~0.6" if pavg < 0.6 else "0.6+")),
                            "n:" + ("5~9" if rb["trades"] < 10 else ("10~19" if rb["trades"] < 20 else "20+"))):
                    b = pp.setdefault(key, {"n": 0, "wins": 0, "sumPct": 0.0})
                    b["n"] += 1
                    b["wins"] += 1 if pnl > 0 else 0
                    b["sumPct"] = round(b["sumPct"] + pct, 4)
            # 통과 '이후' 성적만 따로 누적 (통과 판정이 앞으로도 맞는지 확인용, 매매 영향 없음)
            for box in (rb.setdefault("post", {"n": 0, "wins": 0, "sumPct": 0.0, "pnl": 0}),
                        self.state.setdefault("passStats", {"n": 0, "wins": 0, "sumPct": 0.0, "pnl": 0})):
                box["n"] += 1
                box["wins"] += 1 if pnl > 0 else 0
                box["sumPct"] = round(box["sumPct"] + pct, 4)
                box["pnl"] = round(box["pnl"] + pnl)
        rb["trades"] += 1
        rb["wins"] += 1 if pnl > 0 else 0
        rb["sumPct"] = round(rb["sumPct"] + pct, 4)
        rb["pnl"] = round(rb["pnl"] + pnl)
        rb["lastAt"] = int(time.time() * 1000)
        n = rb["trades"]
        avg = rb["sumPct"] / n
        coin = rb["market"].split("-")[1]
        # 3아웃제: 5회 묶음마다 성적표 → 나쁘면 1아웃, 3아웃이면 퇴출 (규칙 하나하나를 '선수'처럼 관리)
        win = rb.get("win")
        if win is None:                               # 이전 버전 규칙: 지금 묶음에 든 횟수만큼 평균으로 채워 시작
            k = (n - 1) % RULE_OUT_WINDOW
            win = {"n": k, "sumPct": round((rb["sumPct"] - pct) / (n - 1) * k, 4) if n > 1 else 0.0}
        win["n"] += 1
        win["sumPct"] = round(win["sumPct"] + pct, 4)
        rb["win"] = win
        old = rb["status"]
        proven_sour = old == "proven" and avg <= 0    # 실주문 중 누적이 0% 이하로 꺾이면 묶음을 기다리지 않고 바로 1아웃
        if win["n"] >= RULE_OUT_WINDOW or proven_sour:
            wavg = win["sumPct"] / win["n"]
            wn = win["n"]
            rb["win"] = {"n": 0, "sumPct": 0.0}
            if wavg <= 0 or proven_sour:
                rb["outs"] = rb.get("outs", 0) + 1
                if rb["outs"] >= RULE_OUT_LIMIT:
                    rb["status"] = "retired"
                    verdict = f"{rb['outs']}아웃 → 퇴출(다시 채택 안 함)"
                else:
                    rb["status"] = "testing"
                    if old == "proven":
                        rb["demoted"] = rb.get("demoted", 0) + 1
                    verdict = (f"{rb['outs']}아웃 → " + ("실주문 자격 잃고 " if old == "proven" else "")
                               + f"검증(모의) 계속 ({RULE_OUT_LIMIT}아웃이면 퇴출)")
            elif avg >= RULE_PROMOTE_MIN_AVG:
                rb["status"] = "proven"
                verdict = "계속 사용(실주문 자격)" + (f" · 아웃 {rb['outs']}개" if rb.get("outs") else "")
            else:
                rb["status"] = "testing"          # 이번 묶음은 플러스지만 누적 여유가 작음 → 모의로 더 지켜봄
                verdict = f"이번 묶음 플러스, 누적 +{RULE_PROMOTE_MIN_AVG}% 미만 → 검증(모의) 계속"
            log(f"[규칙 판정] {coin} {rb['id']} 이번 {wn}회 거래당 {wavg:+.2f}% · 누적 {n}회 승률 {rb['wins'] / n * 100:.0f}% "
                f"거래당 {avg:+.2f}% (백테스트 예상 {rb.get('btEdge') or 0:+.2f}%) → {verdict}")
            if rb["status"] == "retired" and old != "retired" and self.state["strategies"].get(rb["market"], {}).get("rid") == rid:
                del self.state["strategies"][rb["market"]]
        else:
            left = RULE_OUT_WINDOW - win["n"]
            outs = rb.get("outs", 0)
            log(f"[규칙 기록] {coin} {rb['id']} {'검증(모의)' if pos.get('paper') else '실전'} {n}회 · 승률 {rb['wins'] / n * 100:.0f}% · 거래당 {avg:+.2f}%"
                + f" · 판정까지 {left}회" + (f" · {outs}아웃" if outs else ""))
        self.trim_rulebook()
        save_state(self.state)

    def cond_record(self, pos, pnl):
        """조건 이름별 실전 성적 → 다음 전략 생성 때 잘된 조건을 더 자주 시도"""
        rb = self.state["ruleBook"].get(pos.get("rid") or "")
        if not rb or not pos.get("cost"):
            return
        pct = pnl / pos["cost"] * 100
        cs = self.state.setdefault("condStats", {})
        for c in rb["spec"].get("conds", []):
            st = cs.setdefault(c[0], {"n": 0, "sumPct": 0.0, "wins": 0})
            st["n"] += 1
            st["sumPct"] = round(st["sumPct"] + pct, 4)
            st["wins"] += 1 if pnl > 0 else 0

    def cond_weights(self):
        """실전 거래당 순익이 좋은 조건일수록 가중치↑ (0.3~3배)
        기준은 '0%'가 아니라 '전체 조건 평균' — 모든 조건이 손실이어도 상대적으로 나은 조건을 더 자주 시도함
        (절대 기준이면 전부 0.3배 하한에 붙어 균등 = 학습이 사실상 꺼짐)"""
        cs = {k: v for k, v in self.state.get("condStats", {}).items() if v.get("n", 0) >= COND_LEARN_MIN}
        tot_n = sum(v["n"] for v in cs.values())
        if not tot_n:
            return {}
        base = sum(v["sumPct"] for v in cs.values()) / tot_n
        return {name: min(3.0, max(0.3, 1 + (st["sumPct"] / st["n"] - base) / 0.3)) for name, st in cs.items()}

    def shape_weights(self):
        """규칙 장부(퇴출 포함) 실전 성적을 익절폭·보유시간별로 묶어, 전체 평균보다 나은 칸일수록 가중치↑ (0.5~2배)
        한 규칙이 칸 성적을 끌고 가지 않도록 거래가 가장 많은 규칙 하나를 뺀 평균을 씀(남은 거래가 충분할 때)"""
        out = {}
        try:
            for key, choices in (("tp", TP_CHOICES), ("hold", HOLD_CHOICES)):
                g = {}
                for r in self.state.get("ruleBook", {}).values():
                    n = r.get("trades") or 0
                    v = (r.get("spec") or {}).get(key)
                    if n <= 0 or v not in choices:
                        continue
                    st = g.setdefault(v, {"n": 0, "sum": 0.0, "rules": 0, "topN": 0, "topSum": 0.0})
                    st["n"] += n; st["sum"] += r.get("sumPct") or 0.0; st["rules"] += 1
                    if n > st["topN"]:
                        st["topN"], st["topSum"] = n, r.get("sumPct") or 0.0
                avg = {}
                for v, st in g.items():
                    if st["n"] < SHAPE_LEARN_MIN or st["rules"] < 3:
                        continue
                    rest = st["n"] - st["topN"]
                    avg[v] = ((st["sum"] - st["topSum"]) / rest) if rest >= SHAPE_LEARN_MIN else st["sum"] / st["n"]
                if len(avg) < 2:
                    continue
                base = sum(avg.values()) / len(avg)
                lo, hi = SHAPE_LEARN_RANGE
                out[key] = {v: round(min(hi, max(lo, 1 + (a - base) / 0.3)), 2) for v, a in avg.items()}
        except Exception as e:
            log(f"[경고] 익절폭·보유시간 학습 집계 실패 (균등으로 생성): {e}")
            return {}
        return out

    def rule_open(self, market, spec):
        """채택한 규칙을 장부에 등록 (이미 있으면 그대로)"""
        rid = spec.get("rid") or rule_id(market, spec)
        spec["rid"] = rid
        rb = self.state["ruleBook"].setdefault(rid, {
            "id": rid, "market": market, "desc": describe(spec), "spec": {k: spec[k] for k in SPEC_KEYS if k in spec},
            "btWinrate": spec.get("winrate"), "btEdge": spec.get("edgePct"), "btVol": spec.get("btVol"),
            "trades": 0, "wins": 0, "sumPct": 0.0, "pnl": 0, "status": "testing",
            "firstAt": int(time.time() * 1000), "lastAt": 0})
        return rb

    def trim_rulebook(self):
        rbk = self.state["ruleBook"]
        if len(rbk) <= RULE_BOOK_MAX:
            return
        order = {"retired": 0, "testing": 1, "proven": 2}
        drop = sorted(rbk.values(), key=lambda r: (order.get(r["status"], 1), r["trades"] > 0, r.get("lastAt") or r["firstAt"]))
        for r in drop[:len(rbk) - RULE_BOOK_MAX]:
            if r["status"] != "proven":
                rbk.pop(r["id"], None)

    def keep_rule(self, market):
        """지금 규칙을 유지해야 하면 그 spec 반환: 실전 5회 전(검증 중)이거나 실전에서 통한 규칙"""
        cur = self.state["strategies"].get(market)
        if cur:
            rb = self.state["ruleBook"].get(cur.get("rid") or rule_id(market, cur))
            if rb and rb["status"] == "proven":
                return cur
            if rb and rb["status"] == "testing" and rb.get("outs", 0) < RULE_OUT_LIMIT:
                # 6시간 동안 한 번도 신호가 없던 규칙은 붙잡고 있지 않음 (기록은 장부에 남음)
                # 단, 이 규칙으로 산 코인을 아직 들고 있으면 신호가 났던 것 → 유지 (안 그러면 매도 후 1/5회에서 끊겨 판정 못 함)
                holding = (self.state["positions"].get(market) or {}).get("rid") == rb["id"]
                if rb["trades"] > 0 or holding or time.time() * 1000 - rb["firstAt"] < 6 * 3600 * 1000:
                    return cur
            if not rb:
                return cur                                # 이전 버전에서 고른 규칙: 장부에 등록하고 검증 시작
        # 예전에 실전에서 통한 규칙이 있으면 그것부터 (거래당 평균 순익 높은 순)
        best = None
        for rb in self.state["ruleBook"].values():
            if rb["market"] == market and rb["status"] == "proven" and rb.get("spec"):
                avg = rb["sumPct"] / rb["trades"]
                if best is None or avg > best[0]:
                    best = (avg, rb)
        if not best:
            # 실전 1~4회에서 끊긴 검증 중 규칙(종목이 상위권에서 잠시 빠졌다 돌아온 경우 등)은 5회를 마저 채움
            for rb in self.state["ruleBook"].values():
                if (rb["market"] == market and rb["status"] == "testing" and rb.get("spec")
                        and rb["trades"] > 0 and rb.get("outs", 0) < RULE_OUT_LIMIT):
                    if best is None or (rb.get("lastAt") or 0) > (best[1].get("lastAt") or 0):
                        best = (0, rb)
        if best:
            rb = best[1]
            spec = dict(rb["spec"])
            spec.update({"rid": rb["id"], "winrate": rb.get("btWinrate"), "edgePct": rb.get("btEdge"),
                         "btVol": rb.get("btVol"), "trades": rb["trades"], "fromBook": True})
            return spec
        return None

    def order_size(self):
        size = self.cfg["ORDER_KRW"] * (2 ** self.ledger["martLevel"])
        return min(size, self.available())

    # ---------- 종목 선정 ----------
    def pick_universe(self):
        log(f"거래대금 상위 종목 계산 중 (최근 {UNIVERSE_DAYS}일, 상장 {UNIVERSE_MIN_LISTED_DAYS}일 이상)…")
        details = self.upbit.markets_detail()
        candidates = []
        for m in details:
            mk = m["market"]
            if not mk.startswith("KRW-") or mk.split("-")[1] in PROTECTED_COINS | STABLE_COINS:
                continue
            warn = m.get("market_warning") == "CAUTION"
            ev = m.get("market_event") or {}
            if ev.get("warning") or any((ev.get("caution") or {}).values()):
                warn = True
            if not warn:
                candidates.append(mk)
        totals = []
        for idx, mk in enumerate(candidates, 1):
            if idx % 40 == 0:
                self.push_progress(f"거래대금 계산 {idx}/{len(candidates)}")
            try:
                data = self.upbit.day_candles(mk, UNIVERSE_MIN_LISTED_DAYS)
                time.sleep(0.12)
                if len(data) < UNIVERSE_MIN_LISTED_DAYS:
                    continue                              # 상장한 지 얼마 안 된 종목은 제외
                total = sum(c["candle_acc_trade_price"] for c in data[:UNIVERSE_DAYS])
                totals.append((total, mk))
            except UpbitError as e:
                log(f"[경고] {mk} 일봉 조회 실패: {e}")
        totals.sort(reverse=True)
        top = [mk for _, mk in totals[:TOP_N]]
        self.state["universe"] = {"day": today_key(), "markets": top, "topN": TOP_N}
        save_state(self.state)
        log("상위 종목: " + ", ".join(m.split("-")[1] for m in top))

    # ---------- 전략 선정 ----------
    def optimize(self):
        markets = self.state["universe"]["markets"]
        log(f"전략 백테스트 시작: {len(markets)}종목 × {BT_TRIALS}개 조합")
        chosen = {}
        weights = self.cond_weights()
        if weights:
            log("조건 학습 가중치: " + ", ".join(f"{k} ×{v:.1f}" for k, v in sorted(weights.items(), key=lambda x: -x[1])))
        shape = self.shape_weights()
        if shape:
            log("익절폭·보유시간 학습 가중치: " + " / ".join(
                ("익절 " if k == "tp" else "보유 ") + ", ".join(f"{('+%g%%' % v) if k == 'tp' else ('%d분' % v)} ×{w:.1f}"
                                                              for v, w in sorted(m.items(), key=lambda x: -x[1]))
                for k, m in shape.items()))
        for idx, mk in enumerate(markets, 1):
            if self.state["positions"]:
                try:
                    self.manage_positions()            # 오래 걸리는 선정 중에도 보유 코인 익절·손절 관리
                except Exception as e:
                    log(f"[경고] 선정 중 보유 관리 오류: {e}")
            kept = self.keep_rule(mk)
            if kept:
                rb = self.rule_open(mk, kept)
                chosen[mk] = kept
                why = "실전에서 통한 규칙" if rb["status"] == "proven" else f"실전 검증 중 {rb['trades']}회 · {rb.get('outs', 0)}아웃"
                log(f"  유지 {mk.split('-')[1]}: {rb['desc']} ({why})")
                continue
            self.push_progress(f"전략 백테스트 {idx}/{len(markets)} ({mk.split('-')[1]})")
            try:
                series = Series(self.upbit.minute_candles(mk, BT_CANDLES))
            except UpbitError as e:
                log(f"[경고] {mk} 분봉 조회 실패: {e}")
                continue
            if series.n < 300:
                continue
            best = None
            for _ in range(BT_TRIALS):
                spec = random_spec(self.rng, weights, shape)
                rb = self.state["ruleBook"].get(rule_id(mk, spec))
                if rb and rb["status"] == "retired":
                    continue                              # 실전에서 퇴출된 규칙은 다시 안 씀
                r = evaluate(series, spec)
                if r and (best is None or r["score"] > best[1]["score"]):
                    best = (spec, r)
            if best:
                spec, r = best
                spec.update(r)
                spec["btVol"] = round(avg_range_pct(series, series.n), 4)
                self.rule_open(mk, spec)
                chosen[mk] = spec
                log(f"  채택 {mk.split('-')[1]}: {describe(spec)} · 승률 {r['winrate']*100:.0f}% "
                    f"{r['trades']}회 · 거래당 {r['edgePct']:+.2f}% · 검증구간 {r['holdoutTrades']}회 승률 {r['holdoutWinrate']*100:.0f}% 거래당 {r['holdoutEdgePct']:+.2f}%")
            else:
                log(f"  {mk.split('-')[1]}: 학습·검증 구간 모두 수익 나는 익절/손절 조합 없음 → 이번 시간 거래 안 함")
        self.state["strategies"] = chosen
        self.state["lastOptimize"] = int(time.time())
        save_state(self.state)
        log(f"전략 선정 완료: {len(chosen)}종목 거래 대상")

    # ---------- 진입/청산 ----------
    def can_enter(self):
        lg = self.ledger
        if self.state["paused"]:
            return False, "일시정지"
        if time.time() < lg["cooldownUntil"]:
            return False, "손실 후 대기"
        if self.cfg["DAILY_LOSS_LIMIT"] and lg["dayPnl"] <= -self.cfg["DAILY_LOSS_LIMIT"]:
            return False, "오늘 손실 한도 도달"
        if not EXPLORE_MODE and lg["gateTrades"] >= LIVE_DAY_MIN_TRADES and lg["gateWins"] / lg["gateTrades"] < LIVE_DAY_MIN_WINRATE:
            return False, f"현재 전략 실전 승률 {lg['gateWins']}/{lg['gateTrades']} 부진 → 9시까지 쉼"
        if len(self.state["positions"]) >= MAX_OPEN_POSITIONS:
            return False, "동시 보유 한도"
        if self.order_size() < MIN_ORDER_KRW:
            return False, "시드 부족"
        return True, ""

    def enter(self, market, spec, price, ctx=None):
        krw = self.order_size()
        shadow = self.is_shadow(market, spec)
        if shadow:
            fill = price * (1 + SHADOW_SLIPPAGE)
            vol = krw * (1 - FEE_RATE) / fill
            cost = krw
        elif self.live:
            krw_balance = next((float(a["balance"]) for a in self.upbit.accounts() if a["currency"] == "KRW"), 0)
            krw = min(krw, krw_balance * 0.995)
            if krw < MIN_ORDER_KRW:
                log("[경고] 업비트 원화 잔고 부족으로 매수 건너뜀")
                return
            res = self.upbit.buy_market(market, krw)
            vol, funds, fee = self.upbit.wait_fill(res["uuid"])
            if vol <= 0:
                log(f"[경고] {market} 매수 체결 확인 실패")
                return
            cost = funds + fee
            self.exec_record("buy", (funds / vol / price - 1) * 100)
        else:
            vol = krw * (1 - FEE_RATE) / price
            cost = krw
        unit = cost / vol
        # 목표가: 매수 수수료 포함 단가 기준, 매도 수수료까지 빼고 순수익 0.5%
        self.state["positions"][market] = {
            "volume": vol, "cost": cost, "unit": unit, "tp": target_price(unit, spec.get("tp", TARGET_NET_PCT)),
            "sl": unit * (1 + spec["sl"] / 100), "deadline": int(time.time()) + spec["hold"] * 60,
            "at": int(time.time() * 1000), "strategy": describe(spec), "mart": self.ledger["martLevel"],
            "maxP": unit, "minP": unit, "btVol": spec.get("btVol"),
            "btWinrate": spec.get("winrate"), "btEdge": spec.get("edgePct"),
            "rid": spec.get("rid") or self.rule_open(market, spec)["id"], **(ctx or {}),
        }
        if shadow:
            self.state["positions"][market]["paper"] = True
        try:
            self.state["positions"][market]["btcAtEntry"] = self.upbit.prices(["KRW-BTC"]).get("KRW-BTC")
        except Exception:
            pass
        save_state(self.state)
        tag = "검증 모의매수" if shadow else ("실매수" if self.live else "모의매수")
        log(f"[{tag}] {market} {cost:,.0f}원 @ {unit:,.4f} · 목표 {self.state['positions'][market]['tp']:,.4f} "
            f"· 손절 {self.state['positions'][market]['sl']:,.4f} · {describe(spec)}")
        self._record("buy", market, unit, cost, describe(spec), None, paper=shadow)
        try:
            self.monkey_spawn(market, spec)
        except Exception as e:                    # 분석 전용 기록 실패가 매매를 막으면 안 됨
            log(f"[경고] 원숭이 기준선 기록 실패: {e}")
        self.upload_status()

    def exit(self, market, price, reason):
        pos = self.state["positions"].get(market)
        if not pos:
            return
        shadow = bool(pos.get("paper"))
        if shadow:
            proceeds = pos["volume"] * price * (1 - SHADOW_SLIPPAGE) * (1 - FEE_RATE)
        elif self.live:
            res = self.upbit.sell_market(market, int(pos["volume"] * 1e8) / 1e8)
            vol, funds, fee = self.upbit.wait_fill(res["uuid"])
            proceeds = funds - fee
            if vol > 0:
                fill = funds / vol
                slip = (fill / price - 1) * 100
                tp_gap = (pos["tp"] / fill - 1) * 100 if reason == "익절" else None
                self.exec_record(reason, slip, tp_gap)
                if abs(slip) >= 0.2:
                    log(f"[체결 확인] {market} 신호가 {price:,.4f} → 체결가 {fill:,.4f} ({slip:+.2f}%)")
            if vol < pos["volume"] * 0.999:
                log(f"[경고] {market} 매도 일부만 체결 {vol:.8f}/{pos['volume']:.8f} — 남은 수량은 업비트에서 확인 필요")
        else:
            proceeds = pos["volume"] * price * (1 - FEE_RATE)
        pnl = proceeds - pos["cost"]
        if reason == "익절" and pnl <= 0:
            reason = "익절 신호였으나 체결 손실"     # 장부·학습에 '익절'로 잘못 남지 않게
        del self.state["positions"][market]
        if shadow:
            self.shadow_book(pnl, market)
        else:
            self.book(pnl, market)
        self.learn(market, pnl, reason)
        self.cond_record(pos, pnl)
        self.rule_record(pos, pnl)
        actual_pct = pnl / pos["cost"] * 100
        self.hour_record(pos, pnl, actual_pct)
        self.btcflow_record(pos, pnl, actual_pct)
        self.tp_limit_record(market, pos, reason, actual_pct)
        if pos.get("monkey"):                             # 원숭이 기준선: 같은 시각에 산 우리 거래 성적
            self.monkey_record("ours", actual_pct)
            if isinstance(pos["monkey"], str):            # 짝 비교: 같은 순간 산 원숭이와 1:1로 묶어 차이를 잼
                rb_ = self.state["ruleBook"].get(pos.get("rid") or "") or {}
                conds_ = [c[0] for c in (rb_.get("spec") or {}).get("conds", [])]
                self.monkey_pair(pos["monkey"], "ours", actual_pct, conds_)
        for rid, mk_ in (pos.get("exitMarks") or {}).items():
            if self.exit_stat(rid)["mode"] != "active" or reason != next((r["name"] for r in EXIT_RULES if r["id"] == rid), ""):
                self.exit_record(rid, mk_["pct"], actual_pct, mk_["at"])   # 관찰: 그때 정리했다면 vs 실제
        if "gateHits" in pos:                             # 관문 기능 이후 산 거래만 관문 성적에 반영
            self.gate_record(pos["gateHits"], pnl / pos["cost"] * 100, pos["at"])
            self.overlap_record(pos["gateHits"], pnl / pos["cost"] * 100)
        analysis = None
        if pnl <= 0:
            analysis = self.post_mortem(market, pos, price, reason, pnl)
        tl = self.state.setdefault("tradeLog", [])
        tl.append({"t": datetime.now(KST).strftime("%m-%d %H:%M"), "coin": market.split("-")[1],
                   "pnl": round(pnl), "cost": round(pos["cost"]), "exit": reason, "strategy": pos.get("strategy", ""),
                   "heldMin": round((time.time() * 1000 - pos["at"]) / 60000, 1),
                   "btWinrate": pos.get("btWinrate"), "mart": pos.get("mart"),
                   "cause": analysis["label"] if analysis else None, "paper": shadow,
                   "gates": pos.get("gateHits"),
                   "details": [c["detail"] for c in analysis["causes"]] if analysis else None})
        del tl[:-AI_TRADE_LOG]
        if pnl <= 0:
            self.state["aiLossesSince"] = self.state.get("aiLossesSince", 0) + 1
        tag = "검증 모의매도" if shadow else ("실매도" if self.live else "모의매도")
        log(f"[{tag}] {market} 손익 {pnl:+,.0f}원 ({reason}) · 시드 {self.ledger['seed']:,.0f} · 잠김 {self.ledger['locked']:,.0f}"
            + ("" if pnl > 0 else f" · {LOSS_COOLDOWN_MIN}분 대기"))
        self._record("sell", market, price, proceeds, reason, round(pnl), analysis, paper=shadow)
        self.upload_status()

    def overlap_record(self, hits, pct):
        """분석 전용: 매수 순간 '관찰 중 관문이 몇 개 동시에 감지했는지'별 실전·검증 성적 — 매매에는 영향 없음.
        관문 하나하나는 약해도 여러 개가 겹칠 때만 나쁜지 보려는 것"""
        try:
            k = str(min(len(hits or []), 3))
            st = self.state.setdefault("overlapStats", {}).setdefault(k, {"n": 0, "wins": 0, "sumPct": 0.0, "recent": []})
            st["n"] += 1
            st["wins"] += 1 if pct > 0 else 0
            st["sumPct"] += pct
            st["recent"] = (st.get("recent", []) + [round(pct, 3)])[-OVERLAP_RECENT:]
        except Exception as e:
            log(f"[경고] 관문 겹침 성적 기록 실패: {e}")

    def overlap_view(self):
        out = []
        for k, v in sorted((self.state.get("overlapStats") or {}).items()):
            if not v.get("n"):
                continue
            r = v.get("recent") or []
            out.append({"hits": int(k), "n": v["n"], "wins": v["wins"], "avgPct": round(v["sumPct"] / v["n"], 3),
                        "recentN": len(r), "recentAvg": round(sum(r) / len(r), 3) if r else None})
        return out or None

    def hour_record(self, pos, pnl, pct):
        """분석 전용: 매수한 시각(한국시간)별 실전 성적 누적 — 매매에는 영향 없음"""
        try:
            h = "%02d" % datetime.fromtimestamp(pos["at"] / 1000, KST).hour
            st = self.state.setdefault("hourStats", {}).setdefault(h, {"n": 0, "wins": 0, "sumPct": 0.0, "pnl": 0.0})
            st["n"] += 1
            st["wins"] += 1 if pnl > 0 else 0
            st["sumPct"] += pct
            st["pnl"] += pnl
        except Exception as e:
            log(f"[경고] 시간대 성적 기록 실패: {e}")

    @staticmethod
    def btcflow_buckets(pos):
        """매수 순간 BTC 흐름 → (30분 변화 구간, 60분 고저폭 구간). 기록이 없으면 None"""
        c, r = pos.get("btc30"), pos.get("btcRng60")
        cb = None if c is None else ("1:-0.5% 이하" if c <= -0.5 else "2:-0.5~-0.2%" if c < -0.2 else "3:-0.2~+0.2%" if c <= 0.2
                                     else "4:+0.2~+0.5%" if c < 0.5 else "5:+0.5% 이상")
        rb = None if r is None else ("1:0.4% 미만" if r < 0.4 else "2:0.4~0.8%" if r < 0.8 else "3:0.8% 이상")
        return cb, rb

    def btcflow_record(self, pos, pnl, pct):
        """분석 전용: 매수 순간 BTC 흐름(30분 변화·60분 고저폭) 구간별 실전·검증 성적 누적 — 매매에는 영향 없음.
        BTC 급락·하락 추세·급등·잠잠함 관문의 '막았을/통과' 두 칸 비교를 구간별 곡선으로 넓혀 봄"""
        try:
            cb, rb = self.btcflow_buckets(pos)
            fs = self.state.setdefault("btcFlowStats", {})
            for key, b in (("chg", cb), ("rng", rb)):
                if b is None:
                    continue
                st = fs.setdefault(key, {}).setdefault(b, {"n": 0, "wins": 0, "sumPct": 0.0, "pnl": 0.0, "live": 0, "liveSum": 0.0})
                st["n"] += 1
                st["wins"] += 1 if pnl > 0 else 0
                st["sumPct"] += pct
                st["pnl"] += pnl
                if not pos.get("paper"):
                    st["live"] += 1
                    st["liveSum"] += pct
        except Exception as e:
            log(f"[경고] BTC 흐름별 성적 기록 실패: {e}")

    def btcflow_view(self):
        fs = self.state.get("btcFlowStats") or {}
        out = []
        for key, label in (("chg", "BTC 30분 변화"), ("rng", "BTC 60분 고저폭")):
            rows = []
            for b, v in sorted((fs.get(key) or {}).items()):
                if not v.get("n"):
                    continue
                rows.append({"bucket": b.split(":", 1)[1], "n": v["n"], "wins": v["wins"], "avgPct": round(v["sumPct"] / v["n"], 3),
                             "pnl": round(v["pnl"]), "liveN": v.get("live", 0),
                             "liveAvg": round(v["liveSum"] / v["live"], 3) if v.get("live") else None})
            if rows:
                out.append({"key": key, "label": label, "rows": rows})
        return out or None

    def hours_view(self):
        """3시간 묶음(00-02, 03-05 …)으로 합쳐 대시보드에 보냄"""
        hs = self.state.get("hourStats", {})
        out = []
        for b in range(0, 24, 3):
            n = w = 0
            sp = pn = 0.0
            for h in range(b, b + 3):
                st = hs.get("%02d" % h)
                if st:
                    n += st["n"]; w += st["wins"]; sp += st["sumPct"]; pn += st["pnl"]
            out.append({"from": b, "to": b + 2, "n": n, "wins": w,
                        "avgPct": round(sp / n, 3) if n else None, "pnl": round(pn)})
        return out

    def spec_view(self):
        """분석 전용: 규칙 장부 전체(퇴출 포함)를 규칙 특성(익절폭·보유시간·조건 개수·손절폭·익절/손절 비율·백테스트 우위)별로 묶은 실전 성적.
        어떤 모양의 규칙이 실전에서 덜 손해 보는지 보는 용도이며 매매에는 영향 없음"""
        try:
            def edge_b(e):
                return None if e is None else ("우위 0.3% 미만" if e < 0.3 else "우위 0.3~0.5%" if e < 0.5 else "우위 0.5% 이상")
            def rr_b(tp, sl):
                # 익절폭 ÷ 손절폭: 작을수록 '조금 벌고 크게 잃는' 모양 (예: 익절 +1% / 손절 -5% → 0.2)
                if tp is None or not sl:
                    return None
                rr = tp / abs(sl)
                return "비율 0.35 미만" if rr < 0.35 else "비율 0.35~1" if rr < 1 else "비율 1 이상"
            keys = (("tp", "익절폭", lambda sp, r: "익절 +%g%%" % sp.get("tp") if sp.get("tp") is not None else None),
                    ("hold", "보유시간", lambda sp, r: "최대 %d분" % sp.get("hold") if sp.get("hold") else None),
                    ("ncond", "조건 개수", lambda sp, r: "조건 %d개" % len(sp.get("conds") or []) if sp.get("conds") else None),
                    ("sl", "손절폭", lambda sp, r: "손절 %g%%" % sp.get("sl") if sp.get("sl") is not None else None),
                    ("rr", "익절÷손절 비율(작을수록 조금 벌고 크게 잃음)", lambda sp, r: rr_b(sp.get("tp"), sp.get("sl"))),
                    ("edge", "백테스트 우위", lambda sp, r: edge_b(r.get("btEdge"))))
            out = []
            for k, label, fn in keys:
                g = {}
                for r in self.state.get("ruleBook", {}).values():
                    n = r.get("trades") or 0
                    if n <= 0:
                        continue
                    b = fn(r.get("spec") or {}, r)
                    if b is None:
                        continue
                    st = g.setdefault(b, {"n": 0, "wins": 0, "sumPct": 0.0, "pnl": 0.0, "rules": 0, "topN": 0, "topSum": 0.0})
                    st["n"] += n; st["wins"] += r.get("wins") or 0
                    st["sumPct"] += r.get("sumPct") or 0.0; st["pnl"] += r.get("pnl") or 0
                    st["rules"] += 1
                    if n > st["topN"]:                     # 거래가 가장 많은 규칙 하나 — 한 규칙이 칸 평균을 끌고 가는지 보려고
                        st["topN"], st["topSum"] = n, r.get("sumPct") or 0.0
                rows = []
                for b, v in g.items():
                    row = {"bucket": b, "n": v["n"], "wins": v["wins"], "avgPct": round(v["sumPct"] / v["n"], 3),
                           "pnl": round(v["pnl"]), "rules": v["rules"], "topShare": round(v["topN"] / v["n"], 3)}
                    if v["n"] > v["topN"]:                 # 그 규칙 하나를 뺀 평균 (쏠림 확인용)
                        row["exTopAvg"] = round((v["sumPct"] - v["topSum"]) / (v["n"] - v["topN"]), 3)
                    rows.append(row)
                rows.sort(key=lambda x: -x["avgPct"])
                if rows:
                    out.append({"key": k, "label": label, "rows": rows})
            return out
        except Exception as e:
            log(f"[경고] 규칙 특성별 성적 집계 실패: {e}")
            return []

    def exec_record(self, kind, slip_pct, tp_gap=None):
        """체결 비용 기록(분석 전용, 매매에는 영향 없음): 시장가 주문이 신호가 대비 얼마나 밀려 체결됐는지.
        익절은 '목표가에 지정가 매도를 미리 걸어뒀다면' 더 받았을 몫(tp_gap)도 함께 누적 → 지정가 전환 판단 근거"""
        try:
            key = "buy" if kind == "buy" else "sell:" + ("익절" if kind.startswith("익절") else kind if kind in ("손절", "보유시간 초과") else "조기 정리")
            es = self.state.setdefault("execStats", {}).setdefault(key, {"n": 0, "sumSlip": 0.0, "worse": 0, "sumTpGap": 0.0})
            es["n"] += 1
            es["sumSlip"] += slip_pct
            es["worse"] += 1 if (slip_pct > 0.05 if kind == "buy" else slip_pct < -0.05) else 0
            if tp_gap is not None:
                es["sumTpGap"] += max(tp_gap, 0.0)
        except Exception:
            pass

    def tp_limit_record(self, market, pos, reason, actual_pct):
        """관찰 전용(매매에는 영향 없음): '매수 직후 목표가에 지정가 매도를 미리 걸어뒀다면' 결과를 실제와 비교.
        거래가 끝날 때 보유 구간 1분봉 고가가 목표가를 '넘었으면'(같기만 하면 대기열 탓에 안 채워질 수 있어 제외) 목표가에 체결됐다고 봄.
        → 시장가 익절 밀림 + 1분봉 안에서 잠깐 찍고 내려와 놓친 익절(감시 주기 사이)까지 함께 측정. 안 넘었으면 실제와 같음(차이 0)"""
        try:
            entry_ms, now_ms = pos["at"], time.time() * 1000
            need = int((now_ms - entry_ms) / 60000) + 2
            if need > TP_LIMIT_MAX_MIN:
                return
            candles = self.upbit.minute_candles(market, need)
            start = (int(entry_ms // 60000) + 1) * 60000            # 매수한 그 1분봉은 매수 전 고가일 수 있어 제외
            touched = False
            for c in candles:
                t = datetime.strptime(c["candle_date_time_utc"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000
                if start <= t <= now_ms and c["high_price"] > pos["tp"]:
                    touched = True
                    break
            self.martin_record(market, pos, candles, start, now_ms, actual_pct)
            virt = (pos["tp"] * (1 - FEE_RATE) / pos["unit"] - 1) * 100 if touched else actual_pct
            diff = virt - actual_pct
            st = self.state.setdefault("tpLimit", {"n": 0, "touched": 0, "missed": 0, "better": 0, "worse": 0, "sumDiff": 0.0})
            st["n"] += 1
            st["touched"] += touched
            st["missed"] += touched and not reason.startswith("익절")      # 목표가를 넘었는데 익절로 못 끝난 거래
            st["better"] += diff > 0.01
            st["worse"] += diff < -0.01
            st["sumDiff"] += diff
            if touched and not reason.startswith("익절"):
                log(f"[지정가 관찰] {market.split('-')[1]} 보유 중 1분봉 고가가 목표가를 넘었음 → 지정가였다면 {virt:+.2f}% (실제 {reason} {actual_pct:+.2f}%)")
        except Exception:
            pass

    def martin_record(self, market, pos, candles, start, now_ms, actual_pct):
        """관찰 전용(실제 주문 없음): 같은 거래를 '손실 나면 MARTIN_MULT배로 물타기 + 평단 기준 익절가에 지정가 매도'로 했다면.
        보유 구간 1분봉을 시간순으로 따라가며 저가가 물타기 가격에 닿으면 추가 매수, 그 다음 봉부터 고가가 새 목표가를 넘으면 익절로 봄.
        규칙의 실제 보유가 끝날 때까지 목표에 못 가면 마지막 가격으로 정리했다고 셈(실제 물타기는 더 오래 버티므로 꼬리 위험은 이보다 큼)"""
        try:
            base, unit = float(pos["cost"]), float(pos["unit"])
            tp_frac = pos["tp"] / unit - 1
            bars = []
            for c in candles:
                t = datetime.strptime(c["candle_date_time_utc"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000
                if start <= t <= now_ms:
                    bars.append((t, c["low_price"], c["high_price"], c["trade_price"]))
            if not bars:
                return
            bars.sort()
            invested, vol, adds, won, pnl = base, base * (1 - FEE_RATE) / unit, 0, False, None
            target = unit * (1 + tp_frac)
            for _, lo, hi, _cl in bars:
                added = False
                while adds < MARTIN_MAX_ADDS and lo <= unit * (1 - MARTIN_STEP_PCT * (adds + 1) / 100):
                    adds += 1
                    lp = unit * (1 - MARTIN_STEP_PCT * adds / 100)
                    amt = base * MARTIN_MULT ** adds
                    vol += amt * (1 - FEE_RATE) / lp
                    invested += amt
                    added = True
                target = invested / vol * (1 + tp_frac)              # 평단(수수료 포함) 기준 익절가
                if not added and hi > target:
                    won, pnl = True, vol * target * (1 - FEE_RATE) - invested
                    break
            if pnl is None:
                pnl = vol * bars[-1][3] * (1 - FEE_RATE) - invested
            st = self.state.setdefault("martinSim", {"n": 0, "wins": 0, "pnl": 0.0, "actualPnl": 0.0, "maxInvest": 0,
                                                     "legs": [0] * (MARTIN_MAX_ADDS + 1), "worst": 0.0})
            st["n"] += 1
            st["wins"] += won
            st["pnl"] += pnl
            st["actualPnl"] += base * actual_pct / 100
            st["maxInvest"] = max(st["maxInvest"], int(invested))
            legs = st.setdefault("legs", [0] * (MARTIN_MAX_ADDS + 1))
            if len(legs) < MARTIN_MAX_ADDS + 1:
                legs.extend([0] * (MARTIN_MAX_ADDS + 1 - len(legs)))
            legs[adds] += 1
            st["worst"] = min(st["worst"], pnl)
            if adds >= 2:
                log(f"[물타기 관찰] {market.split('-')[1]} {adds}번 물탔다면 투입 {invested:,.0f}원 → "
                    f"{'익절' if won else '미도달'} {pnl:+,.0f}원 (실제 {base * actual_pct / 100:+,.0f}원) · 실제 주문 없음")
        except Exception:
            pass

    def post_mortem(self, market, pos, price, reason, pnl):
        """실패로 끝난 거래 원인 분석 → 기록·통계"""
        btc = None
        try:
            if pos.get("btcAtEntry"):
                now_btc = self.upbit.prices(["KRW-BTC"]).get("KRW-BTC")
                btc = round((now_btc / pos["btcAtEntry"] - 1) * 100, 2)
        except Exception:
            pass
        now_vol = 0.0
        try:
            now_vol = avg_range_pct(Series(self.upbit.minute_candles(market, 31)[:-1]), 30)
        except Exception:
            pass
        a = diagnose(pos, price, reason, btc, now_vol)
        a.update({"strategy": pos.get("strategy", ""), "btWinrate": pos.get("btWinrate"), "btEdge": pos.get("btEdge")})
        fs = self.state.setdefault("failStats", {})
        for c in a["causes"]:
            fs[c["code"]] = fs.get(c["code"], 0) + 1
        save_state(self.state)
        log(f"[실패 분석] {market.split('-')[1]} {pnl:+,.0f}원 · 주원인: {a['label']} · "
            + " / ".join(f"{c['label']}({c['detail']})" for c in a["causes"]))
        return a

    def _record(self, side, market, price, krw, reason, pnl, analysis=None, paper=False):
        try:
            data = {"mode": "live" if self.live and not paper else "paper", "side": side, "market": market,
                    "price": float(price), "krw": round(krw), "reason": reason, "pnl": pnl,
                    "at": int(time.time() * 1000)}
            if analysis:
                data["analysis"] = analysis
            self.cloud.add_trade(data)
        except Exception as e:
            log(f"[경고] 거래 기록 업로드 실패: {e}")

    def manage_positions(self):
        pos = self.state["positions"]
        if not pos:
            return
        prices = self.upbit.prices(list(pos.keys()))
        for mk, p in list(pos.items()):
            price = prices.get(mk)
            if not price:
                continue
            p["maxP"] = max(p.get("maxP", price), price)
            p["minP"] = min(p.get("minP", price), price)
            if price < p["tp"] and price > p["sl"] and self.exit_check(mk, p, price):
                continue
            if price >= p["tp"]:
                self.exit(mk, price, "익절")
            elif price <= p["sl"]:
                self.exit(mk, price, "손절")
            elif time.time() >= p["deadline"]:
                self.exit(mk, price, "보유시간 초과")

    def scan_entries(self):
        ok, _ = self.can_enter()
        if not ok:
            return
        cool = self.state.setdefault("coolMarkets", {})
        now_min = int(time.time() // 60)
        seen = getattr(self, "_scan_min", {})
        self._scan_min = seen
        for mk, spec in list(self.state["strategies"].items()):
            if mk in self.state["positions"] or mk in self.state["blocked"] or mk.split("-")[1] in PROTECTED_COINS:
                continue
            if cool.get(mk, 0) > time.time():
                continue                                  # 이 종목만 손실 후 대기
            if seen.get(mk) == now_min:
                continue                                  # 1분봉은 1분에 한 번만 새로 생김
            seen[mk] = now_min
            candles = self.upbit.minute_candles(mk, 200)
            time.sleep(0.1)
            closed = candles[:-1]                         # 진행 중인 봉 제외
            if len(closed) < 60:
                continue
            key = closed[-1]["candle_date_time_kst"]
            if self.state["lastCandle"].get(mk) == key:
                continue
            self.state["lastCandle"][mk] = key
            series = Series(closed)
            if len(series.c) > BREADTH_MIN:               # 시장 폭 관문용: 종목별 최근 30분 변화율 기록
                self.__dict__.setdefault("_mk_chg", {})[mk] = ((series.c[-1] / series.c[-1 - BREADTH_MIN] - 1) * 100, time.time())
            if series.signal(spec)[-1]:
                price = self.upbit.prices([mk]).get(mk)
                if price:
                    ctx = {"signalPrice": series.c[-1], "entryVol": round(avg_range_pct(series), 4),
                           "tpPct": spec.get("tp", TARGET_NET_PCT), "btVol": spec.get("btVol")}
                    if len(series.c) > REACH_MIN:         # 힘 부족 관문용: 평균|1분 변화| × √보유분
                        mv = sum(abs(series.c[k] / series.c[k - 1] - 1) for k in range(len(series.c) - REACH_MIN, len(series.c))) / REACH_MIN * 100
                        ctx["reachPct"] = round(mv * spec["hold"] ** 0.5, 3)
                        ctx["tpPct"] = spec.get("tp", TARGET_NET_PCT)
                    try:                                  # BTC 흐름별 성적(분석 전용)용: 매수 순간 BTC 30분 변화·60분 고저폭
                        bc = self.btc_closes()
                        if len(bc) > BTC_QUIET_MIN:
                            ctx["btc30"] = round((bc[-1] / bc[-1 - BTC_DROP_MIN] - 1) * 100, 3)
                            w_ = bc[-1 - BTC_QUIET_MIN:]
                            ctx["btcRng60"] = round((max(w_) / min(w_) - 1) * 100, 3)
                    except Exception:
                        pass
                    if len(series.c) > COIN_TREND_MIN:    # 종목 하락 추세 관문용
                        ctx["coinChg"] = round((series.c[-1] / series.c[-1 - COIN_TREND_MIN] - 1) * 100, 3)
                    try:
                        ctx["entryInfo"] = entry_snapshot(series, spec)
                    except Exception as e:                # 팝업용 기록 실패가 매수를 막으면 안 됨
                        log(f"[경고] {mk} 진입 근거 기록 실패: {e}")
                    if not self.gate_entry(mk, spec, price, ctx):
                        continue
                    self.enter(mk, spec, price, ctx)
                    ok, _ = self.can_enter()
                    if not ok:
                        break
        save_state(self.state)

    # ---------- 시장 판단 관문 ----------
    def btc_closes(self):
        now_min = int(time.time() // 60)
        if getattr(self, "_btc_min", None) != now_min:
            try:
                self._btc_c = [c["trade_price"] for c in self.upbit.minute_candles("KRW-BTC", max(BTC_DROP_MIN, BTC_TREND_MIN) + 3)[:-1]]
                self._btc_min = now_min
            except Exception:
                return getattr(self, "_btc_c", [])
        return self._btc_c

    def events(self):
        if time.time() - getattr(self, "_events_at", 0) >= EVENTS_REFRESH:
            self._events_at = time.time()
            try:
                res = requests.get(EVENTS_URL, params={"t": int(time.time())}, timeout=10)
                if res.status_code == 200:
                    ev = res.json().get("events", [])
                    if isinstance(ev, list):
                        self.state["eventsCache"] = ev
            except Exception:
                pass
        return self.state.get("eventsCache", [])

    def gate_stat(self, gid):
        g = self.state.setdefault("marketGates", {}).setdefault(gid, {})
        g.setdefault("mode", "shadow")
        g.setdefault("blocked", {"n": 0, "wins": 0, "sumPct": 0.0})
        g.setdefault("passed", {"n": 0, "wins": 0, "sumPct": 0.0})
        g.setdefault("hours", [])
        g.setdefault("nextEval", GATE_EVAL_MIN)
        return g

    def gate_check(self):
        """1분에 한 번 모든 관문을 평가 → {gid: (막을지, 설명)}"""
        now_min = int(time.time() // 60)
        if getattr(self, "_gate_min", None) == now_min:
            return self._gate_now
        out = {}
        for g in MARKET_GATES:
            if g.get("entry"):
                continue                                   # 종목별 관문은 gate_entry 에서 확인
            try:
                out[g["id"]] = g["fn"](self)
            except Exception as e:
                out[g["id"]] = (False, f"확인 실패: {e}")
            prev = getattr(self, "_gate_now", {}).get(g["id"], (False, ""))[0]
            if out[g["id"]][0] != prev:
                st = self.gate_stat(g["id"])
                how = "적용 중 → 새 매수 보류(가상 거래로 추적)" if st["mode"] == "active" else "관찰 중 → 매수는 하되 '막았을 거래'로 표시"
                log(f"[시장 판단] {g['name']} {'감지' if out[g['id']][0] else '해제'}: {out[g['id']][1]}" + (f" · {how}" if out[g["id"]][0] else ""))
        self._gate_min, self._gate_now = now_min, out
        return out

    def gate_record(self, gids, pct, at_ms):
        """거래 결과를 관문 성적에 반영. gids = 이 거래를 막았을(막은) 관문들"""
        for g in MARKET_GATES:
            st = self.gate_stat(g["id"])
            side = st["blocked"] if g["id"] in gids else st["passed"]
            side["n"] += 1
            side["sumPct"] += pct
            side["wins"] += pct > 0
            if g["id"] in gids:
                hk = datetime.fromtimestamp(at_ms / 1000, KST).strftime("%m%d%H")
                if hk not in st["hours"]:
                    st["hours"] = (st["hours"] + [hk])[-50:]
                if st["blocked"]["n"] >= st["nextEval"]:
                    self.gate_judge(g, st)

    def gate_judge(self, g, st):
        b, p = st["blocked"], st["passed"]
        if len(st["hours"]) < GATE_EVAL_EPISODES or p["n"] < GATE_PASS_MIN:
            return                                         # 아직 판단 근거 부족 → 다음 거래에서 다시 봄
        st["nextEval"] = b["n"] + GATE_RECHECK
        ba, pa = b["sumPct"] / b["n"], p["sumPct"] / p["n"]
        if st["mode"] == "active":
            # 이미 적용 중이면 '더 이상 도움이 안 될 때'(막은 거래가 손실이 아니거나 통과 거래보다 나쁘지 않을 때)만 되돌림
            # → 문턱 근처에서 적용↔관찰이 오락가락하지 않게 함(들어갈 때 0.15%p, 나올 때 0%p)
            want = "active" if (ba < 0 and ba < pa) else "shadow"
        else:
            want = "active" if (ba < 0 and ba <= pa - GATE_MARGIN) else "shadow"
        why = (f"막았을 거래 {b['n']}건 거래당 {ba:+.2f}% vs 통과 거래 {p['n']}건 {pa:+.2f}% ({len(st['hours'])}개 시간대)")
        st.setdefault("history", []).append({"at": int(time.time() * 1000), "want": want, "blockedAvg": round(ba, 3),
                                             "passedAvg": round(pa, 3), "n": b["n"]})
        st["history"] = st["history"][-10:]
        log(f"[관문 판정] {g['name']}: {why} → " + ("적용이 낫다고 봄" if want == "active" else "관찰 유지가 낫다고 봄"))
        if want != st["mode"]:
            self.propose("gate", g["id"], g["name"], st["mode"], want, why)
        else:
            self.withdraw("gate", g["id"], why)

    # ---------- 승인 요청 (봇이 스스로 바꾸고 싶은 것) ----------
    def propose(self, kind, target, name, cur, want, why):
        props = self.state.setdefault("proposals", [])
        for pr in props:
            if pr["status"] == "pending" and pr["kind"] == kind and pr["target"] == target:
                pr.update({"to": want, "why": why, "at": int(time.time() * 1000)})      # 같은 대상은 최신 근거로 갱신
                break
        else:
            pid = f"bot-{kind}-{target}-{int(time.time())}"
            verb = {"active": "적용", "shadow": "관찰로 되돌림"}[want]
            props.append({"id": pid, "kind": kind, "target": target, "title": f"{name} → {verb}", "from": cur, "to": want,
                          "why": why, "at": int(time.time() * 1000), "status": "pending"})
            log(f"[승인 요청] {name} → {verb} · 근거: {why} · 대시보드 '승인 대기'에서 승인/거절")
        self.state["proposals"] = [p for p in props if p["status"] == "pending"] + \
            [p for p in props if p["status"] != "pending"][-10:]
        save_state(self.state)

    def withdraw(self, kind, target, why):
        """새 판정이 지금 상태 유지가 낫다고 보면, 아직 결정 안 된 같은 대상의 승인 요청은 봇이 스스로 거둬들임(설정은 안 바뀜)"""
        hit = False
        for pr in self.state.get("proposals", []):
            if pr["status"] == "pending" and pr["kind"] == kind and pr["target"] == target:
                pr.update({"status": "withdrawn", "decidedAt": int(time.time() * 1000), "withdrawWhy": why})
                hit = True
                log(f"[승인 요청 취소] {pr['title']} · 새 판정은 지금 상태 유지가 낫다고 봄 · {why}")
        if hit:
            save_state(self.state)

    def decide(self, pid, approve):
        """대시보드 승인/거절 처리. 봇 제안은 바로 반영, AI(코드) 제안은 기록만 → 다음 AI 점검 때 병합/닫기"""
        for pr in self.state.get("proposals", []):
            if pr["id"] == pid and pr["status"] == "pending":
                pr["status"] = "approved" if approve else "rejected"
                pr["decidedAt"] = int(time.time() * 1000)
                if approve:
                    st = self.gate_stat(pr["target"]) if pr["kind"] == "gate" else self.exit_stat(pr["target"])
                    st["mode"] = pr["to"]
                save_state(self.state)
                return "done", f"{pr['title']} {'승인 → 지금부터 반영' if approve else '거절 → 지금 상태 유지(기록은 계속 쌓음)'}"
        if pid.startswith("ai-"):
            self.state.setdefault("aiDecisions", {})[pid] = {"decision": "approve" if approve else "reject",
                                                             "at": int(time.time() * 1000)}
            save_state(self.state)
            return "done", ("승인 기록 → 다음 AI 점검(3시간 이내) 때 반영" if approve else "거절 기록 → 다음 AI 점검 때 제안 닫음")
        return "rejected", "이미 처리됐거나 없는 제안"

    # ---------- 청산 판단 ----------
    def exit_stat(self, rid):
        st = self.state.setdefault("exitRules", {}).setdefault(rid, {})
        st.setdefault("mode", "shadow")
        st.setdefault("n", 0)
        st.setdefault("sumDiff", 0.0)
        st.setdefault("better", 0)
        st.setdefault("hours", [])
        st.setdefault("nextEval", EXIT_EVAL_MIN)
        return st

    def exit_check(self, mk, p, price):
        """보유 중 청산 규칙 확인. 적용 중 규칙이 걸리면 True(이미 정리함)"""
        marks = p.setdefault("exitMarks", {})
        for r in EXIT_RULES:
            if r["id"] in marks:
                continue
            try:
                hit, why = r["fn"](self, p, price)
            except Exception:
                continue
            if not hit:
                continue
            early = (price * (1 - slip_for(price)) * (1 - FEE_RATE) / p["unit"] - 1) * 100
            marks[r["id"]] = {"at": int(time.time() * 1000), "pct": round(early, 3), "why": why}
            st = self.exit_stat(r["id"])
            if st["mode"] == "active":
                ghosts = self.state.setdefault("holdGhosts", {})
                ghosts[mk] = {"rule": r["id"], "early": early, "unit": p["unit"], "tp": p["tp"], "sl": p["sl"],
                              "deadline": p["deadline"], "at": marks[r["id"]]["at"]}
                log(f"[청산 판단] {mk.split('-')[1]} {r['name']} 적용 → 지금 정리 ({why}) · 계속 들고 있었다면 어땠을지 가상 추적")
                self.exit(mk, price, r["name"])
                return True
            log(f"[청산 판단] {mk.split('-')[1]} {r['name']} 조건 걸림({why}) · 관찰 중 → 정리했다면 {early:+.2f}%, 실제 결과와 나중에 비교")
        return False

    def exit_record(self, rid, early, actual, at_ms):
        r = next((x for x in EXIT_RULES if x["id"] == rid), None)
        if not r:
            return
        st = self.exit_stat(rid)
        diff = early - actual
        st["n"] += 1
        st["sumDiff"] += diff
        st["better"] += diff > 0
        st["recent"] = (st.get("recent", []) + [round(diff, 3)])[-EXIT_RECENT_N:]   # 분석 전용: 최근 흐름(판정에는 안 씀)
        hk = datetime.fromtimestamp(at_ms / 1000, KST).strftime("%m%d%H")
        if hk not in st["hours"]:
            st["hours"] = (st["hours"] + [hk])[-50:]
        if st["n"] >= st["nextEval"] and len(st["hours"]) >= GATE_EVAL_EPISODES:
            st["nextEval"] = st["n"] + GATE_RECHECK
            avg = st["sumDiff"] / st["n"]
            if st["mode"] == "active":
                want = "active" if avg > 0 else "shadow"      # 적용 중이면 조기 정리가 손해일 때만 되돌림(오락가락 방지)
            else:
                want = "active" if avg >= EXIT_MARGIN else "shadow"
            why = f"조기 정리했다면 {st['n']}건 평균 {avg:+.2f}%p ({st['better']}건 더 나음, {len(st['hours'])}개 시간대)"
            log(f"[청산 판정] {r['name']}: {why} → " + ("적용이 낫다고 봄" if want == "active" else "관찰 유지가 낫다고 봄"))
            if want != st["mode"]:
                self.propose("exit", rid, r["name"], st["mode"], want, why)
            else:
                self.withdraw("exit", rid, why)

    def manage_hold_ghosts(self):
        gs = self.state.get("holdGhosts") or {}
        if not gs:
            return
        prices = self.upbit.prices(list(gs.keys()))
        for mk, g in list(gs.items()):
            price = prices.get(mk)
            if not price:
                continue
            if price >= g["tp"] or price <= g["sl"] or time.time() >= g["deadline"]:
                held = (price * (1 - slip_for(price)) * (1 - FEE_RATE) / g["unit"] - 1) * 100
                del gs[mk]
                self.exit_record(g["rule"], g["early"], held, g["at"])
                log(f"[가상 거래] {mk.split('-')[1]} 계속 들고 있었다면 {held:+.2f}% (실제 조기 정리 {g['early']:+.2f}%) → "
                    + ("조기 정리가 나았음" if g["early"] > held else "들고 있는 게 나았음"))
        save_state(self.state)

    def gate_entry(self, mk, spec, price, ctx):
        """매수 직전 관문 확인. 막으면 False(가상 거래 등록), 통과면 True (관찰 관문 기록은 ctx 에 남김)"""
        now = dict(self.gate_check())
        last = self.__dict__.setdefault("_entry_gate_last", {})
        for g in MARKET_GATES:
            if not g.get("entry"):
                continue
            try:
                now[g["id"]] = g["fn"](self, mk, price, ctx)
            except Exception as e:
                now[g["id"]] = (False, f"확인 실패: {e}")
            last[g["id"]] = now[g["id"]]
            if now[g["id"]][0] and self.gate_stat(g["id"])["mode"] != "active":
                log(f"[시장 판단] {g['name']} 감지: {now[g['id']][1]} · 관찰 중 → 매수는 하되 '막았을 거래'로 표시")
        hits = [gid for gid, (blk, _) in now.items() if blk]
        active = [gid for gid in hits if self.gate_stat(gid)["mode"] == "active"]
        if active:
            ghosts = self.state.setdefault("ghosts", {})
            if mk not in ghosts:
                unit = price * (1 + slip_for(price)) * (1 + FEE_RATE)
                ghosts[mk] = {"gates": hits, "unit": unit, "tp": target_price(unit, spec.get("tp", TARGET_NET_PCT)),
                              "sl": unit * (1 + spec["sl"] / 100), "deadline": int(time.time()) + spec["hold"] * 60,
                              "at": int(time.time() * 1000), "strategy": describe(spec)}
                names = ", ".join(g["name"] for g in MARKET_GATES if g["id"] in active)
                log(f"[시장 판단] {mk.split('-')[1]} 매수 신호 → {names} 관문 적용 중이라 보류 (가상 거래로 결과 추적)")
            return False
        ctx["gateHits"] = hits                             # 관찰 중 관문이 막았을 거래인지 표시
        return True

    def manage_ghosts(self):
        ghosts = self.state.get("ghosts") or {}
        if not ghosts:
            return
        prices = self.upbit.prices(list(ghosts.keys()))
        for mk, g in list(ghosts.items()):
            price = prices.get(mk)
            if not price:
                continue
            reason = "익절" if price >= g["tp"] else "손절" if price <= g["sl"] else "보유시간 초과" if time.time() >= g["deadline"] else None
            if not reason:
                continue
            pct = (price * (1 - slip_for(price)) * (1 - FEE_RATE) / g["unit"] - 1) * 100
            del ghosts[mk]
            self.gate_record(g["gates"], pct, g["at"])
            log(f"[가상 거래] {mk.split('-')[1]} {pct:+.2f}% ({reason}) · 관문이 막은 매수의 결과 → "
                + ("막아서 손실을 피함" if pct <= 0 else "막지 않았으면 이익이었음"))
        save_state(self.state)

    # ---------- 원숭이 기준선 (분석 전용, 매매 영향 없음) ----------
    def monkey_spawn(self, market, spec):
        """우리가 살 때마다, 같은 순간 대상 종목 중 무작위 1개를 같은 익절·손절·보유시간으로 '샀다고 치고' 추적.
        우리 거래당 성적이 이 무작위 진입보다 나은지로 '실력'을 잼 (수수료·미끄러짐 동일 반영)"""
        monkeys = self.state.setdefault("monkeys", {})
        if len(monkeys) >= MONKEY_MAX_OPEN:
            return
        pool = [m for m in self.state["universe"]["markets"] if m != market and m not in monkeys]
        if not pool:
            return
        mk = random.choice(pool)
        price = self.upbit.prices([mk]).get(mk)
        if not price:
            return
        unit = price * (1 + SHADOW_SLIPPAGE) / (1 - FEE_RATE)      # 검증 모의매매와 같은 비용 가정
        pid = uuid.uuid4().hex[:8]
        monkeys[mk] = {"unit": unit, "tp": target_price(unit, spec.get("tp", TARGET_NET_PCT)),
                       "sl": unit * (1 + spec["sl"] / 100), "deadline": int(time.time()) + spec["hold"] * 60,
                       "at": int(time.time() * 1000), "pair": pid}
        self.state["positions"][market]["monkey"] = pid

    def monkey_pair(self, pid, side, pct, conds=None):
        """짝 비교(분석 전용): 우리 거래와 같은 순간 산 원숭이 1건을 묶어 '우리 − 원숭이' 차이를 쌓음.
        같은 시각이라 시장 전체 오르내림이 서로 지워져, 따로 평균 낸 비교보다 적은 표본으로도 실력 차이가 드러남"""
        try:
            pairs = self.state.setdefault("monkeyPairs", {})
            cut = time.time() * 1000 - 2 * 86400 * 1000             # 짝이 끝내 안 맞으면(재시작·상장폐지) 이틀 뒤 버림
            for k in [k for k, v in pairs.items() if v.get("at", 0) < cut]:
                del pairs[k]
            p = pairs.setdefault(pid, {"at": int(time.time() * 1000)})
            p[side] = round(pct, 3)
            if conds:
                p["conds"] = list(conds)
            if "ours" in p and "monkey" in p:
                d = p["ours"] - p["monkey"]
                del pairs[pid]
                # 조건별 짝 비교(분석 전용): 어떤 진입 조건이 들어간 거래가 원숭이보다 못한지/나은지
                for c in p.get("conds") or []:
                    cst = self.state.setdefault("monkeyCondStats", {}).setdefault(c, {"n": 0, "sumD": 0.0, "sumD2": 0.0})
                    cst["n"] += 1
                    cst["sumD"] = round(cst["sumD"] + d, 4)
                    cst["sumD2"] = round(cst["sumD2"] + d * d, 4)
                st = self.state.setdefault("monkeyPairStats", {"n": 0, "sumD": 0.0, "sumD2": 0.0, "recent": []})
                st["n"] += 1
                st["sumD"] += d
                st["sumD2"] += d * d
                st["recent"] = (st["recent"] + [round(d, 3)])[-MONKEY_RECENT:]
        except Exception:
            pass

    def monkey_record(self, side, pct):
        st = self.state.setdefault("monkeyStats", {})
        b = st.setdefault(side, {"n": 0, "wins": 0, "sumPct": 0.0, "recent": []})
        b["n"] += 1
        b["wins"] += 1 if pct > 0 else 0
        b["sumPct"] += pct
        b["recent"] = (b["recent"] + [round(pct, 3)])[-MONKEY_RECENT:]

    def manage_monkeys(self):
        monkeys = self.state.get("monkeys") or {}
        if not monkeys:
            return
        prices = self.upbit.prices(list(monkeys.keys()))
        done = False
        for mk, g in list(monkeys.items()):
            price = prices.get(mk)
            if not price:
                if time.time() >= g["deadline"] + 3600:   # 상장폐지 등으로 값이 끊기면 버림
                    del monkeys[mk]
                continue
            if not (price >= g["tp"] or price <= g["sl"] or time.time() >= g["deadline"]):
                continue
            pct = (price * (1 - SHADOW_SLIPPAGE) * (1 - FEE_RATE) / g["unit"] - 1) * 100
            del monkeys[mk]
            self.monkey_record("monkey", pct)
            if g.get("pair"):
                self.monkey_pair(g["pair"], "monkey", pct)
            done = True
        if done:
            save_state(self.state)

    # ---------- 놓친 큰 움직임 (분석 전용, 매매 영향 없음) ----------
    def move_watch(self):
        """대상 종목 가격을 1분마다 보고, 최근 60분 저점 대비 +2% 이상 오른 움직임을 기록함.
        그때 우리가 실주문·모의로 들고 있었는지, 규칙은 있었지만 신호가 없었는지, 규칙 자체가 없었는지 나눠 셈.
        또 '감지한 순간 따라 샀다면' 60분 뒤 비용 반영 손익을 재서, 눈으로 본 상승을 쫓는 게 이득인지 확인함."""
        now = time.time()
        if now - getattr(self, "_move_at", 0) < 60:
            return
        self._move_at = now
        mks = [m for m in self.state["universe"]["markets"] if m.split("-")[1] not in PROTECTED_COINS]
        if not mks:
            return
        prices = self.upbit.prices(mks)
        hist = self.__dict__.setdefault("_move_px", {})
        mw = self.state.setdefault("moveWatch", {"open": [], "done": []})
        changed = False
        for o in list(mw["open"]):                        # 60분 뒤 따라샀다면 성적 확정
            p = prices.get(o["market"])
            if p and now - o["at"] / 1000 < MOVE_FOLLOW_MIN * 60:
                try:                                      # 가는 길 기록: 15·30분 시점 손익, 60분 안 최고·최저 (비용 반영)
                    buy = o["price"] * (1 + SHADOW_SLIPPAGE) * (1 + FEE_RATE)
                    cur = round((p * (1 - SHADOW_SLIPPAGE) * (1 - FEE_RATE) / buy - 1) * 100, 3)
                    o["hi"] = max(o.get("hi", cur), cur)
                    o["lo"] = min(o.get("lo", cur), cur)
                    el = (now - o["at"] / 1000) / 60
                    for m in MOVE_CHECK_MIN:
                        if el >= m and f"f{m}" not in o:
                            o[f"f{m}"] = cur
                    changed = True
                except Exception:
                    pass
                continue
            if now - o["at"] / 1000 < MOVE_FOLLOW_MIN * 60:
                continue
            mw["open"].remove(o)
            if p:
                buy = o["price"] * (1 + SHADOW_SLIPPAGE) * (1 + FEE_RATE)
                o["followPct"] = round((p * (1 - SHADOW_SLIPPAGE) * (1 - FEE_RATE) / buy - 1) * 100, 3)
                mw["done"] = (mw["done"] + [o])[-300:]
            changed = True
        last = self.state.setdefault("moveLast", {})
        for mk, p in prices.items():
            h = [x for x in hist.get(mk, []) if now - x[0] <= MOVE_WINDOW_MIN * 60] + [(now, p)]
            hist[mk] = h
            if now - h[0][0] < 30 * 60 or now - last.get(mk, 0) < MOVE_WINDOW_MIN * 60:
                continue                                  # 30분 이상 지켜본 뒤부터, 같은 종목은 60분에 한 번만
            lo = min(x[1] for x in h)
            rise = (p / lo - 1) * 100 if lo else 0
            if rise < MOVE_RISE_PCT:
                continue
            pos = self.state["positions"].get(mk)
            if pos:
                kind = "paper" if pos.get("paper") else "real"
            elif mk in self.state["strategies"]:
                rb = self.state["ruleBook"].get(self.state["strategies"][mk].get("rid") or rule_id(mk, self.state["strategies"][mk]))
                kind = "wait_proven" if rb and rb.get("status") == "proven" else "wait"
            else:
                kind = "no_rule"
            last[mk] = now
            mw["open"].append({"at": int(now * 1000), "market": mk, "price": p, "rise": round(rise, 2), "kind": kind})
            log(f"[큰 움직임] {mk.split('-')[1]} 60분 저점 대비 +{rise:.1f}% · "
                + {"real": "실주문 보유 중", "paper": "모의로 보유 중", "wait_proven": "통과 규칙 있으나 신호 없음",
                   "wait": "검증 중 규칙 있으나 신호 없음", "no_rule": "채택된 규칙 없음"}[kind])
            changed = True
        if changed:
            save_state(self.state)

    @staticmethod
    def move_episodes(done):
        """'보고 따라샀다면' 성적을 시간 묶음별로 나눠 봄 (분석 전용).
        시장 전체가 한꺼번에 뛰면 한 번의 사건이 수십 건으로 셈해져 표본이 부풀려지므로,
        30분 넘게 비지 않고 이어진 감지들을 한 묶음(사건)으로 보고 묶음마다 평균을 낸 뒤 다시 평균함.
        또 앞뒤 10분 안에 다른 종목 감지가 3건 이상이면 '동반 상승', 아니면 '혼자 상승'으로 나눔."""
        try:
            d = sorted(done, key=lambda o: o["at"])
            if not d:
                return {}
            eps, cur = [], [d[0]]
            for o in d[1:]:
                if o["at"] - cur[-1]["at"] > 30 * 60 * 1000:
                    eps.append(cur); cur = []
                cur.append(o)
            eps.append(cur)
            means = [sum(o["followPct"] for o in e) / len(e) for e in eps]
            solo, group = [], []
            for o in d:
                near = sum(1 for x in d if x is not o and abs(x["at"] - o["at"]) <= 10 * 60 * 1000)
                (group if near >= 3 else solo).append(o["followPct"])
            avg = lambda xs: round(sum(xs) / len(xs), 3) if xs else None
            return {"episodes": len(eps), "epAvg": avg(means), "epWins": sum(1 for m in means if m > 0),
                    "soloN": len(solo), "soloAvg": avg(solo), "groupN": len(group), "groupAvg": avg(group)}
        except Exception as e:
            log(f"[경고] 큰 움직임 묶음 계산 실패: {e}")
            return {}

    def moves_view(self):
        mw = self.state.get("moveWatch") or {}
        cut = (time.time() - 86400) * 1000
        rows = [o for o in (mw.get("done") or []) + (mw.get("open") or []) if o["at"] >= cut]
        if not rows and not mw.get("done"):
            return None
        kinds = {}
        for o in rows:
            kinds[o["kind"]] = kinds.get(o["kind"], 0) + 1
        fol = [o["followPct"] for o in (mw.get("done") or []) if o.get("followPct") is not None]
        top = sorted(rows, key=lambda o: -o["rise"])[:5]
        ep = self.move_episodes([o for o in (mw.get("done") or []) if o.get("followPct") is not None])
        hold = {}
        try:                                              # 보유시간별·손절 가정 성적 (가는 길이 기록된 건만)
            path = [o for o in (mw.get("done") or []) if o.get("followPct") is not None and "lo" in o
                    and all(f"f{m}" in o for m in MOVE_CHECK_MIN)]
            if path:
                avg = lambda xs: round(sum(xs) / len(xs), 3)
                hold = {"pathN": len(path), "hiAvg": avg([o["hi"] for o in path]), "loAvg": avg([o["lo"] for o in path]),
                        "stopAvg": avg([MOVE_STOP_PCT if o["lo"] <= MOVE_STOP_PCT else o["followPct"] for o in path]),
                        "stopHit": sum(1 for o in path if o["lo"] <= MOVE_STOP_PCT), "stopPct": MOVE_STOP_PCT,
                        "byMin": [{"min": m, "avg": avg([o[f"f{m}"] for o in path]),
                                   "wins": sum(1 for o in path if o[f"f{m}"] > 0)} for m in MOVE_CHECK_MIN]
                                 + [{"min": MOVE_FOLLOW_MIN, "avg": avg([o["followPct"] for o in path]),
                                     "wins": sum(1 for o in path if o["followPct"] > 0)}]}
        except Exception as e:
            log(f"[경고] 큰 움직임 보유시간별 계산 실패: {e}")
        return {"n24": len(rows), "kinds": kinds, **ep, **hold,
                "followN": len(fol), "followAvg": round(sum(fol) / len(fol), 3) if fol else None,
                "followWins": sum(1 for f in fol if f > 0),
                "top": [{"coin": o["market"].split("-")[1], "rise": o["rise"], "kind": o["kind"],
                         "followPct": o.get("followPct")} for o in top]}

    def monkey_view(self):
        st = self.state.get("monkeyStats") or {}
        def side(k):
            b = st.get(k)
            if not b or not b["n"]:
                return None
            r = b.get("recent") or []
            return {"n": b["n"], "wins": b["wins"], "avgPct": round(b["sumPct"] / b["n"], 3),
                    "recentN": len(r), "recentAvg": round(sum(r) / len(r), 3) if r else None}
        o, m = side("ours"), side("monkey")
        if not o and not m:
            return None
        pair = None
        ps = self.state.get("monkeyPairStats")
        if ps and ps.get("n"):
            n = ps["n"]
            avg = ps["sumD"] / n
            sd = math.sqrt(max(ps["sumD2"] / n - avg * avg, 0.0) * n / (n - 1)) if n > 1 else None
            se = sd / math.sqrt(n) if sd else None
            r = ps.get("recent") or []
            pair = {"n": n, "avgDiff": round(avg, 3), "se": round(se, 3) if se else None,
                    "z": round(avg / se, 2) if se else None,
                    "recentN": len(r), "recentAvg": round(sum(r) / len(r), 3) if r else None}
        conds = []
        for c, v in (self.state.get("monkeyCondStats") or {}).items():
            n = v.get("n", 0)
            if not n:
                continue
            avg = v["sumD"] / n
            sd = math.sqrt(max(v["sumD2"] / n - avg * avg, 0.0) * n / (n - 1)) if n > 1 else None
            se = sd / math.sqrt(n) if sd else None
            conds.append({"cond": c, "n": n, "avgDiff": round(avg, 3), "z": round(avg / se, 2) if se else None})
        conds.sort(key=lambda x: x["avgDiff"])
        return {"ours": o, "monkey": m, "open": len(self.state.get("monkeys") or {}), "pair": pair, "conds": conds}

    def exits_view(self):
        out = []
        for r in EXIT_RULES:
            st = self.exit_stat(r["id"])
            watching = sum(1 for p in self.state["positions"].values() if r["id"] in (p.get("exitMarks") or {}))
            out.append({"id": r["id"], "name": r["name"], "desc": r["desc"], "mode": st["mode"], "n": st["n"],
                        "avgDiff": round(st["sumDiff"] / st["n"], 3) if st["n"] else None, "better": st["better"],
                        "episodes": len(st["hours"]), "nextEval": st["nextEval"], "marked": watching,
                        "recentN": len(st.get("recent", [])),
                        "recentAvg": round(sum(st["recent"]) / len(st["recent"]), 3) if st.get("recent") else None,
                        "recentBetter": sum(1 for d in st.get("recent", []) if d > 0),
                        "ghosts": sum(1 for g in (self.state.get("holdGhosts") or {}).values() if g["rule"] == r["id"]),
                        "pending": any(p["status"] == "pending" and p["target"] == r["id"] for p in self.state.get("proposals", []))})
        return out

    def gates_view(self):
        now = getattr(self, "_gate_now", {})
        out = []
        for g in MARKET_GATES:
            st = self.gate_stat(g["id"])
            b, p = st["blocked"], st["passed"]
            if g.get("entry"):
                lb, lr = getattr(self, "_entry_gate_last", {}).get(g["id"], (False, "매수 신호 때마다 확인"))
                blk, reading = False, ("최근 매수 신호: " + lr) if lr != "매수 신호 때마다 확인" else lr
            else:
                blk, reading = now.get(g["id"], (False, "확인 전"))
            out.append({"id": g["id"], "name": g["name"], "desc": g["desc"], "mode": st["mode"], "now": blk, "reading": reading,
                        "blockedN": b["n"], "blockedAvg": round(b["sumPct"] / b["n"], 3) if b["n"] else None,
                        "blockedWins": b["wins"], "passedN": p["n"],
                        "passedAvg": round(p["sumPct"] / p["n"], 3) if p["n"] else None,
                        "episodes": len(st["hours"]), "nextEval": st["nextEval"],
                        "ghosts": sum(1 for x in (self.state.get("ghosts") or {}).values() if g["id"] in x["gates"]),
                        "pending": any(p["status"] == "pending" and p["target"] == g["id"] for p in self.state.get("proposals", []))})
        return out

    # ---------- 화면용 상태 ----------
    def snapshot(self):
        accounts = self.upbit.accounts()
        krw, held = 0.0, []
        for a in accounts:
            cur = a["currency"]
            if cur in PROTECTED_COINS:            # 보호 코인은 화면에서 숨김
                continue
            amount = float(a["balance"]) + float(a["locked"])
            if cur == "KRW":
                krw = amount
            elif amount > 0:
                held.append((cur, amount, float(a["avg_buy_price"])))
        markets = [f"KRW-{c}" for c, _, _ in held]
        prices = {}
        try:
            prices = self.upbit.prices(markets)
        except UpbitError:
            for m in markets:                     # 상장폐지 종목 등이 섞이면 하나씩 조회
                try:
                    prices.update(self.upbit.prices([m]))
                except UpbitError:
                    pass
        holdings, total = [], krw
        for coin, amount, avg in held:
            price = prices.get(f"KRW-{coin}")
            value = amount * price if price else 0.0
            if value < 1:
                continue
            total += value
            holdings.append({"coin": coin, "balance": amount, "avgPrice": avg, "price": price,
                             "value": round(value), "protected": False,
                             "pnlPct": round((price - avg) / avg * 100, 2) if avg > 0 else None,
                             "botOwned": f"KRW-{coin}" in self.state["positions"]})
        holdings.sort(key=lambda h: -h["value"])
        return {"krw": round(krw), "totalValue": round(total), "holdings": holdings}

    def push_progress(self, phase):
        """오래 걸리는 작업 중에도 웹페이지가 '응답 없음'으로 보이지 않게 진행 상황만 올림"""
        try:
            self.cloud.put_status({"phase": phase, "activity": list(ACTIVITY)[-40:],
                                   "updatedAt": int(time.time() * 1000)}, partial=True)
        except Exception:
            pass

    def rule_brief(self, rid):
        rb = self.state["ruleBook"].get(rid) if rid else None
        if not rb:
            return None
        return {"status": rb["status"], "trades": rb["trades"], "wins": rb["wins"],
                "avgPct": round(rb["sumPct"] / rb["trades"], 3) if rb["trades"] else None}

    def positions_detail(self):
        pos = self.state["positions"]
        if not pos:
            return []
        try:
            prices = self.upbit.prices(list(pos.keys()))
        except Exception:
            prices = {}
        out = []
        for mk, p in pos.items():
            price = prices.get(mk)
            out.append({"market": mk, "coin": mk.split("-")[1], "cost": round(p["cost"]), "unit": p["unit"],
                        "tp": p["tp"], "sl": p["sl"], "price": price, "at": p["at"],
                        "deadline": p["deadline"] * 1000, "strategy": p.get("strategy", ""),
                        "signalPrice": p.get("signalPrice"), "btWinrate": p.get("btWinrate"), "btEdge": p.get("btEdge"),
                        "rid": p.get("rid"), "rule": self.rule_brief(p.get("rid")), "entryInfo": p.get("entryInfo"),
                        "paper": bool(p.get("paper")),
                        "pnlPct": round((price * (1 - FEE_RATE) / p["unit"] - 1) * 100, 2) if price else None})
        return out

    # ---------- AI 분석가 ----------
    @staticmethod
    def ai_slot(now=None):
        """가장 최근에 지난 정기 분석 시각 (예: '2026-10-27 15시')"""
        now = now or datetime.now(KST)
        past = [h for h in AI_SCHEDULE_HOURS if h <= now.hour]
        if past:
            return f"{now.strftime('%Y-%m-%d')} {max(past)}시"
        prev = now - timedelta(days=1)
        return f"{prev.strftime('%Y-%m-%d')} {max(AI_SCHEDULE_HOURS)}시"

    def ai_should_run(self):
        if not self.cfg["ANTHROPIC_API_KEY"] or getattr(self, "_ai_busy", False):
            return False
        slot = self.ai_slot()
        if self.state.get("aiSlotDone") != slot:          # 정기 분석 (하루 4번)
            if not self.state.get("tradeLog"):
                self.state["aiSlotDone"] = slot
                log(f"[AI] {slot} 정기 분석: 아직 거래 기록이 없어 건너뜀")
                return False
            self.state["aiSlotDone"] = slot
            return True
        if len(self.state.get("tradeLog", [])) < 3:
            return False
        if time.time() - self.state.get("aiLastAt", 0) < AI_MIN_INTERVAL_MIN * 60:
            return False
        return self.state.get("aiLossesSince", 0) >= AI_EVERY_LOSSES

    def ai_start(self):
        self._ai_busy = True
        self.state["aiDue"] = False
        self.state["aiLossesSince"] = 0
        self.state["aiLastAt"] = int(time.time())
        payload = self.ai_payload()
        threading.Thread(target=self._ai_worker, args=(payload,), daemon=True).start()
        log("[AI] 거래 기록 분석 요청 보냄 (30초~1분)")

    def ai_payload(self):
        lg = self.ledger
        return {
            "규칙": {"기본주문": self.cfg["ORDER_KRW"], "마틴게일최대단계": self.cfg["MARTINGALE_MAX_LEVEL"],
                   "손실후대기분": LOSS_COOLDOWN_MIN, "하루손실한도": self.cfg["DAILY_LOSS_LIMIT"],
                   "수익잠금비율": PROFIT_LOCK_RATIO, "동시보유최대": MAX_OPEN_POSITIONS,
                   "백테스트": {"분봉수": BT_CANDLES, "시험조합": BT_TRIALS, "최소거래": BT_MIN_TRADES,
                             "검증구간비율": BT_HOLDOUT, "거래당최소순익%": BT_MIN_EDGE_PCT,
                             "최소손익비": BT_MIN_PROFIT_FACTOR, "미끄러짐": BT_SLIPPAGE,
                             "익절후보": TP_CHOICES, "손절후보": SL_CHOICES, "보유후보분": HOLD_CHOICES},
                   "실전부진기준": {"종목최소거래": LIVE_MIN_TRADES, "종목최소승률": LIVE_MIN_WINRATE,
                               "종목연속손실": LIVE_MAX_LOSS_STREAK, "하루최소거래": LIVE_DAY_MIN_TRADES,
                               "하루최소승률": LIVE_DAY_MIN_WINRATE},
                   "종목선정": {"상위개수": TOP_N, "거래대금기간일": UNIVERSE_DAYS, "최소상장일": UNIVERSE_MIN_LISTED_DAYS}},
            "장부": {"시드": round(lg["seed"]), "잠긴수익": round(lg["locked"]), "누적실현": round(lg["realizedTotal"]),
                   "누적거래": lg["tradesTotal"], "누적익절": lg["winsTotal"], "오늘손익": round(lg["dayPnl"]),
                   "오늘거래": lg["dayTrades"], "오늘익절": lg["dayWins"]},
            "전날결산": self.state.get("lastReport"),
            "현재전략": [{"종목": m, "설명": describe(sp), "학습승률": sp.get("winrate"), "거래당%": sp.get("edgePct"),
                       "검증승률": sp.get("holdoutWinrate"), "검증거래": sp.get("holdoutTrades")}
                      for m, sp in self.state["strategies"].items()],
            "실패원인누적": self.state.get("failStats", {}),
            "체결비용": self.state.get("execStats", {}),
            "최근거래": self.state.get("tradeLog", []),
        }

    def _ai_worker(self, payload):
        prompt = (
            "너는 업비트 원화마켓 소액 자동매매 봇의 성과 분석가다. 아래 JSON은 봇의 규칙, 장부, 현재 전략, "
            "손해 거래마다 봇이 자동 진단한 실패 원인, 최근 거래 기록이다.\n"
            "할 일: 1) 무엇이 잘되고 무엇이 안되는지 숫자로 짚고 2) 손해의 주된 원인을 찾고 "
            "3) 봇 규칙을 어떻게 바꾸면 좋을지 구체적인 수정안을 낸다. 수정안은 위 규칙 항목 이름과 바꿀 값을 적는다. "
            "표본이 적으면 그렇다고 말하고 과한 결론을 피한다. 수익을 장담하지 말 것. 마틴게일이 손실을 키우는지도 평가한다.\n"
            "반드시 아래 형식의 JSON 하나만 출력한다(설명 문장, 코드블록 금지):\n"
            '{"요약": "2~3문장", "잘된점": ["..."], "문제점": ["..."], '
            '"수정안": [{"제목": "...", "이유": "...", "항목": "규칙 항목 이름", "현재": "...", "제안": "...", "우선순위": "높음|중간|낮음"}], '
            '"주의": "표본 크기나 한계"}\n\n'
            + json.dumps(payload, ensure_ascii=False))
        try:
            res = requests.post("https://api.anthropic.com/v1/messages", timeout=120, headers={
                "x-api-key": self.cfg["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                "content-type": "application/json"},
                json={"model": self.cfg["AI_MODEL"], "max_tokens": 2000,
                      "messages": [{"role": "user", "content": prompt}]})
            if res.status_code != 200:
                raise RuntimeError(f"HTTP {res.status_code}: {res.text[:200]}")
            text = "".join(b.get("text", "") for b in res.json().get("content", []) if b.get("type") == "text")
            start, end = text.find("{"), text.rfind("}")
            report = json.loads(text[start:end + 1])
            report["at"] = int(time.time() * 1000)
            report["trades"] = len(payload["최근거래"])
            self.state["aiReport"] = report
            log(f"[AI] 분석 완료: {report.get('요약', '')[:120]}")
            for sug in report.get("수정안", [])[:5]:
                log(f"[AI 제안·{sug.get('우선순위', '')}] {sug.get('제목', '')}: {sug.get('항목', '')} {sug.get('현재', '')} → {sug.get('제안', '')}")
        except Exception as e:
            self.state["aiReport"] = {"error": f"AI 분석 실패: {str(e)[:200]}", "at": int(time.time() * 1000)}
            log(f"[AI] 분석 실패: {e}")
        finally:
            self._ai_busy = False
            self._ai_dirty = True

    def summary(self):
        lg = self.ledger
        mode = ("실매매" if self.live else "모의매매(실제 주문 없음)") + (" · 탐험 모드" if EXPLORE_MODE else "")
        wait = ""
        if time.time() < lg["cooldownUntil"]:
            wait = f" · 손실 후 대기 {int((lg['cooldownUntil'] - time.time()) / 60) + 1}분"
        ok, why = self.can_enter()
        reason = "" if ok or why in ("손실 후 대기", "일시정지") else f" · 신규 진입 중지: {why}"
        if self.live and PROVEN_ONLY_LIVE:
            mode += f" · 실전 통과 규칙만 실주문(검증 모의 오늘 {lg.get('dayShadowTrades', 0)}회 {lg.get('dayShadowPnl', 0):+,.0f}원)"
        return (f"{mode} · 오늘 {lg['dayTrades']}회(익절 {lg['dayWins']}) {lg['dayPnl']:+,.0f}원 · "
                f"시드 {lg['seed']:,.0f}원 · 잠긴 수익 {lg['locked']:,.0f}원 · "
                f"대상 {len(self.state['strategies'])}종목 · 다음 주문 {self.order_size():,.0f}원{wait}{reason}")

    def upload_status(self):
        data = {"budget": round(self.ledger["seed"]), "device": self.cfg["DEVICE_NAME"],
                "budgetUsed": round(self.in_use()), "settings": settings_view(self.cfg)}
        try:
            data.update(self.snapshot())
            self.error = ""
        except UpbitError as e:
            self.error = "업비트 연결 오류: " + str(e)
            if e.name == "no_authorization_ip":
                self.error = "이 PC의 IP가 업비트 API 키에 등록되지 않았습니다. 업비트에서 허용 IP를 확인하세요."
        except requests.RequestException as e:
            self.error = "업비트 통신 오류: " + str(e)[:120]
        if self.error:
            state, msg = "error", self.error
        elif self.state["paused"]:
            state, msg = "paused", "일시정지됨. " + self.summary()
        else:
            state, msg = "running", self.summary()
        lg = self.ledger
        cw = self.cond_weights()
        data.update({"phase": "", "activity": list(ACTIVITY)[-40:], "positions": self.positions_detail(),
                     "cooldownUntil": lg["cooldownUntil"] * 1000, "nextOrder": round(self.order_size()),
                     "canEnter": self.can_enter()[1] or "가능", "lastOptimize": self.state["lastOptimize"] * 1000})
        data.update({"botState": state, "message": msg, "updatedAt": int(time.time() * 1000),
                     "ledger": {"seed": round(lg["seed"]), "locked": round(lg["locked"]),
                                "realizedTotal": round(lg["realizedTotal"]), "dayPnl": round(lg["dayPnl"]),
                                "dayTrades": lg["dayTrades"], "dayWins": lg["dayWins"], "martLevel": lg["martLevel"],
                                "tradesTotal": lg["tradesTotal"], "winsTotal": lg["winsTotal"],
                                "mode": "live" if self.live else "paper",
                                "provenOnly": bool(self.live and PROVEN_ONLY_LIVE),
                                "shadowTrades": lg.get("shadowTrades", 0), "shadowWins": lg.get("shadowWins", 0),
                                "shadowPnl": round(lg.get("shadowPnl", 0)), "dayShadowTrades": lg.get("dayShadowTrades", 0),
                                "dayShadowPnl": round(lg.get("dayShadowPnl", 0))},
                     "strategies": [{"market": m, "desc": describe(s), "winrate": s.get("winrate"),
                                     "trades": s.get("trades"), "totalPct": s.get("totalPct"),
                                     "holdoutPct": s.get("holdoutPct")} for m, s in self.state["strategies"].items()],
                     "universe": self.state["universe"]["markets"],
                     "blocked": self.state["blocked"], "marketStats": self.state["marketStats"],
                     "ruleBook": self.rulebook_view(),
                     "marketGates": self.gates_view(),
                     "exitRules": self.exits_view(),
                     "proposals": self.state.get("proposals", []),
                     "aiDecisions": self.state.get("aiDecisions", {}),
                     "userRequests": self.state.get("userRequests", [])[-40:],
                     "explore": EXPLORE_MODE,
                     "role": self.cfg.get("ROLE", "owner"), "updateChannel": self.cfg.get("UPDATE_CHANNEL", "main"),
                     "condStats": [{"name": k, "n": v["n"], "wins": v["wins"], "avgPct": round(v["sumPct"] / v["n"], 3),
                                    "weight": round(cw[k], 2) if k in cw else None}
                                   for k, v in self.state.get("condStats", {}).items() if v["n"]],
                     "shapeWeights": {k: {str(v): w for v, w in m.items()} for k, m in self.shape_weights().items()},
                     "cooling": sorted(m.split("-")[1] for m, t in self.state.get("coolMarkets", {}).items() if t > time.time()),
                     "report": self.state["lastReport"],
                     "ai": self.state.get("aiReport"),
                     "aiEnabled": bool(self.cfg["ANTHROPIC_API_KEY"]),
                     "hourStats": self.hours_view(),
                     "btcFlowStats": self.btcflow_view(),
                     "monkeyStats": self.monkey_view(),
                     "moveStats": self.moves_view(),
                     "overlapStats": self.overlap_view(),
                     "specStats": self.spec_view(),
                     "execStats": [{"kind": k, "n": v["n"], "avgSlip": round(v["sumSlip"] / v["n"], 3), "worse": v["worse"],
                                    "avgTpGap": round(v["sumTpGap"] / v["n"], 3) if k == "sell:익절" else None}
                                   for k, v in self.state.get("execStats", {}).items() if v.get("n")],
                     "tpLimit": (lambda t: {"n": t["n"], "touched": t["touched"], "missed": t["missed"], "better": t["better"],
                                            "worse": t["worse"], "avgDiff": round(t["sumDiff"] / t["n"], 3)}
                                 if t and t.get("n") else None)(self.state.get("tpLimit")),
                     "martinSim": (lambda t: {"n": t["n"], "wins": t["wins"], "pnl": round(t["pnl"]), "actualPnl": round(t["actualPnl"]),
                                             "maxInvest": t["maxInvest"], "legs": t.get("legs"), "worst": round(t["worst"])}
                                  if t and t.get("n") else None)(self.state.get("martinSim")),
                     "passStats": (lambda t: {"n": t["n"], "wins": t["wins"], "avgPct": round(t["sumPct"] / t["n"], 3),
                                              "pnl": t["pnl"]} if t and t.get("n") else None)(self.state.get("passStats")),
                     "passPredict": [{"key": k, "n": v["n"], "wins": v["wins"], "avgPct": round(v["sumPct"] / v["n"], 3)}
                                     for k, v in sorted((self.state.get("passPredict") or {}).items()) if v.get("n")],
                     "failStats": [{"code": k, "label": FAIL_LABELS.get(k, k), "count": v}
                                   for k, v in sorted(self.state.get("failStats", {}).items(), key=lambda x: -x[1])]})
        self.cloud.put_status(data)
        self.last_status = time.time()
        self.write_report(data)

    def rulebook_view(self, limit=40):
        """화면용 규칙 장부: 실전 기록 있는 것 먼저, 최근 순"""
        rows = []
        for r in self.state["ruleBook"].values():
            n = r["trades"]
            rows.append({"id": r["id"], "coin": r["market"].split("-")[1], "desc": r["desc"], "status": r["status"],
                         "trades": n, "wins": r["wins"], "winrate": round(r["wins"] / n, 3) if n else None,
                         "avgPct": round(r["sumPct"] / n, 3) if n else None, "pnl": r["pnl"],
                         "btEdge": r.get("btEdge"), "btWinrate": r.get("btWinrate"),
                         "postN": (r.get("post") or {}).get("n", 0),
                         "outs": r.get("outs", 0), "winN": (r.get("win") or {}).get("n", n % RULE_OUT_WINDOW),
                         "postAvg": round(r["post"]["sumPct"] / r["post"]["n"], 3) if (r.get("post") or {}).get("n") else None,
                         "active": self.state["strategies"].get(r["market"], {}).get("rid") == r["id"],
                         "lastAt": r.get("lastAt") or r["firstAt"]})
        rows.sort(key=lambda x: (not x["active"], x["trades"] == 0, -x["lastAt"]))
        return rows[:limit]

    def write_report(self, data):
        """분석용 보고서를 내 문서\\chamdom_report 에 저장 (비밀 정보 없음)"""
        if not REPORT_DIR:
            return
        try:
            os.makedirs(REPORT_DIR, exist_ok=True)
            rep = dict(data)
            rep["tradeLog"] = self.state.get("tradeLog", [])
            rep["savedAt"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
            rep["mode"] = "live" if self.live else "paper"
            tmp = os.path.join(REPORT_DIR, "report.json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(rep, f, ensure_ascii=False, indent=1)
            os.replace(tmp, os.path.join(REPORT_DIR, "report.json"))
            if os.path.exists(LOG_FILE):
                with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()[-600:]
                with open(os.path.join(REPORT_DIR, "recent_log.txt"), "w", encoding="utf-8") as f:
                    f.writelines(lines)
        except Exception as e:
            print(f"[보고서 저장 실패] {e}", flush=True)

    # ---------- 명령 ----------
    def close_bot_positions(self):
        pos = self.state["positions"]
        self.state["paused"] = True
        save_state(self.state)
        if not pos:
            return "done", "봇이 산 코인 없음 · 봇은 일시정지됨"
        prices = self.upbit.prices(list(pos.keys()))
        done = []
        for mk in list(pos.keys()):
            self.exit(mk, prices.get(mk, pos[mk]["unit"]), "비상 정리")
            done.append(mk.split("-")[1])
        return "done", "매도: " + ", ".join(done) + " · 봇은 일시정지됨"

    def handle_inbox(self):
        if time.time() - getattr(self, "_inbox_at", 0) < CLOUD_INBOX_EVERY:
            return
        self._inbox_at = time.time()
        seen = self.state.setdefault("inboxSeen", [])
        changed = False
        for iid, it in sorted(self.cloud.inbox_items().items(), key=lambda x: x[1].get("at") or 0):
            if iid in seen:
                continue
            seen.append(iid)
            changed = True
            kind = it.get("kind")
            if kind == "request":
                reqs = self.state.setdefault("userRequests", [])
                reqs.append({"id": "req-" + iid[-12:], "text": str(it.get("text", ""))[:600],
                             "at": int(it.get("at") or time.time() * 1000)})
                del reqs[:-30]
                log(f"요청 접수: {str(it.get('text', ''))[:60]} → 다음 AI 점검에서 검토")
            elif kind in ("approve", "reject") and it.get("target"):
                st, msg = self.decide(str(it["target"]), kind == "approve")
                log(f"{'승인' if kind == 'approve' else '거절'} 처리: {msg}")
        if changed:
            del seen[:-300]
            save_state(self.state)
            self.upload_status()

    def handle_suggestions(self):
        """운영자 봇만: 친구 제안을 요청 장부(userRequests)에 합쳐 AI 점검이 함께 처리하게 함"""
        if self.cfg.get("ROLE", "owner") != "owner" or time.time() - getattr(self, "_sug_at", 0) < CLOUD_SUG_EVERY:
            return
        self._sug_at = time.time()
        try:
            items = self.cloud.suggestions()
        except Exception as e:
            if not getattr(self, "_sug_warned", False):
                log(f"[안내] 친구 제안함을 아직 읽을 수 없음 (Firebase 보안 규칙 추가 필요): {str(e)[:80]}")
                self._sug_warned = True
            return
        if not getattr(self, "_sug_ok", False):
            self._sug_ok = True
            log(f"[안내] 친구 제안함 연결 확인 (보안 규칙 정상) · 지금까지 제안 {len(items)}건")
        seen = self.state.setdefault("sugSeen", [])
        reqs = self.state.setdefault("userRequests", [])
        new = 0
        for it in sorted(items, key=lambda x: x.get("at") or 0):
            sid = it["_id"]
            if sid in seen:
                continue
            seen.append(sid)
            text = str(it.get("text", ""))[:600]
            who = str(it.get("nick") or "친구")[:20]
            reqs.append({"id": "sug-" + sid[-16:], "text": text, "at": int(it.get("at") or time.time() * 1000),
                         "from": who, "friend": True})
            new += 1
            log(f"친구 제안 접수({who}): {text[:60]} → 다음 AI 점검에서 검토")
        if new:
            del reqs[:-40]
            del seen[:-500]
            save_state(self.state)
            self.upload_status()

    def handle_commands(self):
        # 웹페이지(Firebase) 무료 읽기 한도(하루 5만) 아끼기: 루프(보유 중 5초)마다 묻던 것을 15초마다로
        if time.time() - getattr(self, "_cmd_at", 0) < CLOUD_CMD_EVERY:
            return
        self._cmd_at = time.time()
        for c in self.cloud.pending_commands():
            ctype, created, age = c.get("type"), c.get("createdAt"), None
            if created:
                try:
                    age = time.time() - datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    pass
            try:
                if isinstance(ctype, str) and ctype.startswith("request|"):
                    # 사용자 요청은 늦게 받아도 버리지 않음 → 장부에 남기면 AI 점검이 읽어 처리
                    reqs = self.state.setdefault("userRequests", [])
                    rid = "req-" + c["_name"].rsplit("/", 1)[-1][:12]
                    if not any(r["id"] == rid for r in reqs):
                        reqs.append({"id": rid, "text": ctype.split("|", 1)[1][:600], "at": int(time.time() * 1000)})
                        del reqs[:-30]
                    save_state(self.state)
                    status, result = "done", "요청 접수 → 다음 AI 점검(3시간 이내)에서 검토"
                elif age is not None and age > COMMAND_MAX_AGE:
                    status, result = "rejected", "10분이 지난 명령이라 실행하지 않았습니다"
                elif ctype == "pause":
                    self.state["paused"] = True
                    save_state(self.state)
                    status, result = "done", "일시정지됨 (보유 중인 코인은 익절·손절 규칙대로 계속 관리)"
                elif ctype == "resume":
                    self.state["paused"] = False
                    save_state(self.state)
                    status, result = "done", "재개됨"
                elif ctype == "close_bot_positions":
                    status, result = self.close_bot_positions()
                elif isinstance(ctype, str) and (ctype.startswith("approve|") or ctype.startswith("reject|")):
                    status, result = self.decide(ctype.split("|", 1)[1], ctype.startswith("approve|"))
                else:
                    status, result = "rejected", "알 수 없는 명령"
            except UpbitError as e:
                status, result = "failed", "업비트 오류: " + str(e)
            log(f"명령 처리: {ctype} → {status} ({result})")
            self.cloud.finish_command(c["_name"], status, result)
            self.upload_status()

    # ---------- 메인 루프 ----------
    def run(self):
        uid = self.cloud.connect()
        log(f"웹페이지 계정 연결됨 ({uid[:6]}…) · 기기: {self.cfg['DEVICE_NAME']}")
        log(f"모드: {'★ 실매매 (실제 주문) ★' if self.live else '모의매매 (실제 주문 없음)'} · 거래 금지/숨김: {', '.join(sorted(PROTECTED_COINS)) or '없음'}")
        log(f"사용자 구분: {'친구용' if self.cfg.get('ROLE') == 'friend' else '운영자'} · 업데이트 채널: {self.cfg.get('UPDATE_CHANNEL', 'main')}")
        log("AI 분석가: " + (f"켜짐 ({self.cfg['AI_MODEL']} · 매일 {'/'.join(str(h) for h in AI_SCHEDULE_HOURS)}시 + 손실 {AI_EVERY_LOSSES}건마다)" if self.cfg["ANTHROPIC_API_KEY"]
                            else "꺼짐 (bot_config.env 에 ANTHROPIC_API_KEY 를 넣으면 켜짐)"))
        log(f"기본 주문 {self.cfg['ORDER_KRW']:,}원 · 익절/손절은 전략별 백테스트로 결정 · 손실 후 {LOSS_COOLDOWN_MIN}분 대기 · "
            f"마틴게일 최대 {self.cfg['MARTINGALE_MAX_LEVEL']}단계 · 하루 손실 한도 {self.cfg['DAILY_LOSS_LIMIT']:,}원")
        if EXPLORE_MODE:
            log(f"탐험 모드: 상위 {TOP_N}종목 · 1회 {EXPLORE_ORDER_KRW:,}원 고정 · 마틴게일 끔 · 동시 보유 최대 {MAX_OPEN_POSITIONS} · "
                f"손실 난 종목만 {LOSS_COOLDOWN_MIN}분 정지 · 규칙은 5회마다 판정·{RULE_OUT_LIMIT}아웃 퇴출")
        self.roll_day()
        uni = self.state["universe"]
        if any(m.split("-")[1] in STABLE_COINS for m in uni["markets"]):
            uni["markets"] = [m for m in uni["markets"] if m.split("-")[1] not in STABLE_COINS]
            self.state["strategies"] = {m: v for m, v in self.state["strategies"].items() if m in uni["markets"]}
            save_state(self.state)
            log("스테이블코인을 거래 대상에서 뺐습니다")
        self.upload_status()
        last_update_check = time.time()
        while True:
            if time.time() - last_update_check >= UPDATE_EVERY:
                last_update_check = time.time()
                if check_update():
                    log("새 버전을 받았습니다. 봇을 다시 시작합니다 (보유 코인은 이어서 관리)")
                    self.push_progress("새 버전으로 재시작 중")
                    raise RestartForUpdate()
            try:
                self.roll_day()
                uni = self.state["universe"]
                if uni["day"] != today_key() or not uni["markets"] or uni.get("topN", 10) != TOP_N:
                    self.pick_universe()
                    self.state["lastOptimize"] = 0
                if self.state.get("strategyVersion") != STRATEGY_VERSION:
                    self.state["strategyVersion"] = STRATEGY_VERSION
                    self.state["universe"]["day"] = ""        # 새 기준으로 종목·전략 다시 선정
                    self.state["strategies"] = {}
                    self.state["blocked"] = []                 # 예전 전략 성적으로 막힌 종목도 다시 기회
                    self.state["marketStats"] = {}
                    self.ledger["gateTrades"] = self.ledger["gateWins"] = 0
                    save_state(self.state)
                    log(f"전략 선정 기준 v{STRATEGY_VERSION} 적용 → 종목과 전략을 다시 고르고, 부진 판단은 새 전략 거래부터 다시 셉니다")
                    continue
                if time.time() - self.state["lastOptimize"] >= REOPTIMIZE_MIN * 60:
                    self.optimize()
                    self.upload_status()
                if self.ai_should_run():
                    self.ai_start()
                if getattr(self, "_ai_dirty", False):
                    self._ai_dirty = False
                    save_state(self.state)
                    self.upload_status()
                # 긴급 수정(11-06): 웹페이지(Firebase) 읽기 한도 초과(429) 등으로 명령 확인이 실패해도
                # 보유 코인 관리·신규 진입은 계속해야 함 → 클라우드 확인 오류는 따로 잡고, 한도 초과면 10분 쉼
                if time.time() >= getattr(self, "_cloud_backoff", 0):
                    try:
                        self.handle_commands()
                        self.handle_inbox()
                        self.handle_suggestions()
                        if time.time() - getattr(self, "_last_settings", 0) >= APP_SETTINGS_EVERY:
                            self._last_settings = time.time()
                            if sync_app_settings(self.cloud, self.cfg):
                                log("[설정] 대시보드에서 바꾼 설정을 받았습니다 → 적용하려고 다시 시작합니다 (보유 코인은 이어서 관리)")
                                self.push_progress("설정 적용 중 · 다시 시작")
                                raise RestartForUpdate()
                    except RestartForUpdate:
                        raise
                    except Exception as e:
                        if "429" in str(e):
                            self._cloud_backoff = time.time() + 600
                            log("[경고] 웹페이지 읽기 한도 초과(429) → 명령·요청 확인만 10분 쉼 (매매·보유 관리는 계속)")
                        else:
                            log(f"[경고] 명령·요청함 확인 실패 (매매는 계속): {str(e)[:150]}")
                self.manage_positions()
                self.manage_ghosts()
                self.manage_hold_ghosts()
                try:
                    self.manage_monkeys()
                except Exception as e:
                    log(f"[경고] 원숭이 기준선 추적 실패: {e}")
                try:
                    self.move_watch()
                except Exception as e:
                    log(f"[경고] 큰 움직임 기록 실패: {e}")
                self.gate_check()
                if not self.error:
                    self.scan_entries()
                if time.time() - self.last_status >= STATUS_EVERY:
                    self.upload_status()
            except RestartForUpdate:
                raise
            except Exception as e:      # 밤새 돌아야 하므로 어떤 오류든 기록하고 계속
                log(f"[경고] {type(e).__name__}: {e}")
                time.sleep(15)
            time.sleep(LOOP_ACTIVE if self.state["positions"] else LOOP_IDLE)

    def mark_offline(self):
        try:
            if self.cloud.uid:
                self.cloud.put_status({"botState": "paused", "message": "PC에서 봇 프로그램이 종료되었습니다.",
                                       "updatedAt": 0}, partial=True)
        except Exception:
            pass


def main():
    cfg = load_config()                 # 업데이트 채널(main/stable)을 알기 위해 설정부터 읽음
    if check_update():
        print("새 버전을 받았습니다. 다시 시작합니다.", flush=True)
        sys.exit(RESTART_CODE)
    bot = Bot(cfg)
    log("참돔 트레이드 봇 v2 시작 (종료: Ctrl+C)")
    up = {k: v for k, v in cfg["_appSettings"].items() if not k.startswith("BINANCE_")}
    if up:
        log("[설정] 대시보드에서 정한 값 적용: " + ", ".join(f"{k}={v}" for k, v in up.items()))
    restarting = False
    try:
        bot.run()
    except RestartForUpdate:
        restarting = True
    except KeyboardInterrupt:
        log("종료 요청 받음 (보유 중인 코인은 다음 실행 때 이어서 관리)")
    except RuntimeError as e:
        log(f"[중단] {e}")
    finally:
        if not restarting:
            bot.mark_offline()
            log("봇 종료")
    if restarting:
        sys.exit(RESTART_CODE)


if __name__ == "__main__":
    main()
