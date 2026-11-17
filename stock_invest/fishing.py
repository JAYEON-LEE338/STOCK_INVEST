"""FISHING — 오전 급락주 반등 (총평가금액의 FISH_RATIO, 기본 0%)

후보(09:05~12:00, 5분 간격): 장중 저가가 전일 종가 대비 -8% 이하(단 -11% 이하는 제외)로 급락한 종목 기록.
매수(09:05~14:30, 1분 간격): 후보가 -5% 지점까지 반등(-5% ≤ 등락률 ≤ -3%)하고 당일 외국인 순매수 > 0 이면 즉시 체결형 매수.
매도(1분): -1% 회복 50%, 0%(전일 종가) 회복 나머지, 장중 저가 이탈 시 전량 손절, 15:15 잔량 정리.
"""
import datetime as dt
import logging

from . import config
from . import daytrade
from . import notify
from . import orders
from . import risk
from . import state as st
from .kis_client import KIS

log = logging.getLogger("fishing")


def _today() -> str:
    return dt.date.today().isoformat()


def _now_hm() -> str:
    return dt.datetime.now().strftime("%H:%M")


def _low_pct(r: dict) -> float | None:
    """장중 저가의 전일 종가(기준가) 대비 %."""
    base = r.get("base") or 0
    if base <= 0 or not r.get("low"):
        return None
    return (r["low"] / base - 1) * 100


def _watch(s: dict) -> dict:
    w = s.get("fish_watch") or {}
    if w.get("date") != _today():
        w = {"date": _today(), "items": {}}
    s["fish_watch"] = w
    return w


def _record(w: dict, r: dict, sector: str = "") -> bool:
    """오전(FISH_MORNING_END 이전)에 저가 ≤ FISH_DROP_PCT 면 후보로 기록/갱신."""
    lp = _low_pct(r)
    if lp is None or lp > config.FISH_DROP_PCT:
        return False
    if lp <= config.FISH_EXCLUDE_PCT:  # 낙폭이 너무 큰 종목 제외 (이미 후보였어도 뺀다)
        if w["items"].pop(r["code"], None):
            log.info("FISHING 후보 제외 %s: 장중 저가 %.1f%% (≤ %s%%)", r["code"], lp, config.FISH_EXCLUDE_PCT)
        w.setdefault("excluded", {})[r["code"]] = round(lp, 2)
        return False
    it = w["items"].setdefault(r["code"], {"name": r["name"], "sector": sector, "first_time": _now_hm()})
    it.update(low=min(it.get("low", r["low"]), r["low"]), low_pct=round(min(it.get("low_pct", lp), lp), 2),
              base=r["base"], seen=_now_hm())
    return True


# ---------------------------------------------------------------- 후보 스캔
def scan_fishing(kis: KIS) -> None:
    """09:05~12:00 (5분 간격) - 오전 급락 종목(장중 저가 ≤ -8%)을 후보로 기록."""
    now = dt.datetime.now().time()
    if not (dt.time(9, 5) <= now <= config.FISH_MORNING_END):
        return
    if not st.is_on("fish"):
        return
    data = daytrade._sector_rows(kis)
    s = st.load()
    w = _watch(s)
    n = sum(_record(w, r, sec) for sec, rows in data.items() for r in rows)
    st.save(s)
    if n:
        log.info("FISHING 스캔(%s): 급락 후보 %s종목 (누적 %s)", _now_hm(), n, len(w["items"]))


# ---------------------------------------------------------------- 매수
def buy_fishing(kis: KIS) -> None:
    """09:05~14:30 (1분 간격) - 후보가 -5% 지점까지 반등하고 당일 외국인 순매수가 양수면 매수."""
    now = dt.datetime.now().time()
    if not (dt.time(9, 5) <= now <= dt.time(14, 30)):
        return
    if not st.is_on("fish"):
        return
    s = st.load()
    w = _watch(s)
    if not w["items"]:
        return
    ratio = st.alloc()["fish"]
    if ratio <= 0:
        return
    opened = {c for c, p in s["fish_positions"].items() if p.get("date") == _today()}
    remain = config.FISH_MAX_BUYS - len(opened)
    if remain <= 0:
        return
    cands = []
    for code, it in w["items"].items():
        if code in opened:
            continue
        try:
            r = kis.price(code)
        except Exception as e:
            log.warning("FISHING 시세 조회 실패 %s: %s", code, e)
            continue
        if now <= config.FISH_MORNING_END:
            _record(w, r)  # 오전 중이면 저가 갱신
        if code not in w["items"]:
            continue  # 방금 -11% 이하로 제외됨
        it = w["items"][code]
        it.update(last_chg=r["change_pct"], frgn=r.get("frgn_net", 0))
        if config.FISH_BUY_AT <= r["change_pct"] <= config.FISH_BUY_MAX and r.get("frgn_net", 0) > 0:
            cands.append({**r, "day_low": min(it["low"], r["low"] or it["low"]), "low_pct": it["low_pct"]})
    if not cands:
        st.save(s)
        return
    cands.sort(key=lambda r: r.get("frgn_net", 0), reverse=True)  # 외국인 순매수 많은 순
    picks = cands[:remain]

    bal = kis.balance()
    held_val = sum(p["qty"] * bal["holdings"].get(c, {}).get("price", 0)
                   for c, p in s["fish_positions"].items() if p["qty"] > 0) + orders.pending_amount(s, "fish")
    budget = min(bal["total"] * ratio - held_val, bal["cash"])
    sig = {"date": _today(), "time": _now_hm(), "items": [], "result": ""}
    if budget <= 0:
        sig["result"] = "FISHING 예산 여유 없음"
    else:
        per = budget / len(picks)
        for r in picks:
            rp = max(config.FISH_R_MIN, (r["price"] / r["day_low"] - 1) * 100 + 0.5)  # 저가 이탈까지 거리(+여유)
            qty = risk.size_qty(bal["total"], r["price"], rp, per)
            res = kis.order("buy", r["code"], qty, mode="chase") if qty > 0 else False
            if res:
                orders.on_buy(s, "fish", res)
                s["fish_positions"][r["code"]].update(day_low=r["day_low"], base=r["base"], r_pct=round(rp, 3))
                sig["items"].append({"code": r["code"], "name": r["name"], "price": r["price"], "change_pct": r["change_pct"],
                                     "qty": qty, "low_pct": r["low_pct"], "frgn": r.get("frgn_net", 0)})
        sig["result"] = f"급락 후 반등 - {len(sig['items'])}종목 매수"
    log.info("FISHING 판단: %s", sig["result"])
    s["signals"]["fish"] = sig
    st.save(s)


# ---------------------------------------------------------------- 감시
def monitor(kis: KIS) -> None:
    """09:05~15:14 1분 간격 - -1% 회복 50%, 0% 회복 나머지, 장중 저가 이탈 전량 손절."""
    now = dt.datetime.now().time()
    if not (dt.time(9, 5) <= now < dt.time(15, 15)):
        return
    s = st.load()
    todo = {c: p for c, p in s["fish_positions"].items() if p.get("date") == _today() and p["qty"] > 0 and p.get("entry")}
    if not todo:
        return
    held = None if config.DRY_RUN else kis.balance()["holdings"]
    changed = False
    for code, p in todo.items():
        hq = None
        if held is not None:
            hq = held.get(code, {}).get("qty", 0)
            if hq <= 0:
                continue  # 아직 체결 전
        try:
            r = kis.price(code)
        except Exception as e:
            log.warning("FISHING 감시 시세 조회 실패 %s: %s", code, e)
            continue
        px, chg, steps, base = r["price"], r["change_pct"], p["steps"], p["base_qty"]
        name = config.STOCK_NAMES.get(code, code)
        sells = []
        if p.get("day_low") and px < p["day_low"]:
            sells.append(("stop", p["qty"]))  # 장중 저가 이탈
        else:
            if chg >= config.FISH_TP1_AT and "tp1" not in steps:
                sells.append(("tp1", max(1, round(base * config.FISH_TP1_RATIO))))
            if chg >= config.FISH_TP2_AT and "tp2" not in steps:
                sells.append(("tp2", p["qty"]))
        for step, want in sells:
            qty = min(p["qty"], want)
            if hq is not None:
                qty = min(qty, hq)
            if qty <= 0:
                break
            if orders.track_sell(s, "fish", kis.order("sell", code, qty), p["entry"], step):
                p["qty"] -= qty
                if hq is not None:
                    hq -= qty
                steps.append(step)
                changed = True
                pnl = (px / p["entry"] - 1 - config.MOM_ROUNDTRIP_COST) * 100
                log.info("FISHING %s 매도 %s x%s (등락률 %+.2f%%, 수익률 %+.2f%%)", step, code, qty, chg, pnl)
                notify.send(f"🎣 FISHING {step} 매도 {name}({code}) x{qty} (등락률 {chg:+.2f}%, 수익률 {pnl:+.2f}%)")
            else:
                notify.send(f"⚠️ FISHING {step} 매도 주문 실패 {name}({code})")
                break
    if changed:
        st.save(s)


def close_fishing(kis: KIS) -> None:
    """15:15 - FISHING 잔량 전량 시장가 정리 (당일 청산)."""
    s = st.load()
    live = {c: p for c, p in s["fish_positions"].items() if p["qty"] > 0}
    if not live:
        return
    held = kis.balance()["holdings"]
    for code, p in live.items():
        qty = p["qty"] if config.DRY_RUN else min(p["qty"], held.get(code, {}).get("qty", 0))
        if qty > 0 and orders.track_sell(s, "fish", kis.order("sell", code, qty), p.get("entry"), "close"):
            p["qty"] -= qty
            p["steps"].append("close")
            log.info("FISHING 장마감 정리 %s x%s", code, qty)
        elif qty > 0:
            notify.send(f"⚠️ FISHING 15:15 정리 매도 실패 {code} x{qty}")
        else:
            p["qty"] = 0
    st.save(s)


# ---------------------------------------------------------------- 대시보드용 TOP 5
def candidates(kis: KIS, n: int = 5) -> dict:
    """유니버스에서 장중 저가 하락률이 큰 순으로 FISHING 조건 충족 여부를 보여준다 (상태는 바꾸지 않음)."""
    data = daytrade._sector_rows(kis)
    if not data:
        return {"error": "대상 종목 없음 (시총 조건 미충족 또는 시세 조회 실패)"}
    s = st.load()
    w = s.get("fish_watch") or {}
    items = w.get("items", {}) if w.get("date") == _today() else {}
    opened = {c for c, p in s.get("fish_positions", {}).items() if p.get("date") == _today()}
    w_excl = set(w.get("excluded", {})) if w.get("date") == _today() else set()
    out = []
    for sec, rows in data.items():
        for r in rows:
            lp = _low_pct(r)
            it = items.get(r["code"], {})
            low_pct = min(lp if lp is not None else 0, it.get("low_pct", 0))
            excluded = (lp is not None and lp <= config.FISH_EXCLUDE_PCT) or r["code"] in w_excl
            c_drop = not excluded and (bool(it) or (lp is not None and lp <= config.FISH_DROP_PCT
                                                    and dt.datetime.now().time() <= config.FISH_MORNING_END))
            c_rebound = config.FISH_BUY_AT <= r["change_pct"]
            c_nochase = r["change_pct"] <= config.FISH_BUY_MAX
            c_frgn = r.get("frgn_net", 0) > 0
            out.append({"code": r["code"], "name": r["name"], "sector": sec, "price": r["price"],
                        "change_pct": r["change_pct"], "low_pct": round(low_pct, 2), "frgn": r.get("frgn_net", 0),
                        "excluded": excluded, "c_drop": c_drop, "c_rebound": c_rebound, "c_nochase": c_nochase, "c_frgn": c_frgn,
                        "bought": r["code"] in opened})
    out.sort(key=lambda x: (x["c_drop"], not x["excluded"], -x["low_pct"]), reverse=True)  # 후보 우선 → 제외 종목은 뒤 → 저가 하락이 깊은 순
    top = out[:n]
    for x in top:
        x["pick"] = bool(not x["excluded"] and x["c_drop"] and x["c_rebound"] and x["c_nochase"] and x["c_frgn"] and not x["bought"])
    return {"items": top, "ratio": st.alloc()["fish"], "watch_count": len(items)}
