# -*- coding: utf-8 -*-
"""bt-lab 실험 4: 바이낸스 스윙형 vs 회전형(scalp) — 보지 않은 다음 10일 성적 (15분봉, 롱·숏)
- 학습 20일(1920봉)로 종목별 전략 1개 선정 → 다음 960봉(10일)에 그대로 적용해 거래 수·거래당 순익 측정
- 시세: fapi(선물) 실패 시 data-api.binance.vision(현물)로 대체
"""
import json, sys, time, random, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
import requests
import chamdom_bot as cb
import binance_bot as bb

TRAIN, TEST = 1920, 960
SRC = {"name": None}

def get_json(urls):
    for u in urls:
        try:
            r = requests.get(u, timeout=20)
            if r.status_code == 200:
                return r.json(), u
        except Exception:
            pass
    return None, None

def top_syms(n):
    d, u = get_json(["https://fapi.binance.com/fapi/v1/ticker/24hr", "https://data-api.binance.vision/api/v3/ticker/24hr"])
    SRC["name"] = "fapi" if u and "fapi" in u else "spot"
    rows = [x for x in d if x["symbol"].endswith("USDT") and not any(k in x["symbol"] for k in ("XRP", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "UP", "DOWN"))]
    rows.sort(key=lambda x: -float(x.get("quoteVolume") or 0))
    return [x["symbol"] for x in rows[:n]]

def kl(sym, total):
    base = "https://fapi.binance.com/fapi/v1/klines" if SRC["name"] == "fapi" else "https://data-api.binance.vision/api/v3/klines"
    out, end = [], None
    while len(out) < total:
        q = f"{base}?symbol={sym}&interval=15m&limit=1000" + (f"&endTime={end}" if end else "")
        d, _ = get_json([q])
        if not d:
            break
        rows = [{"opening_price": float(k[1]), "high_price": float(k[2]), "low_price": float(k[3]), "trade_price": float(k[4]),
                 "candle_acc_trade_volume": float(k[5]), "candle_date_time_kst": str(k[0]), "t": k[0]} for k in d]
        out = rows + out
        end = d[0][0] - 1
        if len(d) < 1000:
            break
        time.sleep(0.2)
    return out[-total:]

def run(style, min_edge, syms, data):
    bb.STYLE = style
    cb.EXPLORE_MIN_EDGE = min_edge
    res = {"n": 0, "sum": 0.0, "wins": 0, "picked": 0, "long": [0, 0.0], "short": [0, 0.0]}
    for sym in syms:
        cl = data[sym]
        if len(cl) < TRAIN + TEST * 0.9:
            continue
        train, test = cl[:TRAIN], cl[TRAIN:]
        bb.klines = lambda s, t, _c=train: _c
        st = bb.pick_for(sym, random.Random(7))
        if not st:
            continue
        res["picked"] += 1
        ser = bb.series_of(test if st["side"] == "long" else bb.inverted(test))
        trs = cb.backtest(ser, st["spec"])
        for t in trs:
            pct = t if isinstance(t, (int, float)) else t.get("pct", t.get("ret", 0))
            res["n"] += 1; res["sum"] += pct; res["wins"] += 1 if pct > 0 else 0
            res[st["side"]][0] += 1; res[st["side"]][1] += pct
    return res

def main():
    syms = top_syms(40)
    print(f"시세 출처: {SRC['name']} · 종목 {len(syms)}개 · 학습 {TRAIN}봉 → 검증 {TEST}봉(15분)")
    data = {}
    for s in syms:
        data[s] = kl(s, TRAIN + TEST)
    print("BTC 검증 구간 변화:", f"{(data[syms[0]][-1]['trade_price'] / data[syms[0]][TRAIN]['trade_price'] - 1) * 100:+.2f}%" if syms and len(data[syms[0]]) > TRAIN else "-")
    out = {}
    for name, style, edge, n in (("스윙형(기존, 20종목)", "swing", 0.20, 20), ("회전형 최소순익0.20 (40종목)", "scalp", 0.20, 40),
                                 ("회전형 최소순익0.10 (40종목)", "scalp", 0.10, 40), ("회전형 최소순익0.05 (40종목)", "scalp", 0.05, 40)):
        t0 = time.time()
        r = run(style, edge, syms[:n], data)
        avg = r["sum"] / r["n"] if r["n"] else 0
        days = TEST * 15 / 60 / 24
        print(f"{name}: 채택 {r['picked']}종목 · 거래 {r['n']}회(하루 {r['n'] / days:.1f}회) · 승률 {r['wins'] / max(1, r['n']) * 100:.0f}% · "
              f"거래당 {avg:+.3f}% · 합계 {r['sum']:+.1f}% · 롱 {r['long'][0]}회 {r['long'][1] / max(1, r['long'][0]):+.3f}% · "
              f"숏 {r['short'][0]}회 {r['short'][1] / max(1, r['short'][0]):+.3f}% · {time.time() - t0:.0f}초", flush=True)
        out[name] = r
    json.dump(out, open("bt_summary.json", "w"), ensure_ascii=False, indent=1)

if __name__ == "__main__":
    main()
