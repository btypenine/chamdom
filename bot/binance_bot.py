# -*- coding: utf-8 -*-
"""
참돔 트레이드 바이낸스 선물 엔진 — 모의매매 + (사용자가 켜면) 실전 통과 규칙만 실주문

실주문 조건 (모두 만족해야 함, 하나라도 아니면 모의로 처리)
- bot_config.env: BINANCE_TRADE_MODE=live + BINANCE_API_KEY/BINANCE_SECRET_KEY
- 그 규칙이 모의 5회 이상에서 거래당 +0.15% 이상으로 '통과'(proven)
- 오늘(09시 기준) 실주문 손실이 BINANCE_DAILY_LOSS_USDT 미만, 실보유 수 < BINANCE_MAX_LIVE
- 업비트 봇이 대시보드에서 '일시정지' 상태가 아님
- 레버리지 1배·격리 마진 고정, 매도는 reduceOnly 로만 정리
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
PAPER_USDT = 100.0                # 모의 1회 진입 금액(USDT, 레버리지 1배)
MAX_OPEN = 10                     # 동시 모의 보유 최대
EXCLUDE = {"USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "BTCDOMUSDT"}
RULE_EVAL = 5                     # 규칙 판정 시작 횟수 (업비트 봇과 같음)
RULE_PROMOTE_MIN_AVG = 0.15


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
                                         "minNotional": float((f.get("MIN_NOTIONAL") or {}).get("notional", 5))}

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

    def market(self, sym, side, qty, reduce=False):
        p = {"symbol": sym, "side": side, "type": "MARKET", "quantity": f"{qty:.10f}".rstrip("0").rstrip("."),
             "newOrderRespType": "RESULT"}
        if reduce:
            p["reduceOnly"] = "true"
        d = self.call("POST", "/fapi/v1/order", p)
        return float(d.get("avgPrice") or 0), float(d.get("executedQty") or 0)

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


def pick_for(symbol, rng):
    cl = klines(symbol, TRAIN_BARS)
    if len(cl) < TRAIN_BARS * 0.8:
        return None
    cands = []
    for side, ser in (("long", series_of(cl)), ("short", series_of(inverted(cl)))):
        split = int(ser.n * (1 - cb.BT_HOLDOUT))
        for _ in range(cb.BT_TRIALS):
            sp = cb.random_spec(rng)
            tr = cb.stats(cb.backtest(ser, sp, end=split))
            if tr["n"] < cb.EXPLORE_MIN_TRADES or tr["edge"] < cb.EXPLORE_MIN_EDGE:
                continue
            ho = cb.stats(cb.backtest(ser, sp, start=split))
            if ho["n"] and ho["edge"] < cb.EXPLORE_HOLDOUT_FLOOR:
                continue
            score = tr["edge"] if not ho["n"] else (tr["edge"] * tr["n"] + ho["edge"] * ho["n"]) / (tr["n"] + ho["n"])
            cands.append((score, side, sp, tr, ho))
    if not cands:
        return None
    score, side, sp, tr, ho = max(cands, key=lambda x: x[0])
    rid = f"{symbol}-{side}-" + cb.rule_id("USDT-" + symbol, sp).split("-")[-1]
    desc = cb.describe(sp).replace(f"최대{sp['hold']}분", f"최대{sp['hold'] * TF / 60:g}시간")   # 보유시간은 봉 개수 × 15분
    return {"side": side, "spec": sp, "rid": rid, "desc": ("롱 " if side == "long" else "숏 ") + desc,
            "btEdge": round(score, 3), "btTrades": tr["n"], "btWinrate": round(tr["winrate"], 3)}


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
        key, sec = c.get("BINANCE_API_KEY", ""), c.get("BINANCE_SECRET_KEY", "")
        self.live = None
        if (c.get("BINANCE_TRADE_MODE", "paper").lower() == "live" and key and sec
                and not key.startswith("여기에") and not sec.startswith("여기에")):
            self.live = Live(key, sec)
        self.state.setdefault("liveLedger", {"trades": 0, "wins": 0, "pnlUsdt": 0.0, "day": "", "dayPnl": 0.0, "dayTrades": 0})

    # --- 전략 ---
    def reselect(self):
        log(f"종목·전략 선정 시작 (15분봉, 상위 {TOP_N}종목, 롱·숏 각 {cb.BT_TRIALS}개 조합)")
        syms = top_symbols()
        chosen = {}
        for i, sym in enumerate(syms, 1):
            self.push_phase(f"전략 선정 {i}/{len(syms)} ({sym})")
            try:
                st = pick_for(sym, self.rng)
            except Exception as e:
                log(f"[경고] {sym} 선정 실패: {e}")
                continue
            if st:
                rb = self.state["ruleBook"].get(st["rid"])
                if rb and rb.get("status") == "retired":
                    continue
                chosen[sym] = st
                self.state["ruleBook"].setdefault(st["rid"], {"id": st["rid"], "symbol": sym, "side": st["side"],
                                                              "desc": st["desc"], "btEdge": st["btEdge"], "n": 0, "wins": 0,
                                                              "sumPct": 0.0, "status": "testing"})
                log(f"  채택 {sym}: {st['desc']} · 백테스트 거래당 {st['btEdge']:+.2f}% ({st['btTrades']}회)")
        self.state["strategies"] = chosen
        for sym in list(self.state["positions"]):              # 보호 코인으로 바뀐 종목의 모의 보유는 정리
            if sym.replace("1000", "").replace("USDT", "") in cb.PROTECTED_COINS and not self.state["positions"][sym].get("live"):
                self.close_paper(sym, prices([sym]).get(sym, self.state["positions"][sym]["entry"]), "보호 코인 제외")
        self.state["lastSelect"] = int(time.time())
        save_state(self.state)
        log(f"전략 선정 완료: {len(chosen)}종목")
        self.push_phase("")

    # --- 모의 진입·청산 ---
    def scan(self):
        st = self.state
        bar = int(time.time() // (TF * 60))
        if st.get("scannedBar") == bar or time.time() % (TF * 60) < 20:   # 새 봉이 닫힌 뒤 한 번만 확인
            return
        st["scannedBar"] = bar
        if len(st["positions"]) >= MAX_OPEN:
            return
        for sym, strat in list(st["strategies"].items()):
            if sym in st["positions"] or len(st["positions"]) >= MAX_OPEN:
                continue
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
            if self.live_ok(strat):
                self.open_live(sym, strat, px)
            else:
                self.open_paper(sym, strat, px)
        save_state(st)

    # --- 실주문 ---
    def live_day(self):
        ll = self.state["liveLedger"]
        d = cb.today_key()
        if ll["day"] != d:
            ll.update({"day": d, "dayPnl": 0.0, "dayTrades": 0})
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

    def live_ok(self, strat):
        if not self.live:
            return False
        rb = self.state["ruleBook"].get(strat["rid"]) or {}
        if rb.get("status") != "proven":
            return False
        ll = self.live_day()
        if ll["dayPnl"] <= -self.daily_loss:
            return False
        if sum(1 for p in self.state["positions"].values() if p.get("live")) >= self.max_live:
            return False
        if self.owner_paused():
            return False
        return True

    def open_live(self, sym, strat, px):
        side = strat["side"]
        try:
            if not self.live.filters:
                self.live.sync()
                self.live.load_filters()
            qty, need = self.live.qty_for(sym, self.order_usdt, px)
            if not qty:
                log(f"[실주문 건너뜀] {sym} 최소 주문 {need:.0f} USDT > 설정 {self.order_usdt:.0f} USDT → 모의로 처리")
                return self.open_paper(sym, strat, px)
            self.live.prepare(sym)
            avg, filled = self.live.market(sym, "BUY" if side == "long" else "SELL", qty)
        except Exception as e:
            log(f"[실주문 실패] {sym}: {e} → 모의로 처리")
            return self.open_paper(sym, strat, px)
        if filled <= 0 or avg <= 0:
            log(f"[실주문 확인 실패] {sym} 체결 수량 0 → 모의로 처리")
            return self.open_paper(sym, strat, px)
        sp = strat["spec"]
        cost = 2 * FEE * 100
        mv = sp.get("tp", 0.5) + cost
        tp = avg * (1 + mv / 100) if side == "long" else avg * (1 - mv / 100)
        sl = avg * (1 + sp["sl"] / 100) if side == "long" else avg * (1 - sp["sl"] / 100)
        self.state["positions"][sym] = {"side": side, "entry": avg, "tp": tp, "sl": sl, "usdt": filled * avg, "qty": filled,
                                        "live": True, "at": int(time.time() * 1000),
                                        "deadline": int(time.time()) + sp["hold"] * TF * 60,
                                        "rid": strat["rid"], "desc": strat["desc"], "maxFav": 0.0}
        save_state(self.state)
        log(f"★[실주문 진입] {sym} {'롱' if side == 'long' else '숏'} {filled} ({filled * avg:.1f} USDT) @ {avg:.6g} · "
            f"익절 {tp:.6g} · 손절 {sl:.6g} · {strat['desc']}")

    def close_live(self, sym, price, reason):
        p = self.state["positions"][sym]
        long_ = p["side"] == "long"
        try:
            avg, filled = self.live.market(sym, "SELL" if long_ else "BUY", p["qty"], reduce=True)
        except Exception as e:
            log(f"[실주문 청산 실패] {sym}: {e} — 다음 확인 때 다시 시도 (바이낸스 앱에서 직접 정리 가능)")
            return
        avg = avg or price
        gross = (avg / p["entry"] - 1) if long_ else (p["entry"] / avg - 1)
        pct = (gross - 2 * FEE) * 100
        usdt = p["usdt"] * pct / 100
        ll = self.live_day()
        ll["trades"] += 1
        ll["wins"] += 1 if pct > 0 else 0
        ll["pnlUsdt"] = round(ll["pnlUsdt"] + usdt, 4)
        ll["dayPnl"] = round(ll["dayPnl"] + usdt, 4)
        ll["dayTrades"] += 1
        self.state["positions"].pop(sym)
        self.rule_record(p["rid"], pct)
        tl = self.state["tradeLog"]
        tl.append({"t": datetime.now(KST).strftime("%m-%d %H:%M"), "symbol": sym, "side": p["side"], "pct": round(pct, 3),
                   "usdt": round(usdt, 3), "exit": reason, "desc": p["desc"], "live": True,
                   "heldMin": round((time.time() * 1000 - p["at"]) / 60000), "maxFav": round(p.get("maxFav", 0), 2)})
        del tl[:-100]
        save_state(self.state)
        log(f"★[실주문 청산] {sym} {'롱' if long_ else '숏'} {pct:+.2f}% ({usdt:+.2f} USDT, {reason}) · "
            f"오늘 실주문 {ll['dayTrades']}회 {ll['dayPnl']:+.2f} USDT"
            + (f" · 하루 손실 한도 도달 → 9시까지 실주문 중지" if ll["dayPnl"] <= -self.daily_loss else ""))

    def reconcile(self):
        """시작할 때: 장부의 실보유와 거래소 실제 포지션 맞춰 보기 (모르는 포지션은 건드리지 않음)"""
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
                log(f"[경고] {sym} 실보유가 거래소에 없음(직접 정리·청산된 것으로 봄) → 장부에서 제외")
                self.state["positions"].pop(sym)
        mine = {s for s, p in self.state["positions"].items() if p.get("live")}
        for sym, amt in ex.items():
            if sym not in mine:
                log(f"[안내] 봇이 열지 않은 포지션 {sym} {amt} — 봇은 건드리지 않음")
        save_state(self.state)
        log(f"바이낸스 실계좌 연결됨 · 실주문 켜짐(통과 규칙만) · 1회 {self.order_usdt:.0f} USDT · "
            f"하루 손실 한도 {self.daily_loss:.0f} USDT · 동시 실보유 최대 {self.max_live}")

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
            if not price:
                continue
            long_ = p["side"] == "long"
            fav = (price / p["entry"] - 1) * 100 * (1 if long_ else -1)
            p["maxFav"] = max(p.get("maxFav", 0.0), fav)
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
        tl = self.state["tradeLog"]
        tl.append({"t": datetime.now(KST).strftime("%m-%d %H:%M"), "symbol": sym, "side": p["side"], "pct": round(pct, 3),
                   "usdt": round(usdt, 3), "exit": reason, "desc": p["desc"],
                   "heldMin": round((time.time() * 1000 - p["at"]) / 60000), "maxFav": round(p.get("maxFav", 0), 2)})
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
        n, avg = rb["n"], rb["sumPct"] / rb["n"]
        if n == RULE_EVAL or n in (10, 20, 40):
            rb["status"] = "proven" if avg >= RULE_PROMOTE_MIN_AVG else ("retired" if avg <= 0 else "testing")
            log(f"[규칙 판정] {rb['symbol']} {rb['desc']} 모의 {n}회 거래당 {avg:+.2f}% → "
                + {"proven": "통과(2단계 실주문 후보)", "retired": "퇴출", "testing": "검증 계속"}[rb["status"]])
            if rb["status"] == "retired":
                self.state["strategies"].pop(rb["symbol"], None)

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
                        "pnlPct": pct, "at": p["at"], "deadline": p["deadline"] * 1000, "desc": p["desc"]})
        return {
            "mode": "live" if self.live else "paper", "updatedAt": int(time.time() * 1000), "tf": TF,
            "liveLedger": self.state["liveLedger"], "liveOrderUsdt": self.order_usdt, "liveDailyLoss": self.daily_loss,
            "trades": n, "wins": lg["wins"], "pnlUsdt": round(lg["pnlUsdt"], 2),
            "avgPct": round(lg["sumPct"] / n, 3) if n else None,
            "long": {"n": lg["long"]["n"], "avgPct": round(lg["long"]["sumPct"] / lg["long"]["n"], 3) if lg["long"]["n"] else None},
            "short": {"n": lg["short"]["n"], "avgPct": round(lg["short"]["sumPct"] / lg["short"]["n"], 3) if lg["short"]["n"] else None},
            "rules": {"testing": sum(1 for r in rb if r["status"] == "testing"), "proven": sum(1 for r in rb if r["status"] == "proven"),
                      "retired": sum(1 for r in rb if r["status"] == "retired")},
            "strategies": [{"symbol": k, "desc": v["desc"], "btEdge": v["btEdge"]} for k, v in self.state["strategies"].items()],
            "positions": pos, "recent": self.state["tradeLog"][-15:], "activity": ACT[-30:],
            "lastSelect": self.state["lastSelect"] * 1000, "phase": getattr(self, "_phase", ""),
        }

    def push_phase(self, phase):
        self._phase = phase
        self.upload(force=True)

    def upload(self, force=False):
        if not force and time.time() - self.last_status < 30:
            return
        self.last_status = time.time()
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
        log(f"시작 · {'실주문 켜짐(통과 규칙만, 나머지 모의)' if self.live else '모의매매 전용(실제 주문 없음)'} · "
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
                if time.time() - self.state["lastSelect"] > RESELECT_HOURS * 3600 or not self.state["strategies"]:
                    self.reselect()
                self.manage()
                self.scan()
                self.upload()
            except Exception as e:
                log(f"[경고] {type(e).__name__}: {e}")
                time.sleep(20)
            time.sleep(20)


def check_update():
    me = os.path.abspath(__file__)
    try:
        r = requests.get(UPDATE_URL, params={"t": int(time.time())}, timeout=15)
        if r.status_code != 200:
            return False
        new = r.content.decode("utf-8").replace("\r\n", "\n")
        with open(me, encoding="utf-8") as f:
            cur = f.read().replace("\r\n", "\n")
        if new == cur or "바이낸스 선물 엔진" not in new:
            return False
        compile(new, "binance_bot.py", "exec")
        with open(me + ".new", "w", encoding="utf-8", newline="\n") as f:
            f.write(new)
        os.replace(me + ".new", me)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    if check_update():
        sys.exit(RESTART_CODE)
    try:
        Engine().run()
    except KeyboardInterrupt:
        log("종료")
