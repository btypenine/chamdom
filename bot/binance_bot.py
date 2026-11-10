# -*- coding: utf-8 -*-
"""
참돔 트레이드 바이낸스 선물 엔진 — 모의매매 + (사용자가 켜면) 실전 통과 규칙만 실주문

실주문 조건 (모두 만족해야 함, 하나라도 아니면 모의로 처리)
- bot_config.env: BINANCE_TRADE_MODE=live + BINANCE_API_KEY/BINANCE_SECRET_KEY
- 그 규칙이 모의 5회 이상에서 거래당 +0.15% 이상으로 '통과'(proven)
- 오늘(09시 기준) 실주문 손실이 BINANCE_DAILY_LOSS_USDT 미만, 실보유 수 < BINANCE_MAX_LIVE
- 업비트 봇이 대시보드에서 '일시정지' 상태가 아님
- 레버리지 1배·격리 마진 고정, 매도는 reduceOnly 로만 정리

돈 지키기 (11-06 추가)
- 실주문 진입 즉시 거래소에 보호 손절(STOP_MARKET, Algo 주문) — PC가 꺼져도 손절이 걸림. 못 걸면 바로 정리
- 월 손실 한도(BINANCE_MONTH_LOSS_USDT), 연속 3번 손실이면 2시간 쉼 (대시보드에서 승인하면 바로 다시 거래)
- 실주문 금액은 규칙이 이익일 확률로 0.5~2배, 누적 이익이 나면 판을 키우고 손실이면 줄임

진화 (11-06 추가)
- 잘된 규칙의 조건 조합을 유전자 풀에 모아, 새 전략 일부를 변이·교배로 만듦 (종목을 넘나들며 섞임)
- 진화 출신과 무작위 출신의 실제 성적을 따로 재서, 진화가 이기면 진화 비율을 늘리고 지면 줄임
(아래는 1단계 설명)
참돔 트레이드 바이낸스 선물 엔진 — 1단계: 모의매매 전용

근거 (bt-lab 3차, 11-01): 보지 않은 다음 구간, 25종목 × 8창
- 15분봉 선물 비용·롱+숏 거래당 +0.318%(189건), 5분봉 +0.132%(166건) — 업비트 롱만은 -0.28~-0.77%
- 주의: 수익 대부분이 숏(시험 기간 BTC -3~-9% 하락장), 펀딩비 미반영, 업비트 시세로 근사 → 실제 바이낸스 시세로 모의 검증 필요

하는 일
- 바이낸스 USDⓈ-M 무기한 선물 중 24시간 거래대금 상위 종목 선정 (공개 시세만 사용, 키 불필요)
- 15분봉, 업비트 봇과 같은 무작위 조건 조합 백테스트로 롱·숏 전략을 함께 골라 신호가 나오면 '모의' 진입
- 익절·손절·보유시간 관리, 규칙별 실전(모의) 성적 장부, 대시보드(status/binance)와 보고서 파일로 기록
- 실제 주문은 2단계에서, 모의 성적 확인 + 사용자 승인 후에만 추가함

실행: start_binance.bat (같은 폴더의 chamdom_bot.py 와 bot_config.env 필요)
"""
import hashlib
import hmac
import json
import math
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    print("필요 패키지가 없습니다:  python -m pip install requests pyjwt")
    sys.exit(1)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import chamdom_bot as cb          # 전략 생성·백테스트·지표·Firebase 연결을 그대로 씀

FAPI = "https://fapi.binance.com"
KST = timezone(timedelta(hours=9))
STATE_FILE = os.path.join(BASE_DIR, "binance_state.json")
LOG_FILE = os.path.join(BASE_DIR, "binance.log")
UPDATE_URL = "https://raw.githubusercontent.com/btypenine/chamdom/main/bot/binance_bot.py"
RESTART_CODE = 3

# 선물 조건
FEE = 0.0005                      # 시장가(taker) 수수료, 일반 등급
SLIP = 0.0002                     # 체결 밀림 가정
TF = 15                           # 봉 길이(분) — 실험에서 가장 좋았던 15분봉
TRAIN_BARS = 1920                 # 학습 20일
TOP_N = 20                        # 거래대금 상위 종목 수
RESELECT_HOURS = 4                # 전략 다시 고르는 주기
FUNDING_CHECK_SEC = 300           # 펀딩비(관찰 전용): 5분마다 예상 펀딩비를 읽어 보유 포지션에 정산 시점마다 누적 (손익·판정에는 안 씀)
REGIME_BARS = 96                  # 시장 국면(관찰 전용): BTC 최근 24시간(15분봉 96개) 변화로 나눔
REGIME_UP, REGIME_DOWN = 1.5, -1.5   # +1.5% 이상 상승장, -1.5% 이하 하락장, 그 사이 횡보
PAPER_USDT = 100.0                # 모의 1회 진입 금액(USDT, 레버리지 1배)
GB_ARM = 0.5                      # 관찰 전용: 익절까지 가격 움직임의 50% 이상 갔다가
GB_FLOOR = 0.0                    #   진입가(움직임 0%)까지 되밀리면 "여기서 본전 정리했다면" 손익을 기록 (실제 청산은 그대로)
SW_LOOK, SW_MIN = 20, 10          # 관찰 전용 '약한 방향': 같은 방향 최근 모의 20회(최소 10회) 평균이 0 미만이면 진입에 표시만 함
MAX_OPEN = 10                     # 동시 모의 보유 최대
# 확장형(wide): 대상 종목 2배 → 거래 기회 2배. 대시보드 설정 BINANCE_STYLE=wide 일 때만 (기본 swing = 기존 동작)
# bt-lab 11-07: 보지 않은 다음 10일 — 20종목 134회 거래당 +0.206% / 40종목 284회 +0.284% (회전형 작은 익절은 -0.12~-0.20%로 탈락)
STYLE = "swing"
WIDE_TOP_N = 40
WIDE_MAX_OPEN = 20                # 동시 모의 보유
EXCLUDE = {"USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "BTCDOMUSDT"}
RULE_EVAL = 5                     # 규칙 판정 시작 횟수 (업비트 봇과 같음)
RULE_PROMOTE_MIN_AVG = 0.15
ADOPT_TP, ADOPT_SL, ADOPT_HOURS = 2.0, -3.0, 24   # 봇이 놓친 실제 포지션을 넘겨받을 때(짝 모의 포지션이 없으면) 익절·손절·최대 보유
STICKY_IDLE_HOURS = 24            # 검증 중 규칙은 재선정 때 유지, 단 이 시간 동안 한 번도 신호가 없으면 교체
SIDE_GATE_MIN_N = 50              # (선택) 방향별 통과 기준: 모의 50회 이상
SIDE_GATE_MIN_AVG = 0.30          #   거래당 +0.30% 이상

# 돈 지키기 (실주문에만 적용, 모의에는 영향 없음)
EXCHANGE_STOP_BUFFER = 0.5        # 거래소 보호 손절: 봇 손절가보다 0.5%p 더 바깥 (평소엔 봇이 먼저 정리, PC가 멈추면 거래소가 정리)
EXCHANGE_CHECK_SEC = 60           # 실보유가 거래소에서 정리됐는지 확인하는 주기(초)
STOP_FAIL_BLOCK_HOURS = 6         # 보호 손절을 못 걸면 바로 정리하고 이 시간 동안 실주문 쉼
LOSS_STREAK_MAX = 3               # 실주문 연속 손실 이 횟수면
LOSS_STREAK_REST_HOURS = 2        #   이 시간 동안 새 실주문 쉼 (대시보드에서 승인하면 바로 해제)
RESUME_CHECK_SEC = 60             # 쉬는 동안 대시보드 '쉼 해제 승인'을 확인하는 주기(초)
MONTH_LOSS_DEFAULT = 40.0         # 월 실주문 손실 한도 기본값(USDT) — 대시보드 BINANCE_MONTH_LOSS_USDT 로 변경

# 테스트 실주문 (사용자가 대시보드에서 직접 누를 때만, 한 번 누르면 한 건)
TEST_CHECK_SEC = 60               # 대시보드 '테스트 실주문' 버튼을 확인하는 주기(초) — 웹페이지 읽기 한도(하루 5만) 아끼려고 1분
DASH_READ_FALLBACK_EVERY = 5      # 대시보드 버튼 값은 status/settings 문서만 읽고, 예비(사용자 문서)는 5번에 1번만 같이 읽음
DASH_READ_BACKOFF_SEC = 600       # 읽기 한도 초과(429)면 버튼 확인만 10분 쉼(매매·보유 관리는 계속)
TEST_MAX_AGE_SEC = 600            # 누른 지 이보다 오래된 요청은 실행하지 않음(봇이 꺼져 있다 나중에 켜져도 뒤늦게 주문 안 나가게)
TEST_TP, TEST_SL = 1.0, -1.0      # 익절 +1%(순익 기준) · 손절 -1%
TEST_HOLD_BARS = 16               # 최대 보유 16봉(15분봉 → 4시간), 넘으면 정리

# 성적 기반 실주문 금액 (확률이 높은 규칙에 더, 약한 규칙엔 덜 — 사람이 정하지 않고 결과로 정함)
PRIOR_N = 5                       # 아직 거래가 적은 규칙은 '평균 0' 쪽으로 당겨 봄 (5회분 무게)
PRIOR_SD = 2.0                    # 거래당 손익 흩어짐 기본 추정(%)
SIZE_MIN_X, SIZE_MAX_X = 0.5, 2.0 # 기본 금액의 0.5~2배
SIZE_SKIP_P = 0.40                # 이익일 확률이 40% 미만이면 실주문 대신 모의
BANK_X = 15                       # 실주문 누적 이익 ÷ (기본 금액 × 15) 만큼 판을 키우거나 줄임 (0.5~2배)

# 진화 (잘된 규칙의 '유전자'를 모아 변이·교배로 새 전략을 만들고, 무작위보다 나은지 스스로 재서 비율을 조절)
GENE_POOL_MAX = 40                # 유전자 풀 크기
GENE_MIN_N = 3                    # 이만큼 거래한 규칙부터 풀에 들어갈 자격
EVO_SHARE_START = 0.30            # 새 전략 시도 중 진화 출신 비율 시작값
EVO_SHARE_MIN, EVO_SHARE_MAX = 0.15, 0.75
EVO_JUDGE_N = 10                  # 진화·무작위 출신 각각 이만큼 거래가 쌓이면 비율 조절 시작


def log(msg):
    line = f"[{datetime.now(KST).strftime('%m-%d %H:%M:%S')}] [바이낸스] {msg}"
    print(line, flush=True)
    ACT.append(line[1:15] + " " + msg)
    del ACT[:-60]
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


ACT = []


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("strategies", {})        # {symbol: {"side": long|short, "spec":..., stats...}}
    s.setdefault("positions", {})         # {symbol: {...}}
    s.setdefault("ruleBook", {})
    s.setdefault("ledger", {"trades": 0, "wins": 0, "pnlUsdt": 0.0, "sumPct": 0.0,
                            "long": {"n": 0, "sumPct": 0.0}, "short": {"n": 0, "sumPct": 0.0}})
    s.setdefault("lastSelect", 0)
    s.setdefault("lastBar", {})
    s.setdefault("tradeLog", [])
    s.setdefault("genePool", [])          # [{"side", "spec", "fit", "n", "rid", "gen", "symbol"}]
    s.setdefault("evo", {"share": EVO_SHARE_START, "evo": {"n": 0, "sumPct": 0.0}, "rand": {"n": 0, "sumPct": 0.0},
                         "maxGen": 0, "born": 0})
    s.setdefault("guard", {"streak": 0, "restUntil": 0, "blockUntil": 0, "blockWhy": ""})
    s.setdefault("regimeStats", {})      # 관찰 전용: {"paper|하락|short": {"n","wins","sumPct"}} — 매매에는 안 씀
    return s


def save_state(s):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


# ---------------- 바이낸스 공개 시세 ----------------
def get(path, params=None):
    for a in range(5):
        r = requests.get(FAPI + path, params=params or {}, timeout=10)
        if r.status_code in (418, 429):
            time.sleep(2 + a * 2)
            continue
        if r.status_code != 200:
            raise RuntimeError(f"바이낸스 {path} 실패 {r.status_code}: {r.text[:120]}")
        return r.json()
    raise RuntimeError("바이낸스 요청 한도 초과")


def top_symbols(n=TOP_N):
    info = get("/fapi/v1/exchangeInfo")
    perp = {s["symbol"] for s in info["symbols"]
            if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"}
    tick = get("/fapi/v1/ticker/24hr")
    protected = {c + "USDT" for c in cb.PROTECTED_COINS} | {"1000" + c + "USDT" for c in cb.PROTECTED_COINS}
    rows = [(float(t["quoteVolume"]), t["symbol"]) for t in tick
            if t["symbol"] in perp and t["symbol"] not in EXCLUDE and t["symbol"] not in protected]   # 보호 코인(XRP 등)은 선물에서도 제외
    rows.sort(reverse=True)
    return [sym for _, sym in rows[:n]]


def klines(symbol, total):
    """15분봉 total개, 오래된 것 → 최신, 업비트 캔들 모양으로 변환 (진행 중인 봉 제외)"""
    out, end = [], None
    while len(out) < total:
        p = {"symbol": symbol, "interval": f"{TF}m", "limit": min(1500, total - len(out) + 1)}
        if end:
            p["endTime"] = end
        data = get("/fapi/v1/klines", p)
        if not data:
            break
        out = data + out
        end = data[0][0] - 1
        if len(data) < p["limit"]:
            break
        time.sleep(0.1)
    now = int(time.time() * 1000)
    rows = [k for k in out if k[6] < now]           # 닫힌 봉만
    return [{"opening_price": float(k[1]), "high_price": float(k[2]), "low_price": float(k[3]),
             "trade_price": float(k[4]), "candle_acc_trade_volume": float(k[5]), "t": k[0]} for k in rows[-total:]]


def inverted(cl):
    return [{"opening_price": 1 / c["opening_price"], "high_price": 1 / c["low_price"], "low_price": 1 / c["high_price"],
             "trade_price": 1 / c["trade_price"], "candle_acc_trade_volume": c["candle_acc_trade_volume"], "t": c["t"]}
            for c in cl]


def prices(symbols):
    data = get("/fapi/v1/ticker/price")
    want = set(symbols)
    return {d["symbol"]: float(d["price"]) for d in data if d["symbol"] in want}


# ---------------- 실주문 (서명 요청) ----------------
class Live:
    def __init__(self, key, secret):
        self.key, self.secret = key, secret
        self.offset = 0
        self.filters = {}
        self.prepared = set()

    def sync(self):
        d = get("/fapi/v1/time")
        self.offset = d["serverTime"] - int(time.time() * 1000)

    def call(self, method, path, params=None):
        p = dict(params or {})
        p["timestamp"] = int(time.time() * 1000) + self.offset
        p["recvWindow"] = 5000
        q = urlencode(p)
        q += "&signature=" + hmac.new(self.secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        r = requests.request(method, f"{FAPI}{path}?{q}", headers={"X-MBX-APIKEY": self.key}, timeout=10)
        try:
            data = r.json()
        except ValueError:
            data = {"msg": r.text[:200]}
        if r.status_code != 200:
            raise RuntimeError(f"바이낸스 {path} 실패 {r.status_code} 코드 {data.get('code')}: {data.get('msg')}")
        return data

    def load_filters(self):
        info = get("/fapi/v1/exchangeInfo")
        for s in info["symbols"]:
            f = {x["filterType"]: x for x in s["filters"]}
            lot = f.get("MARKET_LOT_SIZE") or f.get("LOT_SIZE") or {}
            self.filters[s["symbol"]] = {"step": float(lot.get("stepSize", 0.001)), "minQty": float(lot.get("minQty", 0)),
                                         "minNotional": float((f.get("MIN_NOTIONAL") or {}).get("notional", 5)),
                                         "tick": float((f.get("PRICE_FILTER") or {}).get("tickSize", 0) or 0)}

    def round_price(self, sym, px, up):
        """호가 단위에 맞춤. up=True면 올림(숏 손절), False면 내림(롱 손절) — 늘 '더 바깥' 쪽으로"""
        t = (self.filters.get(sym) or {}).get("tick") or 0
        if t <= 0:
            return float(f"{px:.6g}")
        x = px / t
        q = math.ceil(x - 1e-9) if up else math.floor(x + 1e-9)
        dec = max(0, -int(math.floor(math.log10(t)))) if t < 1 else 0
        return round(q * t, dec)

    def stop_order(self, sym, close_side, trigger):
        """거래소 보호 손절 (2025-12-09부터 조건 주문은 Algo 주문 창구로만 받음). 포지션 전체를 시장가로 닫음"""
        d = self.call("POST", "/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL", "symbol": sym, "side": close_side, "type": "STOP_MARKET",
            "triggerPrice": f"{trigger:.10f}".rstrip("0").rstrip("."), "closePosition": "true",
            "workingType": "MARK_PRICE"})
        return d.get("algoId")

    def cancel_stops(self, sym):
        """그 종목에 걸어 둔 조건 주문 모두 취소 (없으면 조용히 넘어감)"""
        try:
            self.call("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": sym})
        except RuntimeError as e:
            if "-2011" not in str(e) and "-2013" not in str(e):     # 취소할 주문 없음은 정상
                log(f"[안내] {sym} 보호 손절 취소 응답: {str(e)[:120]}")

    def prepare(self, sym):
        if sym in self.prepared:
            return
        self.call("POST", "/fapi/v1/leverage", {"symbol": sym, "leverage": 1})
        try:
            self.call("POST", "/fapi/v1/marginType", {"symbol": sym, "marginType": "ISOLATED"})
        except RuntimeError as e:
            if "-4046" not in str(e):          # 이미 격리 마진이면 -4046 (정상)
                raise
        self.prepared.add(sym)

    def qty_for(self, sym, usdt, price):
        f = self.filters.get(sym) or {}
        step = f.get("step", 0.001)
        q = math.floor(usdt / price / step) * step
        q = float(f"{q:.10f}")
        if q < f.get("minQty", 0) or q * price < f.get("minNotional", 5):
            return 0.0, f.get("minNotional", 5)
        return q, None

    def min_qty(self, sym, price):
        """최소 주문(수량·금액)을 만족하는 가장 작은 수량(수량 단위 올림). 반환: (수량, 실제 금액)"""
        f = self.filters.get(sym) or {}
        step = f.get("step", 0.001) or 0.001
        need_q = max(f.get("minQty", 0), f.get("minNotional", 5) * 1.02 / price)
        q = math.ceil(need_q / step - 1e-9) * step
        q = float(f"{q:.10f}")
        return q, q * price

    def market(self, sym, side, qty, reduce=False):
        p = {"symbol": sym, "side": side, "type": "MARKET", "quantity": f"{qty:.10f}".rstrip("0").rstrip("."),
             "newOrderRespType": "RESULT"}
        if reduce:
            p["reduceOnly"] = "true"
        d = self.call("POST", "/fapi/v1/order", p)
        avg, filled = float(d.get("avgPrice") or 0), float(d.get("executedQty") or 0)
        # 선물 시장가 주문은 응답이 '접수(NEW)·체결 0'으로 먼저 오는 경우가 많음 → 주문 조회로 실제 체결을 확인
        oid = d.get("orderId")
        for _ in range(6):
            if (filled > 0 and avg > 0) or not oid:
                break
            time.sleep(0.5)
            try:
                q = self.call("GET", "/fapi/v1/order", {"symbol": sym, "orderId": oid})
            except RuntimeError:
                continue
            avg, filled = float(q.get("avgPrice") or 0), float(q.get("executedQty") or 0)
            if q.get("status") in ("CANCELED", "EXPIRED", "REJECTED"):
                break
        if not (filled > 0 and avg > 0) and oid:
            try:                                                  # 끝내 체결이 안 보이면 남은 주문은 취소해 둠
                self.call("DELETE", "/fapi/v1/order", {"symbol": sym, "orderId": oid})
            except RuntimeError:
                pass
            if not reduce:                                        # 그래도 포지션이 생겼으면 그 값을 씀
                try:
                    pr = self.call("GET", "/fapi/v2/positionRisk", {"symbol": sym})
                    pos = [x for x in pr if x.get("symbol") == sym and float(x.get("positionAmt") or 0) != 0]
                    if pos:
                        filled = abs(float(pos[0]["positionAmt"])); avg = float(pos[0].get("entryPrice") or 0)
                except RuntimeError:
                    pass
        return avg, filled

    def close_fill(self, sym, long_, since_ms, qty):
        """거래소가 대신 닫은 포지션(보호 손절 체결·직접 정리)의 실제 체결 평균가 조회 (읽기 전용).
        진입 이후 닫는 방향 체결을 최근 것부터 qty 만큼 모아 평균. 못 찾으면 None → 예전처럼 손절가로 추정"""
        start = max(int(since_ms or 0), int(time.time() * 1000) - 6 * 86400 * 1000)   # 조회 창은 최대 7일
        rows = self.call("GET", "/fapi/v1/userTrades", {"symbol": sym, "startTime": start, "limit": 100})
        want = "SELL" if long_ else "BUY"
        got, cost = 0.0, 0.0
        for t in sorted(rows, key=lambda x: x.get("time", 0), reverse=True):
            if t.get("side") != want:
                continue
            q = float(t.get("qty") or 0)
            if q <= 0:
                continue
            take = min(q, qty - got)
            got += take
            cost += take * float(t.get("price") or 0)
            if got >= qty * 0.999:
                break
        if got < qty * 0.999 or cost <= 0:
            return None
        return cost / got

    def snapshot(self):
        """대시보드 표시용: 거래소 실제 선물 지갑·포지션 (봇 장부와 상관없이 있는 그대로)"""
        bal = {}
        try:
            for b in self.call("GET", "/fapi/v2/balance"):
                if b.get("asset") == "USDT":
                    bal = {"balance": float(b.get("balance") or 0), "available": float(b.get("availableBalance") or 0),
                           "upnl": float(b.get("crossUnPnl") or 0)}
        except RuntimeError:
            pass
        pos, upnl = [], 0.0
        for x in self.call("GET", "/fapi/v2/positionRisk"):
            amt = float(x.get("positionAmt") or 0)
            if not amt:
                continue
            u = float(x.get("unRealizedProfit") or 0)
            upnl += u
            mark = float(x.get("markPrice") or 0)
            pos.append({"symbol": x["symbol"], "side": "long" if amt > 0 else "short", "amt": abs(amt),
                        "entry": float(x.get("entryPrice") or 0), "mark": mark, "upnl": round(u, 4),
                        "notional": round(abs(amt) * mark, 2), "lev": int(float(x.get("leverage") or 1)),
                        "margin": (x.get("marginType") or "").lower()})
        bal["upnl"] = round(upnl, 4)
        return {"at": int(time.time() * 1000), **bal, "positions": pos}

    def positions(self):
        try:
            d = self.call("GET", "/fapi/v3/positionRisk")
        except RuntimeError:
            d = self.call("GET", "/fapi/v2/positionRisk")
        return {x["symbol"]: float(x["positionAmt"]) for x in d if float(x.get("positionAmt", 0)) != 0}


# ---------------- 전략 선정 ----------------
def series_of(cl):
    s = cb.Series(cl)
    s.slip = SLIP
    return s


# ---------------- 진화: 변이·교배 ----------------
def _near(choices, v, rng):
    """보기 목록에서 지금 값의 바로 옆 칸으로 (끝이면 안쪽으로)"""
    if v not in choices:
        return rng.choice(choices)
    i = choices.index(v) + rng.choice((-1, 1))
    return choices[min(len(choices) - 1, max(0, i))]


def mutate(spec, rng):
    """유전자 하나를 살짝 바꿈: 익절·손절·보유시간 한 칸, 조건 값 바꾸기, 조건 더하기·빼기 중 1~2개"""
    sp = json.loads(json.dumps(spec))
    pool = dict(cb.CONDITION_POOL)
    for _ in range(rng.choice((1, 1, 2))):
        ops = ["tp", "sl", "hold", "tweak"]
        if len(sp["conds"]) < 3:
            ops.append("add")
        if len(sp["conds"]) > 1:
            ops.append("drop")
        op = rng.choice(ops)
        if op == "tp":
            sp["tp"] = _near(cb.TP_CHOICES, sp.get("tp", 0.5), rng)
        elif op == "sl":
            sp["sl"] = _near(cb.SL_CHOICES, sp["sl"], rng)
        elif op == "hold":
            sp["hold"] = _near(cb.HOLD_CHOICES, sp["hold"], rng)
        elif op == "tweak":
            c = rng.choice(sp["conds"])
            c[1] = rng.choice(pool.get(c[0], [c[1]]))
        elif op == "add":
            have = {c[0] for c in sp["conds"]}
            name = rng.choice([n for n in pool if n not in have])
            sp["conds"].append([name, rng.choice(pool[name])])
        else:
            sp["conds"].pop(rng.randrange(len(sp["conds"])))
    return sp


def crossover(a, b, rng):
    """두 부모의 조건을 섞고 익절·손절·보유시간은 각각 한쪽에서 받음"""
    genes = {}
    for c in a["conds"] + b["conds"]:
        genes.setdefault(c[0], c[1])
    names = list(genes)
    rng.shuffle(names)
    k = min(len(names), rng.choice((1, 2, 2, 3)))
    return {"conds": [[n, json.loads(json.dumps(genes[n]))] for n in names[:k]],
            "tp": rng.choice((a, b)).get("tp", 0.5), "sl": rng.choice((a, b))["sl"], "hold": rng.choice((a, b))["hold"]}


def breed(genes, rng):
    """유전자 풀에서 부모 고르기(3개 중 성적 좋은 쪽) → 자식 spec, 세대, 부모 표시"""
    def tour():
        return max(rng.sample(genes, min(3, len(genes))), key=lambda g: g["fit"])
    p = tour()
    if len(genes) >= 2 and rng.random() < 0.35:
        q = tour()
        child = crossover(p["spec"], q["spec"], rng)
        if rng.random() < 0.5:
            child = mutate(child, rng)
        return child, max(p.get("gen", 0), q.get("gen", 0)) + 1, f"{p.get('symbol', '')}×{q.get('symbol', '')}"
    return mutate(p["spec"], rng), p.get("gen", 0) + 1, p.get("symbol", "")


def pick_for(symbol, rng, pool=None, share=0.0):
    cl = klines(symbol, TRAIN_BARS)
    if len(cl) < TRAIN_BARS * 0.8:
        return None
    cands = []
    for side, ser in (("long", series_of(cl)), ("short", series_of(inverted(cl)))):
        split = int(ser.n * (1 - cb.BT_HOLDOUT))
        genes = [g for g in (pool or []) if g.get("side") == side]
        for _ in range(cb.BT_TRIALS):
            if genes and rng.random() < share:
                sp, gen, parent = breed(genes, rng)
                origin = {"origin": "evo", "gen": gen, "parent": parent}
            else:
                sp, origin = cb.random_spec(rng), {"origin": "rand", "gen": 0, "parent": ""}
            tr = cb.stats(cb.backtest(ser, sp, end=split))
            if tr["n"] < cb.EXPLORE_MIN_TRADES or tr["edge"] < cb.EXPLORE_MIN_EDGE:
                continue
            ho = cb.stats(cb.backtest(ser, sp, start=split))
            if ho["n"] and ho["edge"] < cb.EXPLORE_HOLDOUT_FLOOR:
                continue
            score = tr["edge"] if not ho["n"] else (tr["edge"] * tr["n"] + ho["edge"] * ho["n"]) / (tr["n"] + ho["n"])
            cands.append((score, side, sp, tr, ho, origin))
    if not cands:
        return None
    score, side, sp, tr, ho, origin = max(cands, key=lambda x: x[0])
    rid = f"{symbol}-{side}-" + cb.rule_id("USDT-" + symbol, sp).split("-")[-1]
    desc = cb.describe(sp).replace(f"최대{sp['hold']}분", f"최대{sp['hold'] * TF / 60:g}시간")   # 보유시간은 봉 개수 × 15분
    return {"side": side, "spec": sp, "rid": rid, "desc": ("롱 " if side == "long" else "숏 ") + desc,
            "btEdge": round(score, 3), "btTrades": tr["n"], "btWinrate": round(tr["winrate"], 3), **origin}


class Engine:
    def __init__(self):
        self.cfg = cb.load_config()
        self.cloud = cb.Cloud(self.cfg["FIREBASE_LINK_CODE"])
        self.state = load_state()
        self.rng = random.Random()
        self.last_status = 0
        c = self.cfg
        num = lambda k, d: float(str(c.get(k, d)).replace(",", "") or d)
        self.order_usdt = num("BINANCE_ORDER_USDT", 20)
        self.daily_loss = num("BINANCE_DAILY_LOSS_USDT", 10)
        self.max_live = int(num("BINANCE_MAX_LIVE", 3))
        global STYLE
        STYLE = "wide" if str(c.get("BINANCE_STYLE", "swing")).strip().lower() == "wide" else "swing"
        self.max_open = WIDE_MAX_OPEN if STYLE == "wide" else MAX_OPEN
        c.setdefault("BINANCE_MONTH_LOSS_USDT", f"{MONTH_LOSS_DEFAULT:g}")      # 대시보드에 '기본값 40'으로 보이게
        self.month_loss = num("BINANCE_MONTH_LOSS_USDT", MONTH_LOSS_DEFAULT)
        self._last_exchange_check = 0
        self.live_basis = str(c.get("BINANCE_LIVE_BASIS", "rule")).strip().lower()   # rule(기본) | side | all
        key, sec = c.get("BINANCE_API_KEY", ""), c.get("BINANCE_SECRET_KEY", "")
        self.live = None
        has_key = key and sec and not key.startswith("여기에") and not sec.startswith("여기에")
        self.live_new = bool(has_key and c.get("BINANCE_TRADE_MODE", "paper").lower() == "live")   # 새 실주문 허용 여부
        holding = any(p.get("live") for p in self.state.get("positions", {}).values())
        if has_key and (self.live_new or holding):
            # 모의로 바꿔도 이미 들고 있는 실보유는 계좌 연결을 유지해 평소대로 익절·손절·보유시간 정리 (새 실주문만 멈춤)
            self.live = Live(key, sec)
        self.state.setdefault("liveLedger", {"trades": 0, "wins": 0, "pnlUsdt": 0.0, "day": "", "dayPnl": 0.0, "dayTrades": 0})

    # --- 전략 ---
    def reselect(self):
        old = self.state.get("strategies") or {}
        for cur in old.values():                                  # 예전 규칙에도 유전자(spec)를 붙여 둠 → 진화 재료
            rb = self.state["ruleBook"].get(cur.get("rid"))
            if rb is not None and "spec" not in rb and cur.get("spec"):
                rb["spec"] = cur["spec"]
                rb.setdefault("origin", cur.get("origin", "rand"))
        self.refresh_pool()
        evo = self.state["evo"]
        log(f"종목·전략 선정 시작 (15분봉, {'확장형 · ' if STYLE == 'wide' else ''}상위 {WIDE_TOP_N if STYLE == 'wide' else TOP_N}종목, 롱·숏 각 {cb.BT_TRIALS}개 조합 · "
            f"진화 비율 {evo['share'] * 100:.0f}% · 유전자 풀 {len(self.state['genePool'])}개)")
        syms = top_symbols(WIDE_TOP_N if STYLE == "wide" else TOP_N)
        if self.state.get("style", "swing") != STYLE:                # 방식이 바뀌면 전략을 새로 고름(보유 포지션은 그대로 관리)
            log(f"[설정] 매매 방식 {self.state.get('style', 'swing')} → {STYLE}: 전략을 새로 고름")
            self.state["strategies"] = {}
            self.state["style"] = STYLE
            old = {}
        chosen, kept = {}, 0
        now = int(time.time())
        # 1) 검증이 진행 중인 규칙은 그대로 둠 — 4시간마다 새 규칙으로 바꾸면 어떤 규칙도 5회를 못 채움
        for sym, cur in old.items():
            if sym.replace("1000", "").replace("USDT", "") in cb.PROTECTED_COINS or sym in EXCLUDE:
                continue
            rb = self.state["ruleBook"].get(cur.get("rid")) or {}
            if rb.get("status") not in ("testing", "proven"):
                continue
            since = cur.get("since") or self.state.get("lastSelect") or now
            if rb.get("n", 0) == 0 and now - since > STICKY_IDLE_HOURS * 3600:
                continue                                        # 하루 넘게 신호가 없던 규칙은 교체
            cur.setdefault("since", since)
            chosen[sym] = cur
            kept += 1
            log(f"  유지 {sym}: {cur['desc']} · 모의 {rb.get('n', 0)}회 진행 중")
        for i, sym in enumerate(syms, 1):
            if sym in chosen:
                continue
            self.push_phase(f"전략 선정 {i}/{len(syms)} ({sym})")
            try:
                st = pick_for(sym, self.rng, self.state["genePool"], evo["share"])
            except Exception as e:
                log(f"[경고] {sym} 선정 실패: {e}")
                continue
            if st:
                rb = self.state["ruleBook"].get(st["rid"])
                if rb and rb.get("status") == "retired":
                    continue
                st["since"] = now
                chosen[sym] = st
                self.state["ruleBook"].setdefault(st["rid"], {"id": st["rid"], "symbol": sym, "side": st["side"],
                                                              "desc": st["desc"], "btEdge": st["btEdge"], "n": 0, "wins": 0,
                                                              "sumPct": 0.0, "sumSq": 0.0, "status": "testing",
                                                              "spec": st["spec"], "origin": st.get("origin", "rand"),
                                                              "gen": st.get("gen", 0), "parent": st.get("parent", "")})
                if st.get("origin") == "evo":
                    evo["born"] += 1
                    evo["maxGen"] = max(evo["maxGen"], st.get("gen", 0))
                tag = f" · 🧬진화 {st['gen']}세대(부모 {st['parent']})" if st.get("origin") == "evo" else ""
                log(f"  채택 {sym}: {st['desc']} · 백테스트 거래당 {st['btEdge']:+.2f}% ({st['btTrades']}회){tag}")
        self.prune_rules(chosen)
        self.state["strategies"] = chosen
        for sym in list(self.state["positions"]):              # 보호 코인으로 바뀐 종목의 모의 보유는 정리
            if sym.replace("1000", "").replace("USDT", "") in cb.PROTECTED_COINS and not self.state["positions"][sym].get("live"):
                self.close_paper(sym, prices([sym]).get(sym, self.state["positions"][sym]["entry"]), "보호 코인 제외")
        self.state["lastSelect"] = int(time.time())
        save_state(self.state)
        log(f"전략 선정 완료: {len(chosen)}종목 (검증 중 유지 {kept})")
        self.push_phase("")

    def prune_rules(self, chosen):
        """한 번도 거래 안 한 채 버려진 규칙은 장부에서 지움 (장부가 수백 개로 불어나는 것 방지)"""
        live_rids = {v["rid"] for v in chosen.values()} | {p.get("rid") for p in self.state["positions"].values()}
        book = self.state["ruleBook"]
        for rid in [k for k, r in book.items() if r.get("n", 0) == 0 and r.get("status") == "testing" and k not in live_rids]:
            del book[rid]

    # --- 진화·확률 ---
    @staticmethod
    def rule_post(rb):
        """규칙의 '거래당 진짜 실력' 추정: (평균, 흩어짐, 이익일 확률). 거래가 적으면 0 쪽으로 당겨 봄"""
        n, s = rb.get("n", 0), rb.get("sumPct", 0.0)
        mean = s / (n + PRIOR_N)
        if n >= 3 and rb.get("sumSq") is not None and rb.get("sqN", 0) >= 3:
            m = n and s / n
            var = max(0.25, rb["sumSq"] / rb["sqN"] - m * m)
            sd = math.sqrt((var * n + PRIOR_SD ** 2 * PRIOR_N) / (n + PRIOR_N))
        else:
            sd = PRIOR_SD
        se = sd / math.sqrt(n + PRIOR_N)
        p = 0.5 * (1 + math.erf(mean / se / math.sqrt(2)))
        return mean, sd, p

    def refresh_pool(self):
        """유전자 풀 다시 짜기: 거래 3회 이상·추정 실력 플러스인 규칙의 조건 조합 (퇴출 규칙 제외), 실력 순 상위 40개"""
        genes = []
        for rb in self.state["ruleBook"].values():
            if not rb.get("spec") or rb.get("status") == "retired" or rb.get("n", 0) < GENE_MIN_N:
                continue
            mean, _, p = self.rule_post(rb)
            if mean <= 0:
                continue
            genes.append({"side": rb["side"], "spec": rb["spec"], "fit": round(mean, 4), "p": round(p, 3), "n": rb["n"],
                          "rid": rb["id"], "gen": rb.get("gen", 0), "symbol": rb.get("symbol", "").replace("USDT", "")})
        genes.sort(key=lambda g: -g["fit"])
        self.state["genePool"] = genes[:GENE_POOL_MAX]

    def evo_record(self, rb, pct):
        """진화 출신과 무작위 출신의 실제 성적을 따로 쌓고, 진화가 이기면 비율을 늘리고 지면 줄임"""
        evo = self.state["evo"]
        k = "evo" if rb.get("origin") == "evo" else "rand"
        evo[k]["n"] += 1
        evo[k]["sumPct"] = round(evo[k]["sumPct"] + pct, 4)
        e, r = evo["evo"], evo["rand"]
        if e["n"] >= EVO_JUDGE_N and r["n"] >= EVO_JUDGE_N:
            diff = e["sumPct"] / e["n"] - r["sumPct"] / r["n"]
            old = evo["share"]
            evo["share"] = round(min(EVO_SHARE_MAX, max(EVO_SHARE_MIN, old + max(-0.02, min(0.02, diff * 0.05)))), 3)
            if abs(evo["share"] - old) >= 0.01 and (e["n"] + r["n"]) % 10 == 0:
                log(f"[진화] 진화 출신 거래당 {e['sumPct'] / e['n']:+.2f}% vs 무작위 {r['sumPct'] / r['n']:+.2f}% "
                    f"→ 진화 비율 {old * 100:.0f}% → {evo['share'] * 100:.0f}%")

    def size_for(self, rb):
        """실주문 금액(USDT): 기본 × 확률 배수(0.5~2) × 판 크기(누적 실주문 이익 따라 0.5~2). 확률 40% 미만이면 0(모의로)"""
        _, _, p = self.rule_post(rb)
        if p < SIZE_SKIP_P:
            return 0.0, p
        conf = min(1.0, max(0.0, (p - 0.5) / 0.4))
        x = SIZE_MIN_X + (SIZE_MAX_X - SIZE_MIN_X) * conf if p >= 0.5 else SIZE_MIN_X
        bank = min(2.0, max(0.5, 1 + self.state["liveLedger"]["pnlUsdt"] / (self.order_usdt * BANK_X)))
        return round(min(self.order_usdt * x * bank, self.order_usdt * SIZE_MAX_X), 2), p   # 1회 최대 기본의 2배

    def side_ok(self, side, rb):
        """선택 설정 BINANCE_LIVE_BASIS=side: 그 방향(롱/숏) 모의 전체 성적이 충분하면 검증 중 규칙도 실주문"""
        if self.live_basis not in ("side", "all") or rb.get("status") == "retired":
            return False
        if rb.get("n", 0) and rb["sumPct"] / rb["n"] < 0:
            return False
        if self.live_basis == "all":                   # 롱·숏 모두: 퇴출·마이너스 규칙만 빼고 실주문
            return True
        s = self.state["ledger"].get(side) or {}
        return s.get("n", 0) >= SIDE_GATE_MIN_N and s["sumPct"] / s["n"] >= SIDE_GATE_MIN_AVG

    def skip_why(self, rb):
        """실주문 대신 모의로 간 이유를 실제 걸린 조건대로 나눠 적음(표시용, 판단은 side_ok 그대로)"""
        if self.live_basis in ("side", "all"):
            if rb.get("status") == "retired":
                return "퇴출 규칙"
            if rb.get("n", 0) and rb["sumPct"] / rb["n"] < 0:
                return "모의 평균 마이너스 규칙"
            if self.live_basis == "side":
                return "방향별 모의 성적 기준 미달"
        return "통과 전 규칙(모의 검증 중)"

    # --- 모의 진입·청산 ---
    def regime(self):
        """관찰 전용 시장 국면: BTC 최근 24시간 변화 → 상승/하락/횡보. 봉마다 한 번만 계산, 실패하면 None (매매에는 영향 없음)"""
        bar = int(time.time() // (TF * 60))
        if getattr(self, "_regime_bar", None) == bar:
            return self._regime
        self._regime_bar, self._regime, self._regime_chg = bar, None, None
        try:
            cl = klines("BTCUSDT", REGIME_BARS + 1)
            if len(cl) > REGIME_BARS:
                chg = (cl[-1]["trade_price"] / cl[-1 - REGIME_BARS]["trade_price"] - 1) * 100
                self._regime_chg = round(chg, 2)
                self._regime = "상승" if chg >= REGIME_UP else ("하락" if chg <= REGIME_DOWN else "횡보")
        except Exception as e:
            log(f"[안내] 시장 국면(BTC 24시간) 확인 실패 (매매는 그대로): {str(e)[:100]}")
        return self._regime

    def regime_record(self, kind, p, pct):
        """청산 1건을 국면×방향 표에 더함 (진입 때 국면을 모르는 포지션은 건너뜀)"""
        reg = p.get("regime")
        if not reg:
            return
        k = f"{kind}|{reg}|{p['side']}"
        d = self.state.setdefault("regimeStats", {}).setdefault(k, {"n": 0, "wins": 0, "sumPct": 0.0})
        d["n"] += 1
        d["wins"] += 1 if pct > 0 else 0
        d["sumPct"] = round(d["sumPct"] + pct, 4)

    def side_weak(self, side):
        """관찰 전용: 같은 방향의 최근 모의 청산 SW_LOOK회 평균. SW_MIN회 미만이면 None.
        진입·청산·손익·규칙 판정·실주문 판단에는 전혀 쓰지 않음"""
        try:
            xs = [t["pct"] for t in self.state.get("tradeLog") or []
                  if not t.get("live") and t.get("side") == side and t.get("pct") is not None][-SW_LOOK:]
            if len(xs) < SW_MIN:
                return None
            return round(sum(xs) / len(xs), 3)
        except Exception:
            return None

    def side_weak_mark(self, sym, avg):
        try:
            p = self.state["positions"].get(sym)
            if p is None or avg is None or "sideAvg" in p:
                return
            p["sideAvg"] = avg
            p["sideWeak"] = avg < 0
            if p["sideWeak"]:
                log(f"[방향 관찰] {sym} {'롱' if p['side'] == 'long' else '숏'}{' 실주문' if p.get('live') else ''} 진입 · "
                    f"같은 방향 최근 모의 평균 {avg:+.2f}% → '약한 방향' 표시 (관찰만, 매매는 그대로)")
        except Exception as e:
            log(f"[안내] 방향 관찰 표시 실패(매매엔 영향 없음): {str(e)[:120]}")

    def side_weak_record(self, kind, p, pct):
        """관찰 전용: 약한 방향 표시 거래 vs 보통 거래의 결과를 따로 누적"""
        try:
            if "sideWeak" not in p:
                return
            k = f"{kind}|{'weak' if p['sideWeak'] else 'ok'}|{p['side']}"
            d = self.state.setdefault("sideWeakStats", {}).setdefault(k, {"n": 0, "wins": 0, "sumPct": 0.0})
            d["n"] += 1
            d["wins"] += 1 if pct > 0 else 0
            d["sumPct"] = round(d["sumPct"] + pct, 4)
        except Exception as e:
            log(f"[안내] 방향 관찰 집계 실패(매매엔 영향 없음): {str(e)[:120]}")

    def p_calib_record(self, p, pct, usdt):
        """관찰 전용: 실주문 진입 때 '이익 확률(p)' 구간별 실제 결과와, 확률로 키운 금액이 고정 금액보다 나았는지 누적.
        p 가 실제 결과를 잘 맞히는지(확률이 높을수록 성적이 좋은지) 보는 용도. 금액·주문·판정에는 전혀 안 씀"""
        try:
            pv = p.get("p")
            if pv is None:
                return
            b = "lo" if pv < 0.6 else ("mid" if pv < 0.8 else "hi")
            d = self.state.setdefault("pCalib", {}).setdefault(b, {"n": 0, "wins": 0, "sumPct": 0.0, "sumUsdt": 0.0,
                                                                   "sumFlat": 0.0, "sumSize": 0.0})
            d["n"] += 1
            d["wins"] += 1 if pct > 0 else 0
            d["sumPct"] = round(d["sumPct"] + pct, 4)
            d["sumUsdt"] = round(d["sumUsdt"] + usdt, 4)
            d["sumFlat"] = round(d["sumFlat"] + self.order_usdt * pct / 100, 4)   # 같은 거래를 기본 금액 고정으로 했다면
            d["sumSize"] = round(d["sumSize"] + float(p.get("usdt") or 0), 2)
        except Exception as e:
            log(f"[안내] 확률 구간 관찰 집계 실패(매매엔 영향 없음): {str(e)[:120]}")

    def stop_gap_record(self, sym, p, pct, reason):
        """관찰 전용: 실주문이 손절로 끝났을 때 '규칙 손절가대로 나갔다면' 대비 얼마나 더 잃었는지(손절 넘침) 누적.
        누가 닫았는지(봇 손절 / 거래소 보호 손절)로 나눔. 급하게 튄 가격을 봇이 못 따라간 비용을 보는 용도. 매매엔 전혀 안 씀"""
        try:
            rs = str(reason or "")
            if "손절" not in rs or "실패" in rs:
                return
            who = "exchange" if "거래소" in rs else "bot"
            sl_pct = abs(p["sl"] / p["entry"] - 1) * 100
            expect = -sl_pct - 2 * FEE * 100          # 규칙 손절가에 딱 나갔을 때의 결과(수수료 포함)
            over = pct - expect                        # 마이너스일수록 손절가보다 더 밀려 나감
            d = self.state.setdefault("stopGap", {}).setdefault(who, {"n": 0, "sumOver": 0.0, "worst": 0.0, "sumUsdt": 0.0})
            d["n"] += 1
            d["sumOver"] = round(d["sumOver"] + over, 4)
            d["worst"] = round(min(d["worst"], over), 3)
            d["sumUsdt"] = round(d["sumUsdt"] + float(p.get("usdt") or 0) * over / 100, 4)   # 넘친 만큼의 USDT
            rec = self.state.setdefault("stopGapRecent", [])
            rec.append({"t": datetime.now(KST).strftime("%m-%d %H:%M"), "symbol": sym, "side": p["side"],
                        "who": who, "sl": round(-sl_pct, 2), "pct": round(pct, 3), "over": round(over, 3)})
            del rec[:-10]
        except Exception as e:
            log(f"[안내] 손절 넘침 관찰 집계 실패(매매엔 영향 없음): {str(e)[:120]}")

    def stop_gap_view(self):
        st = self.state.get("stopGap") or {}
        rows = [{"who": k, "n": v["n"], "avgOver": round(v["sumOver"] / v["n"], 3), "worst": v["worst"],
                 "usdt": round(v["sumUsdt"], 2)} for k, v in ((k, st.get(k)) for k in ("bot", "exchange")) if v and v.get("n")]
        return {"rows": rows, "buffer": EXCHANGE_STOP_BUFFER, "recent": (self.state.get("stopGapRecent") or [])[-5:]}

    def payoff_view(self):
        """관찰 전용: 최근 청산(tradeLog 최대 100건)의 손익비와 손익분기 승률. 읽기만 함 — 매매·판정·주문에는 전혀 안 씀"""
        try:
            rows = []
            log_all = self.state.get("tradeLog") or []
            # 넘겨받은 포지션(봇이 직접 연 게 아닌 것)은 진입가 기준이 달라 %가 크게 튈 수 있어 손익비 집계에서 뺌
            inh = lambda t: str(t.get("desc") or "").startswith("넘겨받은")
            excluded = sum(1 for t in log_all if t.get("pct") is not None and inh(t))
            for kind in ("live", "paper"):
                for side in ("all", "long", "short"):
                    xs = [t for t in log_all if t.get("pct") is not None and not inh(t)
                          and bool(t.get("live")) == (kind == "live") and (side == "all" or t.get("side") == side)]
                    if not xs:
                        continue
                    w = [t["pct"] for t in xs if t["pct"] > 0]
                    l = [-t["pct"] for t in xs if t["pct"] <= 0]
                    aw = sum(w) / len(w) if w else 0.0
                    al = sum(l) / len(l) if l else 0.0
                    row = {"kind": kind, "side": side, "n": len(xs), "wins": len(w),
                           "avgWin": round(aw, 3), "avgLoss": round(al, 3),
                           "ratio": round(aw / al, 2) if al > 0 else None,
                           "beWin": round(al / (aw + al), 3) if (aw + al) > 0 else None,
                           "winRate": round(len(w) / len(xs), 3),
                           "worst": round(min(t["pct"] for t in xs), 2)}
                    # 실주문은 진입 금액이 확률·판 크기로 달라지므로 실제 USDT 기준 손익비도 함께 (읽기 전용)
                    us = [float(t["usdt"]) for t in xs if t.get("usdt") is not None]
                    if kind == "live" and us:
                        wu = [u for u in us if u > 0]
                        lu = [-u for u in us if u <= 0]
                        awu = sum(wu) / len(wu) if wu else 0.0
                        alu = sum(lu) / len(lu) if lu else 0.0
                        row.update({"avgWinU": round(awu, 3), "avgLossU": round(alu, 3),
                                    "ratioU": round(awu / alu, 2) if alu > 0 else None,
                                    "beWinU": round(alu / (awu + alu), 3) if (awu + alu) > 0 else None,
                                    "sumU": round(sum(us), 2)})
                    rows.append(row)
            return {"rows": rows, "look": 100, "excluded": excluded}
        except Exception:
            return {"rows": [], "look": 100, "excluded": 0}

    def p_calib_view(self):
        st = self.state.get("pCalib") or {}
        rows = [{"band": k, "n": v["n"], "wins": v["wins"], "avgPct": round(v["sumPct"] / v["n"], 3),
                 "usdt": round(v["sumUsdt"], 2), "flat": round(v["sumFlat"], 2), "avgSize": round(v["sumSize"] / v["n"], 1)}
                for k, v in ((k, st.get(k)) for k in ("lo", "mid", "hi")) if v and v.get("n")]
        return {"rows": rows, "base": self.order_usdt,
                "open": sum(1 for q in self.state["positions"].values() if q.get("live") and q.get("p") is not None)}

    def side_weak_view(self):
        rows = [{"kind": k.split("|")[0], "flag": k.split("|")[1], "side": k.split("|")[2], "n": v["n"], "wins": v["wins"],
                 "avgPct": round(v["sumPct"] / v["n"], 3) if v["n"] else None}
                for k, v in sorted((self.state.get("sideWeakStats") or {}).items())]
        return {"look": SW_LOOK, "now": {"long": self.side_weak("long"), "short": self.side_weak("short")},
                "rows": rows, "open": sum(1 for q in self.state["positions"].values() if q.get("sideWeak"))}

    def funding_track(self):
        """관찰 전용 펀딩비: 보유 포지션마다 정산 시각(nextFundingTime)이 지나면 직전에 읽은 예상 펀딩비를 누적.
        롱은 비율만큼 내고(-), 숏은 받음(+). 진입가 기준 금액에 대한 % — 손익·규칙 판정·실주문 판단에는 전혀 안 씀"""
        pos = self.state["positions"]
        if not pos or time.time() - getattr(self, "_fund_at", 0) < FUNDING_CHECK_SEC:
            return
        self._fund_at = time.time()
        try:
            data = get("/fapi/v1/premiumIndex")
        except Exception as e:
            log(f"[안내] 펀딩비 확인 실패 (매매는 그대로): {str(e)[:100]}")
            return
        now_ms = int(time.time() * 1000)
        info = {d["symbol"]: d for d in data if isinstance(d, dict) and "symbol" in d}
        paid = []
        for sym, p in pos.items():
            d = info.get(sym)
            if not d:
                continue
            nft, rate = p.get("fundNext"), p.get("fundRate")
            if nft and rate is not None and now_ms >= nft:          # 정산이 지나감 → 직전 예상치로 정산된 것으로 봄
                f = rate * 100 * (-1 if p["side"] == "long" else 1)
                p["fundPct"] = round(p.get("fundPct", 0.0) + f, 4)
                p["fundN"] = p.get("fundN", 0) + 1
                paid.append(f"{sym.replace('USDT', '')} {'롱' if p['side'] == 'long' else '숏'} {f:+.3f}%")
            try:
                p["fundRate"] = float(d.get("lastFundingRate") or 0)
                p["fundNext"] = int(d.get("nextFundingTime") or 0) or None
            except (TypeError, ValueError):
                pass
        if paid:
            log("[펀딩비 관찰] 정산 반영(손익 계산엔 안 넣음): " + ", ".join(paid[:12]) + (" 외" if len(paid) > 12 else ""))

    def giveback_watch(self, sym, p, price, fav):
        """관찰 전용: 익절 쪽으로 GB_ARM 이상 갔다가 진입가까지 되밀린 첫 순간의 손익(수수료·밀림 포함)을 표시만 해 둠.
        청산·손익·판정·주문에는 전혀 쓰지 않음. 실패해도 매매는 그대로."""
        try:
            if p.get("gbPct") is not None:
                return
            tpm = abs(p["tp"] / p["entry"] - 1) * 100
            if tpm <= 0 or p.get("maxFav", 0.0) < GB_ARM * tpm or fav > GB_FLOOR:
                return
            long_ = p["side"] == "long"
            ex = price * (1 - SLIP) if long_ else price * (1 + SLIP)
            gross = (ex / p["entry"] - 1) if long_ else (p["entry"] / ex - 1)
            p["gbPct"] = round((gross - 2 * FEE) * 100, 4)
            log(f"[청산 관찰] {sym} {'롱' if long_ else '숏'}{' 실주문' if p.get('live') else ''} 익절까지 "
                f"{p['maxFav'] / tpm * 100:.0f}% 갔다가 진입가로 되밀림 → 여기서 본전 정리했다면 {p['gbPct']:+.2f}% "
                f"(관찰만, 실제 청산은 그대로)")
        except Exception as e:
            log(f"[안내] 되밀림 관찰 기록 실패(매매엔 영향 없음): {str(e)[:120]}")

    def giveback_record(self, kind, sym, p, pct):
        """관찰 전용: 되밀림 표시가 붙은 거래가 끝나면 '본전 정리했다면' vs 실제 손익 차이를 누적"""
        try:
            gb = p.get("gbPct")
            if gb is None:
                return
            d = self.state.setdefault("gbStats", {}).setdefault(kind, {"n": 0, "sumDiff": 0.0, "better": 0,
                                                                        "sumActual": 0.0, "sumGb": 0.0})
            diff = gb - pct
            d["n"] += 1
            d["sumDiff"] = round(d["sumDiff"] + diff, 4)
            d["better"] += 1 if diff > 0 else 0
            d["sumActual"] = round(d["sumActual"] + pct, 4)
            d["sumGb"] = round(d["sumGb"] + gb, 4)
            self.state["gbRecent"] = (self.state.get("gbRecent") or [])[-9:] + [
                {"symbol": sym, "side": p["side"], "kind": kind, "gb": round(gb, 3), "actual": round(pct, 3)}]
        except Exception as e:
            log(f"[안내] 되밀림 관찰 집계 실패(매매엔 영향 없음): {str(e)[:120]}")

    def giveback_view(self):
        rows = [{"kind": k, "n": v["n"], "better": v["better"],
                 "avgDiff": round(v["sumDiff"] / v["n"], 3) if v["n"] else None,
                 "avgActual": round(v["sumActual"] / v["n"], 3) if v["n"] else None,
                 "avgGb": round(v["sumGb"] / v["n"], 3) if v["n"] else None}
                for k, v in sorted((self.state.get("gbStats") or {}).items())]
        return {"arm": GB_ARM, "rows": rows, "recent": (self.state.get("gbRecent") or [])[-5:],
                "open": sum(1 for q in self.state["positions"].values() if q.get("gbPct") is not None)}

    def funding_record(self, kind, p, pct):
        """청산 1건의 누적 펀딩비를 방향별 표에 더함 (관찰 전용)"""
        k = f"{kind}|{p['side']}"
        d = self.state.setdefault("fundStats", {}).setdefault(k, {"n": 0, "sumPct": 0.0, "sumFund": 0.0, "paidN": 0})
        d["n"] += 1
        d["sumPct"] = round(d["sumPct"] + pct, 4)
        d["sumFund"] = round(d["sumFund"] + p.get("fundPct", 0.0), 4)
        d["paidN"] += p.get("fundN", 0)

    def events(self):
        """경제 일정표(bot/events.json, 업비트 봇과 같은 파일)를 30분마다 다시 받음. 실패하면 이전 값 (관찰 전용)"""
        if time.time() - getattr(self, "_events_at", 0) >= cb.EVENTS_REFRESH:
            self._events_at = time.time()
            try:
                res = requests.get(cb.EVENTS_URL, params={"t": int(time.time())}, timeout=10)
                if res.status_code == 200:
                    ev = res.json().get("events", [])
                    if isinstance(ev, list):
                        self.state["eventsCache"] = ev
            except Exception:
                pass
        return self.state.get("eventsCache") or []

    def event_now(self, ts=None):
        """관찰 전용: ts(기본 지금)가 일정(FOMC·CPI 등) 앞뒤 구간이면 (이름, 발표시각), 아니면 (None, None).
        진입·청산·금액·실주문 판단에는 전혀 쓰지 않음"""
        now = time.time() if ts is None else ts
        for e in self.events():
            try:
                at = datetime.fromisoformat(e["at"]).timestamp()
                if at - float(e.get("before_h", 2)) * 3600 <= now <= at + float(e.get("after_h", 2)) * 3600:
                    return e.get("name", "일정"), at
            except Exception:
                continue
        return None, None

    def event_mark(self, sym, name):
        try:
            p = self.state["positions"].get(sym)
            if p is None or "evt" in p:
                return
            p["evt"] = "in" if name else "out"
            if name:
                log(f"[일정 관찰] {sym} {'롱' if p['side'] == 'long' else '숏'}{' 실주문' if p.get('live') else ''} 진입 · "
                    f"{name} 앞뒤 구간 → '일정 구간 진입' 표시 (관찰만, 매매는 그대로)")
        except Exception as e:
            log(f"[안내] 일정 관찰 표시 실패(매매엔 영향 없음): {str(e)[:120]}")

    def event_pass(self):
        """관찰 전용: 발표 시각을 들고 넘긴 포지션에 '일정 넘김' 표시 (일정 구간 진입이 아닌 것만)"""
        try:
            now = time.time()
            ats = []
            for e in self.events():
                try:
                    ats.append(datetime.fromisoformat(e["at"]).timestamp())
                except Exception:
                    continue
            for sym, p in self.state["positions"].items():
                t0 = (p.get("at") or 0) / 1000
                if t0 <= 0:
                    continue
                if "evt" not in p:                       # 이 기능 전에 연 포지션: 진입 시각으로 표시를 채움
                    p["evt"] = "in" if self.event_now(t0)[0] else "out"
                if p.get("evt") == "in" or p.get("evtThru"):
                    continue
                if any(t0 < at <= now for at in ats):
                    p["evtThru"] = True
                    log(f"[일정 관찰] {sym} {'롱' if p['side'] == 'long' else '숏'}{' 실주문' if p.get('live') else ''} "
                        f"발표 시각을 보유한 채 넘김 → '일정 넘김' 표시 (관찰만)")
        except Exception as e:
            log(f"[안내] 일정 넘김 표시 실패(매매엔 영향 없음): {str(e)[:120]}")

    def event_record(self, kind, p, pct):
        """관찰 전용: 일정 구간 진입 / 발표 넘김 / 보통 거래 결과를 따로 누적 (표시가 없는 옛 포지션은 건너뜀)"""
        try:
            if "evt" not in p:
                return
            flag = "in" if p["evt"] == "in" else ("thru" if p.get("evtThru") else "out")
            k = f"{kind}|{flag}|{p['side']}"
            d = self.state.setdefault("eventStats", {}).setdefault(k, {"n": 0, "wins": 0, "sumPct": 0.0})
            d["n"] += 1
            d["wins"] += 1 if pct > 0 else 0
            d["sumPct"] = round(d["sumPct"] + pct, 4)
        except Exception as e:
            log(f"[안내] 일정 관찰 집계 실패(매매엔 영향 없음): {str(e)[:120]}")

    def event_view(self):
        try:
            name, at = self.event_now()
            nxt = None
            now = time.time()
            for e in self.events():
                try:
                    t = datetime.fromisoformat(e["at"]).timestamp()
                except Exception:
                    continue
                if t > now and (nxt is None or t < nxt[0]):
                    nxt = (t, e.get("name", "일정"))
            rows = [{"kind": k.split("|")[0], "flag": k.split("|")[1], "side": k.split("|")[2], "n": v["n"], "wins": v["wins"],
                     "avgPct": round(v["sumPct"] / v["n"], 3) if v["n"] else None}
                    for k, v in sorted((self.state.get("eventStats") or {}).items())]
            pos = self.state["positions"].values()
            return {"now": name, "nowAt": int(at * 1000) if at else None,
                    "next": {"name": nxt[1], "at": int(nxt[0] * 1000)} if nxt else None, "rows": rows,
                    "openIn": sum(1 for q in pos if q.get("evt") == "in"), "openThru": sum(1 for q in pos if q.get("evtThru"))}
        except Exception:
            return None

    # --- 손절 직후 재진입 관찰 (진입·청산·금액·실주문 판단에는 전혀 안 씀) ---
    REENTRY_H = 6                                        # 같은 종목·같은 방향이 손절된 뒤 이 시간 안에 다시 들어가면 '재진입'

    def reentry_recent(self, sym, side, ts=None):
        """관찰 전용: sym·side 가 ts(기본 지금) 전 REENTRY_H 시간 안에 손절됐으면 그 손절 시각(초), 아니면 None"""
        try:
            now = time.time() if ts is None else ts
            ls = self.state.get("lastStop")
            if ls is None:                               # 처음 켤 때: 최근 거래 기록의 손절로 채움
                ls = {}
                for x in self.state.get("tradeLog") or []:
                    if "손절" in str(x.get("exit", "")):
                        try:
                            t = datetime.strptime(f"{datetime.now(KST).year}-{x['t']}", "%Y-%m-%d %H:%M").replace(tzinfo=KST).timestamp()
                        except Exception:
                            continue
                        k = f"{x['symbol']}|{x['side']}"
                        ls[k] = max(ls.get(k, 0), t)
                self.state["lastStop"] = ls
            t = ls.get(f"{sym}|{side}")
            return t if t and 0 <= now - t <= self.REENTRY_H * 3600 else None
        except Exception:
            return None

    def reentry_mark(self, sym):
        try:
            p = self.state["positions"].get(sym)
            if p is None or "reEnt" in p:
                return
            t = self.reentry_recent(sym, p["side"], (p.get("at") or 0) / 1000 or None)
            p["reEnt"] = bool(t)
            if t:
                log(f"[재진입 관찰] {sym} {'롱' if p['side'] == 'long' else '숏'}{' 실주문' if p.get('live') else ''} 진입 · "
                    f"같은 방향 손절 {round((time.time() - t) / 60)}분 뒤 다시 들어감 → '손절 후 재진입' 표시 (관찰만, 매매는 그대로)")
        except Exception as e:
            log(f"[안내] 재진입 관찰 표시 실패(매매엔 영향 없음): {str(e)[:120]}")

    def reentry_record(self, kind, sym, p, pct, reason):
        """관찰 전용: 손절 후 재진입 / 보통 거래 결과를 따로 누적하고, 이번 청산이 손절이면 손절 시각을 남김"""
        try:
            if "손절" in str(reason or ""):
                self.reentry_recent(sym, p["side"])      # lastStop 초기화 보장
                self.state.setdefault("lastStop", {})[f"{sym}|{p['side']}"] = time.time()
            if "reEnt" not in p:                          # 이 기능 전에 연 포지션은 건너뜀
                return
            k = f"{kind}|{'re' if p['reEnt'] else 'normal'}|{p['side']}"
            d = self.state.setdefault("reentryStats", {}).setdefault(k, {"n": 0, "wins": 0, "sumPct": 0.0, "worst": 0.0})
            d["n"] += 1
            d["wins"] += 1 if pct > 0 else 0
            d["sumPct"] = round(d["sumPct"] + pct, 4)
            d["worst"] = round(min(d["worst"], pct), 3)
        except Exception as e:
            log(f"[안내] 재진입 관찰 집계 실패(매매엔 영향 없음): {str(e)[:120]}")

    CROWD_HI = 5                                         # 진입 때 같은 방향 실보유가 이 개수 이상이면 '몰림'

    def crowd_mark(self, sym):
        """관찰 전용: 진입 때 같은 방향으로 이미 들고 있던 다른 포지션 수(실주문은 실보유끼리, 모의는 모의끼리)를 표시.
        한쪽으로 몰렸을 때 성적이 나빠지는지(함께 손절되는지) 보는 용도. 진입·청산·금액·실주문 판단에는 전혀 안 씀"""
        try:
            p = self.state["positions"].get(sym)
            if p is None or "crowd" in p:
                return
            live = bool(p.get("live"))
            n = sum(1 for k, q in self.state["positions"].items()
                    if k != sym and q.get("side") == p["side"] and bool(q.get("live")) == live)
            p["crowd"] = n
            if n >= self.CROWD_HI:
                log(f"[몰림 관찰] {sym} {'롱' if p['side'] == 'long' else '숏'}{' 실주문' if live else ''} 진입 · "
                    f"같은 방향 {'실보유' if live else '모의 보유'} 이미 {n}개 → '한쪽 몰림' 표시 (관찰만, 매매는 그대로)")
        except Exception as e:
            log(f"[안내] 몰림 관찰 표시 실패(매매엔 영향 없음): {str(e)[:120]}")

    def crowd_record(self, kind, p, pct):
        """관찰 전용: 몰림(같은 방향 CROWD_HI개 이상) / 보통 진입의 결과를 따로 누적"""
        try:
            if "crowd" not in p:                          # 이 기능 전에 연 포지션은 건너뜀
                return
            k = f"{kind}|{'hi' if p['crowd'] >= self.CROWD_HI else 'lo'}|{p['side']}"
            d = self.state.setdefault("crowdStats", {}).setdefault(k, {"n": 0, "wins": 0, "sumPct": 0.0, "worst": 0.0})
            d["n"] += 1
            d["wins"] += 1 if pct > 0 else 0
            d["sumPct"] = round(d["sumPct"] + pct, 4)
            d["worst"] = round(min(d["worst"], pct), 3)
        except Exception as e:
            log(f"[안내] 몰림 관찰 집계 실패(매매엔 영향 없음): {str(e)[:120]}")

    def crowd_view(self):
        try:
            rows = [{"kind": k.split("|")[0], "flag": k.split("|")[1], "side": k.split("|")[2], "n": v["n"], "wins": v["wins"],
                     "avgPct": round(v["sumPct"] / v["n"], 3) if v["n"] else None, "worst": v.get("worst")}
                    for k, v in sorted((self.state.get("crowdStats") or {}).items())]
            pos = self.state["positions"].values()
            now = {s: sum(1 for q in pos if q.get("live") and q.get("side") == s) for s in ("long", "short")}
            return {"hi": self.CROWD_HI, "rows": rows, "liveNow": now}
        except Exception:
            return None

    def reentry_view(self):
        try:
            rows = [{"kind": k.split("|")[0], "flag": k.split("|")[1], "side": k.split("|")[2], "n": v["n"], "wins": v["wins"],
                     "avgPct": round(v["sumPct"] / v["n"], 3) if v["n"] else None, "worst": v.get("worst")}
                    for k, v in sorted((self.state.get("reentryStats") or {}).items())]
            return {"hours": self.REENTRY_H, "rows": rows,
                    "open": sum(1 for q in self.state["positions"].values() if q.get("reEnt"))}
        except Exception:
            return None

    def scan(self):
        st = self.state
        bar = int(time.time() // (TF * 60))
        if st.get("scannedBar") == bar or time.time() % (TF * 60) < 20:   # 새 봉이 닫힌 뒤 한 번만 확인
            return
        st["scannedBar"] = bar
        self.regime()                                    # 관찰 전용: 신호가 없어도 봉마다 국면을 계산해 대시보드에 지금 국면 표시 (매매 영향 없음)
        self.event_pass()                                # 관찰 전용: 발표 시각을 들고 넘긴 포지션 표시 (매매 영향 없음)
        n_paper = lambda: sum(1 for p in st["positions"].values() if not p.get("live"))
        n_live = lambda: sum(1 for p in st["positions"].values() if p.get("live"))
        # 모의 칸(10)과 실주문 칸(BINANCE_MAX_LIVE)을 따로 셈 — 모의가 칸을 다 채워도 실주문 신호는 계속 확인
        if n_paper() >= self.max_open and (not self.live_on() or n_live() >= self.max_live):
            return
        for sym, strat in list(st["strategies"].items()):
            if sym in st["positions"]:
                continue
            if n_paper() >= self.max_open and (not self.live_on() or n_live() >= self.max_live):
                break
            cl = klines(sym, 300)
            if len(cl) < 100:
                continue
            last_t = cl[-1]["t"]
            if st["lastBar"].get(sym) == last_t:
                continue
            st["lastBar"][sym] = last_t
            ser = series_of(cl if strat["side"] == "long" else inverted(cl))
            if not ser.signal(strat["spec"])[-1]:
                continue
            px = prices([sym]).get(sym)
            if not px:
                continue
            reg = self.regime()                          # 관찰 전용 기록 (진입 판단에는 안 씀)
            sw = self.side_weak(strat["side"])           # 관찰 전용: 약한 방향 표시 (진입 판단에는 안 씀)
            evn, _ = self.event_now()                    # 관찰 전용: 경제 일정 구간 표시 (진입 판단에는 안 씀)
            usdt = self.live_ok(strat)
            if usdt and self.open_live(sym, strat, px, usdt):
                if sym in st["positions"] and reg:
                    st["positions"][sym].setdefault("regime", reg)
                self.side_weak_mark(sym, sw)
                self.event_mark(sym, evn)
                self.reentry_mark(sym)
                self.crowd_mark(sym)
                continue
            if n_paper() < self.max_open:
                self.open_paper(sym, strat, px)
                if sym in st["positions"] and reg:
                    st["positions"][sym].setdefault("regime", reg)
                self.side_weak_mark(sym, sw)
                self.event_mark(sym, evn)
                self.reentry_mark(sym)
                self.crowd_mark(sym)
        save_state(st)

    # --- 실주문 ---
    def live_day(self):
        ll = self.state["liveLedger"]
        d = cb.today_key()
        if ll["day"] != d:
            ll.update({"day": d, "dayPnl": 0.0, "dayTrades": 0})
        m = datetime.now(KST).strftime("%Y-%m")
        if ll.get("month") != m:
            ll.update({"month": m, "monthPnl": 0.0, "monthTrades": 0})
        ll.setdefault("peak", max(0.0, ll.get("pnlUsdt", 0.0)))
        return ll

    def owner_paused(self):
        """업비트 봇이 대시보드에서 일시정지 상태면 바이낸스 실주문도 새로 하지 않음"""
        try:
            h = self.cloud._auth()
            r = requests.get(f"{cb.FS_BASE}/users/{self.cloud.uid}/status/current", headers=h, timeout=10)
            f = r.json().get("fields", {}) if r.status_code == 200 else {}
            return cb.from_fs(f.get("botState", {"stringValue": ""})) == "paused"
        except Exception:
            return False

    def live_skip(self, why):
        """신호가 나왔는데 실주문 대신 모의로 간 이유를 세어 둠(대시보드 '실주문 안 한 이유' 표시용, 판단에는 안 씀)"""
        sk = self.state.setdefault("liveSkip", {})
        d = cb.today_key()
        if sk.get("day") != d:
            sk.clear()
            sk.update({"day": d, "counts": {}})
        sk["counts"][why] = sk["counts"].get(why, 0) + 1
        sk["last"] = why
        sk["lastAt"] = int(time.time() * 1000)
        return 0.0

    def live_on(self):
        """새 실주문을 내도 되는 상태(설정 live + 계좌 연결 정상)"""
        return bool(self.live and getattr(self, "live_new", True))

    def live_ok(self, strat):
        """실주문 해도 되면 금액(USDT), 아니면 0 — 돈 지키기 조건을 모두 통과해야 함"""
        if not self.live_on():
            return self.live_skip("실주문 꺼짐(설정 paper·키 확인 실패)")
        rb = self.state["ruleBook"].get(strat["rid"]) or {}
        if rb.get("status") != "proven" and not self.side_ok(strat["side"], rb):
            return self.live_skip(self.skip_why(rb))
        g, now = self.state["guard"], time.time()
        if g.get("blockUntil", 0) > now:
            return self.live_skip("보호 손절 주문 실패 후 쉬는 중")
        if g.get("restUntil", 0) > now:
            return self.live_skip(f"연속 {LOSS_STREAK_MAX}번 손실 후 쉬는 중")
        ll = self.live_day()
        if ll["monthPnl"] <= -self.month_loss:
            return self.live_skip("월 손실 한도 도달")
        if ll["dayPnl"] <= -self.daily_loss:
            return self.live_skip("하루 손실 한도 도달")
        if sum(1 for p in self.state["positions"].values() if p.get("live")) >= self.max_live:
            return self.live_skip("동시 실보유 최대")
        usdt, p = self.size_for(rb)
        if not usdt:
            return self.live_skip("이익 확률 낮은 규칙(40% 미만)")
        strat["_p"] = p
        return usdt

    def open_live(self, sym, strat, px, usdt):
        """실주문 진입 + 거래소 보호 손절. 성공하면 True, 실주문을 못 하면 False(→ 모의로)"""
        side = strat["side"]
        long_ = side == "long"
        try:
            if not self.live.filters:
                self.live.sync()
                self.live.load_filters()
            qty, need = self.live.qty_for(sym, usdt, px)
            if not qty and need:
                # 확률 배수로 줄인 금액이 최소 주문보다 작으면 최소 수량(수량 단위 올림)으로 — 내림 때문에 다시 미달되던 문제 수정
                mq, mv = self.live.min_qty(sym, px)
                if mq > 0 and mv <= self.order_usdt * SIZE_MAX_X:
                    qty, usdt = mq, mv
                else:
                    need = mv
            if not qty:
                log(f"[실주문 건너뜀] {sym} 최소 주문 {need:.0f} USDT > 허용 {self.order_usdt * SIZE_MAX_X:.0f} USDT → 모의로 처리")
                self.live_skip("최소 주문 금액 부족")
                return False
            self.live.prepare(sym)
            avg, filled = self.live.market(sym, "BUY" if long_ else "SELL", qty)
        except Exception as e:
            log(f"[실주문 실패] {sym}: {e} → 모의로 처리")
            # 바이낸스 -2019(증거금 부족)은 '주문 오류'와 따로 세어 둠 — 동시 실보유·금액이 잔고를 넘는지 대시보드에서 바로 보이게(판단에는 안 씀)
            self.live_skip("증거금 부족(가용 잔고 모자람)" if "-2019" in str(e) else "주문 오류")
            return False
        if filled <= 0 or avg <= 0:
            log(f"[실주문 확인 실패] {sym} 체결 수량 0 → 모의로 처리")
            self.live_skip("체결 확인 실패")
            return False
        sp = strat["spec"]
        cost = 2 * FEE * 100
        mv = sp.get("tp", 0.5) + cost
        tp = avg * (1 + mv / 100) if long_ else avg * (1 - mv / 100)
        sl = avg * (1 + sp["sl"] / 100) if long_ else avg * (1 - sp["sl"] / 100)
        p = {"side": side, "entry": avg, "tp": tp, "sl": sl, "usdt": filled * avg, "qty": filled,
             "live": True, "at": int(time.time() * 1000), "deadline": int(time.time()) + sp["hold"] * TF * 60,
             "rid": strat["rid"], "desc": strat["desc"], "maxFav": 0.0, "p": round(strat.get("_p", 0.5), 3),
             "sigPx": px}
        self.state["positions"][sym] = p
        save_state(self.state)
        log(f"★[실주문 진입] {sym} {'롱' if long_ else '숏'} {filled} ({filled * avg:.1f} USDT · 이익 확률 {p['p'] * 100:.0f}%) "
            f"@ {avg:.6g} · 익절 {tp:.6g} · 손절 {sl:.6g} · {strat['desc']}")
        if not self.arm_stop(sym, p):
            self.state["guard"].update({"blockUntil": time.time() + STOP_FAIL_BLOCK_HOURS * 3600,
                                        "blockWhy": "보호 손절 주문 실패"})
            log(f"[돈 지키기] {sym} 거래소 보호 손절을 못 걸어서 바로 정리 → {STOP_FAIL_BLOCK_HOURS}시간 실주문 쉼")
            self.close_live(sym, px, "보호 손절 실패로 즉시 정리", record=False)
        return True

    def arm_stop(self, sym, p):
        """거래소에 보호 손절 걸기 (봇 손절가보다 0.5%p 바깥). PC·봇이 멈춰도 거래소가 손실을 끊어 줌"""
        long_ = p["side"] == "long"
        move = abs((p["sl"] / p["entry"] - 1) * 100) + EXCHANGE_STOP_BUFFER
        trig = p["entry"] * (1 - move / 100) if long_ else p["entry"] * (1 + move / 100)
        trig = self.live.round_price(sym, trig, up=not long_)
        try:
            p["stopId"] = self.live.stop_order(sym, "SELL" if long_ else "BUY", trig)
            p["stopPx"] = trig
            save_state(self.state)
            log(f"[돈 지키기] {sym} 거래소 보호 손절 걸림 @ {trig:.6g} (-{move:.1f}%)")
            return True
        except Exception as e:
            log(f"[경고] {sym} 거래소 보호 손절 실패: {str(e)[:160]}")
            return False

    def slip_record(self, sym, p, exit_px):
        """관찰 전용: 실주문 체결가가 신호 가격보다 얼마나 불리했는지(+면 불리) 누적.
        모의는 한쪽 SLIP(0.02%)만 가정하므로, 실제 밀림이 더 크면 모의 성적이 실주문보다 좋게 보임.
        손익·판정·주문에는 전혀 쓰지 않음. 실패해도 매매는 그대로."""
        try:
            long_ = p["side"] == "long"
            st = self.state.setdefault("liveSlip", {"entryN": 0, "entrySum": 0.0, "entryWorse": 0,
                                                    "exitN": 0, "exitSum": 0.0, "exitWorse": 0, "recent": []})
            ent = ext = None
            sig, avg = p.get("sigPx"), p.get("entry")
            if sig and avg:
                ent = ((avg / sig - 1) if long_ else (sig / avg - 1)) * 100
                st["entryN"] += 1; st["entrySum"] += ent; st["entryWorse"] += 1 if ent > 0 else 0
            xs = p.pop("exitSigPx", None)
            if xs and exit_px:
                ext = ((xs / exit_px - 1) if long_ else (exit_px / xs - 1)) * 100
                st["exitN"] += 1; st["exitSum"] += ext; st["exitWorse"] += 1 if ext > 0 else 0
            if ent is not None or ext is not None:
                st["recent"] = (st.get("recent") or [])[-9:] + [{"symbol": sym, "side": p["side"],
                                "entry": None if ent is None else round(ent, 3), "exit": None if ext is None else round(ext, 3)}]
        except Exception as e:
            log(f"[안내] 체결 밀림 기록 실패(매매엔 영향 없음): {str(e)[:120]}")

    def book_live(self, sym, p, exit_px, reason, record=True):
        """실주문 1건 정리 결과를 장부에 반영 (봇이 닫았든 거래소 보호 손절이 닫았든)"""
        long_ = p["side"] == "long"
        self.slip_record(sym, p, exit_px)
        gross = (exit_px / p["entry"] - 1) if long_ else (p["entry"] / exit_px - 1)
        pct = (gross - 2 * FEE) * 100
        usdt = p["usdt"] * pct / 100
        ll = self.live_day()
        ll["trades"] += 1
        ll["wins"] += 1 if pct > 0 else 0
        ll["pnlUsdt"] = round(ll["pnlUsdt"] + usdt, 4)
        ll["dayPnl"] = round(ll["dayPnl"] + usdt, 4)
        ll["dayTrades"] += 1
        ll["monthPnl"] = round(ll["monthPnl"] + usdt, 4)
        ll["monthTrades"] += 1
        ll["peak"] = max(ll.get("peak", 0.0), ll["pnlUsdt"])
        self.state["positions"].pop(sym, None)
        if record:
            self.rule_record(p["rid"], pct)
        self.regime_record("live", p, pct)
        self.funding_record("live", p, pct)
        self.giveback_record("live", sym, p, pct)
        self.side_weak_record("live", p, pct)
        self.event_record("live", p, pct)
        self.reentry_record("live", sym, p, pct, reason)
        self.crowd_record("live", p, pct)
        self.p_calib_record(p, pct, usdt)
        self.stop_gap_record(sym, p, pct, reason)
        g = self.state["guard"]
        g["streak"] = g.get("streak", 0) + 1 if pct <= 0 else 0
        rest = ""
        if g["streak"] >= LOSS_STREAK_MAX:
            g["restUntil"] = time.time() + LOSS_STREAK_REST_HOURS * 3600
            g["restStart"] = int(time.time() * 1000)
            g["streak"] = 0
            rest = f" · 연속 {LOSS_STREAK_MAX}번 손실 → {LOSS_STREAK_REST_HOURS}시간 실주문 쉼 (대시보드에서 승인하면 바로 재개)"
        tl = self.state["tradeLog"]
        tl.append({"t": datetime.now(KST).strftime("%m-%d %H:%M"), "symbol": sym, "side": p["side"], "pct": round(pct, 3),
                   "usdt": round(usdt, 3), "exit": reason, "desc": p["desc"], "live": True, "regime": p.get("regime"),
                   "heldMin": round((time.time() * 1000 - p["at"]) / 60000), "maxFav": round(p.get("maxFav", 0), 2),
                   "fund": round(p.get("fundPct", 0.0), 4), "p": p.get("p"), "size": round(p.get("usdt", 0.0), 2)})
        del tl[:-100]
        save_state(self.state)
        log(f"★[실주문 청산] {sym} {'롱' if long_ else '숏'} {pct:+.2f}% ({usdt:+.2f} USDT, {reason}) · "
            f"오늘 실주문 {ll['dayTrades']}회 {ll['dayPnl']:+.2f} USDT · 이번 달 {ll['monthPnl']:+.2f} USDT"
            + (" · 하루 손실 한도 도달 → 9시까지 실주문 중지" if ll["dayPnl"] <= -self.daily_loss else "")
            + (" · 월 손실 한도 도달 → 다음 달까지 실주문 중지" if ll["monthPnl"] <= -self.month_loss else "") + rest)

    def close_live(self, sym, price, reason, record=True):
        p = self.state["positions"][sym]
        long_ = p["side"] == "long"
        try:
            avg, filled = self.live.market(sym, "SELL" if long_ else "BUY", p["qty"], reduce=True)
        except Exception as e:
            if "-2022" in str(e):
                # 코드 -2022(ReduceOnly 거절) = 대부분 거래소 보호 손절이 이미 체결돼 포지션이 없는 경우.
                # 예전엔 20초마다 같은 실패를 되풀이하다 1분 간격 확인 2번(약 80초) 뒤에야 장부에 반영했음.
                # 이제 거절을 '없어진 것 같음' 1번으로 세고 바로 거래소 포지션을 확인 → 거기서도 없으면 즉시 반영.
                # 거래소에 포지션이 남아 있으면 지금처럼 다음 확인 때 다시 청산 시도(보호 손절은 그대로).
                if not p.get("rejN"):
                    log(f"[실주문 청산 실패] {sym}: {e} — 거래소 보호 손절이 먼저 체결됐을 수 있어 바로 포지션 확인")
                p["rejN"] = p.get("rejN", 0) + 1
                p["missing"] = max(p.get("missing", 0), 1)
                self.check_exchange(force=True)
                return
            log(f"[실주문 청산 실패] {sym}: {e} — 다음 확인 때 다시 시도 (거래소 보호 손절은 그대로 걸려 있음)")
            return
        self.live.cancel_stops(sym)
        if avg:
            p["exitSigPx"] = price                                  # 관찰용: 청산 판단 때 가격(체결 밀림 비교)
        self.book_live(sym, p, avg or price, reason, record)

    def dash_fields(self):
        """대시보드 버튼 값(binanceTestOrderAt·binanceResumeAt) 읽기. 쉼 해제·테스트 주문 확인이 같은 문서를 같이 씀(1분 안엔 다시 안 읽음).
        예전엔 30초마다 두 문서를 읽어 하루 5,700회 넘게 읽기 한도를 씀 → status/settings 1개만, 예비 문서는 가끔만"""
        now = time.time()
        if now - getattr(self, "_dash_at", 0) < 50 and getattr(self, "_dash_cache", None) is not None:
            return self._dash_cache
        if now < getattr(self, "_dash_backoff", 0):
            return None
        self._dash_n = getattr(self, "_dash_n", 0) + 1
        urls = [f"{cb.FS_BASE}/users/{self.cloud.uid}/status/settings"]
        if self._dash_n % DASH_READ_FALLBACK_EVERY == 1:
            urls.append(f"{cb.FS_BASE}/users/{self.cloud.uid}")
        out = {}
        h = self.cloud._auth()
        for url in urls:
            r = requests.get(url, headers=h, timeout=10)
            if r.status_code == 429:
                self._dash_backoff = now + DASH_READ_BACKOFF_SEC
                self.state["readLimitedUntil"] = int((now + DASH_READ_BACKOFF_SEC) * 1000)
                log(f"[경고] 웹페이지 읽기 한도 초과(429) → 대시보드 버튼(테스트 실주문·쉼 해제) 확인을 {DASH_READ_BACKOFF_SEC // 60}분 쉼 (매매·보유 관리는 계속)")
                self.upload(force=True)
                return None
            if r.status_code != 200:
                continue
            for k in ("binanceTestOrderAt", "binanceResumeAt"):
                v = r.json().get("fields", {}).get(k)
                if v:
                    out[k] = max(out.get(k, 0), int(float(cb.from_fs(v) or 0)))
        self.state.pop("readLimitedUntil", None)
        self._dash_at, self._dash_cache = now, out
        return out

    def check_resume(self):
        """연속 손실로 쉬는 동안만 1분마다: 대시보드에서 '쉼 해제 승인'을 눌렀으면 바로 다시 실주문
        (보호 손절 실패 쉼·하루·월 손실 한도는 승인으로 풀지 않음)"""
        g = self.state["guard"]
        if g.get("restUntil", 0) <= time.time() or time.time() - getattr(self, "_last_resume_check", 0) < RESUME_CHECK_SEC:
            return
        self._last_resume_check = time.time()
        try:
            f = self.dash_fields()
        except Exception as e:
            log(f"[경고] 쉼 해제 승인 확인 실패(다음에 다시): {str(e)[:100]}")
            return
        if f is None:
            return
        at = f.get("binanceResumeAt", 0)
        if at > g.get("restStart", 0):
            g.update({"restUntil": 0, "streak": 0, "resumedAt": at})
            save_state(self.state)
            log("[돈 지키기] 대시보드에서 쉼 해제 승인 → 연속 손실 쉼을 풀고 바로 실주문 재개 (하루·월 손실 한도는 그대로)")
            self.upload(force=True)

    def check_test_order(self):
        """대시보드 '테스트 실주문' 버튼: 사용자가 누른 시각(binanceTestOrderAt)이 새로 생기면 한 번만,
        거래대금 1위 종목(보호 코인 제외, 이미 보유 중이거나 최소 주문 금액이 너무 크면 다음 순위)에 숏으로 BINANCE_ORDER_USDT 만큼 실주문.
        익절 +1% / 손절 -1% / 최대 4시간, 거래소 보호 손절도 평소처럼 걺. 하루·월 손실 한도와 쉼 상태는 그대로 지킴"""
        if time.time() - getattr(self, "_last_test_check", 0) < TEST_CHECK_SEC:
            return
        self._last_test_check = time.time()
        try:
            f = self.dash_fields()
        except Exception as e:
            log(f"[경고] 테스트 실주문 요청 확인 실패(다음에 다시): {str(e)[:100]}")
            return
        if f is None:
            return
        at = f.get("binanceTestOrderAt", 0)
        to = self.state.setdefault("testOrder", {})
        if not at or at <= to.get("at", 0):
            return

        def done(status, msg):
            self.state["testOrder"] = {"at": at, "status": status, "msg": msg, "doneAt": int(time.time() * 1000)}
            save_state(self.state)
            log(f"[테스트 실주문] {msg}")
            self.upload(force=True)

        if time.time() - at / 1000 > TEST_MAX_AGE_SEC:
            return done("expired", f"누른 지 {TEST_MAX_AGE_SEC // 60}분이 지난 요청이라 실행하지 않음 · 다시 눌러 주세요")
        if not self.live_on():
            return done("skipped", "실주문이 꺼져 있어 실행하지 않음 (bot_config.env BINANCE_TRADE_MODE=live·키 확인)")
        g, now = self.state["guard"], time.time()
        ll = self.live_day()
        if g.get("blockUntil", 0) > now:
            return done("skipped", "보호 손절 실패 후 쉬는 중이라 실행하지 않음")
        if ll["monthPnl"] <= -self.month_loss or ll["dayPnl"] <= -self.daily_loss:
            return done("skipped", "하루 또는 월 손실 한도에 닿아 실행하지 않음")
        skipped = []
        try:
            if not self.live.filters:
                self.live.sync()
                self.live.load_filters()
            cap = self.order_usdt * SIZE_MAX_X                   # 평소 실주문과 같은 최대 금액 안에서만
            sym = None
            for s_ in top_symbols(TOP_N):
                if s_ in self.state["positions"]:
                    continue
                need = (self.live.filters.get(s_) or {}).get("minNotional", 5)
                if need * 1.05 > cap:                            # 예: BTC 는 최소 주문이 커서 기본 금액으로는 못 삼
                    skipped.append(f"{s_.replace('USDT', '')} 최소 {need:g} USDT")
                    continue
                sym = s_
                break
            px = prices([sym]).get(sym) if sym else None
        except Exception as e:
            return done("failed", f"시세 확인 실패: {str(e)[:100]}")
        note = f" (건너뜀: {', '.join(skipped[:3])})" if skipped else ""
        if not sym or not px:
            return done("failed", "주문할 종목을 찾지 못함" + note)
        strat = {"side": "short", "rid": "manual-test", "_p": 0.5,
                 "desc": f"숏 테스트 실주문 (익절 +{TEST_TP:g}% · 손절 {TEST_SL:g}%)",
                 "spec": {"tp": TEST_TP, "sl": TEST_SL, "hold": TEST_HOLD_BARS}}
        log(f"[테스트 실주문] 대시보드 요청 받음 → {sym} 숏 {self.order_usdt:.0f} USDT 시도{note}")
        self.open_live(sym, strat, px, self.order_usdt)
        p = self.state["positions"].get(sym) or {}
        if p.get("live") and p.get("rid") == "manual-test":
            done("opened", f"{sym} 숏 진입 {p['qty']} @ {p['entry']:.6g} · 익절 {p['tp']:.6g} · 손절 {p['sl']:.6g}"
                 + (f" · 거래소 보호 손절 {p['stopPx']:.6g}" if p.get("stopPx") else "") + note)
        else:
            done("failed", f"{sym} 주문이 체결되지 않음 · 마지막 기록: {ACT[-1][15:] if ACT else '-'}")

    def check_exchange(self, force=False):
        """실보유가 거래소에서 이미 정리됐는지(보호 손절 체결·직접 정리) 1분마다 확인. 두 번 연속 없을 때만 정리로 봄"""
        live_pos = {s: p for s, p in self.state["positions"].items() if p.get("live")}
        if not self.live or not live_pos or (not force and time.time() - self._last_exchange_check < EXCHANGE_CHECK_SEC):
            return
        self._last_exchange_check = time.time()
        try:
            ex = self.live.positions()
        except Exception as e:
            log(f"[경고] 거래소 포지션 확인 실패(다음에 다시): {str(e)[:120]}")
            return
        for sym, p in live_pos.items():
            if sym in ex:
                p.pop("missing", None)
                continue
            p["missing"] = p.get("missing", 0) + 1
            if p["missing"] < 2:
                continue
            exit_px, why = self.exchange_exit_px(sym, p)
            log(f"[돈 지키기] {sym} 실보유가 거래소에서 정리됨(보호 손절 체결 또는 직접 정리) → "
                + ("실제 체결가" if why == "실제 체결가" else "손절가") + f" 기준으로 장부 반영 ({exit_px:.6g})")
            self.live.cancel_stops(sym)
            self.book_live(sym, p, exit_px, f"거래소 보호 손절({why})")
        save_state(self.state)

    def exchange_exit_px(self, sym, p):
        """거래소에서 이미 닫힌 실보유의 청산가: 실제 체결 내역을 먼저 보고, 못 찾으면 예전처럼 거래소 보호 손절가로 추정.
        조회 실패해도 장부 반영은 그대로 진행(손절가 추정)"""
        est = p.get("stopPx") or p["sl"]
        try:
            real = self.live.close_fill(sym, p["side"] == "long", p.get("at"), float(p.get("qty") or 0))
            if real and real > 0:
                return real, "실제 체결가"
        except Exception as e:
            log(f"[안내] {sym} 실제 청산 체결가 조회 실패 → 손절가로 추정 반영: {str(e)[:120]}")
        return est, "추정가"

    def reconcile(self):
        """시작할 때: 장부의 실보유와 거래소 실제 포지션 맞춰 보기 (모르는 포지션은 건드리지 않음) + 보호 손절 확인"""
        if not self.live:
            return
        try:
            self.live.sync()
            self.live.load_filters()
            ex = self.live.positions()
        except Exception as e:
            log(f"[경고] 바이낸스 실계좌 확인 실패: {e} — 키·IP·선물 허용 확인 필요. 실주문은 계속 시도하지 않고 모의로 처리")
            self.live = None
            return
        for sym, p in list(self.state["positions"].items()):
            if p.get("live") and sym not in ex:
                log(f"[경고] {sym} 실보유가 거래소에 없음(꺼져 있는 동안 보호 손절 체결 또는 직접 정리) → 손절가 기준으로 장부 반영")
                self.live.cancel_stops(sym)
                exit_px, why = self.exchange_exit_px(sym, p)
                self.book_live(sym, p, exit_px, f"꺼져 있는 동안 정리됨({why})")
            elif p.get("live") and not p.get("stopId"):
                self.arm_stop(sym, p)                           # 이번 버전 전에 들어간 실보유에도 보호 손절을 걸어 둠
        mine = {s for s, p in self.state["positions"].items() if p.get("live")}
        foreign = []
        for sym, amt in ex.items():
            if sym not in mine:
                log(f"[안내] 봇이 열지 않은 포지션 {sym} {amt} — 봇은 건드리지 않음")
                foreign.append({"symbol": sym, "amt": amt})
        # 대시보드 경고용(표시만): 봇이 관리하지 않는 실포지션 — 손절·하루 손실 한도 밖에 있음
        self.state["foreign"] = {"at": int(time.time() * 1000), "list": foreign}
        save_state(self.state)
        basis = {"side": f"통과 규칙 + 방향별 모의 {SIDE_GATE_MIN_N}회·거래당 +{SIDE_GATE_MIN_AVG:.2f}% 이상인 쪽",
                 "all": "롱·숏 모두 (퇴출·평균 마이너스 규칙 제외)"}.get(self.live_basis, "통과 규칙만")
        basis += " · 업비트 봇과 별개 운용"
        if not self.live_on():
            n = sum(1 for p in self.state["positions"].values() if p.get("live"))
            log(f"바이낸스 실계좌 연결됨 · 설정이 모의라 새 실주문은 안 함 · 남은 실보유 {n}개만 평소대로 정리(거래소 보호 손절 유지)")
            return
        log(f"바이낸스 실계좌 연결됨 · 실주문 켜짐({basis}) · 기본 {self.order_usdt:.0f} USDT(확률·판 크기로 "
            f"{SIZE_MIN_X * 0.5:g}~{SIZE_MAX_X:g}배) · 하루 손실 한도 {self.daily_loss:.0f} · 월 {self.month_loss:.0f} USDT · "
            f"동시 실보유 최대 {self.max_live} · 거래소 보호 손절 켜짐")

    def open_paper(self, sym, strat, px):
        side, sp = strat["side"], strat["spec"]
        fill = px * (1 + SLIP) if side == "long" else px * (1 - SLIP)
        tp_pct, sl_pct = sp.get("tp", 0.5), sp["sl"]
        # 순익 기준 목표 → 가격 움직임 기준으로 환산 (왕복 수수료·밀림 포함)
        cost = 2 * (FEE + SLIP) * 100
        move_tp, move_sl = tp_pct + cost, sl_pct
        if side == "long":
            tp, sl = fill * (1 + move_tp / 100), fill * (1 + move_sl / 100)
        else:
            tp, sl = fill * (1 - move_tp / 100), fill * (1 - move_sl / 100)
        self.state["positions"][sym] = {"side": side, "entry": fill, "tp": tp, "sl": sl, "usdt": PAPER_USDT,
                                        "at": int(time.time() * 1000), "deadline": int(time.time()) + sp["hold"] * TF * 60,
                                        "rid": strat["rid"], "desc": strat["desc"], "maxFav": 0.0}
        log(f"[모의 진입] {sym} {'롱' if side == 'long' else '숏'} {PAPER_USDT:.0f} USDT @ {fill:.6g} · "
            f"익절 {tp:.6g} · 손절 {sl:.6g} · {strat['desc']}")

    def manage(self):
        pos = self.state["positions"]
        if not pos:
            return
        px = prices(list(pos.keys()))
        for sym, p in list(pos.items()):
            price = px.get(sym)
            if not price or sym not in pos:                  # 같은 순회 중 거래소 확인으로 이미 장부 반영된 종목은 건너뜀
                continue
            long_ = p["side"] == "long"
            fav = (price / p["entry"] - 1) * 100 * (1 if long_ else -1)
            p["maxFav"] = max(p.get("maxFav", 0.0), fav)
            self.giveback_watch(sym, p, price, fav)
            hit_tp = price >= p["tp"] if long_ else price <= p["tp"]
            hit_sl = price <= p["sl"] if long_ else price >= p["sl"]
            reason = "익절" if hit_tp else ("손절" if hit_sl else ("보유시간 초과" if time.time() >= p["deadline"] else None))
            if reason:
                if p.get("live"):
                    if self.live:
                        self.close_live(sym, price, reason)
                    else:
                        log(f"[경고] {sym} 실보유 청산 시점이지만 실계좌 연결이 꺼져 있음 → 바이낸스 앱에서 직접 정리 필요")
                else:
                    self.close_paper(sym, price, reason)
        save_state(self.state)

    def close_paper(self, sym, price, reason):
        p = self.state["positions"].pop(sym)
        long_ = p["side"] == "long"
        exit_px = price * (1 - SLIP) if long_ else price * (1 + SLIP)
        gross = (exit_px / p["entry"] - 1) if long_ else (p["entry"] / exit_px - 1)
        pct = (gross - 2 * FEE) * 100
        usdt = p["usdt"] * pct / 100
        lg = self.state["ledger"]
        lg["trades"] += 1
        lg["wins"] += 1 if pct > 0 else 0
        lg["pnlUsdt"] = round(lg["pnlUsdt"] + usdt, 4)
        lg["sumPct"] = round(lg["sumPct"] + pct, 4)
        lg[p["side"]]["n"] += 1
        lg[p["side"]]["sumPct"] = round(lg[p["side"]]["sumPct"] + pct, 4)
        self.rule_record(p["rid"], pct)
        self.regime_record("paper", p, pct)
        self.funding_record("paper", p, pct)
        self.giveback_record("paper", sym, p, pct)
        self.side_weak_record("paper", p, pct)
        self.event_record("paper", p, pct)
        self.reentry_record("paper", sym, p, pct, reason)
        self.crowd_record("paper", p, pct)
        tl = self.state["tradeLog"]
        tl.append({"t": datetime.now(KST).strftime("%m-%d %H:%M"), "symbol": sym, "side": p["side"], "pct": round(pct, 3),
                   "usdt": round(usdt, 3), "exit": reason, "desc": p["desc"], "regime": p.get("regime"),
                   "heldMin": round((time.time() * 1000 - p["at"]) / 60000), "maxFav": round(p.get("maxFav", 0), 2),
                   "fund": round(p.get("fundPct", 0.0), 4)})
        del tl[:-100]
        log(f"[모의 청산] {sym} {'롱' if long_ else '숏'} {pct:+.2f}% ({usdt:+.2f} USDT, {reason}) · "
            f"누적 {lg['trades']}회 거래당 {lg['sumPct'] / lg['trades']:+.3f}%")

    def rule_record(self, rid, pct):
        rb = self.state["ruleBook"].get(rid)
        if not rb:
            return
        rb["n"] += 1
        rb["wins"] += 1 if pct > 0 else 0
        rb["sumPct"] = round(rb["sumPct"] + pct, 4)
        rb["sumSq"] = round(rb.get("sumSq", 0.0) + pct * pct, 4)
        rb["sqN"] = rb.get("sqN", 0) + 1
        self.evo_record(rb, pct)
        n, avg = rb["n"], rb["sumPct"] / rb["n"]
        if n == RULE_EVAL or n in (10, 20, 40):
            rb["status"] = "proven" if avg >= RULE_PROMOTE_MIN_AVG else ("retired" if avg <= 0 else "testing")
            log(f"[규칙 판정] {rb['symbol']} {rb['desc']} 모의 {n}회 거래당 {avg:+.2f}% → "
                + {"proven": "통과(2단계 실주문 후보)", "retired": "퇴출", "testing": "검증 계속"}[rb["status"]])
            if rb["status"] == "retired":
                self.state["strategies"].pop(rb["symbol"], None)
        self.refresh_pool()

    # --- 대시보드·보고서 ---
    def view(self):
        lg = self.state["ledger"]
        n = lg["trades"]
        rb = self.state["ruleBook"].values()
        try:
            px = prices(list(self.state["positions"].keys())) if self.state["positions"] else {}
        except Exception:
            px = {}
        pos = []
        for sym, p in self.state["positions"].items():
            cur = px.get(sym)
            pct = None
            if cur:
                g = (cur / p["entry"] - 1) if p["side"] == "long" else (p["entry"] / cur - 1)
                pct = round((g - 2 * FEE - SLIP) * 100, 2)
            pos.append({"symbol": sym, "side": p["side"], "live": bool(p.get("live")), "entry": p["entry"], "tp": p["tp"], "sl": p["sl"], "price": cur,
                        "pnlPct": pct, "at": p["at"], "deadline": p["deadline"] * 1000, "desc": p["desc"],
                        "stopPx": p.get("stopPx"), "p": p.get("p"), "fundPct": p.get("fundPct")})
        g, evo, now = self.state["guard"], self.state["evo"], time.time()
        ll = self.live_day()
        rest = max(g.get("restUntil", 0), g.get("blockUntil", 0))
        guard = {"monthPnl": ll.get("monthPnl", 0.0), "monthLoss": self.month_loss, "dayPnl": ll["dayPnl"],
                 "dayLoss": self.daily_loss, "streak": g.get("streak", 0), "streakMax": LOSS_STREAK_MAX,
                 "restUntil": int(rest * 1000) if rest > now else 0,
                 "restWhy": (g.get("blockWhy") if g.get("blockUntil", 0) > now else f"연속 {LOSS_STREAK_MAX}번 손실") if rest > now else "",
                 "restKind": ("block" if g.get("blockUntil", 0) > now else "streak") if rest > now else "",
                 "restStart": g.get("restStart", 0),
                 "armed": sum(1 for q in self.state["positions"].values() if q.get("live") and q.get("stopId")),
                 "liveOpen": sum(1 for q in self.state["positions"].values() if q.get("live")),
                 "sizeRange": [round(self.order_usdt * SIZE_MIN_X * 0.5, 1), round(self.order_usdt * SIZE_MAX_X, 1)],
                 "bankX": round(min(2.0, max(0.5, 1 + ll["pnlUsdt"] / (self.order_usdt * BANK_X))), 2)}
        avg_of = lambda d: round(d["sumPct"] / d["n"], 3) if d["n"] else None
        evo_view = {"share": evo["share"], "pool": len(self.state["genePool"]), "maxGen": evo["maxGen"], "born": evo["born"],
                    "evoN": evo["evo"]["n"], "evoAvg": avg_of(evo["evo"]), "randN": evo["rand"]["n"], "randAvg": avg_of(evo["rand"]),
                    "activeEvo": sum(1 for v in self.state["strategies"].values() if v.get("origin") == "evo"),
                    "active": len(self.state["strategies"]),
                    "top": [{"symbol": x["symbol"], "side": x["side"], "fit": x["fit"], "p": x["p"], "n": x["n"], "gen": x["gen"]}
                            for x in self.state["genePool"][:5]]}
        return {
            "mode": "live" if self.live_on() else "paper", "updatedAt": int(time.time() * 1000), "tf": TF,
            "liveLedger": self.state["liveLedger"], "liveOrderUsdt": self.order_usdt, "liveDailyLoss": self.daily_loss,
            "trades": n, "wins": lg["wins"], "pnlUsdt": round(lg["pnlUsdt"], 2),
            "avgPct": round(lg["sumPct"] / n, 3) if n else None,
            "long": {"n": lg["long"]["n"], "avgPct": round(lg["long"]["sumPct"] / lg["long"]["n"], 3) if lg["long"]["n"] else None},
            "short": {"n": lg["short"]["n"], "avgPct": round(lg["short"]["sumPct"] / lg["short"]["n"], 3) if lg["short"]["n"] else None},
            "rules": {"testing": sum(1 for r in rb if r["status"] == "testing"), "proven": sum(1 for r in rb if r["status"] == "proven"),
                      "retired": sum(1 for r in rb if r["status"] == "retired")},
            "liveSkip": self.state.get("liveSkip") or {},
            "liveSlip": self.slip_view(),
            "giveback": self.giveback_view(),
            "sideWeak": self.side_weak_view(),
            "eventWatch": self.event_view(),
            "reentry": self.reentry_view(),
            "crowd": self.crowd_view(),
            "pCalib": self.p_calib_view(),
            "stopGap": self.stop_gap_view(),
            "payoff": self.payoff_view(),
            "nearProven": sorted(({"symbol": r.get("symbol", ""), "side": r.get("side", ""), "n": r.get("n", 0),
                                   "avgPct": round(r.get("sumPct", 0) / r["n"], 3) if r.get("n") else None}
                                  for r in rb if r.get("status") == "testing"),
                                 key=lambda x: (-(x["n"] or 0), -(x["avgPct"] or -99)))[:3],
            "ruleEval": RULE_EVAL, "rulePromote": RULE_PROMOTE_MIN_AVG, "liveBasis": self.live_basis, "style": STYLE,
            "guard": guard, "evo": evo_view,
            "settings": cb.settings_view(self.cfg, binance=True),
            "strategies": [{"symbol": k, "desc": v["desc"], "btEdge": v["btEdge"], "origin": v.get("origin", "rand"),
                            "gen": v.get("gen", 0)} for k, v in self.state["strategies"].items()],
            "testOrder": self.state.get("testOrder") or {},
            "regime": {"now": getattr(self, "_regime", None), "btc24h": getattr(self, "_regime_chg", None),
                       "rows": [{"kind": k.split("|")[0], "regime": k.split("|")[1], "side": k.split("|")[2], "n": v["n"],
                                 "wins": v["wins"], "avgPct": round(v["sumPct"] / v["n"], 3) if v["n"] else None}
                                for k, v in sorted(self.state.get("regimeStats", {}).items())]},
            "funding": {"rows": [{"kind": k.split("|")[0], "side": k.split("|")[1], "n": v["n"], "paidN": v.get("paidN", 0),
                                  "avgPct": round(v["sumPct"] / v["n"], 3) if v["n"] else None,
                                  "avgFund": round(v["sumFund"] / v["n"], 4) if v["n"] else None,
                                  "avgWithFund": round((v["sumPct"] + v["sumFund"]) / v["n"], 3) if v["n"] else None}
                                 for k, v in sorted(self.state.get("fundStats", {}).items())],
                        "openFund": round(sum(q.get("fundPct", 0.0) for q in self.state["positions"].values()), 4)},
            "foreign": self.state.get("foreign") or {},
            "readLimitedUntil": self.state.get("readLimitedUntil") or 0,
            "positions": pos, "recent": self.state["tradeLog"][-15:], "activity": ACT[-30:],
            "exchange": self.exchange_view(),
            "lastSelect": self.state["lastSelect"] * 1000, "phase": getattr(self, "_phase", ""),
        }

    def slip_view(self):
        st = self.state.get("liveSlip") or {}
        en, xn = st.get("entryN", 0), st.get("exitN", 0)
        return {"entryN": en, "entryAvg": round(st["entrySum"] / en, 3) if en else None, "entryWorse": st.get("entryWorse", 0),
                "exitN": xn, "exitAvg": round(st["exitSum"] / xn, 3) if xn else None, "exitWorse": st.get("exitWorse", 0),
                "assumed": round(SLIP * 100, 3), "recent": (st.get("recent") or [])[-5:]}

    def adopt_orphans(self):
        """거래소에 실제로 있는데 봇 장부에 없는 포지션을 넘겨받아 관리(익절·손절·보유시간·거래소 보호 손절).
        예전 버그(체결 0 → 모의 처리)로 생긴 포지션: 같은 종목·같은 방향 모의 포지션이 있으면 그 규칙의 익절·손절을 이어 씀"""
        ex = getattr(self, "_exsnap", None) or {}
        if not self.live or ex.get("err"):
            return
        for x in ex.get("positions", []):
            sym, side = x["symbol"], x["side"]
            cur = self.state["positions"].get(sym)
            if cur and cur.get("live"):
                continue
            if sym.replace("1000", "").replace("USDT", "") in cb.PROTECTED_COINS:
                continue
            long_ = side == "long"
            entry, qty = x["entry"] or x["mark"], x["amt"]
            if not entry or not qty:
                continue
            if cur and cur.get("side") == side:                  # 짝 모의 포지션 → 그 규칙 그대로 실주문으로 전환
                tp_m = cur["tp"] / cur["entry"] - 1
                sl_m = cur["sl"] / cur["entry"] - 1
                p = dict(cur, entry=entry, tp=entry * (1 + tp_m), sl=entry * (1 + sl_m), qty=qty, usdt=qty * entry,
                         live=True, adopted=True)
                how = "짝 모의 규칙 이어 씀"
            else:
                if cur:                                           # 같은 종목 반대 방향 모의 포지션은 모의로 정리
                    self.state["positions"].pop(sym, None)
                tp_m, sl_m = ADOPT_TP / 100 + 2 * FEE, ADOPT_SL / 100
                p = {"side": side, "entry": entry, "qty": qty, "usdt": qty * entry, "live": True, "adopted": True,
                     "tp": entry * (1 + tp_m) if long_ else entry * (1 - tp_m),
                     "sl": entry * (1 + sl_m) if long_ else entry * (1 - sl_m),
                     "at": int(time.time() * 1000), "deadline": int(time.time()) + ADOPT_HOURS * 3600,
                     "rid": None, "desc": f"넘겨받은 포지션 (익절 +{ADOPT_TP:g}% · 손절 {ADOPT_SL:g}% · 최대 {ADOPT_HOURS}시간)",
                     "maxFav": 0.0}
                how = "기본 익절·손절"
            self.state["positions"][sym] = p
            log(f"★[관리 넘겨받음] {sym} {'롱' if long_ else '숏'} {qty} ({qty * entry:.1f} USDT) @ {entry:.6g} · {how} · "
                f"익절 {p['tp']:.6g} · 손절 {p['sl']:.6g}")
            self.arm_stop(sym, p)
        mine = {k for k, v in self.state["positions"].items() if v.get("live")}
        self.state["foreign"] = {"at": int(time.time() * 1000),                     # 남은 관리 밖 포지션(보호 코인 등)만
                                 "list": [{"symbol": x["symbol"], "amt": x["amt"]} for x in ex.get("positions", [])
                                          if x["symbol"] not in mine]}
        save_state(self.state)

    def exchange_view(self):
        """거래소 실제 포지션 + 봇 장부 대조: 봇이 관리 중인지(managed) 표시"""
        ex = getattr(self, "_exsnap", None)
        if not self.live or not ex:
            return {}
        mine = {s for s, p in self.state["positions"].items() if p.get("live")}
        ps = [dict(x, managed=x["symbol"] in mine) for x in ex.get("positions", [])]
        out = dict(ex, positions=ps, unmanaged=sum(1 for x in ps if not x["managed"]),
                   missing=sorted(mine - {x["symbol"] for x in ps}),
                   hist=(self.state.get("balHist") or [])[-336:],
                   liveRecent=[t for t in self.state["tradeLog"] if t.get("live")][-20:])
        return out

    def push_phase(self, phase):
        self._phase = phase
        self.upload(force=True)

    def upload(self, force=False):
        if not force and time.time() - self.last_status < 60:   # 대시보드가 이 문서를 실시간으로 받음 → 60초마다(읽기 한도 절약)
            return
        self.last_status = time.time()
        if self.live:
            try:
                self._exsnap = self.live.snapshot()
                self._exsnap.pop("err", None)
                hist = self.state.setdefault("balHist", [])              # 총자산(지갑+미실현) 30분마다 기록, 7일치
                eq = round((self._exsnap.get("balance") or 0) + (self._exsnap.get("upnl") or 0), 3)
                if not hist or time.time() * 1000 - hist[-1][0] >= 30 * 60 * 1000:
                    hist.append([int(time.time() * 1000), eq])
                    del hist[:-336]
                self.adopt_orphans()
            except Exception as e:
                self._exsnap = dict(getattr(self, "_exsnap", None) or {}, err=str(e)[:120])
        data = self.view()
        try:
            h = self.cloud._auth()
            res = requests.patch(f"{cb.FS_BASE}/users/{self.cloud.uid}/status/binance",
                                 json={"fields": {k: cb.to_fs(v) for k, v in data.items()}}, headers=h, timeout=10)
            if res.status_code != 200:
                log(f"[경고] 대시보드 기록 실패 {res.status_code}")
        except Exception as e:
            log(f"[경고] 대시보드 기록 실패: {e}")
        if cb.REPORT_DIR:
            try:
                os.makedirs(cb.REPORT_DIR, exist_ok=True)
                rep = dict(data)
                rep["tradeLog"] = self.state["tradeLog"]
                rep["ruleBook"] = list(self.state["ruleBook"].values())[-80:]
                with open(os.path.join(cb.REPORT_DIR, "binance_report.json"), "w", encoding="utf-8") as f:
                    json.dump(rep, f, ensure_ascii=False, indent=1)
            except Exception:
                pass

    def run(self):
        uid = self.cloud.connect()
        bn = {k: v for k, v in self.cfg.get("_appSettings", {}).items() if k.startswith("BINANCE_")}
        if bn:
            log("[설정] 대시보드에서 정한 값 적용: " + ", ".join(f"{k}={v}" for k, v in bn.items()))
        log(f"시작 · {'실주문 켜짐' if self.live_on() else ('모의매매 · 남은 실보유만 정리' if self.live else '모의매매 전용(실제 주문 없음)')} · "
            f"{TF}분봉 · 모의 1회 {PAPER_USDT:.0f} USDT · 계정 {uid[:6]}…")
        self.reconcile()
        last_update = time.time()
        while True:
            if time.time() - last_update > 600:
                last_update = time.time()
                if check_update():
                    log("새 버전을 받았습니다. 다시 시작합니다")
                    sys.exit(RESTART_CODE)
            try:
                if any(k.replace("1000", "").replace("USDT", "") in cb.PROTECTED_COINS for k in self.state["strategies"]):
                    self.state["lastSelect"] = 0
                if time.time() - getattr(self, "_last_settings", 0) >= cb.APP_SETTINGS_EVERY:
                    self._last_settings = time.time()
                    try:
                        changed = cb.sync_app_settings(self.cloud, self.cfg, binance=True)
                    except Exception as e:
                        changed = False
                        log(f"[경고] 대시보드 설정 확인 실패 (매매는 계속): {str(e)[:120]}")
                    if changed:
                        log("[설정] 대시보드에서 바꾼 설정을 받았습니다 → 적용하려고 다시 시작합니다")
                        save_state(self.state)
                        sys.exit(RESTART_CODE)
                if (time.time() - self.state["lastSelect"] > RESELECT_HOURS * 3600 or not self.state["strategies"]
                        or self.state.get("style", "swing") != STYLE):
                    self.reselect()
                self.manage()
                try:
                    self.funding_track()                 # 관찰 전용 (실패해도 매매 영향 없음)
                except Exception as e:
                    log(f"[안내] 펀딩비 기록 건너뜀: {type(e).__name__}: {str(e)[:80]}")
                self.check_exchange()
                self.check_resume()
                self.check_test_order()
                self.scan()
                self.upload()
            except Exception as e:
                log(f"[경고] {type(e).__name__}: {e}")
                time.sleep(20)
            time.sleep(20)


CB_UPDATE_URL = UPDATE_URL.replace("binance_bot.py", "chamdom_bot.py")
HERE = os.path.dirname(os.path.abspath(__file__))
CB_NEEDS = ("load_config", "Cloud", "settings_view", "sync_app_settings", "disable_quick_edit", "random_spec", "CONDITION_POOL")


def standalone():
    """바이낸스 전용 폴더(업비트 봇 start_bot.bat 이 없는 폴더, 예: 지인 PC C:\\chamdom_binance)인지.
    전용 폴더에서는 아무도 chamdom_bot.py 를 실행하지 않으므로 바이낸스 엔진이 그 공용 파일도 최신(main)으로 맞춤.
    업비트 봇과 같은 폴더(성민님 PC)에서는 업비트 봇이 직접 갱신하므로 건드리지 않음"""
    return not os.path.exists(os.path.join(HERE, "start_bot.bat"))


def _update_file(url, path, marker, name):
    try:
        r = requests.get(url, params={"t": int(time.time())}, timeout=15)
        if r.status_code != 200:
            return False
        new = r.content.decode("utf-8").replace("\r\n", "\n")
        cur = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                cur = f.read().replace("\r\n", "\n")
        if new == cur or marker not in new:
            return False
        compile(new, name, "exec")
        with open(path + ".new", "w", encoding="utf-8", newline="\n") as f:
            f.write(new)
        os.replace(path + ".new", path)
        return True
    except Exception:
        return False


def check_update():
    changed = _update_file(UPDATE_URL, os.path.abspath(__file__), "바이낸스 선물 엔진", "binance_bot.py")
    if standalone():
        changed = _update_file(CB_UPDATE_URL, os.path.join(HERE, "chamdom_bot.py"), "def load_config", "chamdom_bot.py") or changed
    return changed


if __name__ == "__main__":
    missing = [n for n in CB_NEEDS if not hasattr(cb, n)]
    if missing:                                         # 공용 파일(chamdom_bot.py)이 옛 버전 — 예: 지인용 업비트 봇(stable) 폴더에 넣은 경우
        if standalone() and check_update():
            print("공용 파일(chamdom_bot.py)을 최신으로 받았습니다. 다시 시작합니다.")
            sys.exit(RESTART_CODE)
        print("[설치 위치 확인 필요] 이 폴더의 chamdom_bot.py 가 옛 버전이라 바이낸스 엔진을 시작할 수 없습니다.\n"
              "  → 업비트 봇 폴더가 아니라 별도 폴더(예: C:\\chamdom_binance)에 바이낸스 설치 파일을 풀어서 실행하세요.\n"
              "  → 없는 항목: " + ", ".join(missing) + "\n10분 뒤 다시 확인합니다. (이 창은 닫아도 됩니다)", flush=True)
        time.sleep(600)
        sys.exit(RESTART_CODE)
    cb.disable_quick_edit()
    if check_update():
        sys.exit(RESTART_CODE)
    try:
        Engine().run()
    except KeyboardInterrupt:
        log("종료")
