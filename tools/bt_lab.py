# -*- coding: utf-8 -*-
"""전략 선정 기준 검증 실험 (GitHub Actions에서 실행, 실제 주문 없음)
이번 실험: 탐험 모드 선정 기준을 바꾸면 '활동량(채택 종목·거래 수)'과 '보지 않은 다음 6시간 거래당 순익'이 어떻게 변하나
- 롤링 워크포워드: 과거 3일(4320분)로 고르고 다음 6시간 성적, 6개 창
- 미끄러짐: 업비트 실제 호가 단위(/v1/orderbook/instruments) 기준
- 같은 무작위 전략 후보(같은 seed)를 모든 변형이 공유 → 차이는 기준에서만 나옴
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
TRAIN = 4320
STEP = 360
WINDOWS = 6
TOTAL = TRAIN + WINDOWS * STEP
up = bot.Upbit("x", "y")

# (학습 최소 거래, 학습 최소 거래당 순익%, 검증 구간 바닥%)
VARIANTS = {
    "현재(6회·0%·-0.10)": (6, 0.0, -0.10),
    "최소거래4": (4, 0.0, -0.10),
    "검증바닥-0.30": (6, 0.0, -0.30),
    "최소순익0.10": (6, 0.10, -0.10),
    "최소거래4+순익0.10": (4, 0.10, -0.10),
    "최소순익0.20": (6, 0.20, -0.10),
}
print("현재 봇 값:", bot.EXPLORE_MIN_TRADES, bot.EXPLORE_MIN_EDGE, bot.EXPLORE_HOLDOUT_FLOOR, "· 시험 수", bot.BT_TRIALS, flush=True)

real_tick = {}
if os.environ.get("BTLAB_OFFLINE") != "1":
    for i in range(0, len(MARKETS), 20):
        try:
            r = requests.get(f"{bot.UPBIT}/v1/orderbook/instruments", params={"markets": ",".join(MARKETS[i:i + 20])}, timeout=10)
            for d in r.json():
                real_tick[d["market"]] = float(d["tick_size"])
        except Exception as e:
            print("instruments fail", e)
        time.sleep(0.3)


def pick(cands, v):
    """cands: [(spec, tr, ho)] → 이 변형 기준으로 고른 최고 전략"""
    mn, me, fl = v
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


summary = {k: {"windows": 0, "picked": 0, "fwdTrades": 0, "fwdSum": 0.0, "fwdWins": 0} for k in VARIANTS}
t0 = time.time()
for mk in MARKETS:
    try:
        candles = up.minute_candles(mk, TOTAL)
    except Exception as e:
        print(mk, "fail", e); continue
    s = bot.Series(candles)
    if s.n < TOTAL - 50:
        print(mk, "short", s.n); continue
    price = s.c[-1]
    rt = real_tick.get(mk) or bot.tick_size(price)
    s.slip = bot.BT_SLIP_BASE + rt / price
    line = {k: [0, 0, 0.0, 0] for k in VARIANTS}
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
        fwd_cache = {}
        for k, v in VARIANTS.items():
            sp = pick(cands, v)
            d = summary[k]; d["windows"] += 1
            if not sp:
                continue
            if id(sp) not in fwd_cache:
                fwd_cache[id(sp)] = bot.backtest(s, sp, start=end, end=min(end + STEP, s.n))
            res = fwd_cache[id(sp)]
            d["picked"] += 1; d["fwdTrades"] += len(res); d["fwdSum"] += sum(res); d["fwdWins"] += sum(1 for x in res if x > 0)
            L = line[k]; L[0] += 1; L[1] += len(res); L[2] += sum(res); L[3] += sum(1 for x in res if x > 0)
    print(f"== {mk} 가격 {price} 미끄러짐 {s.slip * 100:.3f}% ({time.time() - t0:.0f}s)", flush=True)
    for k, L in line.items():
        print(f"  {k}: 채택 {L[0]}/{WINDOWS}창 · 앞으로 {L[1]}회 승 {L[3]} 합 {L[2]:+.2f}%", flush=True)

print("\n#### 합계 (보지 않은 다음 6시간, 실제 호가 단위 미끄러짐)")
for k, d in summary.items():
    n = d["fwdTrades"]
    d["avg"] = d["fwdSum"] / n if n else 0
    d["winrate"] = d["fwdWins"] / n if n else 0
    print(f"{k}: 채택 {d['picked']}/{d['windows']}창 · 거래 {n} · 승률 {d['winrate'] * 100:.0f}% · 거래당 {d['avg']:+.3f}% · 합 {d['fwdSum']:+.2f}%")
json.dump(summary, open("bt_summary.json", "w"), ensure_ascii=False, indent=1)
