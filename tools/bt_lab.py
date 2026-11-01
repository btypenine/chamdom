# -*- coding: utf-8 -*-
"""이번 실험(3차): 바이낸스 선물 조건 — 숏 허용 + 선물 비용
배경: 2차(11-01) 시험 기간 BTC가 -3~-9% 하락, 'BTC 상승 국면에서만 매수' 필터가 모든 봉 길이에서 +0.15~0.46%p 개선.
     → 하락장에 롱만 가능한 구조가 손실의 큰 원인. 선물은 숏 가능 + 호가가 촘촘해 체결 밀림이 작음.
질문: 보지 않은 다음 구간에서 (A) 업비트 비용·롱만 (B) 선물 비용·롱만 (C) 선물 비용·롱+숏 (D) C + BTC 국면(오를 땐 롱, 내릴 땐 숏)
      의 거래당 순익은?
- 숏 = 가격을 뒤집은 시계열(1/가격)에서의 롱으로 계산(짧은 보유에선 근사 오차 작음). 같은 무작위 조건·선정 방식으로 롱·숏 후보를 함께 고름
- 선물 비용: 시장가 수수료 0.05%(일반 등급 taker) + 체결 밀림 0.02%, 펀딩비는 보유 짧아 생략(보수적이지 않음 주의). 레버리지 1배 가정
- 데이터: 업비트 원화 시세(바이낸스 시세 대신 근사), 25종목, 8개 창, 같은 난수
"""
import importlib.util, os, random, time
spec_ = importlib.util.spec_from_file_location("bot", os.path.join(os.path.dirname(__file__), "..", "bot", "chamdom_bot.py"))
bot = importlib.util.module_from_spec(spec_); spec_.loader.exec_module(bot)
import requests

MARKETS = ["KRW-BTC", "KRW-ETH", "KRW-SOL", "KRW-DOGE", "KRW-XLM", "KRW-ADA", "KRW-SUI", "KRW-NEAR", "KRW-SAND", "KRW-ONDO",
           "KRW-WLD", "KRW-ENA", "KRW-TRUMP", "KRW-AVAX", "KRW-HBAR", "KRW-LINK", "KRW-UNI", "KRW-APT", "KRW-ARB", "KRW-SHIB",
           "KRW-AAVE", "KRW-PENGU", "KRW-AXS", "KRW-BAT", "KRW-ETC"]
TFS = {5: (2880, 288), 15: (1920, 192), 60: (1440, 120)}
REGIME_LEN = {5: 288, 15: 192, 60: 120}
WINDOWS = 8
UPBIT_FEE = bot.FEE_RATE
FUT_FEE, FUT_SLIP = 0.0005, 0.0002


def fetch(market, unit, total):
    out, to = [], None
    while len(out) < total:
        p = {"market": market, "count": min(200, total - len(out))}
        if to:
            p["to"] = to
        for a in range(5):
            r = requests.get(f"{bot.UPBIT}/v1/candles/minutes/{unit}", params=p, timeout=10)
            if r.status_code == 429:
                time.sleep(1 + a); continue
            break
        data = r.json()
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        to = data[-1]["candle_date_time_utc"] + "Z"
        time.sleep(0.12)
    out.reverse()
    return out


real_tick = {}
try:
    r = requests.get(f"{bot.UPBIT}/v1/orderbook/instruments", params={"markets": ",".join(MARKETS)}, timeout=10)
    for d in r.json():
        real_tick[d["market"]] = float(d["tick_size"])
except Exception as e:
    print("instruments fail", e)


def inverted(cl):
    """숏 계산용: 가격을 뒤집은 캔들 (고가↔저가)"""
    out = []
    for c in cl:
        out.append({"opening_price": 1 / c["opening_price"], "high_price": 1 / c["low_price"], "low_price": 1 / c["high_price"],
                    "trade_price": 1 / c["trade_price"], "candle_acc_trade_volume": c["candle_acc_trade_volume"],
                    "candle_date_time_utc": c["candle_date_time_utc"]})
    return out


def sub_of(s, end):
    sub = bot.Series.__new__(bot.Series)
    for a in "ohlcv":
        setattr(sub, a, getattr(s, a)[:end])
    sub.n, sub._cache, sub.slip = end, {}, s.slip
    return sub


def candidates(s, end, w):
    sub = sub_of(s, end)
    split = int(end * (1 - bot.BT_HOLDOUT))
    rng = random.Random(1000 + w)
    out = []
    for _ in range(bot.BT_TRIALS):
        sp = bot.random_spec(rng)
        tr = bot.stats(bot.backtest(sub, sp, end=split))
        if tr["n"] < 4 or tr["edge"] < 0:
            continue
        out.append((sp, tr, bot.stats(bot.backtest(sub, sp, start=split))))
    return out


def score_pick(cands):
    mn, me, fl = bot.EXPLORE_MIN_TRADES, bot.EXPLORE_MIN_EDGE, bot.EXPLORE_HOLDOUT_FLOOR
    best = None
    for item in cands:
        sp, tr, ho = item[0], item[1], item[2]
        if tr["n"] < mn or tr["edge"] < me or (ho["n"] and ho["edge"] < fl):
            continue
        score = tr["edge"] if not ho["n"] else (tr["edge"] * tr["n"] + ho["edge"] * ho["n"]) / (tr["n"] + ho["n"])
        if best is None or score > best[1]:
            best = (item, score)
    return best[0] if best else None


def sim(series, spec, start, end, ok=None):
    sig = series.signal(spec)
    out, i, cd = [], start, -1
    slip = series.slip
    while i < end - 1:
        if i < cd or not sig[i] or (ok is not None and not ok[i]):
            i += 1; continue
        j = i + 1
        entry = series.o[j] * (1 + slip) * (1 + bot.FEE_RATE)
        tp, sl = bot.target_price(entry, spec.get("tp", bot.TARGET_NET_PCT)), entry * (1 + spec["sl"] / 100)
        ex, k = None, j
        while k < end:
            if series.l[k] <= sl: ex = sl; break
            if series.h[k] >= tp: ex = tp; break
            if k - j >= spec["hold"]: ex = series.c[k]; break
            k += 1
        if ex is None: break
        pnl = (ex * (1 - slip) * (1 - bot.FEE_RATE) / entry - 1) * 100
        out.append(pnl); i = k + 1
        if pnl < 0: cd = i + bot.LOSS_COOLDOWN_MIN
    return out


btc_up = {}
for tf, (train, step) in TFS.items():
    cs = fetch("KRW-BTC", tf, train + WINDOWS * step)
    closes = [c["trade_price"] for c in cs]
    e = bot.ema(closes, REGIME_LEN[tf])
    btc_up[tf] = {c["candle_date_time_utc"][:16]: closes[i] > e[i] for i, c in enumerate(cs)}
    print(f"BTC {tf}분봉 시험 기간 등락 {(closes[-1] / closes[train] - 1) * 100:+.2f}%", flush=True)

KEYS = ["A 업비트·롱만", "B 선물·롱만", "C 선물·롱+숏", "D 선물·롱+숏+BTC국면"]
res = {tf: {k: [] for k in KEYS} for tf in TFS}
side_n = {tf: {"long": 0, "short": 0} for tf in TFS}
t0 = time.time()
for mk in MARKETS:
    line = []
    for tf, (train, step) in TFS.items():
        total = train + WINDOWS * step
        try:
            cl = fetch(mk, tf, total)
        except Exception:
            line.append(f"{tf}분 실패"); continue
        if len(cl) < total - 20:
            line.append(f"{tf}분 짧음"); continue
        up_ok = [btc_up[tf].get(c["candle_date_time_utc"][:16], False) for c in cl]
        dn_ok = [not x for x in up_ok]
        price = cl[-1]["trade_price"]
        s_up = bot.Series(cl); s_up.slip = bot.BT_SLIP_BASE + (real_tick.get(mk) or bot.tick_size(price)) / price
        f_long = bot.Series(cl); f_long.slip = FUT_SLIP
        f_short = bot.Series(inverted(cl)); f_short.slip = FUT_SLIP
        for w in range(WINDOWS):
            end = train + w * step
            stop = min(end + step, len(cl))
            # A: 업비트 비용
            bot.FEE_RATE = UPBIT_FEE
            a = score_pick(candidates(s_up, end, w))
            if a:
                res[tf][KEYS[0]] += sim(s_up, a[0], end, stop)
            # B·C·D: 선물 비용
            bot.FEE_RATE = FUT_FEE
            cl_long = [(sp, tr, ho, "long") for sp, tr, ho in candidates(f_long, end, w)]
            cl_short = [(sp, tr, ho, "short") for sp, tr, ho in candidates(f_short, end, w)]
            b = score_pick(cl_long)
            if b:
                res[tf][KEYS[1]] += sim(f_long, b[0], end, stop)
            c = score_pick(cl_long + cl_short)
            if c:
                ser, ok = (f_long, up_ok) if c[3] == "long" else (f_short, dn_ok)
                r = sim(ser, c[0], end, stop)
                res[tf][KEYS[2]] += r
                side_n[tf][c[3]] += len(r)
                res[tf][KEYS[3]] += sim(ser, c[0], end, stop, ok)
            bot.FEE_RATE = UPBIT_FEE
        line.append(f"{tf}분 " + "/".join(str(len(res[tf][k])) for k in KEYS))
    print(f"== {mk} · " + " · ".join(line) + f" ({time.time() - t0:.0f}s)", flush=True)

summary = {}
for tf in TFS:
    print(f"\n#### {tf}분봉 (보지 않은 다음 구간 × {WINDOWS}창, {len(MARKETS)}종목) · C의 롱/숏 거래 {side_n[tf]}")
    for k in KEYS:
        r = res[tf][k]
        st = bot.stats(r) if r else {"n": 0, "winrate": 0, "edge": 0, "total": 0, "pf": 0}
        summary[f"{tf}-{k}"] = st
        print(f"{k}: {st['n']}건 · 승률 {st['winrate'] * 100:.0f}% · 거래당 {st['edge']:+.3f}% · 합계 {st['total']:+.1f}% · 손익비 {min(st['pf'], 99):.2f}")
import json
json.dump(summary, open("bt_summary.json", "w"), ensure_ascii=False)
