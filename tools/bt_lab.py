# -*- coding: utf-8 -*-
"""bt-lab 실험 11: '추세 묶음'(최근 F일 많이 오른 K개 롱 + 많이 내린 K개 숏, 금액 맞춤)이 얼마나 튼튼한지 (4시간봉 2년)
배경: 실험 10(일봉 2년·912종목)에서 상위80·위아래10·F=14일(직전 1일 제외)·7일마다 교체가 주당 +1.59%(t 2.28)였으나
  96가지 중 t>2 는 2개뿐 → 우연히 고른 시작 요일·시각·이웃 값에서만 좋은 것인지 확인.
보는 것
 1) 교체 시각: 일주일 안 42가지 시작 칸(요일 7 x 4시간 6)마다 같은 전략을 돌려 성적 분포
 2) 나눠서 교체: 7묶음(하루씩 어긋나게 시작) 평균 — 같은 수익에 변동이 얼마나 줄어드는지
 3) 이웃 값: 상위 N(60·80·100·120) x K(7·10·13) x F(10·14·21일), 각 칸은 7개 시작 요일 평균
 4) 급등락 종목 빼기: 직전 1일 ±20% 넘게 움직인 종목, 최근 F일 +150% 넘은 종목, 변동성 상위 20%
 5) 분기별·해마다 일관성, 펀딩비 뺐을 때
개선점(실험 10 대비): 보유 중 상장폐지되면 마지막 가격으로 정리(전에는 그런 종목을 빼서 약간 좋게 나올 수 있었음)
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
                fr[s][b0 + b] = 0.0001
    return kl, fr


class Lab:
    def __init__(self, O, C, Q, Fu, b0):
        self.O, self.C, self.Fu, self.b0 = O, C, Fu, b0
        self.ns, self.nb = O.shape
        valid = ~np.isnan(C)
        self.cq = np.concatenate([np.zeros((self.ns, 1)), np.cumsum(Q, axis=1)], axis=1)
        self.cv = np.concatenate([np.zeros((self.ns, 1)), np.cumsum(valid, axis=1)], axis=1)
        self.cf = np.concatenate([np.zeros((self.ns, 1)), np.cumsum(Fu, axis=1)], axis=1)
        # 마지막 유효 종가(앞으로 채움): 상장폐지 뒤 정리 가격
        ff = C.copy()
        for j in range(1, self.nb):
            m = np.isnan(ff[:, j])
            ff[m, j] = ff[m, j - 1]
        self.ff = ff
        lr = np.diff(np.log(ff), axis=1)
        lr = np.nan_to_num(lr)
        self.cl2 = np.concatenate([np.zeros((self.ns, 2)), np.cumsum(lr ** 2, axis=1)], axis=1)

    def run(self, N, K, F, H, start, skip=1, use_fund=True, excl=None, leg="both"):
        """start: 첫 교체 봉 번호(0 기준). 반환 [(봉, 포지션당 순수익)]"""
        O, ff = self.O, self.ff
        S, Hb, Fb, kb = 7 * DB, H * DB, F * DB, skip * DB
        out, prev = [], set()
        T = start
        need = S + Fb + kb + 2
        while T < need:
            T += Hb
        while T + Hb < self.nb:
            v7 = self.cv[:, T] - self.cv[:, T - S]
            ok = (v7 == S) & ~np.isnan(O[:, T])
            a = ff[:, T - 1 - kb]
            b = ff[:, T - 1 - kb - Fb]
            ok &= ~np.isnan(a) & ~np.isnan(b) & (self.cv[:, T - 1 - kb - Fb + 1] - self.cv[:, T - 1 - kb - Fb] == 1)
            idx = np.where(ok)[0]
            if len(idx) < max(2 * K + 6, 20):
                T += Hb
                continue
            qv = self.cq[idx, T] - self.cq[idx, T - S]
            uni = idx[np.argsort(-qv)[:N]]
            ret = ff[uni, T - 1 - kb] / ff[uni, T - 1 - kb - Fb] - 1
            if excl == "jump1d":
                r1 = ff[uni, T - 1] / ff[uni, T - 1 - DB] - 1
                keep = np.abs(r1) <= 0.20
                uni, ret = uni[keep], ret[keep]
            elif excl == "bigF":
                keep = np.abs(ret) <= 1.5
                uni, ret = uni[keep], ret[keep]
            elif excl == "vol":
                var = (self.cl2[uni, T] - self.cl2[uni, T - S]) / S
                keep = var <= np.quantile(var, 0.8)
                uni, ret = uni[keep], ret[keep]
            if len(uni) < 2 * K:
                T += Hb
                continue
            order = uni[np.argsort(ret)]
            shorts, longs = order[:K], order[-K:]
            tot, cnt, held = 0.0, 0, set()
            for sign, grp in ((1, longs), (-1, shorts)):
                if (leg == "long" and sign < 0) or (leg == "short" and sign > 0):
                    continue
                for i in grp:
                    ex = O[i, T + Hb]
                    if np.isnan(ex):
                        ex = ff[i, T + Hb - 1]           # 보유 중 거래 중단/상장폐지 → 마지막 가격
                    g = sign * (ex / O[i, T] - 1)
                    c = 0.0 if (i, sign) in prev else COST
                    f = -sign * (self.cf[i, T + Hb] - self.cf[i, T]) if use_fund else 0.0
                    tot += g - c + f
                    cnt += 1
                    held.add((i, sign))
            prev = held
            out.append((T, tot / cnt))
            T += Hb
        return out


def stats(series, H):
    per = [r for _, r in series]
    if len(per) < 15:
        return None
    m = sum(per) / len(per)
    sd = (sum((x - m) ** 2 for x in per) / len(per)) ** 0.5
    acc = peak = dd = 0.0
    for x in per:
        acc += x
        peak = max(peak, acc)
        dd = min(dd, acc - peak)
    return {"n": len(per), "mean": m * 100, "sd": sd * 100, "t": m / sd * len(per) ** 0.5 if sd else 0,
            "sharpe": m / sd * (365 / H) ** 0.5 if sd else 0, "dd": dd * 100, "win": sum(1 for x in per if x > 0) / len(per) * 100}


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
    print(f"비용: 바뀌는 종목 왕복 {COST * 100:.2f}% + 실제 펀딩비 · 보유 중 상장폐지는 마지막 가격으로 정리 · 시각은 UTC")
    # 시작 칸 0 = 월요일 0시(UTC)
    wd0 = datetime.fromtimestamp(b0 * 4 * 3600, timezone.utc)
    base = ((7 - wd0.weekday()) % 7) * DB - wd0.hour // 4
    base %= 7 * DB
    WD = "월화수목금토일"
    BASE = dict(N=80, K=10, F=14, H=7)
    if SELFTEST:
        BASE = dict(N=40, K=5, F=14, H=7)

    print("\n===== 1) 교체 시각 42칸(요일 x 4시간) — 기준 조합(상위80·위아래10·F=14일·직전1일 제외·7일마다)")
    rows = []
    for off in range(7 * DB):
        st = stats(lab.run(start=(base + off) % (7 * DB), **BASE), 7)
        if st:
            rows.append((off, st))
    ms = sorted(r["mean"] for _, r in rows)
    ts = sorted(r["t"] for _, r in rows)
    print(f"  칸 {len(rows)}개 · 주당 순수익 최저 {ms[0]:+.2f}% / 중앙 {ms[len(ms) // 2]:+.2f}% / 최고 {ms[-1]:+.2f}% · t 최저 {ts[0]:+.2f} / 중앙 {ts[len(ts) // 2]:+.2f} / 최고 {ts[-1]:+.2f}"
          f" · 플러스 칸 {sum(1 for m in ms if m > 0)}/{len(ms)} · t>2 칸 {sum(1 for t in ts if t > 2)}")
    for d in range(7):
        if any(off // DB == d for off, _ in rows):
            print(f"  {WD[d]}요일 시작: " + " · ".join(f"{(off % DB) * 4:02d}시 {r['mean']:+.2f}%(t {r['t']:+.1f})" for off, r in rows if off // DB == d))
    hour_avg = {}
    for off, r in rows:
        hour_avg.setdefault((off % DB) * 4, []).append(r["mean"])
    print("  시각별 평균(7요일): " + " · ".join(f"{h:02d}시 {sum(v) / len(v):+.2f}%" for h, v in sorted(hour_avg.items())))

    print("\n===== 2) 나눠서 교체: 7묶음(요일마다 하나씩, 0시 UTC) 평균 vs 한 번에")
    tr = [lab.run(start=(base + d * DB) % (7 * DB), **BASE) for d in range(7)]
    wk = {}
    for d, s in enumerate(tr):
        for T, r in s:
            k = (T - (base + d * DB) % (7 * DB)) // (7 * DB)
            wk.setdefault(k, []).append(r)
    comb = [(k, sum(v) / len(v)) for k, v in sorted(wk.items()) if len(v) >= 4]
    sc = stats(comb, 7)
    singles = [x for x in (stats(s, 7) for s in tr) if x]
    print(f"  한 번에(7요일 각각): 주당 평균 {sum(x['mean'] for x in singles) / len(singles):+.2f}% · 주간 변동 평균 ±{sum(x['sd'] for x in singles) / len(singles):.2f}% · 샤프 평균 {sum(x['sharpe'] for x in singles) / len(singles):+.2f} · 낙폭 평균 {sum(x['dd'] for x in singles) / len(singles):+.0f}%p")
    if sc:
        print(f"  7묶음 나눠서: 주당 {sc['mean']:+.2f}% · 주간 변동 ±{sc['sd']:.2f}% · 샤프 {sc['sharpe']:+.2f} · t {sc['t']:+.2f} · 낙폭 {sc['dd']:+.0f}%p · 플러스 주 {sc['win']:.0f}% ({sc['n']}주)")
        q = {}
        for k, r in comb:
            T = base + k * 7 * DB
            dt = datetime.fromtimestamp((b0 + T) * 4 * 3600, timezone.utc)
            q.setdefault(f"{dt.year}Q{(dt.month - 1) // 3 + 1}", []).append(r)
        print("  분기별(7묶음, 주당 평균): " + " · ".join(f"{k} {sum(v) / len(v) * 100:+.2f}%({len(v)})" for k, v in sorted(q.items())))
        print(f"  플러스 분기 {sum(1 for v in q.values() if sum(v) > 0)}/{len(q)}")

    def avg7(**kw):
        sts = []
        p = dict(BASE)
        p.update(kw)
        for d in range(7):
            s = stats(lab.run(start=(base + d * DB) % (7 * DB), **p), p["H"])
            if s:
                sts.append(s)
        if not sts:
            return None
        return {"mean": sum(x["mean"] for x in sts) / len(sts), "t": sum(x["t"] for x in sts) / len(sts),
                "dd": sum(x["dd"] for x in sts) / len(sts), "tmin": min(x["t"] for x in sts), "pos": sum(1 for x in sts if x["mean"] > 0), "k": len(sts)}

    print("\n===== 3) 이웃 값 (각 칸 = 7개 시작 요일 평균 · 주당 순수익 / t 평균 / 7요일 중 플러스)")
    grid = []
    Ns, Ks, Fs = ((60, 80, 100, 120), (7, 10, 13), (10, 14, 21)) if not SELFTEST else ((30, 40), (5,), (10, 14))
    for N in Ns:
        for K in Ks:
            line = []
            for F in Fs:
                r = avg7(N=N, K=K, F=F)
                if r:
                    grid.append(((N, K, F), r))
                    line.append(f"F{F}일 {r['mean']:+.2f}%/t{r['t']:+.1f}/{r['pos']}of{r['k']}")
            print(f"  상위{N}·위아래{K}: " + " · ".join(line), flush=True)
    if grid:
        mm = sorted(r["mean"] for _, r in grid)
        print(f"  요약: {len(grid)}칸 중 플러스 {sum(1 for m in mm if m > 0)} · 중앙값 {mm[len(mm) // 2]:+.2f}% · t평균>1.5 {sum(1 for _, r in grid if r['t'] > 1.5)}칸 · t평균>2 {sum(1 for _, r in grid if r['t'] > 2)}칸")

    print("\n===== 4) 급등락 종목 빼기 · 펀딩 빼기 · 한쪽만 (기준 조합, 7요일 평균)")
    for name, kw in (("기준", {}), ("직전 1일 ±20% 넘은 종목 제외", {"excl": "jump1d"}), ("최근 F일 ±150% 넘은 종목 제외", {"excl": "bigF"}),
                     ("변동성 상위 20% 제외", {"excl": "vol"}), ("펀딩비 빼고", {"use_fund": False}), ("롱 쪽만", {"leg": "long"}), ("숏 쪽만", {"leg": "short"})):
        r = avg7(**kw)
        if r:
            print(f"  {name}: 주당 {r['mean']:+.2f}% · t 평균 {r['t']:+.2f}(최저 {r['tmin']:+.2f}) · 낙폭 평균 {r['dd']:+.0f}%p · 7요일 중 플러스 {r['pos']}")

    print("\n===== 5) 3일·14일마다 교체 비교 (기준 나머지 동일, 7요일 평균)")
    for H in (3, 14):
        r = avg7(H=H)
        if r:
            print(f"  H={H}일: 회당 {r['mean']:+.2f}%(하루 {r['mean'] / H:+.3f}%) · t 평균 {r['t']:+.2f} · 낙폭 평균 {r['dd']:+.0f}%p · 플러스 {r['pos']}/{r['k']}")
    json.dump({"src": src, "grid": [{"N": k[0], "K": k[1], "F": k[2], **v} for k, v in grid]}, open("bt_summary.json", "w"), ensure_ascii=False, indent=1)
    print(f"\n소요 {time.time() - t0:.0f}초")


if __name__ == "__main__":
    main()
