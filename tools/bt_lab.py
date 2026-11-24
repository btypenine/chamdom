# -*- coding: utf-8 -*-
"""bt-lab 실험 12: '펀딩비 받는 묶음' — 최근 L일 펀딩비가 크게 마이너스인 K개 롱 + 크게 플러스인 K개 숏, 금액 맞춤 (4시간봉 2년)
배경: 실험 11에서 추세 묶음 수익의 약 90%가 펀딩비였음(펀딩 빼면 주당 +0.09%) → 펀딩비 자체를 기준으로 고르면 더 나은지 직접 확인.
보는 것
 1) 기준 조합(상위100·위아래10·L=3일·1일마다, 하루씩 어긋난 묶음으로 나눠 교체)의 수익을 가격/펀딩/비용으로 나눠 보기
 2) 이웃 값: 상위 N(60·100·150) x K(5·10·15) x L(1·3·7일) x 교체 H(1·3·7일)
 3) 펀딩 문턱: 8시간당 평균 |펀딩| 0.03%·0.05% 넘는 종목만
 4) 비용 더 비싸게(왕복 0.30%: 소형 종목 호가 벌어짐 가정), 롱 쪽만 / 숏 쪽만
 5) 펀딩 부호 뒤집힘: 보유 중 받은 펀딩 vs 고를 때 펀딩, 최악의 주·하루, 분기별
 6) 비교: 실험 11 추세 묶음(상위80·위아래10·F=14·7일, 7묶음)
펀딩 계산은 보수적으로: 진입 봉·청산 봉 시각의 정산은 넣지 않음, 고를 때는 그 시점까지 이미 정산된 펀딩만 씀
분석 전용 — 봇 동작에는 영향 없음
"""
import io, math, os, re, time, zipfile, json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import numpy as np
import requests

SELFTEST = os.environ.get("LAB_SELFTEST") == "1"
ARCH = "https://data.binance.vision/data/futures/um"
LIST = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
BAR = 4 * 3600 * 1000
DB = 6                      # 하루 = 4시간봉 6개
COST = 2 * (0.0005 + 0.0002)
SKIP = re.compile(r"^(USDC|FDUSD|TUSD|BUSD|DAI|USDP|USDE|USD1|PYUSD|EUR|BTCDOM|DEFI|XRP|1000XRP|BLUEBIRD|FOOTBALL)USDT$")
MONTHS_BACK = 24


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
        out += re.findall(r"<Prefix>data/futures/um/monthly/klines/([A-Z0-9]+)/</Prefix>", x)
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
    tasks = [(s, "k", f"{ARCH}/monthly/klines/{s}/4h/{s}-4h-{m}.zip") for s in syms for m in months]
    tasks += [(s, "f", f"{ARCH}/monthly/fundingRate/{s}/{s}-fundingRate-{m}.zip") for s in syms for m in months]

    def job(t):
        return t, fetch(t[2], 2)
    with ThreadPoolExecutor(32) as ex:
        for (s, kind, _), c in ex.map(job, tasks):
            if not c:
                continue
            if kind == "k":
                for t, o, cl, q in rows_of(c, (1, 4, 7)):
                    kl[s][t // BAR] = (o, cl, q)
            else:
                for t, rate in rows_of(c, (2,)):
                    b = t // BAR
                    fr[s][b] = fr[s].get(b, 0.0) + rate
    kl = {s: v for s, v in kl.items() if len(v) >= 40 * DB}
    return kl, fr


def to_mat(kl, fr):
    syms = sorted(kl)
    b0 = min(min(v) for v in kl.values())
    b1 = max(max(v) for v in kl.values())
    nb = b1 - b0 + 1
    O = np.full((len(syms), nb), np.nan)
    C = np.full((len(syms), nb), np.nan)
    Q = np.zeros((len(syms), nb))
    Fu = np.zeros((len(syms), nb))
    for i, s in enumerate(syms):
        for b, (o, c, q) in kl[s].items():
            O[i, b - b0], C[i, b - b0], Q[i, b - b0] = o, c, q
        for b, r in fr.get(s, {}).items():
            if b0 <= b <= b1:
                Fu[i, b - b0] = r
    return syms, b0, O, C, Q, Fu


def synth(n_sym=60, n_day=420):
    import random
    rng = random.Random(5)
    kl, fr = {}, {}
    b0 = 20000 * DB
    for i in range(n_sym):
        s = f"S{i:02d}USDT"
        p, mom = 10.0, 0.0
        kl[s], fr[s] = {}, {}
        for b in range(n_day * DB):
            mom = 0.98 * mom + rng.gauss(0, 0.0008)
            o = p
            p *= math.exp(mom + rng.gauss(0, 0.016))
            kl[s][b0 + b] = (o, p, 1e5 * (1 + i % 7))
            if b % 2 == 0:
                fr[s][b0 + b] = 0.0001 + 0.3 * mom + rng.gauss(0, 0.0002)
    return kl, fr


class Lab:
    def __init__(self, O, C, Q, Fu, b0):
        self.O, self.C, self.Fu, self.b0 = O, C, Fu, b0
        self.ns, self.nb = O.shape
        valid = ~np.isnan(C)
        self.cq = np.concatenate([np.zeros((self.ns, 1)), np.cumsum(Q, axis=1)], axis=1)
        self.cv = np.concatenate([np.zeros((self.ns, 1)), np.cumsum(valid, axis=1)], axis=1)
        self.cf = np.concatenate([np.zeros((self.ns, 1)), np.cumsum(Fu, axis=1)], axis=1)
        ff = C.copy()
        for j in range(1, self.nb):
            m = np.isnan(ff[:, j])
            ff[m, j] = ff[m, j - 1]
        self.ff = ff

    def run(self, N, K, H, start, L=3, F=14, sig="fund", cost=COST, use_fund=True, thr=0.0, leg="both"):
        """sig: fund(최근 L일 펀딩 합) / trend(실험 11 추세: F일 수익률, 직전 1일 제외).
        반환 [(봉, 포지션당 순수익, 가격, 펀딩, 비용, 고를 때 펀딩(8시간당), 받은 펀딩(8시간당), 롱 성적, 숏 성적)]"""
        O, ff, cf = self.O, self.ff, self.cf
        S, Hb, Lb, Fb, kb = 7 * DB, H * DB, L * DB, F * DB, DB
        out, prev = [], set()
        T = start
        need = S + max(Lb, Fb + kb) + 2
        while T < need:
            T += Hb
        while T + Hb < self.nb:
            v7 = self.cv[:, T] - self.cv[:, T - S]
            ok = (v7 == S) & ~np.isnan(O[:, T])
            if sig == "trend":
                ok &= ~np.isnan(ff[:, T - 1 - kb - Fb]) & (self.cv[:, T - 1 - kb - Fb + 1] - self.cv[:, T - 1 - kb - Fb] == 1)
            else:
                ok &= (self.cv[:, T] - self.cv[:, T - Lb] == Lb)
            idx = np.where(ok)[0]
            if len(idx) < max(2 * K + 6, 20):
                T += Hb
                continue
            qv = self.cq[idx, T] - self.cq[idx, T - S]
            uni = idx[np.argsort(-qv)[:N]]
            if sig == "trend":
                score = ff[uni, T - 1 - kb] / ff[uni, T - 1 - kb - Fb] - 1
                order = uni[np.argsort(score)]
                shorts, longs = order[:K], order[-K:]
            else:
                fs = cf[uni, T] - cf[uni, T - Lb]                 # 최근 L일 펀딩 합(이미 정산된 것만)
                per8 = fs / (L * 3)
                order = uni[np.argsort(fs)]
                longs, shorts = order[:K], order[-K:]               # 펀딩 마이너스 → 롱(받음), 플러스 → 숏(받음)
                if thr > 0:
                    pm = dict(zip(uni, per8))
                    longs = np.array([i for i in longs if pm[i] < -thr], dtype=int)
                    shorts = np.array([i for i in shorts if pm[i] > thr], dtype=int)
            tot = pr = fu = co = 0.0
            cnt, held, pick, got = 0, set(), [], []
            side = {1: [0.0, 0], -1: [0.0, 0]}
            for sign, grp in ((1, longs), (-1, shorts)):
                if (leg == "long" and sign < 0) or (leg == "short" and sign > 0):
                    continue
                for i in grp:
                    ex = O[i, T + Hb]
                    if np.isnan(ex):
                        ex = ff[i, T + Hb - 1]
                    g = sign * (ex / O[i, T] - 1)
                    c = 0.0 if (i, sign) in prev else cost
                    f = -sign * (cf[i, T + Hb] - cf[i, T + 1]) if use_fund else 0.0   # 진입·청산 봉 시각 정산 제외
                    r = g - c + f
                    tot += r; pr += g; fu += f; co += c
                    cnt += 1
                    held.add((i, sign))
                    side[sign][0] += r; side[sign][1] += 1
                    if sig == "fund":
                        pick.append(-sign * (cf[i, T] - cf[i, T - Lb]) / (L * 3))
                        got.append(f / max(1, H * 3 - 1))
            prev = held
            if cnt == 0:
                T += Hb
                continue
            out.append((T, tot / cnt, pr / cnt, fu / cnt, co / cnt,
                        (sum(pick) / len(pick)) if pick else 0.0, (sum(got) / len(got)) if got else 0.0,
                        side[1][0] / side[1][1] if side[1][1] else None, side[-1][0] / side[-1][1] if side[-1][1] else None))
            T += Hb
        return out


def stats(per, H):
    if len(per) < 15:
        return None
    m = sum(per) / len(per)
    sd = (sum((x - m) ** 2 for x in per) / len(per)) ** 0.5
    acc = peak = dd = 0.0
    for x in per:
        acc += x
        peak = max(peak, acc)
        dd = min(dd, acc - peak)
    return {"n": len(per), "mean": m * 100, "day": m * 100 / H, "sd": sd * 100, "t": m / sd * len(per) ** 0.5 if sd else 0,
            "sharpe": m / sd * (365 / H) ** 0.5 if sd else 0, "dd": dd * 100, "win": sum(1 for x in per if x > 0) / len(per) * 100,
            "worst": min(per) * 100}


def main():
    t0 = time.time()
    if SELFTEST:
        kl, fr = synth()
        src = "자체 점검용 가짜 시세"
    else:
        syms = list_symbols()
        src = f"선물 자료실 전 종목 {len(syms)}개(상장폐지 포함)"
        kl, fr = load(syms, month_list(MONTHS_BACK))
    syms, b0, O, C, Q, Fu = to_mat(kl, fr)
    lab = Lab(O, C, Q, Fu, b0)
    ds = lambda b: datetime.fromtimestamp((b0 + b) * 4 * 3600, timezone.utc).strftime("%Y-%m-%d %H시")
    print(f"시세 출처: {src} · 4시간봉 받은 종목 {len(syms)}개 · {ds(0)} ~ {ds(lab.nb - 1)} · 받는 데 {time.time() - t0:.0f}초")
    print(f"비용: 바뀌는 종목 왕복 {COST * 100:.2f}% + 실제 펀딩비(진입·청산 시각 정산 제외) · 보유 중 상장폐지는 마지막 가격 · 시각 UTC")
    wd0 = datetime.fromtimestamp(b0 * 4 * 3600, timezone.utc)
    base = (((7 - wd0.weekday()) % 7) * DB - wd0.hour // 4) % (7 * DB)
    BASE = dict(N=100, K=10, L=3, H=1)
    if SELFTEST:
        BASE = dict(N=40, K=5, L=3, H=1)

    def split(**kw):
        """H일 보유를 하루씩 어긋난 H묶음으로 나눔(H=1 이면 0시 UTC 하나). 기간 = H일 단위, 묶음 평균."""
        p = dict(BASE); p.update(kw)
        H = p["H"]
        runs = [lab.run(start=(base + d * DB) % (H * DB), **p) for d in range(H)]
        per = {}
        for d, s in enumerate(runs):
            o = (base + d * DB) % (H * DB)
            for row in s:
                per.setdefault((row[0] - o) // (H * DB), []).append(row)
        rows = []
        for k, v in sorted(per.items()):
            if len(v) >= max(1, (H + 1) // 2):
                rows.append((k, [sum(r[j] for r in v) / len(v) for j in range(1, 7)],
                             [r[7] for r in v if r[7] is not None], [r[8] for r in v if r[8] is not None]))
        st = stats([r[1][0] for r in rows], H)
        if not st:
            return None, rows, p
        n = len(rows)
        st["price"] = sum(r[1][1] for r in rows) / n * 100
        st["fund"] = sum(r[1][2] for r in rows) / n * 100
        st["cost"] = sum(r[1][3] for r in rows) / n * 100
        st["pick8"] = sum(r[1][4] for r in rows) / n * 100
        st["got8"] = sum(r[1][5] for r in rows) / n * 100
        lv = [x for r in rows for x in r[2]]; sv = [x for r in rows for x in r[3]]
        st["long"] = sum(lv) / len(lv) * 100 / H if lv else None
        st["short"] = sum(sv) / len(sv) * 100 / H if sv else None
        half = n // 2
        st["h1"] = sum(r[1][0] for r in rows[:half]) / max(1, half) * 100 / H
        st["h2"] = sum(r[1][0] for r in rows[half:]) / max(1, n - half) * 100 / H
        return st, rows, p

    def line(st):
        return (f"하루 {st['day']:+.3f}% · t {st['t']:+.2f} · 샤프 {st['sharpe']:+.2f} · 낙폭 {st['dd']:+.0f}%p · 앞/뒤 절반 하루 {st['h1']:+.3f}/{st['h2']:+.3f}%")

    def qtr(rows, H):
        q = {}
        for k, v, _, _ in rows:
            dt = datetime.fromtimestamp((b0 + base + k * H * DB) * 4 * 3600, timezone.utc)
            q.setdefault(f"{dt.year}Q{(dt.month - 1) // 3 + 1}", []).append(v[0])
        return q

    print("\n===== 1) 기준 조합: 상위100·위아래10·펀딩 L=3일·1일마다 교체")
    st, rows, p = split()
    if st:
        H = p["H"]
        print(f"  {line(st)} · 플러스 날 {st['win']:.0f}% · 최악의 기간 {st['worst']:+.2f}% ({st['n']}기간)")
        print(f"  나눠 보기(하루): 가격 {st['price'] / H:+.3f}% · 펀딩 {st['fund'] / H:+.3f}% · 비용 -{st['cost'] / H:.3f}% · 롱 쪽 {st['long']:+.3f}% · 숏 쪽 {st['short']:+.3f}%")
        print(f"  펀딩(8시간당, 받는 쪽 기준): 고를 때 {st['pick8']:+.4f}% → 보유 중 실제 {st['got8']:+.4f}% (유지율 {st['got8'] / st['pick8'] * 100 if st['pick8'] else 0:.0f}%)")
        q = qtr(rows, H)
        print("  분기별(하루 평균): " + " · ".join(f"{k} {sum(v) / len(v) * 100 / H:+.3f}%" for k, v in sorted(q.items())) + f" · 플러스 분기 {sum(1 for v in q.values() if sum(v) > 0)}/{len(q)}")

    print("\n===== 2) 이웃 값 (하루 순수익 / t / 낙폭 · 가격·펀딩 나눔)")
    grid = []
    Ns, Ks, Ls, Hs = ((60, 100, 150), (5, 10, 15), (1, 3, 7), (1, 3, 7)) if not SELFTEST else ((30,), (5,), (1, 3), (1, 3))
    for H in Hs:
        for N in Ns:
            for K in Ks:
                parts = []
                for L in Ls:
                    s, _, _ = split(N=N, K=K, L=L, H=H)
                    if s:
                        grid.append(((N, K, L, H), s))
                        parts.append(f"L{L} {s['day']:+.3f}%/t{s['t']:+.1f}/가{s['price'] / H:+.2f}·펀{s['fund'] / H:+.2f}")
                print(f"  H{H}일 상위{N}·위아래{K}: " + " · ".join(parts), flush=True)
    if grid:
        mm = sorted(s["day"] for _, s in grid)
        print(f"  요약: {len(grid)}칸 중 플러스 {sum(1 for m in mm if m > 0)} · 중앙값 하루 {mm[len(mm) // 2]:+.3f}% · t>2 {sum(1 for _, s in grid if s['t'] > 2)}칸 · 앞·뒤 절반 모두 플러스 {sum(1 for _, s in grid if s['h1'] > 0 and s['h2'] > 0)}칸")
        for H in Hs:
            g = [s for k, s in grid if k[3] == H]
            if g:
                print(f"  교체 {H}일: 하루 중앙값 {sorted(x['day'] for x in g)[len(g) // 2]:+.3f}% · 가격 평균 {sum(x['price'] for x in g) / len(g) / H:+.3f}% · 펀딩 평균 {sum(x['fund'] for x in g) / len(g) / H:+.3f}% · 비용 평균 -{sum(x['cost'] for x in g) / len(g) / H:.3f}%")
        best = sorted(grid, key=lambda x: -x[1]["t"])[:5]
        print("  t 상위 5: " + " · ".join(f"N{k[0]}K{k[1]}L{k[2]}H{k[3]} 하루 {s['day']:+.3f}%(t {s['t']:+.1f}, 앞/뒤 {s['h1']:+.2f}/{s['h2']:+.2f})" for k, s in best))

    print("\n===== 3) 펀딩 문턱 · 비싼 비용 · 한쪽만 · 펀딩 빼기 (기준 조합 + H=3일)")
    for H in (1, 3):
        for name, kw in (("기준", {}), ("|펀딩| 8시간당 0.03% 넘는 종목만", {"thr": 0.0003}), ("0.05% 넘는 종목만", {"thr": 0.0005}),
                         ("비용 왕복 0.30%", {"cost": 0.003}), ("롱 쪽만(펀딩 마이너스 종목 롱)", {"leg": "long"}),
                         ("숏 쪽만(펀딩 플러스 종목 숏)", {"leg": "short"}), ("펀딩 빼고(가격만)", {"use_fund": False})):
            s, _, _ = split(H=H, **kw)
            if s:
                print(f"  H{H}일 {name}: {line(s)}")

    print("\n===== 4) 비교: 실험 11 추세 묶음(상위80·위아래10·F=14·7일, 7묶음) 같은 계산법")
    s, rows, _ = split(sig="trend", N=80, K=10, F=14, H=7)
    if s:
        print(f"  {line(s)} · 가격 하루 {s['price'] / 7:+.3f}% · 펀딩 하루 {s['fund'] / 7:+.3f}%")
    json.dump({"src": src, "grid": [{"N": k[0], "K": k[1], "L": k[2], "H": k[3], **v} for k, v in grid]}, open("bt_summary.json", "w"), ensure_ascii=False, indent=1)
    print(f"\n소요 {time.time() - t0:.0f}초")


if __name__ == "__main__":
    main()
