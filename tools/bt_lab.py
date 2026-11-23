# -*- coding: utf-8 -*-
"""bt-lab 실험 9: 진짜 우위 찾기 — 지정가(메이커) 체결·모든 종목 공통 규칙·시장 중립 묶음·국면별 진입
배경: 실험 8(실제 선물 15분봉 125일, 포트폴리오 단위)에서 지금 방식은 거래 2,651회 거래당 -0.007%(사실상 0),
  단기 청산(본전·추적·시간 손절·짧은 손절)과 BTC 헤지는 모두 더 나빴음(헤지는 수수료만 2배). 같은 방향 최대 3만 낙폭을 절반으로 줄임.
이번에 보는 것
- A 지정가 체결: 진입을 신호 봉 종가 지정가(다음 1~2봉 안에 그 가격을 지나가야 체결), 익절도 지정가 → 수수료 0.02%·밀림 없음.
  손절·시간 정리는 시장가 그대로. 못 들어간 신호(가격이 그냥 가 버림)까지 반영
- B 공통 규칙: 종목마다 따로 고르지 않고, 40종목을 한데 모아 거래 수백 회로 검증된 규칙만 골라 모든 종목에 적용(우연히 맞은 규칙 줄이기)
- C 시장 중립 묶음: 일정 시간마다 많이 오른 5종목 숏 + 많이 내린 5종목 롱(되돌림) 또는 그 반대(추세) — 롱·숏 금액이 같아 시장 방향과 무관
- D 국면별 진입: BTC 24시간 기준 횡보에서만 / 추세 방향만 / 횡보 숏+추세 방향
분석 전용 — 봇 동작에는 영향 없음
"""
import io, json, math, os, random, sys, time, zipfile, zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import multiprocessing as mp
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
import requests
import chamdom_bot as cb
import binance_bot as bb

SELFTEST = os.environ.get("LAB_SELFTEST") == "1"
ARCH = "https://data.binance.vision/data/futures/um"
SPOT = "https://data-api.binance.vision/api/v3/klines"
TF_MS = 15 * 60 * 1000
TRAIN, STEP, WARM = (600, 200, 150) if SELFTEST else (1920, 480, 300)
TOPN = 8 if SELFTEST else 40
NOTIONAL = 50.0
FEE, SLIP = bb.FEE, bb.SLIP
FEE_M = 0.0002                    # 지정가(메이커) 수수료
DAYS_BACK = 114
REG_BARS, REG_UP, REG_DOWN = 96, 1.5, -1.5
FLOW_BARS = 8
BETA_BARS = 300 if SELFTEST else 672

CANDS = """BTCUSDT ETHUSDT SOLUSDT BNBUSDT DOGEUSDT ADAUSDT LINKUSDT AVAXUSDT LTCUSDT BCHUSDT SUIUSDT TRXUSDT DOTUSDT
NEARUSDT APTUSDT ARBUSDT OPUSDT UNIUSDT AAVEUSDT FILUSDT INJUSDT TAOUSDT HYPEUSDT ENAUSDT WLDUSDT 1000PEPEUSDT
1000SHIBUSDT 1000BONKUSDT WIFUSDT TONUSDT ZECUSDT XMRUSDT XLMUSDT HBARUSDT ONDOUSDT SEIUSDT TIAUSDT FETUSDT ATOMUSDT
ETCUSDT CRVUSDT LDOUSDT PENGUUSDT FARTCOINUSDT VIRTUALUSDT TRUMPUSDT PUMPUSDT ASTERUSDT XPLUSDT PAXGUSDT
1000LUNCUSDT ACEUSDT AKEUSDT ARIAUSDT BARDUSDT BLESSUSDT BTWUSDT BULLAUSDT CYBERUSDT DODOXUSDT ELSAUSDT ENSOUSDT
EVAAUSDT HUMAUSDT ICNTUSDT IDOLUSDT IRYSUSDT KERNELUSDT LISTAUSDT LITUSDT MAGMAUSDT MMTUSDT NAORISUSDT NIGHTUSDT
ORCAUSDT PHAROSUSDT PIEVERSEUSDT PIPPINUSDT PROMUSDT RAREUSDT SENTUSDT SKYAIUSDT SKYUSDT VVVUSDT XAUTUSDT ZILUSDT
ZKCUSDT ZKPUSDT""".split()

# 전략 모양(익절·손절·보유) 후보 묶음. base = 지금 봇 그대로
SPACES = {
    "base": {"tp": list(cb.TP_CHOICES), "sl": list(cb.SL_CHOICES), "hold": list(cb.HOLD_CHOICES)},
}

D = {}            # sym -> {"o","h","l","c","q","ok"} (전체 시간축에 맞춘 배열)
T = []            # 시간축(ms)
SRC = {"name": "선물 자료실"}


# ---------------- 시세 받기 ----------------
def fetch(url, tries=3):
    for a in range(tries):
        try:
            r = requests.get(url, timeout=40)
            if r.status_code == 200:
                return r.content
            if r.status_code in (403, 404):
                return None
        except Exception:
            pass
        time.sleep(1 + a)
    return None


def parse_zip(content):
    rows = []
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            for name in z.namelist():
                with z.open(name) as f:
                    for line in io.TextIOWrapper(f, "utf-8"):
                        p = line.strip().split(",")
                        if len(p) < 8 or not p[0][:1].isdigit():
                            continue
                        t = int(p[0])
                        if t > 10 ** 14:
                            t //= 1000
                        rows.append((t, float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[7])))
    except Exception:
        return []
    return rows


def load_archive(syms, start, end):
    """월 파일(끝난 달) + 일 파일(이번 달). 월 파일이 없으면 그 달도 일 파일로"""
    months, days = [], []
    d = date(start.year, start.month, 1)
    while d <= end:
        nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        if nxt - timedelta(days=1) <= end and nxt <= date(end.year, end.month, 1):
            months.append(d)
        else:
            x = d
            while x <= end and x < nxt:
                days.append(x)
                x += timedelta(days=1)
        d = nxt
    tasks = [(s, "m", m, f"{ARCH}/monthly/klines/{s}/15m/{s}-15m-{m:%Y-%m}.zip") for s in syms for m in months]
    tasks += [(s, "d", x, f"{ARCH}/daily/klines/{s}/15m/{s}-15m-{x:%Y-%m-%d}.zip") for s in syms for x in days]
    raw = {s: {} for s in syms}

    def job(t):
        return t, fetch(t[3])
    miss = []
    with ThreadPoolExecutor(24) as ex:
        for t, c in ex.map(job, tasks):
            if c is None:
                if t[1] == "m":
                    miss.append(t)
                continue
            for r in parse_zip(c):
                raw[t[0]][r[0]] = r
    # 월 파일이 없던 달: 그 종목이 자료실에 있기는 하면(다른 자료가 받아졌으면) 일 파일로 보충
    extra = []
    for s, _, m, _ in miss:
        if not raw[s]:
            continue
        nxt = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
        x = m
        while x < nxt:
            extra.append((s, "d", x, f"{ARCH}/daily/klines/{s}/15m/{s}-15m-{x:%Y-%m-%d}.zip"))
            x += timedelta(days=1)
    if extra:
        with ThreadPoolExecutor(24) as ex:
            for t, c in ex.map(job, extra):
                if c:
                    for r in parse_zip(c):
                        raw[t[0]][r[0]] = r
    return {s: sorted(v.values()) for s, v in raw.items() if v}


def load_spot(syms, total):
    out = {}
    for s in syms:
        sp = s[4:] if s.startswith("1000") else s
        rows, end = [], None
        while len(rows) < total:
            q = f"{SPOT}?symbol={sp}&interval=15m&limit=1000" + (f"&endTime={end}" if end else "")
            c = fetch(q, 2)
            if not c:
                break
            d = json.loads(c)
            if not d:
                break
            rows = [(k[0], float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[7])) for k in d] + rows
            end = d[0][0] - 1
            if len(d) < 1000:
                break
            time.sleep(0.15)
        if rows:
            out[s] = rows[-total:]
    return out


def synth(syms, n):
    """자체 점검용 가짜 시세 (네트워크 없이 코드만 확인)"""
    rng = random.Random(1)
    t0 = 1790000000000 // TF_MS * TF_MS
    base = [0.0]
    for _ in range(n):
        base.append(base[-1] + rng.gauss(0, 0.002))
    out = {}
    for s in syms:
        px, rows = 100.0 * (1 + rng.random()), []
        beta = 1.0 if s == "BTCUSDT" else 0.6 + rng.random()
        for i in range(n):
            r = beta * (base[i + 1] - base[i]) + (0 if s == "BTCUSDT" else rng.gauss(0, 0.004))
            o = px
            px = px * math.exp(r)
            hi, lo = max(o, px) * (1 + abs(rng.gauss(0, 0.0015))), min(o, px) * (1 - abs(rng.gauss(0, 0.0015)))
            rows.append((t0 + i * TF_MS, o, hi, lo, px, 1000 * px * (1 + rng.random())))
        out[s] = rows
    return out


def build(raw):
    global T
    T = [r[0] for r in raw["BTCUSDT"]]
    idx = {t: i for i, t in enumerate(T)}
    n = len(T)
    for s, rows in raw.items():
        a = {"o": [0.0] * n, "h": [0.0] * n, "l": [0.0] * n, "c": [0.0] * n, "q": [0.0] * n, "ok": bytearray(n)}
        for t, o, h, l, c, q in rows:
            i = idx.get(t)
            if i is None or o <= 0 or l <= 0:
                continue
            a["o"][i], a["h"][i], a["l"][i], a["c"][i], a["q"][i] = o, h, l, c, q
            a["ok"][i] = 1
        # 앞에서부터 이어진 '빈 곳 없는' 길이를 빨리 알 수 있게 누적 합
        cum, acc = [0] * (n + 1), 0
        for i in range(n):
            acc += a["ok"][i]
            cum[i + 1] = acc
        a["cum"] = cum
        D[s] = a


def full(s, a, b):
    return D[s]["cum"][b] - D[s]["cum"][a] == b - a


def candles(s, a, b, inv=False):
    d = D[s]
    if inv:
        return [{"opening_price": 1 / d["o"][i], "high_price": 1 / d["l"][i], "low_price": 1 / d["h"][i],
                 "trade_price": 1 / d["c"][i], "candle_acc_trade_volume": d["q"][i], "t": T[i]} for i in range(a, b)]
    return [{"opening_price": d["o"][i], "high_price": d["h"][i], "low_price": d["l"][i],
             "trade_price": d["c"][i], "candle_acc_trade_volume": d["q"][i], "t": T[i]} for i in range(a, b)]


# ---------------- 전략 선정 (봇 pick_for 와 같은 기준, 진화 재료만 없음) ----------------
def sel_task(args):
    space, w, s0, sym = args
    sp = SPACES[space]
    cb.TP_CHOICES, cb.SL_CHOICES, cb.HOLD_CHOICES = sp["tp"], sp["sl"], sp["hold"]
    rng = random.Random(zlib.crc32(f"{w}:{sym}".encode()))
    best = None
    for side in ("long", "short"):
        ser = bb.series_of(candles(sym, s0 - TRAIN, s0, inv=(side == "short")))
        split = int(ser.n * (1 - cb.BT_HOLDOUT))
        for _ in range(cb.BT_TRIALS):
            spec = cb.random_spec(rng)
            tr = cb.stats(cb.backtest(ser, spec, end=split))
            if tr["n"] < cb.EXPLORE_MIN_TRADES or tr["edge"] < cb.EXPLORE_MIN_EDGE:
                continue
            ho = cb.stats(cb.backtest(ser, spec, start=split))
            if ho["n"] and ho["edge"] < cb.EXPLORE_HOLDOUT_FLOOR:
                continue
            score = tr["edge"] if not ho["n"] else (tr["edge"] * tr["n"] + ho["edge"] * ho["n"]) / (tr["n"] + ho["n"])
            if best is None or score > best[0]:
                best = (score, side, spec)
    if not best:
        return space, w, sym, None
    score, side, spec = best
    # 다음 구간 신호 (지표 예열 WARM봉 포함) → 전체 시간축 번호로
    e = min(s0 + STEP, len(T))
    ser = bb.series_of(candles(sym, s0 - WARM, e, inv=(side == "short")))
    sig = ser.signal(spec)
    hits = [s0 - WARM + k for k, v in enumerate(sig) if v and s0 - WARM + k >= s0 - 1]
    # 종목 민감도(베타): 직전 7일 15분 수익률을 BTC 수익률에 맞춤
    c, bc = D[sym]["c"], D["BTCUSDT"]["c"]
    xs = [bc[i] / bc[i - 1] - 1 for i in range(s0 - BETA_BARS + 1, s0)]
    ys = [c[i] / c[i - 1] - 1 for i in range(s0 - BETA_BARS + 1, s0)]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    var = sum((x - mx) ** 2 for x in xs)
    beta = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var if var > 0 else 1.0
    return space, w, sym, {"side": side, "spec": spec, "sig": hits, "score": round(score, 3), "beta": min(2.5, max(0.3, beta))}


# ---------------- 포트폴리오 진행 ----------------
BASE_P = {"max_open": 10, "side_cap": None, "net_cap": None, "regime": False, "flow": None, "be": None, "trail": None,
          "tstop": None, "sl_cap": None, "hold_cap": None, "hedge": None, "hedge_band": 25.0, "hedge_free": 0.0,
          "maker_entry": None, "maker_tp": False, "allow": None}


def simulate(sel, W0, NW, P):
    """sel[w] = [(sym, info), ...] (같은 종목이 여러 규칙으로 여러 번 나와도 됨, 종목당 포지션은 1개). 반환: 지표 dict"""
    P = {**BASE_P, **P}
    NT = len(T)
    bc = D["BTCUSDT"]["c"]
    pos, cool, trades, pend = {}, {}, [], {}
    realized = hedge_pnl = hedge_fee = 0.0
    H = 0.0
    eq = []
    skipped = {"cap": 0, "side": 0, "net": 0, "regime": 0, "flow": 0, "allow": 0, "nofill": 0}
    max_same = 0
    sigsets = [[set(info["sig"]) for _, info in sel[w]] for w in range(NW)]

    def open_pos(sym, info, i, entry, reg, fillbar):
        spec = info["spec"]
        slp = spec["sl"] if P["sl_cap"] is None else max(spec["sl"], P["sl_cap"])
        hold = spec["hold"] if P["hold_cap"] is None else min(spec["hold"], P["hold_cap"])
        pos[sym] = {"side": info["side"], "entry": entry, "tp": cb.target_price(entry, spec["tp"]), "stop": entry * (1 + slp / 100),
                    "j": i, "hold": hold, "maxfav": 0.0, "reg": reg, "beta": info["beta"], "moved": False, "fillbar": fillbar}

    for i in range(W0, NT):
        w = min((i - W0) // STEP, NW - 1)
        b24 = (bc[i - 1] / bc[i - 1 - REG_BARS] - 1) * 100
        b2 = (bc[i - 1] / bc[i - 1 - FLOW_BARS] - 1) * 100
        reg = "상승" if b24 >= REG_UP else ("하락" if b24 <= REG_DOWN else "횡보")
        # 0) 걸어 둔 지정가 진입: 이번 봉이 그 가격을 지나가면 체결, 기한이 지나면 취소
        for sym in list(pend):
            o = pend[sym]
            d = D[sym]
            if not d["ok"][i]:
                del pend[sym]
                continue
            lo = d["l"][i] if o["info"]["side"] == "long" else 1 / d["h"][i]
            if lo < o["limit"] * (1 - 1e-4):
                open_pos(sym, o["info"], i, o["limit"] * (1 + FEE_M), o["reg"], True)
                del pend[sym]
            elif i >= o["expire"]:
                skipped["nofill"] += 1
                del pend[sym]
        # 1) 진입 (직전 봉 신호 → 이번 봉)
        for k, (sym, info) in enumerate(sel[w]):
            if (i - 1) not in sigsets[w][k] or sym in pos or sym in pend or cool.get(sym, 0) > i or not D[sym]["ok"][i]:
                continue
            side = info["side"]
            nl = sum(1 for p in pos.values() if p["side"] == "long") + sum(1 for o in pend.values() if o["info"]["side"] == "long")
            ns = len(pos) + len(pend) - nl
            if len(pos) + len(pend) >= P["max_open"]:
                skipped["cap"] += 1
                continue
            if P["side_cap"] and (nl if side == "long" else ns) >= P["side_cap"]:
                skipped["side"] += 1
                continue
            if P["net_cap"] is not None and abs((nl + (side == "long")) - (ns + (side == "short"))) > P["net_cap"]:
                skipped["net"] += 1
                continue
            if P["regime"] and ((reg == "상승" and side == "short") or (reg == "하락" and side == "long")):
                skipped["regime"] += 1
                continue
            if P["allow"] is not None and side not in P["allow"].get(reg, ()):
                skipped["allow"] += 1
                continue
            if P["flow"] is not None and b2 * (1 if side == "short" else -1) >= P["flow"]:
                skipped["flow"] += 1
                continue
            if P["maker_entry"]:
                c1 = D[sym]["c"][i - 1]
                pend[sym] = {"info": info, "limit": c1 if side == "long" else 1 / c1, "expire": i - 1 + P["maker_entry"], "reg": reg}
                # 걸자마자 이번 봉에서 체결되는지 바로 확인
                d = D[sym]
                lo = d["l"][i] if side == "long" else 1 / d["h"][i]
                if lo < pend[sym]["limit"] * (1 - 1e-4):
                    open_pos(sym, info, i, pend[sym]["limit"] * (1 + FEE_M), reg, True)
                    del pend[sym]
                elif i >= pend[sym]["expire"]:
                    skipped["nofill"] += 1
                    del pend[sym]
                continue
            o = D[sym]["o"][i]
            ao = o if side == "long" else 1 / o
            open_pos(sym, info, i, ao * (1 + SLIP) * (1 + FEE), reg, False)
        nl = sum(1 for p in pos.values() if p["side"] == "long")
        max_same = max(max_same, nl, len(pos) - nl)
        # 2) 청산
        unreal = 0.0
        taker = (1 - SLIP) * (1 - FEE)
        for sym in list(pos):
            p, d = pos[sym], D[sym]
            if not d["ok"][i]:
                continue
            if p["side"] == "long":
                o, h, l, c = d["o"][i], d["h"][i], d["l"][i], d["c"][i]
            else:
                o, h, l, c = 1 / d["o"][i], 1 / d["l"][i], 1 / d["h"][i], 1 / d["c"][i]
            ex = why = None
            cost = taker
            first = p["fillbar"] and i == p["j"]            # 지정가로 방금 체결된 봉: 고가가 체결 전일 수 있어 익절은 다음 봉부터
            if l <= p["stop"]:
                ex, why = (p["stop"] if (o >= p["stop"] or first) else o), ("본전·추적" if p["moved"] else "손절")
            elif not first and ((h > p["tp"] * (1 + 1e-4)) if P["maker_tp"] else (h >= p["tp"])):
                ex, why = p["tp"], "익절"
                if P["maker_tp"]:
                    cost = 1 - FEE_M
            elif i - p["j"] >= p["hold"]:
                ex, why = c, "시간"
            elif P["tstop"] and i - p["j"] >= P["tstop"][0] and (c * taker / p["entry"] - 1) * 100 < P["tstop"][1]:
                ex, why = c, "시간손절"
            if ex is not None:
                pct = (ex * cost / p["entry"] - 1) * 100
                realized += pct / 100 * NOTIONAL
                trades.append((i, sym, p["side"], pct, why, i - p["j"], p["reg"], w))
                if pct < 0:
                    cool[sym] = i + 1 + cb.LOSS_COOLDOWN_MIN
                del pos[sym]
                continue
            p["maxfav"] = max(p["maxfav"], (h / p["entry"] - 1) * 100)
            if P["be"] and p["maxfav"] >= P["be"][0]:
                ns_ = p["entry"] * (1 + P["be"][1] / 100) / taker
                if ns_ > p["stop"]:
                    p["stop"], p["moved"] = ns_, True
            if P["trail"] and p["maxfav"] >= P["trail"][0]:
                ns_ = p["entry"] * (1 + p["maxfav"] / 100) * (1 - P["trail"][1] / 100)
                if ns_ > p["stop"]:
                    p["stop"], p["moved"] = ns_, True
            unreal += (c * taker / p["entry"] - 1) * NOTIONAL
        # 3) 헤지
        if P["hedge"]:
            if H:
                hedge_pnl += H * (bc[i] / bc[i - 1] - 1)
            net = sum((1 if p["side"] == "long" else -1) * NOTIONAL * (p["beta"] if P["hedge"][0] == "beta" else 1.0)
                      for p in pos.values())
            if P["hedge_free"]:
                net = math.copysign(max(0.0, abs(net) - P["hedge_free"]), net)
            target = -net * P["hedge"][1]
            if abs(target - H) >= P["hedge_band"]:
                hedge_fee += abs(target - H) * (FEE + SLIP)
                H = target
        eq.append(realized + hedge_pnl - hedge_fee + unreal)
    return summarize(trades, eq, hedge_pnl, hedge_fee, skipped, max_same, NW)


def summarize(trades, eq, hedge_pnl, hedge_fee, skipped, max_same, NW):
    n = len(trades)
    pcts = [t[3] for t in trades]
    wins = sum(1 for x in pcts if x > 0)
    peak, dd = 0.0, 0.0
    for v in eq:
        peak = max(peak, v)
        dd = min(dd, v - peak)
    days = [eq[min(len(eq) - 1, k + 95)] - (eq[k - 1] if k else 0.0) for k in range(0, len(eq), 96)]
    md = sum(days) / len(days) if days else 0
    sd = (sum((x - md) ** 2 for x in days) / len(days)) ** 0.5 if days else 0
    wins_w = [eq[min(len(eq) - 1, k + STEP - 1)] - (eq[k - 1] if k else 0.0) for k in range(0, len(eq), STEP)]

    def cell(f):
        xs = [t[3] for t in trades if f(t)]
        return [len(xs), round(sum(xs) / len(xs), 3) if xs else 0.0]
    why = {}
    for t in trades:
        a = why.setdefault(t[4], [0, 0.0, 0])
        a[0] += 1; a[1] += t[3]; a[2] += t[5]
    hold_w = [t[5] for t in trades if t[3] > 0]
    hold_l = [t[5] for t in trades if t[3] <= 0]
    med = lambda xs: (sorted(xs)[len(xs) // 2] * 15 / 60) if xs else 0
    return {"n": n, "win": round(wins / n * 100, 1) if n else 0, "avg": round(sum(pcts) / n, 3) if n else 0,
            "total": round(eq[-1], 1) if eq else 0, "tradePnl": round(sum(pcts) / 100 * NOTIONAL, 1),
            "hedge": round(hedge_pnl - hedge_fee, 1), "hedgeFee": round(hedge_fee, 1),
            "dd": round(dd, 1), "worstDay": round(min(days), 1) if days else 0, "bestDay": round(max(days), 1) if days else 0,
            "sharpe": round(md / sd * math.sqrt(365), 2) if sd > 0 else 0, "posDays": f"{sum(1 for x in days if x > 0)}/{len(days)}",
            "posWin": f"{sum(1 for x in wins_w if x > 0)}/{len(wins_w)}", "winPnl": [round(x, 1) for x in wins_w],
            "long": cell(lambda t: t[2] == "long"), "short": cell(lambda t: t[2] == "short"),
            "reg": {f"{r}·{s}": cell(lambda t, r=r, s=s: t[6] == r and t[2] == s)
                    for r in ("상승", "횡보", "하락") for s in ("long", "short")},
            "why": {k: [v[0], round(v[1] / v[0], 2), round(v[2] / v[0] * 15 / 60, 1)] for k, v in why.items()},
            "holdMedWinH": round(med(hold_w), 1), "holdMedLossH": round(med(hold_l), 1),
            "skipped": skipped, "maxSame": max_same}


def line(name, r):
    return (f"{name:<34} 거래 {r['n']:>4} · 승률 {r['win']:>4.1f}% · 거래당 {r['avg']:+.3f}% · 합계 {r['total']:+7.1f} "
            f"(거래 {r['tradePnl']:+.1f} 헤지 {r['hedge']:+.1f}·비용 {r['hedgeFee']:.1f}) · 최대낙폭 {r['dd']:+6.1f} · 최악하루 {r['worstDay']:+5.1f} · "
            f"샤프 {r['sharpe']:+.2f} · 플러스 구간 {r['posWin']} 날 {r['posDays']} · 롱 {r['long'][0]}회 {r['long'][1]:+.2f}% "
            f"숏 {r['short'][0]}회 {r['short'][1]:+.2f}% · 같은방향 최대 {r['maxSame']}")


# ---------------- B: 모든 종목 공통 규칙 (한데 모아 검증) ----------------
POOL_SPECS = 12 if SELFTEST else 25      # 작업 1개가 시험하는 무작위 규칙 수
POOL_CHUNKS = 2 if SELFTEST else 6       # 방향마다 작업 수 → 방향당 150개 규칙
POOL_TOP = 3                             # 방향마다 채택할 규칙 수
POOL_MIN_N = 40 if SELFTEST else 120
UNI = []                                 # 구간별 대상 종목 (main 에서 채움, 작업 프로세스가 물려받음)


def pool_task(args):
    """한 구간·한 방향: 무작위 규칙 POOL_SPECS개를 대상 종목 전부의 학습 구간에 돌려 '모은' 성적을 냄"""
    w, s0, side, chunk = args
    sp = SPACES["base"]
    cb.TP_CHOICES, cb.SL_CHOICES, cb.HOLD_CHOICES = sp["tp"], sp["sl"], sp["hold"]
    rng = random.Random(zlib.crc32(f"pool:{w}:{side}:{chunk}".encode()))
    specs = [cb.random_spec(rng) for _ in range(POOL_SPECS)]
    acc = [[0, 0.0, 0.0, 0, 0.0] for _ in specs]          # 학습 n, 합, 제곱합 / 검증 n, 합
    for sym in UNI[w]:
        ser = bb.series_of(candles(sym, s0 - TRAIN, s0, inv=(side == "short")))
        split = int(ser.n * (1 - cb.BT_HOLDOUT))
        for k, spec in enumerate(specs):
            for x in cb.backtest(ser, spec, end=split):
                acc[k][0] += 1; acc[k][1] += x; acc[k][2] += x * x
            for x in cb.backtest(ser, spec, start=split):
                acc[k][3] += 1; acc[k][4] += x
    return w, side, [(spec, a) for spec, a in zip(specs, acc)]


def pool_sig_task(args):
    """채택된 공통 규칙 1개를 다음 구간의 모든 대상 종목에 적용 → 종목별 신호"""
    w, s0, side, spec = args
    out = []
    e = min(s0 + STEP, len(T))
    bc = D["BTCUSDT"]["c"]
    for sym in UNI[w]:
        ser = bb.series_of(candles(sym, s0 - WARM, e, inv=(side == "short")))
        sig = ser.signal(spec)
        hits = [s0 - WARM + k for k, v in enumerate(sig) if v and s0 - WARM + k >= s0 - 1]
        if hits:
            out.append((sym, {"side": side, "spec": spec, "sig": hits, "beta": 1.0}))
    return w, out


def pooled_selection(pool, W0, NW):
    res = {}
    tasks = [(w, W0 + w * STEP, side, ch) for w in range(NW) for side in ("long", "short") for ch in range(POOL_CHUNKS)]
    for w, side, rows in pool.imap_unordered(pool_task, tasks, chunksize=1):
        res.setdefault((w, side), []).extend(rows)
    picks, info = [], []
    for w in range(NW):
        for side in ("long", "short"):
            cand = []
            for spec, (n, sm, sq, hn, hs) in res.get((w, side), []):
                if n < POOL_MIN_N:
                    continue
                mean = sm / n
                sd = max(1e-9, (sq / n - mean * mean)) ** 0.5
                t = mean / sd * n ** 0.5
                if mean < 0.10 or (hn >= 20 and hs / hn < 0):
                    continue
                cand.append((t, mean, n, hn, (hs / hn if hn else 0.0), spec))
            cand.sort(key=lambda x: -x[0])
            for t, mean, n, hn, hm, spec in cand[:POOL_TOP]:
                picks.append((w, W0 + w * STEP, side, spec))
                info.append((w, side, round(t, 2), round(mean, 3), n, hn, round(hm, 3)))
    sel = [[] for _ in range(NW)]
    for w, rows in pool.imap_unordered(pool_sig_task, picks, chunksize=1):
        sel[w].extend(rows)
    for w in range(NW):                                    # 거래대금 순서로(같은 종목이면 먼저 고른 규칙 먼저)
        rank = {s: i for i, s in enumerate(UNI[w])}
        sel[w].sort(key=lambda x: rank.get(x[0], 999))
    return sel, info


# ---------------- C: 시장 중립 묶음 (많이 오른 것 숏 + 많이 내린 것 롱, 또는 반대) ----------------
def basket(W0, NW, L, Hd, K, mode, cost):
    """L봉 수익률로 줄 세워 위 K개·아래 K개를 반대 방향으로 들고 Hd봉 뒤 전부 바꿈. cost = 왕복 비용(비율)"""
    NT = len(T)
    pnl, n, gross_sum, wins = [], 0, 0.0, 0
    for t in range(W0, NT - Hd, Hd):
        w = min((t - W0) // STEP, NW - 1)
        rows = []
        for sym in UNI[w]:
            d = D[sym]
            if not (d["ok"][t - 1] and d["ok"][t - 1 - L] and d["ok"][t] and d["ok"][t + Hd]):
                continue
            rows.append((d["c"][t - 1] / d["c"][t - 1 - L] - 1, sym))
        if len(rows) < 2 * K + 4:
            continue
        rows.sort()
        low, high = rows[:K], rows[-K:]
        longs, shorts = (low, high) if mode == "되돌림" else (high, low)
        step = 0.0
        for side, grp in ((1, longs), (-1, shorts)):
            for _, sym in grp:
                d = D[sym]
                g = side * (d["o"][t + Hd] / d["o"][t] - 1)
                gross_sum += g
                step += (g - cost) * NOTIONAL
                n += 1
                wins += 1 if g - cost > 0 else 0
        pnl.append(step)
    if not n:
        return None
    eq, peak, dd, acc = [], 0.0, 0.0, 0.0
    for x in pnl:
        acc += x
        peak = max(peak, acc)
        dd = min(dd, acc - peak)
    m = sum(pnl) / len(pnl)
    sd = (sum((x - m) ** 2 for x in pnl) / len(pnl)) ** 0.5
    per_year = 365 * 96 / Hd
    half = len(pnl) // 2
    return {"n": n, "gross": gross_sum / n * 100, "net": (gross_sum / n - cost) * 100, "total": acc, "dd": dd,
            "sharpe": m / sd * per_year ** 0.5 if sd > 0 else 0, "win": wins / n * 100,
            "h1": sum(pnl[:half]), "h2": sum(pnl[half:])}


ONLY_FLAT = {"횡보": ("long", "short")}
TREND_ONLY = {"상승": ("long",), "하락": ("short",)}
FLAT_SHORT_TREND = {"상승": ("long",), "하락": ("short",), "횡보": ("short",)}
FLAT_SHORT = {"횡보": ("short",)}
MK1 = {"maker_entry": 1, "maker_tp": True}
MK2 = {"maker_entry": 2, "maker_tp": True}
VARIANTS = [
    ("현재 방식(시장가)", {}),
    ("익절만 지정가", {"maker_tp": True}),
    ("진입·익절 지정가(1봉 대기)", MK1),
    ("진입·익절 지정가(2봉 대기)", MK2),
    ("같은 방향 최대 3", {"side_cap": 3}),
    ("같은 방향 3 + 지정가(2봉)", {"side_cap": 3, **MK2}),
    ("횡보에서만 진입", {"allow": ONLY_FLAT}),
    ("추세 방향만 진입", {"allow": TREND_ONLY}),
    ("횡보 숏 + 추세 방향", {"allow": FLAT_SHORT_TREND}),
    ("횡보 숏만", {"allow": FLAT_SHORT}),
    ("횡보 숏 + 추세 방향 + 지정가(2봉)", {"allow": FLAT_SHORT_TREND, **MK2}),
    ("횡보 숏+추세 방향·같은 방향 3·지정가", {"allow": FLAT_SHORT_TREND, "side_cap": 3, **MK2}),
]
VARIANTS_POOL = [
    ("공통 규칙(시장가)", {}),
    ("공통 규칙 + 같은 방향 3", {"side_cap": 3}),
    ("공통 규칙 + 지정가(2봉)", MK2),
    ("공통 규칙 + 같은 방향 3 + 지정가", {"side_cap": 3, **MK2}),
    ("공통 규칙 + 횡보 숏+추세 방향", {"allow": FLAT_SHORT_TREND}),
    ("공통 규칙 동시 20개(시장가)", {"max_open": 20}),
]


def show(name, r, NW):
    wp = r["winPnl"]
    h = len(wp) // 2
    print(line(name, r) + f" · 앞 절반 {sum(wp[:h]):+.1f} / 뒤 절반 {sum(wp[h:]):+.1f}"
          + (f" · 못 채운 지정가 {r['skipped']['nofill']}" if r["skipped"].get("nofill") else ""), flush=True)


def main():
    global UNI
    t0 = time.time()
    end = datetime.now(timezone.utc).date() - timedelta(days=1)
    start = end - timedelta(days=DAYS_BACK)
    if SELFTEST:
        raw = synth(CANDS[:12], 2600)
        SRC["name"] = "자체 점검용 가짜 시세"
        cb.BT_TRIALS = 30
    else:
        raw = load_archive(CANDS, start, end)
        if "BTCUSDT" not in raw or len(raw["BTCUSDT"]) < TRAIN + STEP * 3:
            SRC["name"] = "현물 API(선물 자료실 접속 실패)"
            raw = load_spot(CANDS, (DAYS_BACK + 1) * 96)
    build(raw)
    NT = len(T)
    W0 = TRAIN
    NW = (NT - W0 + STEP - 1) // STEP
    kst = lambda ms: datetime.fromtimestamp(ms / 1000, timezone(timedelta(hours=9))).strftime("%m-%d %H:%M")
    print(f"시세 출처: {SRC['name']} · 종목 {len(D)}개 · 15분봉 {NT}개 ({kst(T[0])} ~ {kst(T[-1])} KST) · 학습 {TRAIN}봉 → 다음 {STEP}봉씩 {NW}구간(검증 {(NT - W0) / 96:.0f}일) · 1회 {NOTIONAL:g} USDT")
    bc = D["BTCUSDT"]["c"]
    print(f"BTC 검증 기간 변화: {(bc[-1] / bc[W0] - 1) * 100:+.1f}% · 비용: 시장가 왕복 {(FEE + SLIP) * 200:.2f}% / 지정가 진입·익절 {FEE_M * 200:.2f}%")
    for w in range(NW):
        s0 = W0 + w * STEP
        e = min(s0 + STEP, NT)
        ok = [s for s in D if full(s, s0 - TRAIN, e)]
        ok.sort(key=lambda s: -sum(D[s]["q"][s0 - 96:s0]))
        UNI.append(ok[:TOPN])
    tasks = [("base", w, W0 + w * STEP, s) for w in range(NW) for s in UNI[w]]
    picked = [dict() for _ in range(NW)]
    out = {"src": SRC["name"], "bars": NT, "windows": NW, "from": kst(T[W0]), "to": kst(T[-1])}
    with mp.get_context("fork").Pool(os.cpu_count() or 2) as pool:
        t1 = time.time()
        for sp, w, sym, info in pool.imap_unordered(sel_task, tasks, chunksize=4):
            if info:
                picked[w][sym] = info
        print(f"종목별 전략 선정 {len(tasks)}건 · {time.time() - t1:.0f}초")
        t1 = time.time()
        psel, pinfo = pooled_selection(pool, W0, NW)
        print(f"공통 규칙 선정(구간·방향마다 무작위 {POOL_SPECS * POOL_CHUNKS}개를 {TOPN}종목에 모아 검증) · {time.time() - t1:.0f}초")
    ordered = [[(s, picked[w][s]) for s in UNI[w] if s in picked[w]] for w in range(NW)]

    print("\n===== A·D: 지금의 종목별 전략 선정 그대로 + 체결 방식·진입 조건만 바꿈")
    res = {}
    for name, p in VARIANTS:
        r = simulate(ordered, W0, NW, p)
        res[name] = r
        show(name, r, NW)
    b = res["현재 방식(시장가)"]
    print("  국면×방향(횟수·거래당%):", " · ".join(f"{k} {v[0]}회 {v[1]:+.2f}%" for k, v in b["reg"].items()))
    m = res["진입·익절 지정가(2봉 대기)"]
    print("  지정가(2봉) 청산 사유:", " · ".join(f"{k} {v[0]}회 {v[1]:+.2f}% {v[2]}h" for k, v in m["why"].items()))
    print("  지정가(2봉) 국면×방향:", " · ".join(f"{k} {v[0]}회 {v[1]:+.2f}%" for k, v in m["reg"].items()))
    out["perSymbol"] = res

    print(f"\n===== B: 모든 종목 공통 규칙 — 채택 {len(pinfo)}개(구간·방향마다 최대 {POOL_TOP}개, 모은 거래 {POOL_MIN_N}회 이상·거래당 +0.10% 이상·검증 구간 플러스)")
    ts = [x[2] for x in pinfo]
    ns = [x[4] for x in pinfo]
    if pinfo:
        print(f"  채택 규칙 학습 성적: 거래 수 중앙값 {sorted(ns)[len(ns) // 2]}회 · 거래당 평균 {sum(x[3] for x in pinfo) / len(pinfo):+.3f}% · t값 중앙값 {sorted(ts)[len(ts) // 2]:.1f} · 롱 {sum(1 for x in pinfo if x[1] == 'long')}개 숏 {sum(1 for x in pinfo if x[1] == 'short')}개")
    resp = {}
    for name, p in VARIANTS_POOL:
        r = simulate(psel, W0, NW, p)
        resp[name] = r
        show(name, r, NW)
    if resp:
        b = resp["공통 규칙(시장가)"]
        print("  국면×방향(횟수·거래당%):", " · ".join(f"{k} {v[0]}회 {v[1]:+.2f}%" for k, v in b["reg"].items()))
        print("  청산 사유:", " · ".join(f"{k} {v[0]}회 {v[1]:+.2f}% {v[2]}h" for k, v in b["why"].items()))
    out["pooled"] = resp

    print("\n===== C: 시장 중립 묶음 (위 5종목·아래 5종목, 포지션마다 50 USDT) — 수익률은 포지션 1개당 %")
    cost_t, cost_m = 2 * (FEE + SLIP), 2 * FEE_M
    rowsC = []
    for mode in ("되돌림", "추세"):
        for L in ((4, 16, 96) if SELFTEST else (4, 16, 96, 288, 672)):
            for Hd in ((4, 16) if SELFTEST else (4, 16, 96)):
                r = basket(W0, NW, L, Hd, 5, mode, cost_t)
                if not r:
                    continue
                rm = basket(W0, NW, L, Hd, 5, mode, cost_m)
                rowsC.append((mode, L, Hd, r, rm))
                print(f"{mode} · 기준 {L * 15 / 60:g}시간 수익률 · {Hd * 15 / 60:g}시간마다 교체: 거래 {r['n']} · 비용 전 {r['gross']:+.3f}% · "
                      f"시장가 {r['net']:+.3f}% 합계 {r['total']:+.0f}(낙폭 {r['dd']:+.0f}, 샤프 {r['sharpe']:+.2f}, 앞 {r['h1']:+.0f}/뒤 {r['h2']:+.0f}) · "
                      f"지정가 가정 {rm['net']:+.3f}% 합계 {rm['total']:+.0f}(앞 {rm['h1']:+.0f}/뒤 {rm['h2']:+.0f})", flush=True)
    out["basket"] = [{"mode": m_, "L": L, "H": Hd, "taker": r, "maker": rm} for m_, L, Hd, r, rm in rowsC]

    print("\n===== 구간별 손익(USDT) — BTC 변화와 함께")
    keys = [("현재", res["현재 방식(시장가)"]), ("지정가2", res["진입·익절 지정가(2봉 대기)"]), ("횡보숏+추세", res["횡보 숏 + 추세 방향"]),
            ("공통", resp["공통 규칙(시장가)"]), ("공통+지정가", resp["공통 규칙 + 지정가(2봉)"])]
    for w in range(NW):
        s0 = W0 + w * STEP
        e = min(s0 + STEP, NT) - 1
        print(f"[{w + 1:>2}] {kst(T[s0])}~ BTC {(bc[e] / bc[s0] - 1) * 100:+5.1f}% · " + " · ".join(f"{k} {r['winPnl'][w]:+6.1f}" for k, r in keys))
    for grp in (res, resp):
        for r in grp.values():
            r.pop("winPnl", None)
    json.dump(out, open("bt_summary.json", "w"), ensure_ascii=False, indent=1)
    print(f"\n소요 {time.time() - t0:.0f}초")


if __name__ == "__main__":
    main()
