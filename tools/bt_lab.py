# -*- coding: utf-8 -*-
"""전략 선정 기준 검증 실험 (GitHub Actions에서 실행, 실제 주문 없음)
- 봇과 같은 백테스트 함수를 그대로 가져다 씀
- 롤링 워크포워드: 과거 3일로 전략을 고른 뒤, 그 다음 6시간(보지 않은 구간) 성적을 잼
"""
import importlib.util, json, os, random, sys, time
spec_ = importlib.util.spec_from_file_location("bot", os.path.join(os.path.dirname(__file__), "..", "bot", "chamdom_bot.py"))
bot = importlib.util.module_from_spec(spec_); spec_.loader.exec_module(bot)
import requests

MARKETS = sys.argv[1].split(",") if len(sys.argv) > 1 else ["KRW-BTC","KRW-ETH","KRW-SOL","KRW-DOGE","KRW-ONDO","KRW-WLD","KRW-SUI","KRW-NEAR","KRW-SAND","KRW-ENSO","KRW-TRUMP","KRW-XLM"]
TOTAL = 4320 + 6 * 360          # 3일 학습창 + 6시간 x 6번 앞으로 이동
STEP = 360
up = bot.Upbit("x", "y")

VARIANTS = {
  "현재(v4)": dict(),
  "미끄러짐0.05": dict(BT_SLIPPAGE=0.0005),
  "최소거래12": dict(BT_MIN_TRADES=12),
  "PF1.2": dict(BT_MIN_PROFIT_FACTOR=1.2),
  "완화묶음": dict(BT_SLIPPAGE=0.0005, BT_MIN_TRADES=12, BT_MIN_PROFIT_FACTOR=1.2),
}

def run_variant(series_full, over, seed):
    saved = {k: getattr(bot, k) for k in over}
    for k, v in over.items(): setattr(bot, k, v)
    try:
        out = []
        for w in range(6):
            end = 4320 + w * STEP
            sub = bot.Series.__new__(bot.Series)
            for a in "ohlcv": setattr(sub, a, getattr(series_full, a)[:end])
            sub.n, sub._cache = end, {}
            rng = random.Random(seed + w)
            best = None
            for _ in range(bot.BT_TRIALS):
                sp = bot.random_spec(rng)
                r = bot.evaluate(sub, sp)
                if r and (best is None or r["score"] > best[1]["score"]): best = (sp, r)
            if not best:
                out.append(None); continue
            # 앞으로 6시간: 실제 비용(미끄러짐 0.1% 고정)으로 평가
            fwd_bt = getattr(bot, "BT_SLIPPAGE"); bot.BT_SLIPPAGE = 0.001
            res = bot.backtest(series_full, best[0], start=end, end=min(end + STEP, series_full.n))
            bot.BT_SLIPPAGE = fwd_bt
            out.append({"spec": bot.describe(best[0]), "train": best[1], "fwd": res})
        return out
    finally:
        for k, v in saved.items(): setattr(bot, k, v)

summary = {}
for mk in MARKETS:
    try:
        candles = up.minute_candles(mk, TOTAL)
    except Exception as e:
        print(mk, "fail", e); continue
    s = bot.Series(candles)
    if s.n < TOTAL - 50: print(mk, "short", s.n); continue
    # 비용 측정: 1분봉 평균 변동폭
    print(f"== {mk} n={s.n} 평균1분변동 {bot.avg_range_pct(s, s.n):.3f}%", flush=True)
    for name, over in VARIANTS.items():
        res = run_variant(s, over, 7)
        picked = [r for r in res if r]
        fwd = [x for r in picked for x in r["fwd"]]
        d = summary.setdefault(name, {"windows": 0, "picked": 0, "fwdTrades": 0, "fwdSum": 0.0, "fwdWins": 0})
        d["windows"] += len(res); d["picked"] += len(picked)
        d["fwdTrades"] += len(fwd); d["fwdSum"] += sum(fwd); d["fwdWins"] += sum(1 for x in fwd if x > 0)
        print(f"  {name}: 채택 {len(picked)}/6창 · 앞으로 {len(fwd)}회 승 {sum(1 for x in fwd if x>0)} 합 {sum(fwd):+.2f}%", flush=True)
        for r in picked[:2]:
            print(f"     {r['spec']} 학습 {r['train']['trades']}회 거래당{r['train']['edgePct']:+.2f}% → 앞 {len(r['fwd'])}회 {sum(r['fwd']):+.2f}%")
print("\n#### 합계 (앞으로 6시간 구간, 미끄러짐 0.1% 가정)")
for k, d in summary.items():
    avg = d["fwdSum"] / d["fwdTrades"] if d["fwdTrades"] else 0
    wr = d["fwdWins"] / d["fwdTrades"] * 100 if d["fwdTrades"] else 0
    print(f"{k}: 채택 {d['picked']}/{d['windows']}창 · 거래 {d['fwdTrades']} · 승률 {wr:.0f}% · 거래당 {avg:+.3f}% · 합 {d['fwdSum']:+.2f}%")
json.dump(summary, open("bt_summary.json", "w"), ensure_ascii=False, indent=1)
