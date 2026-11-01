# -*- coding: utf-8 -*-
"""이번 실험: 봉 길이(시간 단위) 비교 — 1분봉 vs 5분·15분·60분봉
배경: 직전 실험에서 현재 봇(1분봉)이 고르는 전략의 '보지 않은 다음 구간' 성적이 거래당 -0.382%.
1분봉의 짧은 움직임은 수수료·체결 밀림(왕복 약 0.2~0.3%)을 넘기 어려울 수 있음.
질문: 같은 전략 생성·선정 방식을 더 긴 봉에 적용하면, 보지 않은 다음 구간에서 거래당 순익이 나아지는가?
- 같은 25종목, 롱폼 워크포워드 8개 창, 같은 난수(창마다 고정) → 공정 비교
- 봉 단위별 학습/시험 길이: 1분(3일/6시간) · 5분(10일/1일) · 15분(20일/2일) · 60분(60일/5일)
- 보유시간·손실 후 대기는 '봉 개수'로 해석(봇의 1분봉 기준 값을 그대로 봉 단위로 씀)
"""
import importlib.util, os, random, time
spec_ = importlib.util.spec_from_file_location("bot", os.path.join(os.path.dirname(__file__), "..", "bot", "chamdom_bot.py"))
bot = importlib.util.module_from_spec(spec_); spec_.loader.exec_module(bot)
import requests

MARKETS = ["KRW-BTC", "KRW-ETH", "KRW-SOL", "KRW-DOGE", "KRW-XLM", "KRW-ADA", "KRW-SUI", "KRW-NEAR", "KRW-SAND", "KRW-ONDO",
           "KRW-WLD", "KRW-ENA", "KRW-TRUMP", "KRW-AVAX", "KRW-HBAR", "KRW-LINK", "KRW-UNI", "KRW-APT", "KRW-ARB", "KRW-SHIB",
           "KRW-AAVE", "KRW-PENGU", "KRW-KAT", "KRW-BAT", "KRW-AXS"]
TFS = {1: (4320, 360), 5: (2880, 288), 15: (1920, 192), 60: (1440, 120)}   # 봉 단위(분): (학습 봉 수, 시험 봉 수)
WINDOWS = 8


def fetch(market, unit, total):
    out, to = [], None
    while len(out) < total:
        p = {"market": market, "count": min(200, total - len(out))}
        if to:
            p["to"] = to
        for a in range(5):
            r = requests.get(f"{bot.UPBIT}/v1/candles/minutes/{unit}", params=p, timeout=10)
            if r.status_code == 429:
                time.sleep(1 + a); continue
            break
        data = r.json()
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        to = data[-1]["candle_date_time_utc"] + "Z"
        time.sleep(0.12)
    out.reverse()
    return out


real_tick = {}
try:
    r = requests.get(f"{bot.UPBIT}/v1/orderbook/instruments", params={"markets": ",".join(MARKETS)}, timeout=10)
    for d in r.json():
        real_tick[d["market"]] = float(d["tick_size"])
except Exception as e:
    print("instruments fail", e)


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


res = {tf: [] for tf in TFS}
tp_hist = {tf: {} for tf in TFS}
t0 = time.time()
for mk in MARKETS:
    line = []
    for tf, (train, step) in TFS.items():
        total = train + WINDOWS * step
        try:
            s = bot.Series(fetch(mk, tf, total))
        except Exception as e:
            line.append(f"{tf}분 실패"); continue
        if s.n < total - 20:
            line.append(f"{tf}분 짧음({s.n})"); continue
        price = s.c[-1]
        s.slip = bot.BT_SLIP_BASE + (real_tick.get(mk) or bot.tick_size(price)) / price
        nt = 0
        for w in range(WINDOWS):
            end = train + w * step
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
                cands.append((sp, tr, bot.stats(bot.backtest(sub, sp, start=split))))
            sp = pick(cands)
            if sp:
                r = bot.backtest(s, sp, start=end, end=min(end + step, s.n))
                res[tf] += r
                nt += len(r)
                tp_hist[tf][sp["tp"]] = tp_hist[tf].get(sp["tp"], 0) + len(r)
        line.append(f"{tf}분 {nt}건")
    print(f"== {mk} · " + " · ".join(line) + f" ({time.time() - t0:.0f}s)", flush=True)

print(f"\n#### 합계 (보지 않은 다음 구간 × {WINDOWS}개 창, {len(MARKETS)}종목)")
days = {tf: WINDOWS * step * tf / 1440 for tf, (_, step) in TFS.items()}
summary = {}
for tf in TFS:
    st = bot.stats(res[tf]) if res[tf] else {"n": 0, "winrate": 0, "edge": 0, "total": 0, "pf": 0}
    summary[tf] = st
    print(f"{tf:>2}분봉: {st['n']}건 · 승률 {st['winrate'] * 100:.0f}% · 거래당 {st['edge']:+.3f}% · 합계 {st['total']:+.1f}% · "
          f"손익비 {min(st['pf'], 99):.2f} · 시험 기간 {days[tf]:.1f}일 · 하루 약 {st['n'] / max(days[tf], 0.1) / len(MARKETS):.2f}건/종목 · "
          f"익절폭 분포 {dict(sorted(tp_hist[tf].items()))}")
import json
json.dump({str(k): v for k, v in summary.items()}, open("bt_summary.json", "w"), ensure_ascii=False)
