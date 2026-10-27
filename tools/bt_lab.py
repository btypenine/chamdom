# -*- coding: utf-8 -*-
"""전략 선정 기준 검증 실험 (GitHub Actions에서 실행, 실제 주문 없음)
이번 실험: 호가 단위 표 검증
- 업비트 /v1/orderbook/instruments 의 실제 호가 단위와 봇의 tick_size() 표를 비교
- 롤링 워크포워드(과거 3일로 고르고 다음 6시간 성적): 현재 표 vs 수정 표로 전략을 고르되,
  '앞으로 6시간' 성적은 둘 다 실제 호가 단위 기준 미끄러짐으로 잼
"""
import importlib.util, json, os, random, sys, time
spec_ = importlib.util.spec_from_file_location("bot", os.path.join(os.path.dirname(__file__), "..", "bot", "chamdom_bot.py"))
bot = importlib.util.module_from_spec(spec_); spec_.loader.exec_module(bot)
import requests

MARKETS = ["KRW-BTC","KRW-ETH","KRW-IRYS","KRW-NEAR","KRW-ONDO","KRW-SAND","KRW-WLD","KRW-SOL","KRW-BIRB","KRW-SUI","KRW-DOGE",
  "KRW-XLM","KRW-ENA","KRW-ADA","KRW-ORDER","KRW-ERA","KRW-TRUMP","KRW-ARK","KRW-ORCA","KRW-DRV","KRW-HBAR","KRW-WAL","KRW-UP2",
  "KRW-STX","KRW-IN","KRW-T","KRW-DATA","KRW-AXS","KRW-LINK","KRW-W","KRW-AVAX","KRW-TRAC","KRW-LA","KRW-TREE","KRW-IOST",
  "KRW-UNI","KRW-ARB","KRW-XPL","KRW-APT","KRW-PUMP","KRW-SHIB","KRW-0G","KRW-MINA","KRW-ZRO","KRW-MEGA","KRW-VIRTUAL",
  "KRW-BAT","KRW-PENGU","KRW-SEI","KRW-ALGO","KRW-PEPE","KRW-TRX","KRW-AAVE","KRW-ENSO"]
TOTAL = 4320 + 6 * 360
STEP = 360
up = bot.Upbit("x", "y")
OLD_TICK = bot.tick_size


def new_tick(price):
    """수정 후보: 업비트 원화마켓 호가 단위 (100~1,000원 1원, 10~100원 0.1원 …)"""
    for floor, tick in ((2000000, 1000), (1000000, 1000), (500000, 500), (100000, 100), (50000, 50), (10000, 10),
                        (5000, 5), (1000, 1), (100, 1), (10, 0.1), (1, 0.01), (0.1, 0.001), (0.01, 0.0001),
                        (0.001, 0.00001), (0.0001, 0.000001)):
        if price >= floor:
            return tick
    return 0.0000001


# 1) 실제 호가 단위 / 현재 스프레드
real_tick, spread = {}, {}
for i in range(0, len(MARKETS), 20):
    chunk = MARKETS[i:i + 20]
    try:
        r = requests.get(f"{bot.UPBIT}/v1/orderbook/instruments", params={"markets": ",".join(chunk)}, timeout=10)
        for d in r.json():
            real_tick[d["market"]] = float(d["tick_size"])
    except Exception as e:
        print("instruments fail", e, getattr(r, "text", "")[:200])
    try:
        r = requests.get(f"{bot.UPBIT}/v1/orderbook", params={"markets": ",".join(chunk)}, timeout=10)
        for d in r.json():
            u = d["orderbook_units"][0]
            spread[d["market"]] = (u["ask_price"], u["bid_price"])
    except Exception as e:
        print("orderbook fail", e)
    time.sleep(0.3)

print("#### 호가 단위 비교 (가격 · 실제 · 현재표 · 수정표 · 실제 스프레드)")
mismatch_old = mismatch_new = 0
for mk in MARKETS:
    if mk not in spread:
        print(mk, "호가 없음"); continue
    ask, bid = spread[mk]
    p = (ask + bid) / 2
    rt, ot, nt = real_tick.get(mk), OLD_TICK(p), new_tick(p)
    mo = rt is not None and abs(ot - rt) > rt * 1e-6
    mn = rt is not None and abs(nt - rt) > rt * 1e-6
    mismatch_old += mo; mismatch_new += mn
    print(f"{mk:12s} {p:>14,.6f} 실제 {rt} · 현재표 {ot}{' ✗' if mo else ''} · 수정표 {nt}{' ✗' if mn else ''} · 스프레드 {(ask - bid) / p * 100:.3f}%")
print(f"불일치: 현재표 {mismatch_old} · 수정표 {mismatch_new} (총 {len(spread)})\n", flush=True)


def slip_with(tick_fn, price):
    return bot.BT_SLIP_BASE + (tick_fn(price) / price if price else 0.001)


VARIANTS = {"현재표": OLD_TICK, "수정표": new_tick}


def run_variant(series_full, sel_slip, seed):
    out = []
    for w in range(6):
        end = 4320 + w * STEP
        sub = bot.Series.__new__(bot.Series)
        for a in "ohlcv": setattr(sub, a, getattr(series_full, a)[:end])
        sub.n, sub._cache, sub.slip = end, {}, sel_slip
        rng = random.Random(seed + w)
        best = None
        for _ in range(bot.BT_TRIALS):
            sp = bot.random_spec(rng)
            r = bot.evaluate(sub, sp)
            if r and (best is None or r["score"] > best[1]["score"]): best = (sp, r)
        if not best:
            out.append(None); continue
        res = bot.backtest(series_full, best[0], start=end, end=min(end + STEP, series_full.n))   # 실제 호가 기준
        out.append({"spec": bot.describe(best[0]), "train": best[1], "fwd": res})
    return out


summary = {}
for mk in MARKETS:
    try:
        candles = up.minute_candles(mk, TOTAL)
    except Exception as e:
        print(mk, "fail", e); continue
    s = bot.Series(candles)
    if s.n < TOTAL - 50: print(mk, "short", s.n); continue
    price = s.c[-1]
    rt = real_tick.get(mk)
    s.slip = bot.BT_SLIP_BASE + ((rt if rt else new_tick(price)) / price)
    print(f"== {mk} 가격 {price} 실제미끄러짐 {s.slip*100:.3f}% (현재표 {slip_with(OLD_TICK, price)*100:.3f}%)", flush=True)
    for name, fn in VARIANTS.items():
        res = run_variant(s, slip_with(fn, price), 7)
        picked = [r for r in res if r]
        fwd = [x for r in picked for x in r["fwd"]]
        d = summary.setdefault(name, {"windows": 0, "picked": 0, "fwdTrades": 0, "fwdSum": 0.0, "fwdWins": 0})
        d["windows"] += len(res); d["picked"] += len(picked)
        d["fwdTrades"] += len(fwd); d["fwdSum"] += sum(fwd); d["fwdWins"] += sum(1 for x in fwd if x > 0)
        print(f"  {name}: 채택 {len(picked)}/6창 · 앞으로 {len(fwd)}회 승 {sum(1 for x in fwd if x>0)} 합 {sum(fwd):+.2f}%", flush=True)
print("\n#### 합계 (앞으로 6시간, 실제 호가 단위 미끄러짐으로 측정)")
for k, d in summary.items():
    avg = d["fwdSum"] / d["fwdTrades"] if d["fwdTrades"] else 0
    wr = d["fwdWins"] / d["fwdTrades"] * 100 if d["fwdTrades"] else 0
    print(f"{k}: 채택 {d['picked']}/{d['windows']}창 · 거래 {d['fwdTrades']} · 승률 {wr:.0f}% · 거래당 {avg:+.3f}% · 합 {d['fwdSum']:+.2f}%")
json.dump(summary, open("bt_summary.json", "w"), ensure_ascii=False, indent=1)
