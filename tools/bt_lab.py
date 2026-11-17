# -*- coding: utf-8 -*-
"""bt-lab 실험 7: 업비트 5분봉 걸어가며 검증 — 봉을 키우면 비용 부담이 줄어 선정이 플러스가 되는가
배경: 실험 6(1분봉, 학습 3일→다음 1일 x5)에서 선정 131회 거래당 -0.29%(무작위 -0.88%), 플러스 구간 1/5.
  선정이 무작위보다 +0.59%p 낫지만 왕복 비용(약 0.2~0.4%)을 못 넘김 → 한 번에 더 크게 움직이는 5분봉에서 재측정.
- A: 5분봉 학습 3일(864봉)→다음 1일(288봉) x5 / B: 5분봉 학습 7일(2016봉)→다음 1일 x5
- 보유시간(분)은 5분봉 개수로 환산(hold/5), 지표 기간은 봉 단위 그대로(=5배 긴 시간)
- 비용: 봇과 같은 수수료 0.05%x2 + 종목별 미끄러짐. 롱만(현물). 무작위 스펙 20개 기준선 동시 측정
분석 전용 — 봇 동작에는 영향 없음
"""
import json, sys, time, random, os
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
import requests
import chamdom_bot as cb

UP = "https://api.upbit.com/v1"
N_COINS = 30
CFG = {"A 5분봉 학습3일": {"unit": 5, "train": 864, "test": 288},
       "B 5분봉 학습7일": {"unit": 5, "train": 2016, "test": 288}}
WINDOWS = 5


def get(url, params=None):
    for _ in range(4):
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(1.0)
                continue
        except Exception:
            time.sleep(0.5)
    return None


def top_markets(n):
    mk = [m["market"] for m in (get(f"{UP}/market/all") or []) if m["market"].startswith("KRW-")]
    mk = [m for m in mk if m not in ("KRW-XRP",)]
    tick = []
    for i in range(0, len(mk), 100):
        tick += get(f"{UP}/ticker", {"markets": ",".join(mk[i:i + 100])}) or []
        time.sleep(0.15)
    tick.sort(key=lambda x: -float(x.get("acc_trade_price_24h") or 0))
    return [t["market"] for t in tick[:n]]


def candles(market, unit, total):
    out, to = [], None
    while len(out) < total:
        p = {"market": market, "count": 200}
        if to:
            p["to"] = to
        d = get(f"{UP}/candles/minutes/{unit}", p)
        if not d:
            break
        out = list(reversed(d)) + out
        to = d[-1]["candle_date_time_utc"].replace("T", " ")
        if len(d) < 200:
            break
        time.sleep(0.12)
    return out[-total:]


UNIT = 1


def conv(sp):
    sp = dict(sp)
    sp["hold"] = max(1, int(round(sp["hold"] / UNIT)))
    return sp


def pick(train, rng):
    ser = cb.Series(train)
    split = int(ser.n * (1 - cb.BT_HOLDOUT))
    best = None
    for _ in range(cb.BT_TRIALS):
        sp = conv(cb.random_spec(rng))
        tr = cb.stats(cb.backtest(ser, sp, end=split))
        if tr["n"] < cb.EXPLORE_MIN_TRADES or tr["edge"] < cb.EXPLORE_MIN_EDGE:
            continue
        ho = cb.stats(cb.backtest(ser, sp, start=split))
        if ho["n"] and ho["edge"] < cb.EXPLORE_HOLDOUT_FLOOR:
            continue
        score = tr["edge"] if not ho["n"] else (tr["edge"] * tr["n"] + ho["edge"] * ho["n"]) / (tr["n"] + ho["n"])
        if not best or score > best[0]:
            best = (score, sp)
    return best and best[1]


def run_window(data, off, c):
    sel = {"n": 0, "sum": 0.0, "wins": 0, "picked": 0, "coins": 0, "tp": {}}
    base = {"n": 0, "sum": 0.0, "wins": 0}
    for mk, cl in data.items():
        need = off + c["train"] + c["test"]
        if len(cl) < need:
            continue
        sel["coins"] += 1
        train, test = cl[off:off + c["train"]], cl[off + c["train"]:need]
        tser = cb.Series(test)
        sp = pick(train, random.Random(7 + off))
        if sp:
            sel["picked"] += 1
            for pct in cb.backtest(tser, sp):
                sel["n"] += 1; sel["sum"] += pct; sel["wins"] += pct > 0
                b = sel["tp"].setdefault(str(sp.get("tp")), [0, 0.0]); b[0] += 1; b[1] += pct
        rng = random.Random(11 + off)
        for _ in range(20):
            for pct in cb.backtest(tser, conv(cb.random_spec(rng))):
                base["n"] += 1; base["sum"] += pct; base["wins"] += pct > 0
    return sel, base


def fmt(r):
    return f"거래 {r['n']}회 · 승률 {r['wins'] / max(1, r['n']) * 100:.0f}% · 거래당 {r['sum'] / max(1, r['n']):+.3f}%"


def main():
    global UNIT
    t0 = time.time()
    mks = top_markets(N_COINS)
    print(f"업비트 원화마켓 거래대금 상위 {len(mks)}종목(XRP 제외) · 5분봉 걸어가며 검증(다음 1일 x {WINDOWS}구간) · 수수료 {cb.FEE_RATE * 100:.2f}%x2 + 미끄러짐 · 조합 {cb.BT_TRIALS}개", flush=True)
    need = max(c["train"] + c["test"] * WINDOWS for c in CFG.values())
    data = {m: candles(m, 5, need) for m in mks}
    btc = candles("KRW-BTC", 5, need)
    summ = {}
    for name, c in CFG.items():
        UNIT = c["unit"]
        lead = need - (c["train"] + c["test"] * WINDOWS)  # 두 실험의 검증 구간을 같은 날짜로 맞춤
        dd = {m: cl[lead:] for m, cl in data.items() if len(cl) >= need}
        bt = btc[lead:]
        print(f"== {name} (학습 {c['train']}봉 → 다음 {c['test']}봉) · 종목 {len(dd)}", flush=True)
        out, S, B = [], {"n": 0, "sum": 0.0, "wins": 0}, {"n": 0, "sum": 0.0, "wins": 0}
        for w in range(WINDOWS):
            off = w * c["test"]
            a, b = off + c["train"], off + c["train"] + c["test"] - 1
            chg = (bt[b]["trade_price"] / bt[a]["trade_price"] - 1) * 100 if len(bt) > b else 0
            sel, base = run_window(dd, off, c)
            print(f"[구간 {w + 1}] BTC {chg:+.2f}% · 채택 {sel['picked']}/{sel['coins']} · 선정 {fmt(sel)} · 무작위 {fmt(base)}", flush=True)
            for k in ("n", "sum", "wins"):
                S[k] += sel[k]; B[k] += base[k]
            out.append({"btcChg": chg, "sel": sel, "base": base})
        pos = sum(1 for o in out if o["sel"]["n"] and o["sel"]["sum"] > 0)
        print(f"[합계 {WINDOWS}일] 선정 {fmt(S)} · 하루 {S['n'] / WINDOWS:.1f}회 · 무작위 {fmt(B)} · 플러스 구간 {pos}/{WINDOWS}", flush=True)
        summ[name] = {"windows": out, "sel": S, "base": B}
    print("참고: 실험 6(1분봉 학습3일) 선정 131회 거래당 -0.288% · 무작위 -0.880% · 플러스 구간 1/5")
    print(f"소요 {time.time() - t0:.0f}초")
    json.dump(summ, open("bt_summary.json", "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
