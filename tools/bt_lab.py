# -*- coding: utf-8 -*-
"""bt-lab 실험 10: 시장 중립 '추세 묶음'이 긴 기간·전체 종목에서도 통하는지 (일봉 2년, 그때그때의 종목만 사용)
배경: 실험 9(15분봉 125일·후보 88종목)에서 지금 방식·공통 규칙·지정가 체결은 모두 거래당 0% 안팎이었고,
  유일하게 '최근 7일 많이 오른 5종목 롱 + 많이 내린 5종목 숏, 하루마다 교체'가 포지션당 하루 +0.62%(비용 뒤)였음.
  다만 30가지 중 2가지만 플러스였고 종목 후보가 11월 기준이라 우연·뒤늦은 종목 선택 편향일 수 있음 → 제대로 다시 확인.
방법
- 시세: 바이낸스 공개 자료실 USDⓈ-M 선물 일봉, 상장폐지 종목 포함 전 종목(USDT 무기한), 약 2년
- 날마다(UTC 0시): 직전 7일 평균 거래대금 상위 N종목(그날 기준, 미래 정보 없음) 중에서
  최근 F일 수익률 위 K개 롱 + 아래 K개 숏(같은 금액) → H일 뒤 시가에 교체. XRP·스테이블·BTCDOM 제외
- 비용: 바뀌는 종목만 왕복 0.14%(수수료 0.05%x2 + 밀림 0.02%x2). 펀딩비는 자료가 받아지면 실제 값 반영
- 본다: 포지션당 순수익, 연 환산 샤프, 최대 낙폭, t값, 2025·2026 따로, 롱 쪽·숏 쪽 따로
분석 전용 — 봇 동작에는 영향 없음
"""
import io, json, math, os, re, sys, time, zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import requests

SELFTEST = os.environ.get("LAB_SELFTEST") == "1"
ARCH = "https://data.binance.vision/data/futures/um"
LIST = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
DAY = 86400000
COST = 2 * (0.0005 + 0.0002)
SKIP = re.compile(r"^(USDC|FDUSD|TUSD|BUSD|DAI|USDP|USDE|USD1|PYUSD|EUR|BTCDOM|DEFI|XRP|1000XRP|BLUEBIRD|FOOTBALL)USDT$")
MONTHS_BACK = 24
FALLBACK = """BTCUSDT ETHUSDT SOLUSDT BNBUSDT DOGEUSDT ADAUSDT LINKUSDT AVAXUSDT LTCUSDT BCHUSDT SUIUSDT TRXUSDT DOTUSDT
NEARUSDT APTUSDT ARBUSDT OPUSDT UNIUSDT AAVEUSDT FILUSDT INJUSDT TAOUSDT HYPEUSDT ENAUSDT WLDUSDT 1000PEPEUSDT
1000SHIBUSDT 1000BONKUSDT WIFUSDT TONUSDT ZECUSDT XMRUSDT XLMUSDT HBARUSDT ONDOUSDT SEIUSDT TIAUSDT FETUSDT ATOMUSDT
ETCUSDT CRVUSDT LDOUSDT PENGUUSDT FARTCOINUSDT VIRTUALUSDT TRUMPUSDT PUMPUSDT ASTERUSDT XPLUSDT PAXGUSDT
1000LUNCUSDT ACEUSDT AKEUSDT ARIAUSDT BARDUSDT BLESSUSDT BTWUSDT BULLAUSDT CYBERUSDT DODOXUSDT ELSAUSDT ENSOUSDT
EVAAUSDT HUMAUSDT ICNTUSDT IDOLUSDT IRYSUSDT KERNELUSDT LISTAUSDT LITUSDT MAGMAUSDT MMTUSDT NAORISUSDT NIGHTUSDT
ORCAUSDT PHAROSUSDT PIEVERSEUSDT PIPPINUSDT PROMUSDT RAREUSDT SENTUSDT SKYAIUSDT SKYUSDT VVVUSDT XAUTUSDT ZILUSDT
ZKCUSDT ZKPUSDT""".split()


def fetch(url, tries=3):
    for a in range(tries):
        try:
            r = requests.get(url, timeout=40)
            if r.status_code == 200:
                return r.content
            if r.status_code in (403, 404):
                return None
        except Exception:
            pass
        time.sleep(1 + a)
    return None


def list_symbols():
    out, marker = [], ""
    for _ in range(20):
        c = fetch(f"{LIST}?delimiter=/&prefix=data/futures/um/monthly/klines/" + (f"&marker={marker}" if marker else ""))
        if not c:
            break
        x = c.decode("utf-8", "replace")
        got = re.findall(r"<Prefix>data/futures/um/monthly/klines/([A-Z0-9]+)/</Prefix>", x)
        out += got
        m = re.search(r"<NextMarker>([^<]+)</NextMarker>", x)
        if "<IsTruncated>true</IsTruncated>" not in x or not m:
            break
        marker = m.group(1)
    return sorted(set(s for s in out if s.endswith("USDT") and not SKIP.match(s)))


def rows_of(content, cols):
    out = []
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            for name in z.namelist():
                with z.open(name) as f:
                    for line in io.TextIOWrapper(f, "utf-8"):
                        p = line.strip().split(",")
                        if len(p) <= max(cols) or not p[0][:1].isdigit():
                            continue
                        t = int(p[0])
                        if t > 10 ** 14:
                            t //= 1000
                        out.append((t,) + tuple(float(p[i]) for i in cols))
    except Exception:
        return []
    return out


def month_list(n):
    today = datetime.now(timezone.utc).date()
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append(f"{y}-{m:02d}")
    return list(reversed(out))


def load(syms, months):
    kl = {s: {} for s in syms}
    fr = {s: {} for s in syms}
    tasks = [(s, "k", f"{ARCH}/monthly/klines/{s}/1d/{s}-1d-{m}.zip") for s in syms for m in months]
    tasks += [(s, "f", f"{ARCH}/monthly/fundingRate/{s}/{s}-fundingRate-{m}.zip") for s in syms for m in months]

    def job(t):
        return t, fetch(t[2], 2)
    with ThreadPoolExecutor(32) as ex:
        for (s, kind, _), c in ex.map(job, tasks):
            if not c:
                continue
            if kind == "k":
                for t, o, h, l, cl, q in rows_of(c, (1, 2, 3, 4, 7)):
                    kl[s][t // DAY] = (o, cl, q)
            else:
                for t, rate in rows_of(c, (2,)):            # calc_time, funding_interval_hours, last_funding_rate
                    d = t // DAY
                    fr[s][d] = fr[s].get(d, 0.0) + rate
    return {s: v for s, v in kl.items() if len(v) >= 40}, fr


def synth(n_sym=60, n_day=500):
    import random
    rng = random.Random(5)
    d0 = 20000
    kl, fr = {}, {}
    for i in range(n_sym):
        s = f"S{i:02d}USDT"
        p, mom = 10.0, 0.0
        kl[s], fr[s] = {}, {}
        for d in range(n_day):
            mom = 0.9 * mom + rng.gauss(0, 0.004)            # 약한 추세 지속을 일부러 넣음(코드 확인용)
            r = mom + rng.gauss(0, 0.04)
            o = p
            p *= math.exp(r)
            kl[s][d0 + d] = (o, p, 1e6 * (1 + i % 7))
            fr[s][d0 + d] = 0.0003
    return kl, fr


def run(kl, fr, days, F, H, K, N, skip, use_fund=True, leg="both"):
    """days: 날짜 번호 목록(오름차순). 반환 지표 dict 또는 None"""
    per, held_prev = [], set()
    n_pos = 0
    by_year = {}
    for i in range(F + skip + 8, len(days) - H, H):
        d = days[i]
        cand = []
        for s, v in kl.items():
            a, b = v.get(d - 1 - skip), v.get(d - 1 - skip - F)
            if not a or not b or d not in v or (d + H) not in v:
                continue
            vol = [v[x][2] for x in range(d - 7, d) if x in v]
            if len(vol) < 7:
                continue
            cand.append((sum(vol) / 7, a[1] / b[1] - 1, s))
        if len(cand) < max(2 * K + 6, 20):
            continue
        cand.sort(reverse=True)
        uni = sorted(cand[:N], key=lambda x: x[1])
        shorts, longs = [x[2] for x in uni[:K]], [x[2] for x in uni[-K:]]
        tot, cnt = 0.0, 0
        held = set()
        for sign, grp in ((1, longs), (-1, shorts)):
            if (leg == "long" and sign < 0) or (leg == "short" and sign > 0):
                continue
            for s in grp:
                v = kl[s]
                g = sign * (v[d + H][0] / v[d][0] - 1)
                key = (s, sign)
                c = 0.0 if key in held_prev else COST
                f = -sign * sum(fr.get(s, {}).get(x, 0.0) for x in range(d, d + H)) if use_fund else 0.0   # 롱은 펀딩을 내고(+율), 숏은 받음
                tot += g - c + f
                cnt += 1
                held.add(key)
        held_prev = held
        r = tot / cnt
        per.append(r)
        n_pos += cnt
        y = datetime.fromtimestamp(d * 86400, timezone.utc).year
        by_year.setdefault(y, []).append(r)
    if len(per) < 20:
        return None
    m = sum(per) / len(per)
    sd = (sum((x - m) ** 2 for x in per) / len(per)) ** 0.5
    acc, peak, dd = 0.0, 0.0, 0.0
    for x in per:
        acc += x
        peak = max(peak, acc)
        dd = min(dd, acc - peak)
    return {"periods": len(per), "n": n_pos, "mean": m * 100, "t": m / sd * len(per) ** 0.5 if sd else 0,
            "sharpe": m / sd * (365 / H) ** 0.5 if sd else 0, "sum": acc * 100, "dd": dd * 100,
            "year": {y: (sum(v) / len(v) * 100, len(v)) for y, v in sorted(by_year.items())},
            "win": sum(1 for x in per if x > 0) / len(per) * 100}


def main():
    t0 = time.time()
    if SELFTEST:
        kl, fr = synth()
        src = "자체 점검용 가짜 시세"
    else:
        syms = list_symbols()
        src = f"선물 자료실 전 종목 {len(syms)}개(상장폐지 포함)"
        if len(syms) < 50:                                   # 목록을 못 받으면 실험 9의 후보 종목으로 대신(이 경우 뒤늦은 종목 선택 편향이 남음)
            syms = [x for x in FALLBACK if not SKIP.match(x)]
            src = f"선물 자료실(전 종목 목록 실패 → 후보 {len(syms)}개만, 편향 주의)"
        kl, fr = load(syms, month_list(MONTHS_BACK))
    days = sorted({d for v in kl.values() for d in v})
    ds = lambda d: datetime.fromtimestamp(d * 86400, timezone.utc).strftime("%Y-%m-%d")
    has_fund = sum(1 for v in fr.values() if v)
    print(f"시세 출처: {src} · 일봉 받은 종목 {len(kl)}개 · {ds(days[0])} ~ {ds(days[-1])} ({len(days)}일) · 펀딩비 받은 종목 {has_fund}개 · 받는 데 {time.time() - t0:.0f}초")
    print(f"날마다 살아 있던 종목 수(중앙값): {sorted(sum(1 for v in kl.values() if d in v) for d in days)[len(days) // 2]}개 · 비용: 바뀌는 종목 왕복 {COST * 100:.2f}% + 실제 펀딩비")
    out = []
    print("\n===== 추세 묶음: 최근 F일 위 K개 롱 + 아래 K개 숏, H일마다 교체 (수익률은 포지션 1개·1회 보유 기준 %, 비용·펀딩 뒤)")
    grid = [(F, H, K, N, sk) for N in (40, 80) for K in (5, 10) for F in (3, 7, 14, 28) for H in (1, 3, 7) for sk in (0, 1)]
    if SELFTEST:
        grid = [(F, H, 5, 40, 0) for F in (7, 14) for H in (1, 3)]
    for F, H, K, N, sk in grid:
        r = run(kl, fr, days, F, H, K, N, sk)
        if not r:
            continue
        out.append({"F": F, "H": H, "K": K, "N": N, "skip": sk, **r})
    out.sort(key=lambda x: -x["t"])
    fmt = lambda r: (f"상위{r['N']}종목·위아래 {r['K']}개·F={r['F']}일{'(직전 1일 제외)' if r['skip'] else ''}·H={r['H']}일: 회차 {r['periods']} · 순 {r['mean']:+.3f}% · t {r['t']:+.2f} · "
                     f"샤프 {r['sharpe']:+.2f} · 누적 {r['sum']:+.0f}%p · 낙폭 {r['dd']:+.0f}%p · 플러스 회차 {r['win']:.0f}% · "
                     + " ".join(f"{y}년 {m:+.3f}%({n})" for y, (m, n) in r["year"].items()))
    print(f"조합 {len(out)}가지 중 t값 상위 12:")
    for r in out[:12]:
        print(" ", fmt(r), flush=True)
    print("t값 하위 5:")
    for r in out[-5:]:
        print(" ", fmt(r))
    pos = sum(1 for r in out if r["mean"] > 0)
    sig = sum(1 for r in out if r["t"] > 2)
    print(f"전체 요약: 플러스 {pos}/{len(out)} · t>2 {sig}개 · 순수익 중앙값 {sorted(r['mean'] for r in out)[len(out) // 2]:+.3f}%")
    print("\n===== 실험 9에서 좋았던 조합(F=7일·H=1일·K=5·상위40)을 여러 각도로")
    for name, kw in (("기본(비용+펀딩)", {}), ("펀딩 빼고", {"use_fund": False}), ("롱 쪽만", {"leg": "long"}), ("숏 쪽만", {"leg": "short"})):
        r = run(kl, fr, days, 7, 1, 5, 40, 0, **kw)
        if r:
            print(f"  {name}: 회차 {r['periods']} · 순 {r['mean']:+.3f}% · t {r['t']:+.2f} · 샤프 {r['sharpe']:+.2f} · 누적 {r['sum']:+.0f}%p · 낙폭 {r['dd']:+.0f}%p · "
                  + " ".join(f"{y}년 {m:+.3f}%({n})" for y, (m, n) in r["year"].items()))
    print("\n===== F·H 별 평균(상위 40·80, K 5·10, 직전 1일 포함/제외를 모두 평균) — 한두 조합의 우연인지 넓게 보기")
    for F in (3, 7, 14, 28):
        cells = []
        for H in (1, 3, 7):
            rs = [r for r in out if r["F"] == F and r["H"] == H]
            if rs:
                cells.append(f"H={H}일 순 {sum(r['mean'] for r in rs) / len(rs):+.3f}%/하루 {sum(r['mean'] / H for r in rs) / len(rs):+.3f}% (t 평균 {sum(r['t'] for r in rs) / len(rs):+.2f})")
        print(f"  F={F}일: " + " · ".join(cells))
    json.dump({"src": src, "from": ds(days[0]), "to": ds(days[-1]), "rows": out}, open("bt_summary.json", "w"), ensure_ascii=False, indent=1)
    print(f"\n소요 {time.time() - t0:.0f}초")


if __name__ == "__main__":
    main()
