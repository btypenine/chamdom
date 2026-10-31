# -*- coding: utf-8 -*-
"""이번 실험: '종목 하락 추세' 관문 — 매수 신호 때 그 종목이 최근 N분 X% 이하로 밀려 있으면 막기
배경: 실패 원인 2위 '서서히 하락(추세 반대 진입)' 55건, 조건 학습에서 눌림 매수 조건(볼린저하단 -0.53%·RSI과매도 -0.52%)이 최하위
질문: 보지 않은 다음 6시간에서 막힌 거래가 남은 거래보다 나쁜가? 기준(60·120·240분 × -1·-2·-3%)별 비교
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





def backtest_idx(series, spec, start, end):
    """bot.backtest 와 같은 계산, 거래마다 신호 봉 위치도 함께 돌려줌"""
    sig = series.signal(spec)
    out, i, cd = [], start, -1
    slip = getattr(series, "slip", bot.BT_SLIPPAGE)
    while i < end - 1:
        if i < cd or not sig[i]:
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
        out.append((i, pnl)); i = k + 1
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
rows = []   # (종목 변화율{60,120,240분}, 거래 순익%, 종목+시간대)
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
            for i, pnl in backtest_idx(s, sp, end, stop):
                chg = {m: (s.c[i] / s.c[i - m] - 1) * 100 for m in (60, 120, 240)}
                rows.append((chg, pnl, mk + ts[i][:13]))
                nt += 1
    print(f"== {mk} 거래 {nt} ({time.time() - t0:.0f}s)", flush=True)

def line(name, r):
    st = bot.stats(r)
    return f"{name}: {st['n']}건 · 승률 {st['winrate'] * 100:.0f}% · 거래당 {st['edge']:+.3f}% · 합계 {st['total']:+.1f}%"
allr = [r[1] for r in rows]
print(f"\n#### 합계 (보지 않은 다음 6시간 × {WINDOWS}개 창)")
print(line("현재 봇 그대로", allr))
out = {"all": bot.stats(allr), "gate": {}}
for m in (60, 120, 240):
    for th in (-1.0, -2.0, -3.0):
        blk = [r for r in rows if r[0][m] <= th]; keep = [r for r in rows if r[0][m] > th]
        bs, ks = bot.stats([r[1] for r in blk]), bot.stats([r[1] for r in keep])
        print(f"관문 '종목 {m}분 {th:+.0f}% 이하 막기': 막힌 {bs['n']}건 거래당 {bs['edge']:+.3f}% (시간대·종목 {len(set(r[2] for r in blk))}) · 남은 {ks['n']}건 거래당 {ks['edge']:+.3f}% · 차이 {ks['edge'] - bs['edge']:+.3f}%p")
        out["gate"][f"{m}_{th}"] = {"blocked": bs, "kept": ks}
json.dump(out, open("bt_summary.json", "w"), ensure_ascii=False, indent=1)
