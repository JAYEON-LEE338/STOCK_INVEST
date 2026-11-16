"""매수·매도 주문 체결 추적.

- 전략 보유 수량은 '체결된' 수량만 센다 (주문 접수만으로 늘리지 않음).
- DRY_RUN 주문은 즉시 전량 체결된 것으로 본다.
- 1분마다 당일 주문체결을 조회해 반영하고, 모멘텀·초단타 매수는 BUY_CANCEL_MIN 분 지나면 잔량 취소.
"""
import datetime as dt
import logging

from . import config
from . import journal
from . import notify
from . import state as st
from .kis_client import KIS

log = logging.getLogger("orders")

POS_KEY = {"box": "box_positions", "mom": "mom_positions", "scalp": "scalp_positions", "break": "break_positions"}
CANCELABLE = ("mom", "scalp")  # 박스권·돌파(종가 단일가), 공포지수는 취소하지 않음
EXPIRE_AT = dt.time(15, 35)  # 이 시각 이후 남은 정규장 주문은 만료로 처리
NXT_EXPIRE_AT = dt.time(20, 1)  # NXT 애프터마켓 종료(20:00) 이후
LABEL = {"box": "박스권", "mom": "모멘텀", "scalp": "초단타", "fear": "공포지수", "break": "돌파매매"}


def _today() -> str:
    return dt.date.today().isoformat()


def _new_position(strategy: str) -> dict:
    now = dt.datetime.now().isoformat(timespec="seconds")
    if strategy == "box":
        return {"qty": 0, "peak_qty": 0, "sold_steps": [], "date": _today()}
    if strategy == "break":
        return {"qty": 0, "base_qty": 0, "entry": None, "date": _today(), "steps": [], "high": 0}
    if strategy == "mom":
        return {"qty": 0, "base_qty": 0, "entry": None, "date": _today(), "sold_order": False,
                "steps": [], "was_neg": False}
    return {"qty": 0, "base_qty": 0, "entry": None, "date": _today(), "time": now,
            "steps": [], "was_neg": False, "loss_alerted": False}


def _position(s: dict, strategy: str, code: str) -> dict | None:
    key = POS_KEY.get(strategy)
    if not key:
        return None
    p = s[key].get(code)
    if strategy in ("box", "break"):  # 여러 날 보유하는 전략
        if p is None:
            p = s[key][code] = _new_position(strategy)
    elif p is None or p.get("date") != _today():
        p = s[key][code] = _new_position(strategy)
    return p


def apply_fill(s: dict, strategy: str, code: str, qty: int, price: float) -> None:
    """체결 qty 주를 price 에 전략 보유로 반영 (거래 기록에도 남김)."""
    if qty > 0:
        journal.record(strategy, "buy", code, qty, price)
    p = _position(s, strategy, code)
    if p is None or qty <= 0:
        return
    old = p["qty"]
    if strategy == "break" and old <= 0:  # 청산 후 재진입: 이전 단계 기록 초기화
        p.update(steps=[], base_qty=0, date=_today(), entry=None, high=0)
    if strategy == "box":
        p["entry"] = price if old <= 0 or not p.get("entry") else (p["entry"] * old + price * qty) / (old + qty)
        p["qty"] = old + qty
        p["peak_qty"] = p["qty"]
        return
    p["entry"] = price if old <= 0 or not p.get("entry") else (p["entry"] * old + price * qty) / (old + qty)
    p["qty"] = old + qty
    p["base_qty"] = p.get("base_qty", 0) + qty


def on_buy(s: dict, strategy: str, res: dict) -> None:
    """kis.order('buy') 성공 직후 호출. DRY 면 즉시 반영, 실주문이면 체결 대기 목록에 등록."""
    code = res["code"]
    p = _position(s, strategy, code)
    if strategy == "box" and p is not None:  # 새로 사면 매도 단계 초기화
        p["sold_steps"], p["date"] = [], _today()
    if res.get("dry"):
        apply_fill(s, strategy, code, res["qty"], res["price"])
        return
    s.setdefault("orders", {})[res["odno"]] = {
        "strategy": strategy, "side": "buy", "code": code, "qty": res["qty"], "price": res["price"],
        "orgno": res.get("orgno", ""), "date": _today(),
        "time": dt.datetime.now().isoformat(timespec="seconds"),
        "applied": 0, "applied_cost": 0.0, "status": "open"}


def track_sell(s: dict, strategy: str, res, entry: float | None = None, note: str = ""):
    """kis.order('sell') 결과를 그대로 돌려주면서 실주문이면 체결 확인 목록에 등록.
    entry(매수가)를 주면 거래 기록에 실현 손익이 남는다. DRY 는 즉시, 실주문은 체결될 때 기록."""
    if res and res.get("dry"):
        journal.record(strategy, "sell", res["code"], res["qty"], res["price"], entry, note)
    if res and not res.get("dry") and res.get("odno"):
        s.setdefault("orders", {})[res["odno"]] = {
            "strategy": strategy, "side": "sell", "code": res["code"], "qty": res["qty"], "price": res["price"],
            "orgno": res.get("orgno", ""), "exchange": res.get("exchange", "KRX"), "date": _today(),
            "time": dt.datetime.now().isoformat(timespec="seconds"),
            "applied": 0, "applied_cost": 0.0, "status": "open", "alerted": False,
            "entry": entry, "note": note}
    return res


def pending_amount(s: dict, strategy: str) -> int:
    """아직 체결되지 않은 매수 주문 금액 (예산 사용액에 포함)."""
    return int(sum((o["qty"] - o["applied"]) * o["price"] for o in s.get("orders", {}).values()
                   if o["strategy"] == strategy and o.get("side", "buy") == "buy"
                   and o["status"] in ("open", "cancel_sent")))


def _has_open(s: dict, strategy: str, code: str) -> bool:
    return any(o["strategy"] == strategy and o["code"] == code and o.get("side", "buy") == "buy"
               and o["status"] in ("open", "cancel_sent")
               for o in s.get("orders", {}).values())


def _finish(s: dict, odno: str, o: dict, status: str) -> None:
    o["status"] = status
    key = POS_KEY.get(o["strategy"])
    if key:
        p = s[key].get(o["code"])
        if p is not None and p["qty"] <= 0 and not _has_open(s, o["strategy"], o["code"]):
            del s[key][o["code"]]  # 하나도 체결 안 됨 -> 보유 기록 삭제
    name = config.STOCK_NAMES.get(o["code"], o["code"])
    if status != "filled":
        log.info("매수 주문 종료(%s) %s %s: 체결 %s/%s", status, LABEL.get(o["strategy"]), o["code"], o["applied"], o["qty"])
        notify.send(f"ℹ️ {LABEL.get(o['strategy'])} 매수 {name}({o['code']}) {status}: "
                    f"체결 {o['applied']}/{o['qty']}주")


def _sync_sell(s: dict, odno: str, o: dict, r: dict | None, now: dt.datetime) -> None:
    """매도 체결 확인: 다 체결되면 종료, 오래 미체결이면 알림, 마감 후 미체결분은 알림 + (모멘텀 NXT) 다음날 정리 수량으로."""
    if r:
        d = r["ccld_qty"] - o["applied"]
        if d > 0:  # 새로 체결된 매도 수량을 거래 기록에 (평균 체결가 기준)
            cost = r["avg"] * r["ccld_qty"]
            px = (cost - o.get("applied_cost", 0)) / d if cost else o["price"]
            journal.record(o["strategy"], "sell", o["code"], d, px, o.get("entry"), o.get("note", ""))
            o["applied_cost"] = cost
        o["applied"] = r["ccld_qty"]
    name = config.STOCK_NAMES.get(o["code"], o["code"])
    label = LABEL.get(o["strategy"], o["strategy"])
    if o["applied"] >= o["qty"]:
        o["status"] = "filled"
        if o["exchange"] != "KRX" and o["strategy"] == "mom":
            p = s["mom_positions"].get(o["code"])
            if p is not None:
                p["qty"] = 0  # NXT 전량 체결 -> 다음날 정리할 것 없음
        return
    nxt = o["exchange"] != "KRX"
    end = NXT_EXPIRE_AT if nxt else EXPIRE_AT
    if o["date"] < _today() or now.time() >= end or (r and (r["cancelled"] or r["rmn_qty"] == 0)):
        left = o["qty"] - o["applied"]
        o["status"] = "expired"
        if nxt and o["strategy"] == "mom":
            p = s["mom_positions"].get(o["code"])
            if p is not None:
                p["qty"] = left  # 미체결분만 다음날 09:05 정리
        notify.send(f"⚠️ {label} 매도 미체결 {name}({o['code']}) {left}/{o['qty']}주 ({o['exchange']})"
                    + ("\n다음 영업일 09:05에 정규장에서 정리합니다." if nxt and o["strategy"] == "mom" else
                       "\n계좌를 확인하세요."))
        return
    age = (now - dt.datetime.fromisoformat(o["time"])).total_seconds() / 60
    if not nxt and not o.get("alerted") and age >= config.SELL_ALERT_MIN:
        o["alerted"] = True
        notify.send(f"⏳ {label} 매도 {name}({o['code']}) {age:.0f}분째 미체결: {o['applied']}/{o['qty']}주 체결")


def sync(kis: KIS) -> None:
    """1분마다: 체결 반영, 미체결 잔량 취소, 지난 주문 정리."""
    if config.DRY_RUN:
        return
    s = st.load()
    orders = s.get("orders", {})
    live = {k: o for k, o in orders.items() if o["status"] in ("open", "cancel_sent")}
    if not live:
        # 오래된 기록 정리 (3일 지난 종료 주문)
        cutoff = (dt.date.today() - dt.timedelta(days=3)).isoformat()
        old = [k for k, o in orders.items() if o["date"] < cutoff]
        for k in old:
            del orders[k]
        if old:
            st.save(s)
        return

    now = dt.datetime.now()
    fills = {}
    for d in sorted({o["date"] for o in live.values()}):
        try:
            fills.update(kis.order_fills(d.replace("-", "")))
        except Exception as e:
            log.warning("주문체결 조회 실패(%s): %s", d, e)
    for odno, o in live.items():
        if o.get("side") == "sell":
            _sync_sell(s, odno, o, fills.get(odno), now)
            continue
        r = fills.get(odno)
        if r:
            d = r["ccld_qty"] - o["applied"]
            if d > 0:
                cost = r["avg"] * r["ccld_qty"]
                price = (cost - o["applied_cost"]) / d if cost else o["price"]
                apply_fill(s, o["strategy"], o["code"], d, price)
                o["applied"], o["applied_cost"] = r["ccld_qty"], cost
                log.info("체결 반영 %s %s +%s주 @%.0f (누적 %s/%s)", LABEL.get(o["strategy"]), o["code"],
                         d, price, o["applied"], o["qty"])
            if o["applied"] >= o["qty"]:
                _finish(s, odno, o, "filled")
                continue
            if r["cancelled"] or (r["rmn_qty"] == 0 and r["ccld_qty"] < o["qty"]):
                _finish(s, odno, o, "cancelled")
                continue
        # 지난 날 주문이거나 장 마감 이후 -> 남은 수량은 소멸
        if o["date"] < _today() or now.time() >= EXPIRE_AT:
            _finish(s, odno, o, "expired")
            continue
        # 모멘텀·초단타는 일정 시간 지나면 잔량 취소
        age = (now - dt.datetime.fromisoformat(o["time"])).total_seconds() / 60
        if o["status"] == "open" and o["strategy"] in CANCELABLE and age >= config.BUY_CANCEL_MIN:
            try:
                kis.cancel(o["orgno"], odno)
                o["status"] = "cancel_sent"
            except Exception as e:
                log.error("미체결 취소 실패 %s: %s", odno, e)
                notify.send(f"⚠️ 미체결 매수 취소 실패 {o['code']} (주문 {odno}): {e}")
    st.save(s)
