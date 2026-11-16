"""단타 전략 — 급등주 눌림목 (모멘텀과 예산 공용)

후보 스캔(09:05/09:30/10:00): 전일 대비 +5% 이상 & 전일 거래량의 50% 이상인 종목을 기록.
매수(10:05~13:55, 5분 간격): 후보가 장중 평균가(VWAP) 근처까지 눌렸다가(현재가 ≤ VWAP×1.01) 다시 오르고
  (현재가 ≥ VWAP×1.003, 직전 확인가보다 높음) 등락률이 +3% 이상 유지되면 매수 2호가 지정가 매수.
  지수 하락 추세가 아니어야 한다. 종목당 하루 1회, 하루 최대 SCALP_MAX_BUYS 종목.
감시(1분): R = ATR×1.0. -1R 전량, -2% 경험 후 본전 회복 50%, +2R 50%, 이후 최고가-1R 추적 손절, 30분 손실 알림.
정리(15:15): 잔량 전량 매도. 익절한 종목은 그날 재매수하지 않는다.
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

log = logging.getLogger("scalp")


def _today() -> str:
    return dt.date.today().isoformat()


def _now_hm() -> str:
    return dt.datetime.now().strftime("%H:%M")


def _qualifies(r: dict) -> bool:
    """후보 조건: 급등(+5% 이상, 과열 제외) & 거래량 급증."""
    return config.SCALP_MIN_CHANGE <= r["change_pct"] < config.MAX_CHANGE_PCT and r["vol_rate"] >= config.SCALP_VOL_RATE


def _watch(s: dict) -> dict:
    w = s.get("scalp_watch") or {}
    if w.get("date") != _today():
        w = {"date": _today(), "items": {}}
    s["scalp_watch"] = w
    return w


def _done_codes(s: dict) -> list:
    d = s.get("scalp_done") or {}
    if d.get("date") != _today():
        d = {"date": _today(), "codes": []}
    s["scalp_done"] = d
    return d["codes"]


def _pullback(it: dict, r: dict) -> dict:
    """눌림목 상태 갱신. 반환: {touched, rebound, vwap}. it 에 touched/last_px 를 기록한다."""
    vwap = r.get("vwap") or 0
    if vwap <= 0:
        return {"touched": False, "rebound": False, "vwap": 0}
    if r["price"] <= vwap * (1 + config.SCALP_VWAP_BAND):
        it["touched"] = True
    last = it.get("last_px")
    rebound = bool(it.get("touched") and r["price"] >= vwap * 1.003 and last is not None and r["price"] > last)
    it["last_px"] = r["price"]
    return {"touched": bool(it.get("touched")), "rebound": rebound, "vwap": vwap}


# ---------------------------------------------------------------- 후보 스캔
def scan(kis: KIS) -> None:
    """09:05 / 09:30 / 10:00 - 급등 + 거래량 급증 종목을 후보로 기록."""
    if not st.is_on("scalp"):
        return
    data = daytrade._sector_rows(kis)
    s = st.load()
    w = _watch(s)
    n = 0
    for sector, rows in data.items():
        for r in rows:
            if not _qualifies(r):
                continue
            it = w["items"].setdefault(r["code"], {"name": r["name"], "sector": sector,
                                                  "first_time": _now_hm(), "first_price": r["price"]})
            it.update(ref_price=r["price"], ref_time=_now_hm(), change_pct=r["change_pct"], vol_rate=r["vol_rate"])
            n += 1
    st.save(s)
    log.info("단타 스캔(%s): 후보 %s종목 (누적 %s)", _now_hm(), n, len(w["items"]))


# ---------------------------------------------------------------- 매수 (눌림목)
def buy(kis: KIS) -> None:
    """10:05~13:55 (5분 간격) - 후보가 VWAP 근처까지 눌렸다가 반등하면 매수."""
    now = dt.datetime.now().time()
    if not (dt.time(10, 0) <= now < dt.time(14, 0)):
        return
    if not st.is_on("scalp"):
        log.info("단타 전략 OFF - 매수 판단 건너뜀")
        return
    s = st.load()
    w = _watch(s)
    done = _done_codes(s)
    tr = daytrade.index_trend(kis)
    sig = {"date": _today(), "time": _now_hm(), "trend": tr, "items": [], "result": ""}

    def finish(msg):
        sig["result"] = msg
        log.info("단타 판단: %s", msg)
        s["signals"]["scalp"] = sig
        st.save(s)

    if not w["items"]:
        return finish("스캔에서 포착된 후보 없음")
    if tr["trend"] == "down":
        return finish(f"지수 하락 추세 ({tr['pct']}%) - 매수 보류")
    bought_today = sum(1 for p in s["scalp_positions"].values() if p.get("date") == _today())
    remain = config.SCALP_MAX_BUYS - bought_today
    if remain <= 0:
        return finish(f"오늘 매수 한도 {config.SCALP_MAX_BUYS}종목 소진")

    cands = []
    for code, it in w["items"].items():
        pos = s["scalp_positions"].get(code)
        if code in done or (pos and pos.get("date") == _today()):
            continue  # 익절했거나 오늘 이미 매수한 종목
        try:
            r = kis.price(code)
        except Exception as e:
            log.warning("단타 시세 조회 실패 %s: %s", code, e)
            continue
        pb = _pullback(it, r)
        if pb["rebound"] and config.SCALP_HOLD_CHANGE <= r["change_pct"] < config.MAX_CHANGE_PCT:
            cands.append({**r, "vwap": pb["vwap"]})
    cands.sort(key=lambda r: r["vol_rate"], reverse=True)
    picks = cands[:min(config.PICK_COUNT, remain)]
    if not picks:
        return finish("눌림 후 반등한 후보 없음 (대기 중)")

    bal = kis.balance()
    held_val = sum(p["qty"] * bal["holdings"].get(c, {}).get("price", 0)
                   for c, p in s["scalp_positions"].items() if p["qty"] > 0) + orders.pending_amount(s, "scalp") \
        + sum(p["qty"] * bal["holdings"].get(c, {}).get("price", 0)
              for c, p in s["mom_positions"].items() if p["qty"] > 0 and p.get("date") == _today()) + orders.pending_amount(s, "mom")
    budget = min(bal["total"] * st.alloc()["momscalp"] - held_val, bal["cash"])
    if budget <= 0:
        return finish("모멘텀·단타 공용 예산 여유 없음")
    per = budget / len(picks)
    n = 0
    for r in picks:
        rp = risk.r_pct(kis, r["code"], config.SCALP_ATR_MULT, config.SCALP_R_MIN, config.SCALP_R_MAX)
        qty = risk.size_qty(bal["total"], r["price"], rp, per)
        res = kis.order("buy", r["code"], qty, mode="bid2") if qty > 0 else False
        if res:
            orders.on_buy(s, "scalp", res)
            s["scalp_positions"][r["code"]].update(r_pct=round(rp, 3))
            sig["items"].append({"code": r["code"], "name": r["name"], "price": r["price"], "change_pct": r["change_pct"],
                                 "qty": qty, "vol_rate": r["vol_rate"], "r_pct": round(rp, 2)})
            n += 1
    finish(f"눌림목 반등 - {n}종목 매수")


# ---------------------------------------------------------------- 감시
def monitor(kis: KIS) -> None:
    """10:00~15:14 1분 간격 - R 기준 손절/본전/익절/추적 손절, 손실 지속 알림."""
    now = dt.datetime.now()
    if not (dt.time(10, 0) <= now.time() < dt.time(15, 15)):
        return
    s = st.load()
    todo = {c: p for c, p in s["scalp_positions"].items() if p.get("date") == _today() and p["qty"] > 0 and p.get("entry")}
    if not todo:
        return
    done = _done_codes(s)
    changed = False
    held = None if config.DRY_RUN else kis.balance()["holdings"]
    for code, p in todo.items():
        hq = None
        if held is not None:
            hq = held.get(code, {}).get("qty", 0)
            if hq <= 0:
                continue  # 아직 체결 전
        try:
            px = kis.price(code)["price"]
        except Exception as e:
            log.warning("단타 감시 시세 조회 실패 %s: %s", code, e)
            continue
        name = config.STOCK_NAMES.get(code, code)
        entry, rp = p["entry"], p.get("r_pct") or config.SCALP_R_MAX
        pnl = (px / entry - 1 - config.MOM_ROUNDTRIP_COST) * 100
        steps, base = p["steps"], p["base_qty"]
        if px > p.get("high", 0):
            p["high"] = px
            changed = True
        if pnl <= config.BE_ARM_PCT and not p["was_neg"]:
            p["was_neg"] = True  # -2% 이하 경험 -> 본전 회복 매도 활성
            changed = True
        if pnl < 0 and not p["loss_alerted"]:  # 손실 상태로 30분 이상 경과 시 알림 (1회)
            held_min = (now - dt.datetime.fromisoformat(p["time"])).total_seconds() / 60
            if held_min >= config.SCALP_LOSS_ALERT_MIN:
                notify.send(f"⏰ 단타 {name}({code}) 매수 후 {held_min:.0f}분째 손실 중 (수익률 {pnl:+.2f}%)")
                p["loss_alerted"] = True
                changed = True

        sells = []  # (단계, 수량)
        if pnl <= -rp:
            sells.append(("stop", p["qty"]))
        else:
            if p["was_neg"] and pnl >= 0 and "be" not in steps:
                sells.append(("be", max(1, round(base * config.SCALP_BREAKEVEN_RATIO))))
            if pnl >= config.TP_R * rp and "tp" not in steps:
                sells.append(("tp", max(1, round(base * config.TP_RATIO))))
            if "tp" in steps and p.get("high") and px <= p["high"] * (1 - config.SCALP_TRAIL_R * rp / 100):
                sells.append(("trail", p["qty"]))
        for step, want in sells:
            qty = min(p["qty"], want)
            if hq is not None:
                qty = min(qty, hq)
            if qty <= 0:
                break
            if orders.track_sell(s, "scalp", kis.order("sell", code, qty), entry, step):
                p["qty"] -= qty
                if hq is not None:
                    hq -= qty
                steps.append(step)
                changed = True
                if step == "tp" and code not in done:
                    done.append(code)  # 익절 종목은 오늘 재매수 금지
                log.info("단타 %s 매도 %s x%s (수익률 %+.2f%%, R %.2f%%)", step, code, qty, pnl, rp)
                notify.send(f"⚡ 단타 {step} 매도 {name}({code}) x{qty} (수익률 {pnl:+.2f}%)")
            else:
                notify.send(f"⚠️ 단타 {step} 매도 주문 실패 {name}({code})")
                break
    if changed:
        st.save(s)


# ---------------------------------------------------------------- 정리
def close(kis: KIS) -> None:
    """15:15 - 단타 잔량(전일 이월분 포함) 전량 시장가 정리."""
    s = st.load()
    live = {c: p for c, p in s["scalp_positions"].items() if p["qty"] > 0}
    if not live:
        return
    held = kis.balance()["holdings"]
    for code, p in live.items():
        qty = p["qty"] if config.DRY_RUN else min(p["qty"], held.get(code, {}).get("qty", 0))
        if qty > 0:
            if orders.track_sell(s, "scalp", kis.order("sell", code, qty), p.get("entry"), "close"):
                p["qty"] -= qty
                p["steps"].append("close")
                log.info("단타 장마감 정리 %s x%s", code, qty)
            else:
                notify.send(f"⚠️ 단타 15:15 정리 매도 실패 {code} x{qty}")
        else:
            p["qty"] = 0
    st.save(s)


# ---------------------------------------------------------------- 대시보드용 TOP 5
def candidates(kis: KIS, n: int = 5) -> dict:
    """유니버스를 단타 조건(급등·거래량·눌림·급등 유지)으로 평가해 가까운 순 상위 n개. 상태는 바꾸지 않는다."""
    data = daytrade._sector_rows(kis)
    if not data:
        return {"error": "대상 종목 없음 (시총 조건 미충족 또는 시세 조회 실패)"}
    s = st.load()
    w = s.get("scalp_watch") or {}
    items = w.get("items", {}) if w.get("date") == _today() else {}
    d = s.get("scalp_done") or {}
    done = d.get("codes", []) if d.get("date") == _today() else []
    out = []
    for sec, rows in data.items():
        for r in rows:
            it = items.get(r["code"], {})
            vwap = r.get("vwap") or 0
            c_chg = (config.SCALP_MIN_CHANGE <= r["change_pct"] < config.MAX_CHANGE_PCT) or bool(it)
            c_vol = r["vol_rate"] >= config.SCALP_VOL_RATE
            # 눌림: VWAP 근처(-2% ~ +1%)에 와 있거나 오늘 눌림 구간을 거친 후보 (VWAP 아래로 크게 무너진 종목은 제외)
            c_pull = bool(it.get("touched")) or (vwap > 0 and vwap * 0.98 <= r["price"] <= vwap * (1 + config.SCALP_VWAP_BAND))
            c_hold = r["change_pct"] >= config.SCALP_HOLD_CHANGE
            met = int(c_chg) + int(c_vol) + int(c_pull) + int(c_hold)
            out.append({"code": r["code"], "name": r["name"], "sector": sec, "price": r["price"],
                        "change_pct": r["change_pct"], "vol_rate": r["vol_rate"], "vwap": vwap,
                        "vs_vwap": round((r["price"] / vwap - 1) * 100, 2) if vwap else None,
                        "c_chg": c_chg, "c_vol": c_vol, "c_pull": c_pull, "c_hold": c_hold,
                        "watched": r["code"] in items, "done": r["code"] in done, "met": met})
    out.sort(key=lambda x: (x["met"], x["watched"], x["vol_rate"], x["change_pct"]), reverse=True)
    top = out[:n]
    tr = daytrade.index_trend(kis)
    for x in top:
        x["pick"] = bool(x["watched"] and x["c_pull"] and x["c_hold"] and not x["done"] and tr["trend"] != "down")
    return {"trend": tr, "market_ok": tr["trend"] != "down", "watch_count": len(items), "done": done, "items": top}
