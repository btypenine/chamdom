# -*- coding: utf-8 -*-
"""bt-lab 실험 8: 바이낸스 선물 '포트폴리오' 걸어가며 검증 — 단기 청산·같은 방향 제한·국면 필터·BTC 헤지 비교
배경(11-23 대표님 지시): 사람처럼 길게 보지 말고 기계답게 단기·순간 변화로 수익, 시장 흐름이 문제면 헤지.
  11-22~23 실주문 34회 -28 USDT: 숏 31회가 BTC +4.5% 상승과 겹쳐 손절 몰림, 익절 +3% vs 손절 -5%·최대 60시간.
  이긴 거래는 빨리 끝나고(중앙값 약 1.3시간) 진 거래는 오래 끌다(중앙값 약 9.5시간) 손절 → 청산·노출 관리가 핵심 후보.
방법
- 시세: 바이낸스 공개 자료실(data.binance.vision)의 USDⓈ-M 선물 15분봉 실제 기록. 안 되면 현물 API 로 대체(출처 표시)
- 구간마다: 직전 20일(1920봉)로 종목별 전략 1개(롱·숏 중 좋은 쪽) 선정 → 다음 5일(480봉)에 그대로 적용, 5일씩 앞으로
- 대상: 봇이 실제로 고르던 종목 + 주요 종목 중, 구간 시작 직전 24시간 거래대금 상위 40개
- 한 계좌처럼 시간순으로 진행: 동시 보유 한도·같은 방향 제한·국면 필터·헤지·본전 손절·시간 손절을 실제 순서대로 적용
- 비용: 수수료 0.05%x2 + 미끄러짐 0.02%x2 (봇과 같음), 헤지 조정도 같은 비용. 펀딩비는 미반영
- 1회 50 USDT 고정, 손익은 USDT. 최대낙폭·최악의 하루는 미실현 손익 포함 봉 단위 자산 기준
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
    "short": {"tp": [0.8, 1.0, 1.5, 2.0, 3.0], "sl": [-1.0, -1.5, -2.0, -3.0], "hold": [8, 16, 30]},          # 단기형: 최대 2~7.5시간, 손절 -3% 이내
    "mid": {"tp": [1.5, 2.0, 3.0], "sl": [-1.5, -2.0, -3.0], "hold": [30, 60]},                               # 균형형: 최대 7.5~15시간, 손절 -3% 이내
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
          "tstop": None, "sl_cap": None, "hold_cap": None, "hedge": None, "hedge_band": 25.0, "hedge_free": 0.0}


def simulate(sel, W0, NW, P):
    """sel[w] = [(sym, info), ...] 거래대금 순. 반환: 지표 dict"""
    P = {**BASE_P, **P}
    NT = len(T)
    bc = D["BTCUSDT"]["c"]
    pos, cool, trades = {}, {}, []
    realized = hedge_pnl = hedge_fee = 0.0
    H = 0.0
    eq = []
    skipped = {"cap": 0, "side": 0, "net": 0, "regime": 0, "flow": 0}
    max_same = 0
    sigsets = [{sym: set(info["sig"]) for sym, info in sel[w]} for w in range(NW)]
    for i in range(W0, NT):
        w = min((i - W0) // STEP, NW - 1)
        b24 = (bc[i - 1] / bc[i - 1 - REG_BARS] - 1) * 100
        b2 = (bc[i - 1] / bc[i - 1 - FLOW_BARS] - 1) * 100
        reg = "상승" if b24 >= REG_UP else ("하락" if b24 <= REG_DOWN else "횡보")
        # 1) 진입 (직전 봉 신호 → 이번 봉 시가)
        for sym, info in sel[w]:
            if (i - 1) not in sigsets[w][sym] or sym in pos or cool.get(sym, 0) > i or not D[sym]["ok"][i]:
                continue
            side = info["side"]
            nl = sum(1 for p in pos.values() if p["side"] == "long")
            ns = len(pos) - nl
            if len(pos) >= P["max_open"]:
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
            if P["flow"] is not None and b2 * (1 if side == "short" else -1) >= P["flow"]:
                skipped["flow"] += 1
                continue
            spec = info["spec"]
            o = D[sym]["o"][i]
            ao = o if side == "long" else 1 / o
            entry = ao * (1 + SLIP) * (1 + FEE)
            slp = spec["sl"] if P["sl_cap"] is None else max(spec["sl"], P["sl_cap"])
            hold = spec["hold"] if P["hold_cap"] is None else min(spec["hold"], P["hold_cap"])
            pos[sym] = {"side": side, "entry": entry, "tp": cb.target_price(entry, spec["tp"]), "stop": entry * (1 + slp / 100),
                        "j": i, "hold": hold, "maxfav": 0.0, "reg": reg, "beta": info["beta"], "moved": False}
        nl = sum(1 for p in pos.values() if p["side"] == "long")
        max_same = max(max_same, nl, len(pos) - nl)
        # 2) 청산
        unreal = 0.0
        for sym in list(pos):
            p, d = pos[sym], D[sym]
            if not d["ok"][i]:
                continue
            if p["side"] == "long":
                o, h, l, c = d["o"][i], d["h"][i], d["l"][i], d["c"][i]
            else:
                o, h, l, c = 1 / d["o"][i], 1 / d["l"][i], 1 / d["h"][i], 1 / d["c"][i]
            ex = why = None
            if l <= p["stop"]:
                ex, why = (p["stop"] if o >= p["stop"] else o), ("본전·추적" if p["moved"] else "손절")
            elif h >= p["tp"]:
                ex, why = p["tp"], "익절"
            elif i - p["j"] >= p["hold"]:
                ex, why = c, "시간"
            elif P["tstop"] and i - p["j"] >= P["tstop"][0] and (c * (1 - SLIP) * (1 - FEE) / p["entry"] - 1) * 100 < P["tstop"][1]:
                ex, why = c, "시간손절"
            if ex is not None:
                pct = (ex * (1 - SLIP) * (1 - FEE) / p["entry"] - 1) * 100
                realized += pct / 100 * NOTIONAL
                trades.append((i, sym, p["side"], pct, why, i - p["j"], p["reg"], w))
                if pct < 0:
                    cool[sym] = i + 1 + cb.LOSS_COOLDOWN_MIN
                del pos[sym]
                continue
            p["maxfav"] = max(p["maxfav"], (h / p["entry"] - 1) * 100)
            if P["be"] and p["maxfav"] >= P["be"][0]:
                ns_ = p["entry"] * (1 + P["be"][1] / 100) / ((1 - SLIP) * (1 - FEE))
                if ns_ > p["stop"]:
                    p["stop"], p["moved"] = ns_, True
            if P["trail"] and p["maxfav"] >= P["trail"][0]:
                ns_ = p["entry"] * (1 + p["maxfav"] / 100) * (1 - P["trail"][1] / 100)
                if ns_ > p["stop"]:
                    p["stop"], p["moved"] = ns_, True
            unreal += (c * (1 - SLIP) * (1 - FEE) / p["entry"] - 1) * NOTIONAL
        # 3) 헤지 (BTC 선물로 순노출 상쇄): 이번 봉 동안 들고 있던 헤지의 손익 → 봉 끝에서 목표로 조정
        if P["hedge"]:
            if H:
                hedge_pnl += H * (bc[i] / bc[i - 1] - 1)
            net = sum((1 if p["side"] == "long" else -1) * NOTIONAL * (p["beta"] if P["hedge"][0] == "beta" else 1.0)
                      for p in pos.values())
            if P["hedge_free"]:                                  # 여유분까지는 그대로 두고, 넘는 만큼만 상쇄
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


VARIANTS = [
    ("현재 방식(제한 없음)", {}),
    ("같은 방향 최대 3", {"side_cap": 3}),
    ("같은 방향 최대 5", {"side_cap": 5}),
    ("롱-숏 차이 최대 2", {"net_cap": 2}),
    ("국면 필터(BTC 24h ±1.5%)", {"regime": True}),
    ("BTC 2시간 역행 1% 쉬기", {"flow": 1.0}),
    ("헤지 100%(금액 기준)", {"hedge": ("flat", 1.0)}),
    ("헤지 100%(민감도 반영)", {"hedge": ("beta", 1.0)}),
    ("헤지 50%(민감도 반영)", {"hedge": ("beta", 0.5)}),
    ("헤지 100%·조정폭 75", {"hedge": ("beta", 1.0), "hedge_band": 75.0}),
    ("헤지: 순노출 100 넘는 만큼만", {"hedge": ("beta", 1.0), "hedge_free": 100.0, "hedge_band": 50.0}),
    ("헤지: 순노출 150 넘는 만큼만", {"hedge": ("beta", 1.0), "hedge_free": 150.0, "hedge_band": 50.0}),
    ("본전 손절(+1.0% 뒤)", {"be": (1.0, 0.0)}),
    ("본전 손절(+1.5% 뒤)", {"be": (1.5, 0.0)}),
    ("본전+0.3% 잠금(+1.5% 뒤)", {"be": (1.5, 0.3)}),
    ("추적 손절(+1.5% 뒤 1.0% 간격)", {"trail": (1.5, 1.0)}),
    ("시간 손절(4시간 뒤 손실이면)", {"tstop": (16, 0.0)}),
    ("시간 손절(8시간 뒤 손실이면)", {"tstop": (32, 0.0)}),
    ("손절 최대 -3%", {"sl_cap": -3.0}),
    ("손절 최대 -2%", {"sl_cap": -2.0}),
    ("최대 보유 7.5시간", {"hold_cap": 30}),
    ("최대 보유 15시간", {"hold_cap": 60}),
    ("조합A: 방향3+국면", {"side_cap": 3, "regime": True}),
    ("조합B: A+본전(+1.0%)", {"side_cap": 3, "regime": True, "be": (1.0, 0.0)}),
    ("조합C: B+시간 손절 8h", {"side_cap": 3, "regime": True, "be": (1.0, 0.0), "tstop": (32, 0.0)}),
    ("조합D: B+손절 -3%", {"side_cap": 3, "regime": True, "be": (1.0, 0.0), "sl_cap": -3.0}),
    ("조합E: 본전+시간8h+손절-3%", {"be": (1.0, 0.0), "tstop": (32, 0.0), "sl_cap": -3.0}),
    ("조합F: E+헤지100%(민감도)", {"be": (1.0, 0.0), "tstop": (32, 0.0), "sl_cap": -3.0, "hedge": ("beta", 1.0)}),
    ("조합G: 국면+본전+헤지50%", {"regime": True, "be": (1.0, 0.0), "hedge": ("beta", 0.5)}),
    ("조합H: 차이2+본전+시간8h", {"net_cap": 2, "be": (1.0, 0.0), "tstop": (32, 0.0)}),
]
VARIANTS_ALT = [
    ("제한 없음", {}),
    ("방향3+국면", {"side_cap": 3, "regime": True}),
    ("본전(+1.0%)", {"be": (1.0, 0.0)}),
    ("방향3+국면+본전", {"side_cap": 3, "regime": True, "be": (1.0, 0.0)}),
    ("헤지 100%(민감도)", {"hedge": ("beta", 1.0)}),
    ("차이2+헤지50%", {"net_cap": 2, "hedge": ("beta", 0.5)}),
    ("헤지: 순노출 100 넘는 만큼만", {"hedge": ("beta", 1.0), "hedge_free": 100.0, "hedge_band": 50.0}),
]


def main():
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
    print(f"시세 출처: {SRC['name']} · 종목 {len(D)}개 받음(후보 {len(CANDS)}) · 15분봉 {NT}개 ({kst(T[0])} ~ {kst(T[-1])} KST)")
    print(f"학습 {TRAIN}봉 → 다음 {STEP}봉씩 {NW}구간 (검증 {(NT - W0) / 96:.0f}일) · 구간마다 직전 24시간 거래대금 상위 {TOPN}종목 · 1회 {NOTIONAL:g} USDT · 받는 데 {time.time() - t0:.0f}초")
    bc = D["BTCUSDT"]["c"]
    print(f"BTC 검증 기간 변화: {(bc[-1] / bc[W0] - 1) * 100:+.1f}%")
    # 구간별 대상 종목
    uni = []
    for w in range(NW):
        s0 = W0 + w * STEP
        e = min(s0 + STEP, NT)
        ok = [s for s in D if full(s, s0 - TRAIN, e)]
        ok.sort(key=lambda s: -sum(D[s]["q"][s0 - 96:s0]))
        uni.append(ok[:TOPN])
    print("구간별 대상 종목 수:", " ".join(str(len(u)) for u in uni))
    tasks = [(sp, w, W0 + w * STEP, s) for sp in SPACES for w in range(NW) for s in uni[w]]
    sel = {sp: [dict() for _ in range(NW)] for sp in SPACES}
    t1 = time.time()
    with mp.get_context("fork").Pool(os.cpu_count() or 2) as pool:
        for sp, w, sym, info in pool.imap_unordered(sel_task, tasks, chunksize=4):
            if info:
                sel[sp][w][sym] = info
    print(f"전략 선정 {len(tasks)}건 · {time.time() - t1:.0f}초 (CPU {os.cpu_count()})")
    out = {"src": SRC["name"], "bars": NT, "windows": NW, "from": kst(T[W0]), "to": kst(T[-1]), "spaces": {}}
    for sp in SPACES:
        ordered = [[(s, sel[sp][w][s]) for s in uni[w] if s in sel[sp][w]] for w in range(NW)]
        specs = [info["spec"] for w in range(NW) for _, info in ordered[w]]
        sides = [info["side"] for w in range(NW) for _, info in ordered[w]]
        cnt = lambda key: " ".join(f"{k:g}:{v}" for k, v in sorted({x: sum(1 for s in specs if s[key] == x) for x in set(s[key] for s in specs)}.items()))
        print(f"\n===== 전략 모양 '{sp}' — 익절 {SPACES[sp]['tp']} · 손절 {SPACES[sp]['sl']} · 최대 보유(15분봉 수) {SPACES[sp]['hold']}")
        print(f"채택 {len(specs)}개(구간·종목) · 롱 {sides.count('long')} 숏 {sides.count('short')} · 익절폭 [{cnt('tp')}] · 손절폭 [{cnt('sl')}] · 보유 [{cnt('hold')}]")
        res = {}
        for name, p in (VARIANTS if sp == "base" else VARIANTS_ALT):
            r = simulate(ordered, W0, NW, p)
            res[name] = r
            print(line(name, r), flush=True)
        out["spaces"][sp] = res
        b = res[(VARIANTS if sp == "base" else VARIANTS_ALT)[0][0]]
        print("  청산 사유(횟수·평균%·평균 보유시간):", " · ".join(f"{k} {v[0]}회 {v[1]:+.2f}% {v[2]}h" for k, v in b["why"].items()),
              f"· 이긴 거래 보유 중앙값 {b['holdMedWinH']}h / 진 거래 {b['holdMedLossH']}h")
        print("  국면×방향(횟수·거래당%):", " · ".join(f"{k} {v[0]}회 {v[1]:+.2f}%" for k, v in b["reg"].items()))
        print("  못 들어간 신호:", b["skipped"])
    # 구간별 흐름: 현재 방식과 주요 조합
    print("\n===== 구간별 손익(USDT, base 모양) — BTC 변화와 함께")
    keys = ["현재 방식(제한 없음)", "조합A: 방향3+국면", "조합B: A+본전(+1.0%)", "헤지 100%(민감도 반영)", "조합F: E+헤지100%(민감도)"]
    for w in range(NW):
        s0 = W0 + w * STEP
        e = min(s0 + STEP, NT) - 1
        row = " · ".join(f"{k.split(':')[0]} {out['spaces']['base'][k]['winPnl'][w]:+6.1f}" for k in keys if w < len(out['spaces']['base'][k]['winPnl']))
        print(f"[{w + 1:>2}] {kst(T[s0])}~ BTC {(bc[e] / bc[s0] - 1) * 100:+5.1f}% · {row}")
    for sp in out["spaces"]:
        for r in out["spaces"][sp].values():
            r.pop("winPnl", None)
    json.dump(out, open("bt_summary.json", "w"), ensure_ascii=False, indent=1)
    print(f"\n소요 {time.time() - t0:.0f}초")


if __name__ == "__main__":
    main()
