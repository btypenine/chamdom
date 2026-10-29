# -*- coding: utf-8 -*-
"""이번 실험: 보유시간(최대 N분)별 '보지 않은 다음 6시간' 성적 (GitHub Actions에서 실행, 실제 주문 없음)
배경: 실전 최근 60건 중 최대 30분 전략 6건 0승 거래당 -0.74%, 60분 19건 -0.41%, 240분 29건 -0.23% (표본 작음)
질문: (1) 봇 기준대로 고른 전략을 보유시간별로 나누면 보지 않은 다음 6시간 성적이 다른가?
      (2) '최대 30분 전략 매수 막기' 관문이었다면 전체 거래당 성적이 나아지는가? (막힌 거래는 안 한 것으로)
      (3) 아예 30분 후보를 빼고 골랐다면(전략 생성 변경) 어떤가? — 참고용
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
            best = (sp, score)
    return best[0] if best else None




print("현재 봇 값:", bot.EXPLORE_MIN_TRADES, bot.EXPLORE_MIN_EDGE, bot.EXPLORE_HOLDOUT_FLOOR, "· 시험 수", bot.BT_TRIALS, "· 보유시간 후보", bot.HOLD_CHOICES, flush=True)
byhold = {h: [] for h in bot.HOLD_CHOICES}
byhold_hours = {h: set() for h in bot.HOLD_CHOICES}
alt = {"30분 후보 제외": [], "60분 이하 후보 제외": []}
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
        sp = pick(cands)
        if sp:
            res = bot.backtest(s, sp, start=end, end=stop)
            byhold[sp["hold"]] += res; nt += len(res)
            if res: byhold_hours[sp["hold"]].add(ts[end][:13])
        for name, lim in (("30분 후보 제외", 30), ("60분 이하 후보 제외", 60)):
            sp2 = pick([c for c in cands if c[0]["hold"] > lim])
            if sp2:
                alt[name] += bot.backtest(s, sp2, start=end, end=stop)
    print(f"== {mk} 거래 {nt} ({time.time() - t0:.0f}s)", flush=True)

def line(name, r):
    st = bot.stats(r)
    return f"{name}: {st['n']}건 · 승률 {st['winrate'] * 100:.0f}% · 거래당 {st['edge']:+.3f}% · 합계 {st['total']:+.1f}%"
allr = [x for h in byhold for x in byhold[h]]
print(f"\n#### 합계 (보지 않은 다음 6시간 × {WINDOWS}개 창)")
print(line("현재 봇 그대로", allr))
out = {"all": bot.stats(allr), "byHold": {}, "gate": {}, "alt": {}}
for h in bot.HOLD_CHOICES:
    print(line(f"최대 {h}분 전략", byhold[h]) + f" · 시간대 {len(byhold_hours[h])}")
    out["byHold"][h] = bot.stats(byhold[h])
for lim in (30, 60):
    keep = [x for h in byhold if h > lim for x in byhold[h]]
    blocked = [x for h in byhold if h <= lim for x in byhold[h]]
    st, bs = bot.stats(keep), bot.stats(blocked)
    print(f"관문 '최대 {lim}분 이하 전략 매수 막기': 막힌 {bs['n']}건(거래당 {bs['edge']:+.3f}%) · 남은 {st['n']}건 거래당 {st['edge']:+.3f}% · 합계 {bot.stats(allr)['total']:+.1f}% → {st['total']:+.1f}%")
    out["gate"][lim] = {"blocked": bs, "kept": st}
for name, r in alt.items():
    print(line(f"참고: 후보에서 {name} 후 다시 고름", r)); out["alt"][name] = bot.stats(r)
json.dump(out, open("bt_summary.json", "w"), ensure_ascii=False, indent=1)
