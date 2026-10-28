# -*- coding: utf-8 -*-
"""이번 실험: 뉴스·경제 일정과 BTC 시세의 관계 (이벤트 스터디, 실제 주문 없음)
- 업비트 KRW-BTC 1시간봉 약 400일
- FOMC(14:00 ET)·미국 CPI(08:30 ET) 발표 전후 변동성/방향을 '같은 시각 평소'와 비교
- 시간대(한국시간)별 평균 변동폭, 급락 뒤 다음 1·3시간 흐름(추세 지속 vs 반등)
- 최근 30일 가장 크게 움직인 1시간 목록(뉴스 대조용)
- 원자료는 results/btc_1h.json 으로 저장
"""
import json, math, statistics as st, time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests

ET, KST, UTC = ZoneInfo("America/New_York"), ZoneInfo("Asia/Seoul"), timezone.utc
FOMC = ["2025-06-18", "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10", "2026-01-28", "2026-03-18", "2026-04-29",
        "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28"]
CPI = ["2025-06-11", "2025-07-15", "2025-08-12", "2025-09-11", "2025-10-24", "2025-12-18", "2026-01-13", "2026-02-13",
       "2026-03-11", "2026-04-10", "2026-05-12", "2026-06-10", "2026-07-14", "2026-08-12", "2026-09-11", "2026-10-14"]


def fetch(n_hours=9800):
    out, to = [], None
    while len(out) < n_hours:
        p = {"market": "KRW-BTC", "count": 200}
        if to:
            p["to"] = to
        for _ in range(5):
            r = requests.get("https://api.upbit.com/v1/candles/minutes/60", params=p, timeout=10)
            if r.status_code == 200:
                break
            time.sleep(1)
        d = r.json()
        if not d:
            break
        out += d
        to = d[-1]["candle_date_time_utc"] + "Z"
        time.sleep(0.15)
    rows = {}
    for c in out:
        t = int(datetime.fromisoformat(c["candle_date_time_utc"]).replace(tzinfo=UTC).timestamp())
        rows[t] = (c["opening_price"], c["high_price"], c["low_price"], c["trade_price"])
    return sorted((t,) + v for t, v in rows.items())


rows = fetch()
json.dump(rows, open("results/btc_1h.json", "w"))
T = [r[0] for r in rows]; O = [r[1] for r in rows]; C = [r[4] for r in rows]
idx = {t: i for i, t in enumerate(T)}
R = [0.0] + [math.log(C[i] / C[i - 1]) * 100 for i in range(1, len(C))]     # 1시간 수익률 %
print(f"BTC 1시간봉 {len(rows)}개: {datetime.fromtimestamp(T[0], KST):%Y-%m-%d} ~ {datetime.fromtimestamp(T[-1], KST):%Y-%m-%d %H시} (한국시간)\n")


def hour_start(dt_et):
    t = int(dt_et.astimezone(UTC).timestamp())
    return t - t % 3600


def window(i, pre=1, post=3):
    """i = 발표가 들어 있는 1시간봉. [i-pre, i+post) 동안의 실현 변동(|r| 합)과 방향"""
    a, b = i - pre, i + post
    if a < 1 or b >= len(C):
        return None
    vol = sum(abs(R[k]) for k in range(a, b))
    ret = math.log(C[b - 1] / O[a]) * 100
    after24 = math.log(C[min(len(C) - 1, i + 24)] / C[i - 1]) * 100
    return vol, ret, after24


def study(name, days, hhmm):
    hh, mm = hhmm
    ev, ev_hours = [], set()
    for d in days:
        dt = datetime.fromisoformat(d).replace(hour=hh, minute=mm, tzinfo=ET)
        t = hour_start(dt)
        if t in idx:
            ev.append((d, idx[t], dt))
            ev_hours.add(t)
    # 평소: 같은 한국시각(시) 창, 발표일 ±1일 제외
    excl = {t + k * 3600 for t in ev_hours for k in range(-36, 37)}
    base = {}
    for _, i, _ in ev:
        h = datetime.fromtimestamp(T[i], KST).hour
        if h not in base:
            vals = [window(j) for j in range(len(T)) if datetime.fromtimestamp(T[j], KST).hour == h and T[j] not in excl]
            base[h] = [v for v in vals if v]
    print(f"#### {name} (발표 1시간 전 ~ 발표 후 3시간, 실현 변동 = 1시간 변동폭 합)")
    ratios, ranks, downs, a24 = [], [], [], []
    for d, i, dt in ev:
        w = window(i)
        if not w:
            continue
        h = datetime.fromtimestamp(T[i], KST).hour
        bv = [x[0] for x in base[h]]
        med = st.median(bv)
        rank = sum(1 for x in bv if x < w[0]) / len(bv) * 100
        ratios.append(w[0] / med); ranks.append(rank); downs.append(w[1] < 0); a24.append(w[2])
        print(f"  {d} ({datetime.fromtimestamp(T[i], KST):%m-%d %H시} KST): 변동 {w[0]:.2f}% (평소 중앙값 {med:.2f}%, 상위 {100 - rank:.0f}%) · 창 수익 {w[1]:+.2f}% · 24시간 {w[2]:+.2f}%")
    if ratios:
        print(f"  → {len(ratios)}회 평균: 평소 대비 변동 {st.mean(ratios):.2f}배(중앙 {st.median(ratios):.2f}배) · 평소 상위 30% 안에 든 비율 {sum(r >= 70 for r in ranks) / len(ranks) * 100:.0f}%"
              f" · 하락 마감 {sum(downs)}/{len(downs)} · 24시간 뒤 평균 {st.mean(a24):+.2f}%\n")
    return {"n": len(ratios), "volRatioMean": st.mean(ratios) if ratios else None, "volRatioMedian": st.median(ratios) if ratios else None,
            "top30pct": sum(r >= 70 for r in ranks) / len(ranks) if ranks else None, "down": sum(downs), "after24Mean": st.mean(a24) if a24 else None}


summary = {"FOMC": study("FOMC 금리 발표 (14:00 ET)", FOMC, (14, 0)), "CPI": study("미국 CPI 발표 (08:30 ET)", CPI, (8, 30))}

print("#### 한국시간 시각별 평균 1시간 변동폭(|수익률|) — 최근 90일")
cut = T[-1] - 90 * 86400
byh = {h: [] for h in range(24)}
for i in range(1, len(T)):
    if T[i] >= cut:
        byh[datetime.fromtimestamp(T[i], KST).hour].append(abs(R[i]))
allm = st.mean([x for v in byh.values() for x in v])
for h in range(24):
    m = st.mean(byh[h])
    print(f"  {h:02d}시 {m:.3f}% {'█' * int(m / allm * 10)}{'  ← 평균의 %.1f배' % (m / allm) if m / allm >= 1.3 else ''}")
summary["hourVol"] = {h: st.mean(byh[h]) for h in range(24)}

print("\n#### 1시간에 크게 빠진 뒤 흐름 (최근 400일) — 추세 지속이면 'BTC 급락 관문'이 의미 있음")
for th in (-0.7, -1.0, -1.5):
    nxt1, nxt3 = [], []
    for i in range(1, len(T) - 4):
        if R[i] <= th:
            nxt1.append(R[i + 1]); nxt3.append(sum(R[i + 1:i + 4]))
    if nxt1:
        print(f"  1시간 {th}% 이하 {len(nxt1)}회 → 다음 1시간 평균 {st.mean(nxt1):+.3f}% (하락 {sum(x < 0 for x in nxt1) / len(nxt1) * 100:.0f}%) · 다음 3시간 평균 {st.mean(nxt3):+.3f}% (하락 {sum(x < 0 for x in nxt3) / len(nxt3) * 100:.0f}%)")
allnext = [sum(R[i + 1:i + 4]) for i in range(1, len(T) - 4)]
print(f"  (참고: 아무 때나 다음 3시간 평균 {st.mean(allnext):+.3f}%, 하락 {sum(x < 0 for x in allnext) / len(allnext) * 100:.0f}%)")

print("\n#### 최근 30일 가장 크게 움직인 1시간 (뉴스 대조용, 한국시간)")
cut = T[-1] - 30 * 86400
big = sorted([i for i in range(1, len(T)) if T[i] >= cut], key=lambda i: -abs(R[i]))[:20]
for i in sorted(big):
    print(f"  {datetime.fromtimestamp(T[i], KST):%m-%d %H:00} {R[i]:+.2f}% (가격 {C[i]:,.0f}원)")
json.dump(summary, open("bt_summary.json", "w"), ensure_ascii=False, indent=1)
