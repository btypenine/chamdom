# -*- coding: utf-8 -*-
"""bt-lab 실험 15: 추세 묶음 낙폭 줄이기 (4시간봉 2년, 상장폐지 포함)
배경: 실험 14에서 작은 계좌용으로 가장 고른 모양은 3묶음·위아래 7(42종목), 모의 설정은 7묶음·위아래 10.
  수익률은 괜찮으나 돈으로 바꾸면 낙폭이 커서(700 USDT 전부 넣으면 최악 약 -400) 실주문과 맞지 않음.
보는 것 (모두 같은 총 금액 — 롱·숏 합 = 1)
 1) 변동성으로 종목별 금액 조절(최근 7일 변동 작은 종목에 더 많이, 1/변동)
 2) BTC 방향(베타) 맞추기: 최근 14일 BTC 대비 민감도로 롱·숏 쪽 금액 비율을 바꿔 순 베타 0 (각 쪽 30~70%)
 3) 1+2 함께
 4) 주간 손실 멈춤: 묶음 하나가 주 중 -5% / -8% / -12% 에 닿으면 그 주는 모두 정리(정리 비용 추가, 다음 주 새로 진입)
 5) 해마다·앞/뒤 절반, 최악의 주, 200 USDT 투입 때 최대 낙폭(USDT)
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
        self.lr = np.concatenate([np.zeros((self.ns, 1)), lr], axis=1)   # lr[:, j] = log(ff[j]/ff[j-1])

    def run(self, N, K, F, H, start, skip=1, use_fund=True, excl=None, leg="both", cost=COST, seed=None, split=False,
            wmode="eq", stop=None, btc=None):
        O, ff = self.O, self.ff
        S, Hb, Fb, kb = 7 * DB, H * DB, F * DB, skip * DB
        W = 14 * DB
        out, prev = [], set()
        rng = np.random.default_rng(seed) if seed is not None else None
        T = start
        need = max(S + Fb + kb + 2, W + 3)
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
            order = uni[np.argsort(ret)] if rng is None else rng.permutation(uni)
            shorts, longs = order[:K], order[-K:]
            # 무게
            def volw(g):
                v = np.sqrt(np.maximum((self.cl2[g, T] - self.cl2[g, T - S]) / S, 1e-8))
                w = 1 / v
                return w / w.sum()
            if wmode in ("vol", "volbeta"):
                wl, ws = volw(longs), volw(shorts)
            else:
                wl, ws = np.full(K, 1 / K), np.full(K, 1 / K)
            gl = gs = 0.5
            if wmode in ("beta", "volbeta") and btc is not None:
                rb = self.lr[btc, T - 1 - W:T - 1]
                vb = rb.var()
                def beta(g):
                    r = self.lr[g, T - 1 - W:T - 1]
                    return ((r - r.mean(1, keepdims=True)) * (rb - rb.mean())).mean(1) / vb if vb > 0 else np.ones(len(g))
                bl, bs = float((wl * beta(longs)).sum()), float((ws * beta(shorts)).sum())
                if bl > 0 and bs > 0:
                    gl = min(0.7, max(0.3, bs / (bl + bs)))
                    gs = 1 - gl
            pos = [(i, 1, gl * w) for i, w in zip(longs, wl)] + [(i, -1, gs * w) for i, w in zip(shorts, ws)]
            if leg == "long":
                pos = [p for p in pos if p[1] > 0]
            elif leg == "short":
                pos = [p for p in pos if p[1] < 0]
            tw = sum(p[2] for p in pos)
            pos = [(i, s, w / tw) for i, s, w in pos]
            # 주 중 손실 멈춤 확인 (4시간봉 종가 기준)
            stop_at = None
            if stop is not None:
                for j in range(T, T + Hb - 1):
                    v = 0.0
                    for i, sg, w in pos:
                        px = ff[i, j]
                        c0 = 0.0 if (i, sg) in prev else cost
                        f = -sg * (self.cf[i, j + 1] - self.cf[i, T]) if use_fund else 0.0
                        v += w * (sg * (px / O[i, T] - 1) - c0 + f)
                    if v <= -stop:
                        stop_at = j
                        break
            pg = pf = pc = 0.0
            held = set()
            for i, sg, w in pos:
                if stop_at is not None:
                    ex = ff[i, stop_at]
                    fe = stop_at + 1
                else:
                    ex = O[i, T + Hb]
                    if np.isnan(ex):
                        ex = ff[i, T + Hb - 1]
                    fe = T + Hb
                g = sg * (ex / O[i, T] - 1)
                c = (0.0 if (i, sg) in prev else cost) + (cost / 2 if stop_at is not None else 0.0)
                f = -sg * (self.cf[i, fe] - self.cf[i, T]) if use_fund else 0.0
                pg += w * g; pf += w * f; pc += w * c
                held.add((i, sg))
            prev = set() if stop_at is not None else held
            tot = pg + pf - pc
            out.append((T, tot, pg, pf, pc, stop_at is not None) if split else (T, tot))
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
    btc = syms.index("BTCUSDT") if "BTCUSDT" in syms else 0
    ds = lambda b: datetime.fromtimestamp((b0 + b) * 4 * 3600, timezone.utc).strftime("%Y-%m-%d %H시")
    print(f"시세 출처: {src} · 4시간봉 받은 종목 {len(syms)}개 · {ds(0)} ~ {ds(lab.nb - 1)} · 받는 데 {time.time() - t0:.0f}초")
    print(f"비용 기준: 바뀌는 종목 왕복 {COST * 100:.2f}% + 실제 펀딩비 · 손실 멈춤 때 정리 비용 편도 추가 · 시각 UTC · 7일 보유 · 상위100·14일(직전 1일 제외)")
    wd0 = datetime.fromtimestamp(b0 * 4 * 3600, timezone.utc)
    base = (((7 - wd0.weekday()) % 7) * DB - wd0.hour // 4) % (7 * DB)
    BASE = dict(N=100, F=14, H=7) if not SELFTEST else dict(N=40, F=14, H=7)
    yr = lambda T: datetime.fromtimestamp((b0 + T) * 4 * 3600, timezone.utc)
    cache = {}

    def daily(d, **kw):
        p = dict(BASE); p.update(kw); p["split"] = True; p["btc"] = btc
        key = (d, tuple(sorted((k, v) for k, v in p.items())))
        if key not in cache:
            st = (base + d * DB) % (7 * DB)
            cache[key] = (st, lab.run(start=st, **p))
        return cache[key]

    def tranche(days, **kw):
        wk = {}
        for d in days:
            st, ser = daily(d, **kw)
            for T, r, g, f, c, s in ser:
                k = (T - st) // (7 * DB)
                wk.setdefault(k, []).append((r, g, f, c, s))
        need = max(1, (len(days) * 4 + 6) // 7)
        out = []
        for k, v in sorted(wk.items()):
            if len(v) >= need:
                n = len(v)
                out.append((base + k * 7 * DB, sum(x[0] for x in v) / n, sum(x[1] for x in v) / n, sum(x[2] for x in v) / n,
                            sum(x[3] for x in v) / n, sum(1 for x in v if x[4]) / n))
        return out

    SHAPES = {3: [[d, (d + 2) % 7, (d + 4) % 7] for d in range(7)], 7: [list(range(7))]}
    VARS = [("같은 금액(기준)", dict(wmode="eq")), ("변동성 조절", dict(wmode="vol")), ("BTC 방향 맞춤", dict(wmode="beta")),
            ("변동성+BTC 방향", dict(wmode="volbeta")), ("주간 멈춤 -5%", dict(wmode="eq", stop=0.05)),
            ("주간 멈춤 -8%", dict(wmode="eq", stop=0.08)), ("주간 멈춤 -12%", dict(wmode="eq", stop=0.12)),
            ("변동성+BTC+멈춤 -8%", dict(wmode="volbeta", stop=0.08))]
    summary = []

    def agg(s):
        st = stats(s, 7)
        if not st:
            return None
        h = len(s) // 2
        st["a"] = stats(s[:h], 7)["mean"]; st["b"] = stats(s[h:], 7)["mean"]
        st["worst"] = min(x[1] for x in s) * 100
        st["g"] = sum(x[2] for x in s) / len(s) * 100; st["f"] = sum(x[3] for x in s) / len(s) * 100
        st["stopped"] = sum(x[5] for x in s) / len(s) * 100
        acc = 0.0; pk = 0.0; worst4 = 0.0
        cum = [0.0]
        for x in s:
            acc += x[1]; cum.append(acc)
        for i in range(4, len(cum)):
            worst4 = min(worst4, cum[i] - cum[i - 4])
        st["w4"] = worst4 * 100
        st["yr"] = {}
        for Y in (2025, 2026):
            v = [x[1] for x in s if yr(x[0]).year == Y]
            if len(v) >= 4:
                st["yr"][Y] = sum(v) / len(v) * 100
        return st

    for nt, K in ((3, 7), (7, 10)) if not SELFTEST else ((3, 3), (7, 5)):
        print(f"\n===== 묶음{nt}·위아래{K} (동시 {nt * 2 * K}종목)" + (" — 시작 요일 7가지 평균(낙폭·최악은 가장 나쁜 칸)" if nt < 7 else ""))
        for name, kw in VARS:
            res = [agg(tranche(days, K=K, **kw)) for days in SHAPES[nt]]
            res = [r for r in res if r]
            if not res:
                continue
            av = lambda k: sum(r[k] for r in res) / len(res)
            mn = lambda k: min(r[k] for r in res)
            y25 = [r["yr"].get(2025) for r in res if 2025 in r["yr"]]; y26 = [r["yr"].get(2026) for r in res if 2026 in r["yr"]]
            row = dict(nt=nt, K=K, name=name, mean=av("mean"), min=mn("mean"), t=av("t"), sd=av("sd"), sharpe=av("sharpe"), dd=mn("dd"),
                       ddavg=av("dd"), worst=mn("worst"), w4=mn("w4"), a=av("a"), b=av("b"), g=av("g"), f=av("f"), stopped=av("stopped"),
                       y25=sum(y25) / len(y25) if y25 else 0, y26=sum(y26) / len(y26) if y26 else 0)
            summary.append(row)
            print(f"  {name}: 주당 {row['mean']:+.2f}% (최저 칸 {row['min']:+.2f}) · t {row['t']:+.2f} · 샤프 {row['sharpe']:.2f} · 변동 ±{row['sd']:.1f}%"
                  f" · 낙폭 평균 {row['ddavg']:+.0f} / 최악 {row['dd']:+.0f}%p (200 USDT 면 {row['dd'] * 2:+.0f}) · 최악의 주 {row['worst']:+.1f}% · 최악 4주 {row['w4']:+.1f}%"
                  f" · 앞/뒤 {row['a']:+.2f}/{row['b']:+.2f} · 2025 {row['y25']:+.2f} · 2026 {row['y26']:+.2f} · 가격 {row['g']:+.2f} 펀딩 {row['f']:+.2f}"
                  + (f" · 멈춘 주 {row['stopped']:.0f}%" if kw.get("stop") else ""), flush=True)

    json.dump({"src": src, "rows": summary}, open("bt_summary.json", "w"), ensure_ascii=False, indent=1)
    print(f"\n소요 {time.time() - t0:.0f}초")


if __name__ == "__main__":
    main()
