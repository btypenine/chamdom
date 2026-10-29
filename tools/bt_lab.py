# -*- coding: utf-8 -*-
"""이번 실험: 'BTC 하락 추세' 시장 관문 (GitHub Actions에서 실행, 실제 주문 없음)
배경: 10-29 10~11시(한국시간) BTC가 2시간 가까이 서서히 밀리는 동안 봇이 RSI과매도·단기급락 신호로 BTC·ETH·SOL·ONDO 등을 계속 삼
      → 3시간 동안 실전 7건 중 6건이 '시장 전체 하락'으로 손실. 기존 'BTC 급락(30분 -0.5%)' 관문은 기준선 근처에서 켜졌다 꺼졌다 함
질문: 매수 시점에 BTC가 최근 N분 동안 X% 이상 빠져 있으면 막는 관문이, '보지 않은 다음 6시간' 거래 중
      막았을 거래(거래당 순익)가 통과 거래보다 확실히 나쁜가?
- 롤링 워크포워드: 과거 3일(4320분)로 현재 봇 기준으로 전략을 고르고 다음 6시간 성적, 6개 창 (이전 실험과 동일)
- 미끄러짐: 업비트 실제 호가 단위 기준. 매수 시각(분)으로 BTC 1분봉과 맞춰 BTC 변화율 계산
"""
import importlib.util, json, os, random, time
spec_ = importlib.util.spec_from_file_location("bot", os.path.join(os.path.dirname(__file__), "..", "bot", "chamdom_bot.py"))
bot = importlib.util.module_from_spec(spec_); spec_.loader.exec_module(bot)
import requests

MARKETS = ["KRW-BTC", "KRW-IRYS", "KRW-ETH", "KRW-NEAR", "KRW-SAND", "KRW-ONDO", "KRW-WLD", "KRW-BIRB", "KRW-SOL", "KRW-SUI",
           "KRW-DOGE", "KRW-ENA", "KRW-XLM", "KRW-ADA", "KRW-ORDER", "KRW-ERA", "KRW-ARK", "KRW-ORCA", "KRW-TRUMP", "KRW-DRV",
           "KRW-WAL", "KRW-UP2", "KRW-STX", "KRW-IN", "KRW-T", "KRW-AXS", "KRW-DATA", "KRW-W", "KRW-AVAX", "KRW-HBAR",
           "KRW-TRAC", "KRW-LINK", "KRW-TREE", "KRW-LA", "KRW-IOST", "KRW-UNI", "KRW-APT", "KRW-XPL", "KRW-ARB", "KRW-0G",
           "KRW-PUMP", "KRW-MINA", "KRW-SHIB", "KRW-ZRO", "KRW-MEGA", "KRW-BAT", "KRW-VIRTUAL", "KRW-PENGU", "KRW-KAT", "KRW-AAVE"]
TRAIN, STEP, WINDOWS = 4320, 360, 6
TOTAL = TRAIN + WINDOWS * STEP
up = bot.Upbit("x", "y")

# (BTC 살펴볼 분, 이 % 이하면 막음)
VARIANTS = {"30분 -0.5% (지금 BTC 급락 관문)": (30, -0.5), "60분 -0.7%": (60, -0.7), "120분 -1.0%": (120, -1.0),
            "120분 -0.7%": (120, -0.7), "240분 -1.5%": (240, -1.5), "240분 -1.0%": (240, -1.0)}

real_tick = {}
for i in range(0, len(MARKETS), 20):
    try:
        r = requests.get(f"{bot.UPBIT}/v1/orderbook/instruments", params={"markets": ",".join(MARKETS[i:i + 20])}, timeout=10)
        for d in r.json():
            real_tick[d["market"]] = float(d["tick_size"])
    except Exception as e:
        print("instruments fail", e)
    time.sleep(0.3)


def backtest_t(series, spec, start, end):
    """bot.backtest 와 같고 (매수 봉 인덱스, %) 를 돌려줌"""
    sig = series.signal(spec)
    out, i, cooldown_until = [], start, -1
    slip = series.slip
    while i < end - 1:
        if i < cooldown_until or not sig[i]:
            i += 1
            continue
        j = i + 1
        entry = series.o[j] * (1 + slip) * (1 + bot.FEE_RATE)
        tp, sl = bot.target_price(entry, spec.get("tp", bot.TARGET_NET_PCT)), entry * (1 + spec["sl"] / 100)
        exit_price, k = None, j
        while k < end:
            if series.l[k] <= sl:
                exit_price = sl; break
            if series.h[k] >= tp:
                exit_price = tp; break
            if k - j >= spec["hold"]:
                exit_price = series.c[k]; break
            k += 1
        if exit_price is None:
            break
        pnl = (exit_price * (1 - slip) * (1 - bot.FEE_RATE) / entry - 1) * 100
        out.append((i, pnl))
        i = k + 1
        if pnl < 0:
            cooldown_until = i + bot.LOSS_COOLDOWN_MIN
    return out


def pick(cands):
    mn, me, fl = bot.EXPLORE_MIN_TRADES, bot.EXPLORE_MIN_EDGE, bot.EXPLORE_HOLDOUT_FLOOR
    best = None
    for sp, tr, ho in cands:
        if tr["n"] < mn or tr["edge"] < me:
            continue
        if ho["n"] and ho["edge"] < fl:
            continue
        score = tr["edge"] if not ho["n"] else (tr["edge"] * tr["n"] + ho["edge"] * ho["n"]) / (tr["n"] + ho["n"])
        if best is None or score > best[1]:
            best = (sp, score)
    return best[0] if best else None


print("현재 봇 값:", bot.EXPLORE_MIN_TRADES, bot.EXPLORE_MIN_EDGE, bot.EXPLORE_HOLDOUT_FLOOR, "· 시험 수", bot.BT_TRIALS, flush=True)
btc_c = up.minute_candles("KRW-BTC", TOTAL + 300)
btc = {c["candle_date_time_utc"][:16]: c["trade_price"] for c in btc_c}
btc_keys = sorted(btc)
btc_idx = {k: n for n, k in enumerate(btc_keys)}


def btc_chg(ts, mins):
    n = btc_idx.get(ts)
    if n is None or n < mins:
        return None
    return (btc[btc_keys[n]] / btc[btc_keys[n - mins]] - 1) * 100


summary = {k: {"blocked": [0, 0.0, 0], "passed": [0, 0.0, 0], "hours": set()} for k in VARIANTS}
allt = [0, 0.0]
t0 = time.time()
for mk in MARKETS:
    try:
        candles = up.minute_candles(mk, TOTAL)
    except Exception as e:
        print(mk, "fail", e); continue
    s = bot.Series(candles)
    ts = [c["candle_date_time_utc"][:16] for c in candles]
    if s.n < TOTAL - 50:
        print(mk, "short", s.n); continue
    price = s.c[-1]
    s.slip = bot.BT_SLIP_BASE + (real_tick.get(mk) or bot.tick_size(price)) / price
    nt = 0
    for w in range(WINDOWS):
        end = TRAIN + w * STEP
        sub = bot.Series.__new__(bot.Series)
        for a in "ohlcv":
            setattr(sub, a, getattr(s, a)[:end])
        sub.n, sub._cache, sub.slip = end, {}, s.slip
        split = int(end * (1 - bot.BT_HOLDOUT))
        rng = random.Random(1000 + w)
        cands = []
        for _ in range(bot.BT_TRIALS):
            sp = bot.random_spec(rng)
            tr = bot.stats(bot.backtest(sub, sp, end=split))
            if tr["n"] < 4 or tr["edge"] < 0:
                continue
            ho = bot.stats(bot.backtest(sub, sp, start=split))
            cands.append((sp, tr, ho))
        sp = pick(cands)
        if not sp:
            continue
        for i, pnl in backtest_t(s, sp, end, min(end + STEP, s.n)):
            nt += 1; allt[0] += 1; allt[1] += pnl
            for k, (mins, th) in VARIANTS.items():
                ch = btc_chg(ts[i], mins)
                if ch is None:
                    continue
                side = "blocked" if ch <= th else "passed"
                d = summary[k][side]; d[0] += 1; d[1] += pnl; d[2] += pnl > 0
                if side == "blocked":
                    summary[k]["hours"].add(ts[i][:13])
    print(f"== {mk} 거래 {nt} ({time.time() - t0:.0f}s)", flush=True)

print(f"\n#### 합계 (보지 않은 다음 6시간, 전체 거래 {allt[0]} · 거래당 {allt[1] / max(allt[0], 1):+.3f}%)")
print("관문 판정 기준(봇과 같음): 막았을 거래 20건↑·3개 시간대↑, 거래당 0.15%p 이상 나쁘고 그 자체도 손실")
out = {}
for k, d in summary.items():
    b, p = d["blocked"], d["passed"]
    ba, pa = (b[1] / b[0] if b[0] else 0), (p[1] / p[0] if p[0] else 0)
    ok = b[0] >= 20 and len(d["hours"]) >= 3 and ba <= pa - 0.15 and ba < 0
    print(f"{k}: 막았을 거래 {b[0]}건(시간대 {len(d['hours'])}) 승률 {b[2] / max(b[0], 1) * 100:.0f}% 거래당 {ba:+.3f}% | 통과 {p[0]}건 승률 {p[2] / max(p[0], 1) * 100:.0f}% 거래당 {pa:+.3f}% | 차이 {ba - pa:+.3f}%p → {'적용 기준 충족' if ok else '미충족'}")
    out[k] = {"blocked": b, "passed": p, "hours": len(d["hours"]), "blockedAvg": ba, "passedAvg": pa, "meets": ok}
json.dump(out, open("bt_summary.json", "w"), ensure_ascii=False, indent=1)
