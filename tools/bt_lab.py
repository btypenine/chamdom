# -*- coding: utf-8 -*-
"""이번 실험: 청산 판단 '수익 중 시한 임박 정리' — 보유시간의 일정 비율이 지났고 지금 순손익(수수료·미끄러짐 포함)이
기준 % 이상 플러스면 그 자리에서 정리. 배경: 실패 원인 1위 '보유시간 안에 목표 미도달' 121건, 실전 '보유시간 초과' 매도
13건 평균 미끄러짐 -0.17%. 질문: 보지 않은 다음 6시간에서 이 정리가 '끝까지 들고 가기'보다 거래당 순익이 나은가?
- 롤링 워크포워드: 과거 3일로 현재 봇 기준대로 전략을 고르고 다음 6시간 성적, 10개 창. 같은 난수 → 공정 비교
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
TRAIN, STEP, WINDOWS = 4320, 360, 10
TOTAL = TRAIN + WINDOWS * STEP
up = bot.Upbit("x", "y")
real_tick = {}
for i in range(0, len(MARKETS), 20):
    try:
        r = requests.get(f"{bot.UPBIT}/v1/orderbook/instruments", params={"markets": ",".join(MARKETS[i:i + 20])}, timeout=10)
        for d in r.json():
            real_tick[d["market"]] = float(d["tick_size"])
    except Exception as e:
        print("instruments fail", e)
    time.sleep(0.3)





VARIANTS = [None] + [(f, th) for f in (0.5, 0.7, 0.85) for th in (0.0, 0.2, 0.4)]


def sim(series, spec, start, end, var):
    """bot.backtest 와 같은 계산 + var=(보유시간 비율, 순손익 기준%) 이면 그 조건에서 종가 정리. (순익%, 조기정리여부) 목록"""
    sig = series.signal(spec)
    out, i, cd = [], start, -1
    slip = getattr(series, "slip", bot.BT_SLIPPAGE)
    while i < end - 1:
        if i < cd or not sig[i]:
            i += 1; continue
        j = i + 1
        entry = series.o[j] * (1 + slip) * (1 + bot.FEE_RATE)
        tp, sl = bot.target_price(entry, spec.get("tp", bot.TARGET_NET_PCT)), entry * (1 + spec["sl"] / 100)
        ex, k, early = None, j, False
        while k < end:
            if series.l[k] <= sl: ex = sl; break
            if series.h[k] >= tp: ex = tp; break
            if k - j >= spec["hold"]: ex = series.c[k]; break
            if var and k - j >= var[0] * spec["hold"]:
                net = (series.c[k] * (1 - slip) * (1 - bot.FEE_RATE) / entry - 1) * 100
                if net >= var[1]:
                    ex, early = series.c[k], True; break
            k += 1
        if ex is None: break
        pnl = (ex * (1 - slip) * (1 - bot.FEE_RATE) / entry - 1) * 100
        out.append((pnl, early)); i = k + 1
        if pnl < 0: cd = i + bot.LOSS_COOLDOWN_MIN
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
            best = (sp, score, ho)
    return (best[0], best[2]) if best else (None, None)


print("현재 봇 값:", bot.EXPLORE_MIN_TRADES, bot.EXPLORE_MIN_EDGE, bot.EXPLORE_HOLDOUT_FLOOR, "· 시험 수", bot.BT_TRIALS, flush=True)
res = {str(v): [] for v in VARIANTS}
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
        stop = min(end + STEP, s.n)
        sp, ho = pick(cands)
        if sp:
            for v in VARIANTS:
                r = sim(s, sp, end, stop, v)
                res[str(v)] += r
                if v is None: nt += len(r)
    print(f"== {mk} 거래 {nt} ({time.time() - t0:.0f}s)", flush=True)

base = bot.stats([p for p, _ in res["None"]])
print(f"\n#### 합계 (보지 않은 다음 6시간 × {WINDOWS}개 창)")
print(f"현재 봇 그대로: {base['n']}건 · 승률 {base['winrate'] * 100:.0f}% · 거래당 {base['edge']:+.3f}% · 합계 {base['total']:+.1f}%")
out = {"all": base, "var": {}}
for v in VARIANTS[1:]:
    r = res[str(v)]
    st = bot.stats([p for p, _ in r]); ne = sum(e for _, e in r)
    print(f"시한 {v[0] * 100:.0f}% 경과·순익 {v[1]:+.1f}% 이상 정리: {st['n']}건(조기정리 {ne}) · 승률 {st['winrate'] * 100:.0f}% · 거래당 {st['edge']:+.3f}% · 합계 {st['total']:+.1f}% · 차이 {st['edge'] - base['edge']:+.3f}%p")
    out["var"][str(v)] = {**st, "early": ne}
json.dump(out, open("bt_summary.json", "w"), ensure_ascii=False, indent=1)
