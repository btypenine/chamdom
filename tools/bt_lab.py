# -*- coding: utf-8 -*-
"""이번 실험: '검증 근거 약함' 관문 — 고른 전략의 검증구간(보지 않은 뒤 25%) 거래가 3회 미만이거나 거래당 +0.1% 미만이면 막기
배경: 실전 163건(bot.log) 중 검증 3회 이상·거래당 +0.1% 이상 62건 거래당 -6원, 나머지 101건 -28.5원 (표본 작음·같은 기간 사후 분석)
질문: 보지 않은 다음 6시간에서도 막힌 거래가 남은 거래보다 나쁜가? 기준값(회수 2·3·5, 거래당 0·0.1·0.2%)별로 비교
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
rows = []   # (검증 거래수, 검증 거래당, 결과 리스트, 시간대)
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
            res = bot.backtest(s, sp, start=end, end=stop)
            if res:
                rows.append((ho["n"], ho["edge"] if ho["n"] else 0.0, res, mk + ts[end][:13]))
                nt += len(res)
    print(f"== {mk} 거래 {nt} ({time.time() - t0:.0f}s)", flush=True)

def line(name, r):
    st = bot.stats(r)
    return f"{name}: {st['n']}건 · 승률 {st['winrate'] * 100:.0f}% · 거래당 {st['edge']:+.3f}% · 합계 {st['total']:+.1f}%"
allr = [x for r in rows for x in r[2]]
print(f"\n#### 합계 (보지 않은 다음 6시간 × {WINDOWS}개 창)")
print(line("현재 봇 그대로", allr))
out = {"all": bot.stats(allr), "gate": {}}
for mn in (2, 3, 5):
    for me in (0.0, 0.1, 0.2):
        blk = [r for r in rows if r[0] < mn or r[1] < me]
        keep = [r for r in rows if not (r[0] < mn or r[1] < me)]
        b = [x for r in blk for x in r[2]]; k = [x for r in keep for x in r[2]]
        bs, ks = bot.stats(b), bot.stats(k)
        print(f"관문 '검증 {mn}회 미만 또는 거래당 {me:+.1f}% 미만 막기': 막힌 {bs['n']}건 거래당 {bs['edge']:+.3f}% (시간대·종목 {len(set(r[3] for r in blk))}) · 남은 {ks['n']}건 거래당 {ks['edge']:+.3f}% · 차이 {ks['edge'] - bs['edge']:+.3f}%p")
        out["gate"][f"{mn}_{me}"] = {"blocked": bs, "kept": ks}
json.dump(out, open("bt_summary.json", "w"), ensure_ascii=False, indent=1)
