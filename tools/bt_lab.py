# -*- coding: utf-8 -*-
"""이번 실험: '익절 근처까지 갔다가 되밀린 보유 정리' 청산 규칙 (GitHub Actions에서 실행, 실제 주문 없음)
배경: 실패 원인에 '익절 직전까지 갔다가 반락(익절폭 과대)' 9건. 예) 10-29 NEAR 익절까지 89% 도달 후 반락 → 240분 보유 끝에 손실
질문: 보유 중 익절까지 진행률이 X% 이상 갔던 포지션이, 순손익 G% 아래로 되밀리는 순간 정리했다면
      원래 규칙대로 끝까지 들고 있었을 때보다 거래당 몇 %p 나은가? ('보지 않은 다음 6시간' 기준)
- 롤링 워크포워드: 과거 3일로 현재 봇 기준대로 전략을 고르고 다음 6시간 성적, 10개 창
- 같은 봉 안 순서: 손절 → (조건 충족 상태에서) 되밀림 가격 터치 → 익절. 진행률은 직전 봉까지의 최고가로만 판단(미리 보기 없음)
- 판정 기준(봇 청산 판정과 같음): 걸린 거래 20건↑·3개 시간대↑, 조기 정리가 평균 0.15%p 이상 나음
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

# (익절 진행률 최고 X% 이상 갔다가, 순손익이 G% 이하로 되밀리면 정리)
VARIANTS = {"진행률 50% → 순손익 0%": (50, 0.0), "진행률 70% → 순손익 0%": (70, 0.0), "진행률 90% → 순손익 0%": (90, 0.0),
            "진행률 50% → 순손익 +0.2%": (50, 0.2), "진행률 70% → 순손익 +0.2%": (70, 0.2), "진행률 70% → 순손익 -0.3%": (70, -0.3)}

real_tick = {}
for i in range(0, len(MARKETS), 20):
    try:
        r = requests.get(f"{bot.UPBIT}/v1/orderbook/instruments", params={"markets": ",".join(MARKETS[i:i + 20])}, timeout=10)
        for d in r.json():
            real_tick[d["market"]] = float(d["tick_size"])
    except Exception as e:
        print("instruments fail", e)
    time.sleep(0.3)



def run_trades(series, spec, start, end):
    """bot.backtest 와 같은 진행. 거래마다 (매수 봉, 실제 %, {변형: 조기 정리 % 또는 None}) 반환"""
    sig = series.signal(spec)
    out, i, cooldown_until = [], start, -1
    slip, fee = series.slip, bot.FEE_RATE
    while i < end - 1:
        if i < cooldown_until or not sig[i]:
            i += 1
            continue
        j = i + 1
        entry = series.o[j] * (1 + slip) * (1 + fee)
        tp, sl = bot.target_price(entry, spec.get("tp", bot.TARGET_NET_PCT)), entry * (1 + spec["sl"] / 100)
        room = tp - entry
        trig = {k: entry * (1 + g / 100) / ((1 - slip) * (1 - fee)) for k, (x, g) in VARIANTS.items()}
        early = {k: None for k in VARIANTS}
        exit_price, k, maxh = None, j, entry
        while k < end:
            if series.l[k] <= sl:
                exit_price = sl; break
            prog = (maxh - entry) / room * 100 if room > 0 else 0
            for v, (x, g) in VARIANTS.items():
                if early[v] is None and prog >= x and series.l[k] <= trig[v] < tp:
                    early[v] = (trig[v] * (1 - slip) * (1 - fee) / entry - 1) * 100
            if series.h[k] >= tp:
                exit_price = tp; break
            if k - j >= spec["hold"]:
                exit_price = series.c[k]; break
            maxh = max(maxh, series.h[k])
            k += 1
        if exit_price is None:
            break
        pnl = (exit_price * (1 - slip) * (1 - fee) / entry - 1) * 100
        out.append((i, pnl, early))
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
summary = {k: {"n": 0, "sumDiff": 0.0, "better": 0, "early": 0.0, "actual": 0.0, "hours": set()} for k in VARIANTS}
allt = [0, 0.0, 0]
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
        for i, pnl, early in run_trades(s, sp, end, min(end + STEP, s.n)):
            nt += 1; allt[0] += 1; allt[1] += pnl; allt[2] += pnl > 0
            for v, e in early.items():
                if e is None:
                    continue
                d = summary[v]; d["n"] += 1; d["sumDiff"] += e - pnl; d["better"] += e > pnl
                d["early"] += e; d["actual"] += pnl; d["hours"].add(ts[i][:13])
    print(f"== {mk} 거래 {nt} ({time.time() - t0:.0f}s)", flush=True)

n0 = max(allt[0], 1)
print(f"\n#### 합계 (보지 않은 다음 6시간 × {WINDOWS}개 창, 전체 거래 {allt[0]} · 승률 {allt[2] / n0 * 100:.0f}% · 거래당 {allt[1] / n0:+.3f}%)")
print("판정 기준(봇 청산 판정과 같음): 걸린 거래 20건↑·3개 시간대↑, 조기 정리가 평균 0.15%p 이상 나음")
out = {}
for v, d in summary.items():
    n = max(d["n"], 1)
    avg = d["sumDiff"] / n
    ok = d["n"] >= 20 and len(d["hours"]) >= 3 and avg >= 0.15
    total_gain = d["sumDiff"] / n0
    print(f"{v}: 걸린 거래 {d['n']}건(시간대 {len(d['hours'])}) · 조기 정리 평균 {d['early'] / n:+.3f}% vs 끝까지 {d['actual'] / n:+.3f}% · "
          f"차이 {avg:+.3f}%p ({d['better']}건 더 나음) · 전체 거래당 영향 {total_gain:+.3f}%p → {'적용 기준 충족' if ok else '미충족'}")
    out[v] = {"n": d["n"], "hours": len(d["hours"]), "avgDiff": avg, "better": d["better"], "perTradeImpact": total_gain, "meets": ok}
json.dump(out, open("bt_summary.json", "w"), ensure_ascii=False, indent=1)
