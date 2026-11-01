# -*- coding: utf-8 -*-
"""이번 실험(2차): 봉 길이별 + 'BTC 상승 국면에서만 매수' 필터 비교
1차 결과(11-01): 보지 않은 구간 거래당 1분 -0.48% · 5분 -0.10% · 15분 -0.68% · 60분 -0.69% → 모두 마이너스.
질문: 시장(BTC)이 오르는 국면에서만 매수하면 나아지는가? 같은 기간 BTC 자체 등락도 함께 기록(시장 탓인지 확인).
- BTC 국면: BTC 종가 > BTC 지수이동평균(1분 6시간·5분 1일·15분 2일·60분 5일 길이)일 때만 신규 매수
(아래는 1차 설명)
봉 길이(시간 단위) 비교 — 1분봉 vs 5분·15분·60분봉
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
REGIME_LEN = {1: 360, 5: 288, 15: 192, 60: 120}
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


def sim(series, spec, start, end, ok=None):
    """bot.backtest 와 같은 계산, ok[i] 가 False 면 그 봉에서는 신규 매수 안 함"""
    sig = series.signal(spec)
    out, i, cd = [], start, -1
    slip = getattr(series, "slip", bot.BT_SLIPPAGE)
    while i < end - 1:
        if i < cd or not sig[i] or (ok is not None and not ok[i]):
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
        out.append(pnl); i = k + 1
        if pnl < 0: cd = i + bot.LOSS_COOLDOWN_MIN
    return out


btc = {}
btc_move = {}
for tf, (train, step) in TFS.items():
    cs = fetch("KRW-BTC", tf, train + WINDOWS * step)
    closes = [c["trade_price"] for c in cs]
    e = bot.ema(closes, REGIME_LEN[tf])
    btc[tf] = {c["candle_date_time_utc"][:16]: closes[i] > e[i] for i, c in enumerate(cs)}
    btc_move[tf] = (closes[-1] / closes[train] - 1) * 100 if len(closes) > train else None
    up_share = sum(1 for i in range(train, len(closes)) if closes[i] > e[i]) / max(1, len(closes) - train)
    print(f"BTC {tf}분봉 시험 기간 등락 {btc_move[tf]:+.2f}% · 상승 국면 비율 {up_share * 100:.0f}%", flush=True)

resf = {tf: [] for tf in TFS}
res = {tf: [] for tf in TFS}
tp_hist = {tf: {} for tf in TFS}
t0 = time.time()
for mk in MARKETS:
    line = []
    for tf, (train, step) in TFS.items():
        total = train + WINDOWS * step
        try:
            cl = fetch(mk, tf, total)
            s = bot.Series(cl)
            ok = [btc[tf].get(c["candle_date_time_utc"][:16], False) for c in cl]
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
                r = sim(s, sp, end, min(end + step, s.n))
                res[tf] += r
                resf[tf] += sim(s, sp, end, min(end + step, s.n), ok)
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
print("\n#### BTC 상승 국면에서만 매수했다면")
for tf in TFS:
    st = bot.stats(resf[tf]) if resf[tf] else {"n": 0, "winrate": 0, "edge": 0, "total": 0, "pf": 0}
    print(f"{tf:>2}분봉: {st['n']}건 · 승률 {st['winrate'] * 100:.0f}% · 거래당 {st['edge']:+.3f}% · 합계 {st['total']:+.1f}% · "
          f"손익비 {min(st['pf'], 99):.2f} · (같은 기간 BTC {btc_move[tf]:+.2f}%)")
import json
json.dump({str(k): v for k, v in summary.items()}, open("bt_summary.json", "w"), ensure_ascii=False)
