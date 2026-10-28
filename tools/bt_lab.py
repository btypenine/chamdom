# -*- coding: utf-8 -*-
"""이번 실험: '힘 빠진 보유 일찍 정리' 청산 규칙 (GitHub Actions에서 실행, 실제 주문 없음)
배경: 실전 실패 원인 1위가 '보유시간 안에 목표 미도달(힘 부족)' (40건, 2위 '신호 늦게 따라감' 14건)
질문: 보유시간의 일정 비율이 지났는데 익절까지 진행률이 낮고 손실 중이면 그 자리에서 정리하는 게,
      끝까지 들고 있는 것보다 '보지 않은 다음 6시간' 거래당 순익이 나은가?
- 롤링 워크포워드: 과거 3일(4320분)로 현재 봇 기준(학습 최소 6회·거래당 0.20%·검증 바닥 -0.10%)으로 고르고 다음 6시간 성적, 6개 창
- 미끄러짐: 업비트 실제 호가 단위 기준
- 모든 변형이 같은 전략을 공유 → 차이는 청산 규칙에서만 나옴
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

# (보유시간 경과 비율, 익절 진행률 상한) — 둘 다 만족 + 손실 중이면 그 봉 종가에 정리. None = 끝까지 보유(현재)
VARIANTS = {
    "현재(끝까지 보유)": None,
    "절반·진행20%미만": (0.5, 0.20),
    "절반·진행0%미만": (0.5, 0.0),
    "1/3·진행20%미만": (0.33, 0.20),
    "2/3·진행20%미만": (0.67, 0.20),
    "절반·진행40%미만": (0.5, 0.40),
}

real_tick = {}
for i in range(0, len(MARKETS), 20):
    try:
        r = requests.get(f"{bot.UPBIT}/v1/orderbook/instruments", params={"markets": ",".join(MARKETS[i:i + 20])}, timeout=10)
        for d in r.json():
            real_tick[d["market"]] = float(d["tick_size"])
    except Exception as e:
        print("instruments fail", e)
    time.sleep(0.3)


def backtest_v(series, spec, start, end, rule):
    """bot.backtest 와 같고, rule 이 있으면 '힘 빠진 보유 정리'를 추가. 반환: (거래별 %, 조기정리 건수)"""
    sig = series.signal(spec)
    results, early_n, i, cooldown_until = [], 0, start, -1
    slip = series.slip
    while i < end - 1:
        if i < cooldown_until or not sig[i]:
            i += 1
            continue
        j = i + 1
        fill = series.o[j] * (1 + slip)
        entry = fill * (1 + bot.FEE_RATE)
        tp, sl = bot.target_price(entry, spec.get("tp", bot.TARGET_NET_PCT)), entry * (1 + spec["sl"] / 100)
        exit_price, k, hi = None, j, entry
        while k < end:
            if series.l[k] <= sl:
                exit_price = sl
                break
            if series.h[k] >= tp:
                exit_price = tp
                break
            if k - j >= spec["hold"]:
                exit_price = series.c[k]
                break
            hi = max(hi, series.h[k])
            if rule and k - j >= spec["hold"] * rule[0]:
                prog = (hi - entry) / (tp - entry) if tp > entry else 0
                now_pct = series.c[k] * (1 - slip) * (1 - bot.FEE_RATE) / entry - 1
                if prog < rule[1] and now_pct < 0:
                    exit_price = series.c[k]
                    early_n += 1
                    break
            k += 1
        if exit_price is None:
            break
        pnl = (exit_price * (1 - slip) * (1 - bot.FEE_RATE) / entry - 1) * 100
        results.append(pnl)
        i = k + 1
        if pnl < 0:
            cooldown_until = i + bot.LOSS_COOLDOWN_MIN
    return results, early_n


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
summary = {k: {"trades": 0, "sum": 0.0, "wins": 0, "early": 0, "picked": 0} for k in VARIANTS}
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
    s.slip = bot.BT_SLIP_BASE + (real_tick.get(mk) or bot.tick_size(price)) / price
    line = {k: [0, 0.0, 0] for k in VARIANTS}
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
        for k, rule in VARIANTS.items():
            res, en = backtest_v(s, sp, end, min(end + STEP, s.n), rule)
            d = summary[k]
            d["picked"] += 1; d["trades"] += len(res); d["sum"] += sum(res); d["wins"] += sum(1 for x in res if x > 0); d["early"] += en
            L = line[k]; L[0] += len(res); L[1] += sum(res); L[2] += en
    print(f"== {mk} 미끄러짐 {s.slip * 100:.3f}% ({time.time() - t0:.0f}s)", flush=True)
    for k, L in line.items():
        print(f"  {k}: {L[0]}회 합 {L[1]:+.2f}% (조기정리 {L[2]})", flush=True)

print("\n#### 합계 (보지 않은 다음 6시간, 실제 호가 단위 미끄러짐, 같은 전략에 청산 규칙만 다름)")
for k, d in summary.items():
    n = d["trades"]
    d["avg"] = d["sum"] / n if n else 0
    d["winrate"] = d["wins"] / n if n else 0
    print(f"{k}: 채택창 {d['picked']} · 거래 {n} · 승률 {d['winrate'] * 100:.0f}% · 거래당 {d['avg']:+.3f}% · 합 {d['sum']:+.2f}% · 조기정리 {d['early']}건")
json.dump(summary, open("bt_summary.json", "w"), ensure_ascii=False, indent=1)
