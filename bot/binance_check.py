# -*- coding: utf-8 -*-
"""
바이낸스 선물(USDⓈ-M) 연결 확인 — 실제 주문은 절대 내지 않음
- 같은 폴더 bot_config.env 의 BINANCE_API_KEY, BINANCE_SECRET_KEY 사용 (코드에 키 없음)
- 잔고·포지션 조회, 레버리지 1배 설정, '테스트 주문'(바이낸스가 형식만 검사하고 체결하지 않음)만 함
실행: python binance_check.py
"""
import hashlib
import hmac
import os
import sys
import time
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    print("필요 패키지가 없습니다. 먼저 실행하세요:\n  python -m pip install requests")
    sys.exit(1)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

FAPI = "https://fapi.binance.com"
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_config.env")
TIME_OFFSET = 0


def load_keys():
    if not os.path.exists(CONFIG_FILE):
        print(f"[오류] 설정 파일이 없습니다: {CONFIG_FILE}")
        sys.exit(1)
    cfg = {}
    with open(CONFIG_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    a, s = cfg.get("BINANCE_API_KEY", ""), cfg.get("BINANCE_SECRET_KEY", "")
    if not a or not s or a.startswith("여기에"):
        print("[오류] bot_config.env 에 BINANCE_API_KEY / BINANCE_SECRET_KEY 를 넣으세요.")
        sys.exit(1)
    return a, s


API_KEY, SECRET = None, None


def sign(params):
    params = dict(params)
    params["timestamp"] = int(time.time() * 1000) + TIME_OFFSET
    params["recvWindow"] = 5000
    q = urlencode(params)
    sig = hmac.new(SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    return q + "&signature=" + sig


def call(method, path, params=None, signed=True):
    headers = {"X-MBX-APIKEY": API_KEY} if API_KEY else {}
    q = sign(params or {}) if signed else urlencode(params or {})
    url = f"{FAPI}{path}?{q}" if q else f"{FAPI}{path}"
    res = requests.request(method, url, headers=headers, timeout=10)
    try:
        data = res.json()
    except ValueError:
        data = {"msg": res.text[:200]}
    if res.status_code != 200:
        explain(res.status_code, data)
        return None
    return data


def explain(status, data):
    code, msg = (data.get("code"), data.get("msg")) if isinstance(data, dict) else (None, str(data))
    print(f"[실패] HTTP {status} 코드 {code} - {msg}")
    hints = {
        -2015: "키가 틀렸거나, 이 PC의 IP가 키에 등록되지 않았거나, '선물 거래 허용'이 꺼져 있습니다.",
        -2014: "API 키 형식이 잘못됐습니다 (복사할 때 공백·줄바꿈 확인).",
        -1022: "서명 오류: Secret Key 를 다시 확인하세요.",
        -1021: "PC 시계가 맞지 않습니다. 윈도우 설정 → 시간 → '지금 동기화'를 누르세요.",
    }
    if code in hints:
        print("  → " + hints[code])
    if status == 451 or (isinstance(msg, str) and "restricted location" in msg.lower()):
        print("  → 이 위치(IP)에서는 바이낸스 접속이 제한됩니다.")


def sync_time():
    global TIME_OFFSET
    d = call("GET", "/fapi/v1/time", signed=False)
    if d:
        TIME_OFFSET = d["serverTime"] - int(time.time() * 1000)
        print(f"바이낸스 서버 연결 정상 · 시계 차이 {TIME_OFFSET} ms")


def my_ip():
    try:
        print("이 PC의 외부 IP:", requests.get("https://api.ipify.org", timeout=5).text)
        print("바이낸스 API 관리에서 이 IP를 '신뢰할 수 있는 IP'로 등록하세요.")
    except Exception as e:
        print("[실패] IP 확인 불가:", e)


def balance():
    d = call("GET", "/fapi/v3/balance") or call("GET", "/fapi/v2/balance")
    if d is None:
        return
    rows = [b for b in d if float(b.get("balance", 0)) != 0 or b.get("asset") == "USDT"]
    print("\n선물 지갑 잔고")
    for b in rows:
        print(f"  {b['asset']:>6}  잔고 {float(b['balance']):,.4f}  주문 가능 {float(b.get('availableBalance', 0)):,.4f}")
    if not rows:
        print("  (비어 있음) 현물 지갑에서 USDⓈ-M 선물 지갑으로 USDT를 옮기세요.")


def positions():
    d = call("GET", "/fapi/v3/positionRisk") or call("GET", "/fapi/v2/positionRisk")
    if d is None:
        return
    open_ = [p for p in d if float(p.get("positionAmt", 0)) != 0]
    print("\n보유 포지션" + ("" if open_ else ": 없음"))
    for p in open_:
        print(f"  {p['symbol']} 수량 {p['positionAmt']} 진입가 {p.get('entryPrice')} 미실현 {p.get('unRealizedProfit')}")
    mode = call("GET", "/fapi/v1/positionSide/dual")
    if mode is not None:
        print("포지션 모드:", "양방향(Hedge)" if mode.get("dualSidePosition") else "단방향(One-way) — 봇 기본값으로 적합")


def set_leverage():
    sym = (input("종목 [기본 BTCUSDT]: ").strip().upper() or "BTCUSDT")
    d = call("POST", "/fapi/v1/leverage", {"symbol": sym, "leverage": 1})
    if d:
        print(f"[성공] {sym} 레버리지 {d.get('leverage')}배로 설정")


def test_order():
    sym = (input("종목 [기본 BTCUSDT]: ").strip().upper() or "BTCUSDT")
    side = (input("방향 BUY(롱)/SELL(숏) [기본 BUY]: ").strip().upper() or "BUY")
    px = call("GET", "/fapi/v1/ticker/price", {"symbol": sym}, signed=False)
    if not px:
        return
    price = float(px["price"])
    qty = round(110 / price, 3) or 0.001          # 약 110 USDT 어치 (최소 주문 금액 여유)
    d = call("POST", "/fapi/v1/order/test", {"symbol": sym, "side": side, "type": "MARKET", "quantity": qty})
    if d is not None:
        print(f"[성공] 테스트 주문 형식 통과 — {sym} {side} 수량 {qty} (약 {qty * price:,.1f} USDT). 실제 체결은 없음")


MENU = """
============ 바이낸스 선물 연결 확인 (실제 주문 없음) ============
 1. 내 외부 IP 확인
 2. 서버 연결·시계 확인
 3. 선물 지갑 잔고
 4. 보유 포지션·포지션 모드
 5. 레버리지 1배로 설정 (종목별)
 6. 테스트 주문 (형식 검사만, 체결 안 됨)
 0. 종료
================================================================
"""


def main():
    global API_KEY, SECRET
    API_KEY, SECRET = load_keys()
    sync_time()
    while True:
        print(MENU)
        c = input("번호 선택: ").strip()
        try:
            {"1": my_ip, "2": sync_time, "3": balance, "4": positions, "5": set_leverage, "6": test_order}.get(c, lambda: None)()
        except requests.RequestException as e:
            print("[네트워크 오류]", e)
        if c == "0":
            break
        input("\n엔터를 누르면 메뉴로 돌아갑니다...")


if __name__ == "__main__":
    main()
