# -*- coding: utf-8 -*-
"""bt-lab 실험 14: 추세 묶음을 작은 계좌로 실주문할 때의 모양 (4시간봉 2년, 상장폐지 포함)
배경: 실험 13에서 7묶음·위아래 10개(동시 140종목)는 모의 기준 통과. 그러나 총자산 약 700 USDT 로는 종목당 5 USDT 수준이라
  실주문 모양으로는 묶음 수·종목 수를 줄여야 함.
보는 것
 1) 묶음 수 1·2·3·7 x 위아래 3·5·7·10 (상위 100, 14일 기준) — 묶음 수가 적으면 시작 요일마다 따로(모든 회전) 보고 최저/중앙/최고
 2) 가장 작은 실용 모양(묶음 1~3, 동시 20종목 이하)의 가짜 순위 20번 비교
 3) 해마다
 4) 700 USDT 계좌에서 종목당 금액
---- 아래는 실험 13 설명(틀 재사용) ----
실험 13: 추세 묶음(7묶음 나눠 교체)을 엔진에 넣기 전 마지막 확인 (4시간봉 2년, 상장폐지 포함)
배경: 실험 11에서 상위80·위아래10·F=14일(직전 1일 제외)·7일 보유를 7묶음으로 하루씩 어긋나게 교체하면
  하루 +0.12%(주당 +0.89%, t 1.43)였으나 수익의 약 90%가 펀딩비. 실험 12에서 펀딩비만 노린 묶음은 우위 없음.
보는 것
 1) 가짜 순위(무작위로 K개 롱·K개 숏) 40번과 비교 — 진짜 순위가 무작위보다 얼마나 나은가 (가장 중요한 확인)
 2) 비싼 비용: 왕복 0.14%(기준) · 0.30% · 0.50% (소형 종목 호가 벌어짐·밀림)
 3) 종목 폭 상위 60·80·100·120·150 x 위아래 5·10 (7묶음)
 4) 해마다·2026년만·분기별 (7묶음)
 5) 가격·펀딩·비용 나눠 보기, 롱 쪽·숏 쪽
 6) 최소 주문 금액: 7묶음 x 2K 종목 x 종목당 최소 5 USDT 에 필요한 자금
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

    def run(self, N, K, F, H, start, skip=1, use_fund=True, excl=None, leg="both", cost=COST, seed=None, split=False):
        """start: 첫 교체 봉 번호(0 기준). 반환 [(봉, 포지션당 순수익)]"""
        O, ff = self.O, self.ff
        S, Hb, Fb, kb = 7 * DB, H * DB, F * DB, skip * DB
        out, prev = [], set()
        rng = np.random.default_rng(seed) if seed is not None else None
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
            order = uni[np.argsort(ret)] if rng is None else rng.permutation(uni)
            shorts, longs = order[:K], order[-K:]
            tot, cnt, held = 0.0, 0, set()
            pg = pf = pc = 0.0
            for sign, grp in ((1, longs), (-1, shorts)):
                if (leg == "long" and sign < 0) or (leg == "short" and sign > 0):
                    continue
                for i in grp:
                    ex = O[i, T + Hb]
                    if np.isnan(ex):
                        ex = ff[i, T + Hb - 1]           # 보유 중 거래 중단/상장폐지 → 마지막 가격
                    g = sign * (ex / O[i, T] - 1)
                    c = 0.0 if (i, sign) in prev else cost
                    f = -sign * (self.cf[i, T + Hb] - self.cf[i, T]) if use_fund else 0.0
                    tot += g - c + f
                    pg += g; pf += f; pc += c
                    cnt += 1
                    held.add((i, sign))
            prev = held
            out.append((T, tot / cnt, pg / cnt, pf / cnt, pc / cnt) if split else (T, tot / cnt))
            T += Hb
        return out


def stats(series, H):
    per = [x[1] for x in series]
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
    print(f"비용 기준: 바뀌는 종목 왕복 {COST * 100:.2f}% + 실제 펀딩비 · 보유 중 상장폐지는 마지막 가격 · 시각 UTC · 7일 보유")
    wd0 = datetime.fromtimestamp(b0 * 4 * 3600, timezone.utc)
    base = (((7 - wd0.weekday()) % 7) * DB - wd0.hour // 4) % (7 * DB)
    BASE = dict(N=100, K=10, F=14, H=7) if not SELFTEST else dict(N=40, K=5, F=14, H=7)
    yr = lambda T: datetime.fromtimestamp((b0 + T) * 4 * 3600, timezone.utc)
    WD = ["월", "화", "수", "목", "금", "토", "일"]
    cache = {}

    def daily(d, **kw):
        p = dict(BASE); p.update(kw); p["split"] = True
        key = (d, tuple(sorted(p.items())))
        if key not in cache:
            st = (base + d * DB) % (7 * DB)
            cache[key] = (st, lab.run(start=st, **p))
        return cache[key]

    def tranche(days, **kw):
        wk = {}
        for d in days:
            st, ser = daily(d, **kw)
            for T, r, g, f, c in ser:
                k = (T - st) // (7 * DB)
                wk.setdefault(k, []).append((r, g, f, c))
        need = max(1, (len(days) * 4 + 6) // 7)
        out = []
        for k, v in sorted(wk.items()):
            if len(v) >= need:
                n = len(v)
                out.append((base + k * 7 * DB, sum(x[0] for x in v) / n, sum(x[1] for x in v) / n, sum(x[2] for x in v) / n, sum(x[3] for x in v) / n))
        return out

    def line(name, s, pr=True):
        st = stats(s, 7)
        if not st:
            if pr: print(f"  {name}: 표본 부족")
            return None
        h = len(s) // 2
        a, b = stats(s[:h], 7), stats(s[h:], 7)
        g = sum(x[2] for x in s) / len(s) * 100; f = sum(x[3] for x in s) / len(s) * 100
        st.update(a=a["mean"] if a else 0, b=b["mean"] if b else 0, g=g, f=f)
        if pr:
            print(f"  {name}: 주당 {st['mean']:+.2f}% · t {st['t']:+.2f} · 변동 ±{st['sd']:.2f}% · 낙폭 {st['dd']:+.0f}%p · 플러스 주 {st['win']:.0f}% ({st['n']}주)"
                  f" · 앞/뒤 {st['a']:+.2f}/{st['b']:+.2f}% · 가격 {g:+.2f} · 펀딩 {f:+.2f}", flush=True)
        return st

    SHAPES = {1: [[d] for d in range(7)], 2: [[d, (d + 3) % 7] for d in range(7)], 3: [[d, (d + 2) % 7, (d + 4) % 7] for d in range(7)], 7: [list(range(7))]}
    grid = []
    print("\n===== 1) 묶음 수 x 위아래 (상위100·14일) — 묶음 1~3 은 시작 요일 7가지 모두 따로 계산")
    for nt, rots in SHAPES.items():
        for K in ((3, 5, 7, 10) if not SELFTEST else (3, 5)):
            res = []
            for days in rots:
                st = line("", tranche(days, K=K), pr=False)
                if st:
                    res.append((days, st))
            if not res:
                continue
            ms = sorted(r[1]["mean"] for r in res)
            ts = [r[1]["t"] for r in res]
            dd = min(r[1]["dd"] for r in res)
            sdv = sum(r[1]["sd"] for r in res) / len(res)
            n_pos = nt * 2 * K
            per = 700 / n_pos
            worst = min(res, key=lambda r: r[1]["mean"])
            print(f"  묶음{nt}·위아래{K} (동시 {n_pos}종목, 700 USDT 면 종목당 {per:.0f}): 주당 최저 {ms[0]:+.2f} / 중앙 {ms[len(ms) // 2]:+.2f} / 최고 {ms[-1]:+.2f}%"
                  f" · 플러스 {sum(1 for m in ms if m > 0)}/{len(ms)} · t 평균 {sum(ts) / len(ts):+.2f} · 변동 ±{sdv:.1f}% · 최악 낙폭 {dd:+.0f}%p"
                  + (f" · 가장 나쁜 시작 {'·'.join(WD[d] for d in worst[0])}" if nt < 7 else ""), flush=True)
            grid.append({"nt": nt, "K": K, "min": ms[0], "med": ms[len(ms) // 2], "max": ms[-1], "pos": sum(1 for m in ms if m > 0), "rot": len(ms), "t": sum(ts) / len(ts), "sd": sdv, "dd": dd})

    print("\n===== 1b) 묶음 1 · 위아래 5 시작 요일별")
    for d in range(7):
        line(f"{WD[d]}요일 0시 UTC 교체", tranche([d], K=5))

    print("\n===== 2) 작은 모양의 가짜 순위 비교 (20번, 시작 요일 7가지 평균)")
    for nt, K in ((1, 5), (2, 5), (3, 3), (3, 5)):
        rots = SHAPES[nt]
        realm = [line("", tranche(dd_, K=K), pr=False) for dd_ in rots]
        realm = [x["mean"] for x in realm if x]
        if not realm:
            continue
        rm = sum(realm) / len(realm)
        pl = []
        for sd in range(20 if not SELFTEST else 3):
            vals = [line("", tranche(dd_, K=K, seed=2000 + sd * 7 + i), pr=False) for i, dd_ in enumerate(rots)]
            vals = [x["mean"] for x in vals if x]
            if vals:
                pl.append(sum(vals) / len(vals))
        pl.sort()
        if pl:
            print(f"  묶음{nt}·위아래{K}: 진짜 {rm:+.2f}% · 가짜 최저 {pl[0]:+.2f} / 중앙 {pl[len(pl) // 2]:+.2f} / 최고 {pl[-1]:+.2f}% · 진짜 이상 가짜 {sum(1 for m in pl if m >= rm)}/{len(pl)}", flush=True)

    print("\n===== 3) 해마다 (시작 요일 7가지 평균)")
    for nt, K in ((1, 5), (3, 5), (7, 10)):
        for Y in (2024, 2025, 2026):
            vals = []
            for dd_ in SHAPES[nt]:
                v = [x for x in tranche(dd_, K=K) if yr(x[0]).year == Y]
                if len(v) >= 4:
                    vals.append(sum(x[1] for x in v) / len(v) * 100)
            if vals:
                print(f"  묶음{nt}·위아래{K} {Y}년: 주당 평균 {sum(vals) / len(vals):+.2f}% (시작 요일별 최저 {min(vals):+.2f} / 최고 {max(vals):+.2f})", flush=True)

    print("\n===== 4) 펀딩 빼고 (작은 모양)")
    for nt, K in ((1, 5), (3, 5)):
        vals = [line("", tranche(dd_, K=K, use_fund=False), pr=False) for dd_ in SHAPES[nt]]
        vals = [x["mean"] for x in vals if x]
        if vals:
            print(f"  묶음{nt}·위아래{K} 펀딩 빼고: 주당 중앙 {sorted(vals)[len(vals) // 2]:+.2f}% (최저 {min(vals):+.2f} / 최고 {max(vals):+.2f})")
    json.dump({"src": src, "grid": grid}, open("bt_summary.json", "w"), ensure_ascii=False, indent=1)
    print(f"\n소요 {time.time() - t0:.0f}초")


if __name__ == "__main__":
    main()
