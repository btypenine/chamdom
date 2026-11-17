# -*- coding: utf-8 -*-
"""bt-lab 실험 5: 업비트 — 1분봉(현재) vs 15분봉(바이낸스식 스윙) 보지 않은 다음 구간 성적, 무작위 스펙 기준선과 비교
배경(11-17 실전 데이터): 업비트 모의·실전 거래당 약 -0.5%, 같은 순간 원숭이(무작위 매수)도 -0.5%(짝 비교 z 0.2)
→ 진입 선택보다 '1분봉 짧은 익절 + 체결 비용(매수 +0.12%·익절 매도 -0.39% 밀림)' 구조가 손실 원인인지 확인.
바이낸스는 15분봉(보유 7.5~60시간)으로 같은 조합 생성기를 써서 보지 않은 구간 +0.2~0.3%/건이었음(실험 4).
- A. 1분봉: 학습 4320봉(3일, 봇과 같음) → 다음 1440봉(1일) 적용
- B. 15분봉: 학습 1920봉(20일) → 다음 960봉(10일) 적용, 보유 30~240봉(7.5~60시간)
- 각 방식마다 '선정 없이 무작위 스펙 20개' 평균(기준선)도 같은 검증 구간에서 측정 → 선정이 무작위보다 나은지
- 비용: 봇과 같은 수수료 0.05%×2 + 종목별 미끄러짐(slip_for, 호가 단위 반영). 롱만(현물)
분석 전용 — 봇 동작에는 영향 없음
"""
import json, sys, time, random, os
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
import requests
import chamdom_bot as cb

UP = "https://api.upbit.com/v1"
N_COINS = 30
CFG = {"1m": {"unit": 1, "train": 4320, "test": 1440}, "15m": {"unit": 15, "train": 1920, "test": 960}}


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


def pick(train, rng):
    ser = cb.Series(train)
    split = int(ser.n * (1 - cb.BT_HOLDOUT))
    best = None
    for _ in range(cb.BT_TRIALS):
        sp = cb.random_spec(rng)
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


def run(tf, data):
    c = CFG[tf]
    sel = {"n": 0, "sum": 0.0, "wins": 0, "picked": 0, "coins": 0, "tp": {}}
    base = {"n": 0, "sum": 0.0, "wins": 0}
    for mk, cl in data.items():
        if len(cl) < c["train"] + c["test"] * 0.9:
            continue
        sel["coins"] += 1
        train, test = cl[:c["train"]], cl[c["train"]:]
        tser = cb.Series(test)
        sp = pick(train, random.Random(7))
        if sp:
            sel["picked"] += 1
            for pct in cb.backtest(tser, sp):
                sel["n"] += 1; sel["sum"] += pct; sel["wins"] += pct > 0
                b = sel["tp"].setdefault(str(sp.get("tp")), [0, 0.0]); b[0] += 1; b[1] += pct
        rng = random.Random(11)
        for _ in range(20):
            for pct in cb.backtest(tser, cb.random_spec(rng)):
                base["n"] += 1; base["sum"] += pct; base["wins"] += pct > 0
    return sel, base


def fmt(r):
    return f"거래 {r['n']}회 · 승률 {r['wins'] / max(1, r['n']) * 100:.0f}% · 거래당 {r['sum'] / max(1, r['n']):+.3f}%"


def main():
    t0 = time.time()
    mks = top_markets(N_COINS)
    print(f"업비트 원화마켓 거래대금 상위 {len(mks)}종목(XRP 제외) · 수수료 {cb.FEE_RATE * 100:.2f}%×2 + 종목별 미끄러짐 · 조합 {cb.BT_TRIALS}개 · 학습 최소 순익 {cb.EXPLORE_MIN_EDGE}%", flush=True)
    out = {}
    for tf in ("1m", "15m"):
        c = CFG[tf]
        data = {m: candles(m, c["unit"], c["train"] + c["test"]) for m in mks}
        btc = candles("KRW-BTC", c["unit"], c["train"] + c["test"])
        chg = (btc[-1]["trade_price"] / btc[c["train"]]["trade_price"] - 1) * 100 if len(btc) > c["train"] else 0
        days = c["test"] * c["unit"] / 1440
        sel, base = run(tf, data)
        print(f"[{tf}] 검증 {days:g}일 · BTC 검증 구간 {chg:+.2f}% · 대상 {sel['coins']}종목 · 채택 {sel['picked']}개", flush=True)
        print(f"  선정 전략: {fmt(sel)} · 하루 {sel['n'] / max(days, 1e-9):.1f}회", flush=True)
        print(f"  무작위 스펙 기준선(종목당 20개): {fmt(base)}", flush=True)
        print("  익절폭별(선정): " + " · ".join(f"+{k}% {v[0]}회 {v[1] / max(1, v[0]):+.2f}%" for k, v in sorted(sel["tp"].items(), key=lambda x: float(x[0]))), flush=True)
        out[tf] = {"sel": sel, "base": base, "btcChg": chg, "days": days}
    print(f"소요 {time.time() - t0:.0f}초")
    json.dump(out, open("bt_summary.json", "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
