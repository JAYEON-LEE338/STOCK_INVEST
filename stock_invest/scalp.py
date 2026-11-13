"""초단타 전략 (총평가금액의 SCALP_RATIO)

후보 스캔(09:05/09:30/10:00): 전일 대비 +5% 이상 & 전일 거래량 대비 급증 종목을 기록.
매수(10:10~10:30): 후보가 모멘텀 조건을 만족하고 추세(현재가 >= 최근 스캔 가격)가 살아있으면 시장가 매수.
감시(1분): -3% 전량 매도, -2% 이하 경험 후 본전 회복 50%, +3/6/9% 각 30% 익절, 30분 넘게 손실이면 알림.
정리(15:15): 잔량 전량 매도. 익절한 종목은 그날 재매수하지 않는다.
"""
import datetime as dt
import logging

from . import config
from . import daytrade
from . import notify
from . import orders
from . import state as st
from .kis_client import KIS

log = logging.getLogger("scalp")


def _today() -> str:
    return dt.date.today().isoformat()


def _now_hm() -> str:
    return dt.datetime.now().strftime("%H:%M")


def _qualifies(r: dict) -> bool:
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


# ---------------------------------------------------------------- 후보 스캔
def scan(kis: KIS) -> None:
    """09:05 / 09:30 / 10:00 - 급등 + 거래량 급증 종목을 후보로 기록하고 최근 스캔 가격을 갱신."""
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
    log.info("초단타 스캔(%s): 후보 %s종목 (누적 %s)", _now_hm(), n, len(w["items"]))


# ---------------------------------------------------------------- 매수
def buy(kis: KIS) -> None:
    """10:10 / 10:20 / 10:30 - 후보 중 모멘텀 조건 충족 & 추세가 살아있는 종목을 매수 (종목당 하루 1회, 하루 최대 SCALP_MAX_BUYS종목)."""
    now = dt.datetime.now().time()
    if not (dt.time(10, 10) <= now < dt.time(10, 45)):
        return
    s = st.load()
    w = _watch(s)
    done = _done_codes(s)
    idx = kis.index_price("0001")["change_pct"]
    tr = daytrade.index_trend(kis)
    sig = {"date": _today(), "time": _now_hm(), "index_pct": idx, "trend": tr, "items": [], "result": ""}

    def finish(msg):
        sig["result"] = msg
        log.info("초단타 판단: %s", msg)
        s["signals"]["scalp"] = sig
        st.save(s)

    if config.SCALP_REQUIRE_MOMENTUM:
        if idx < config.MOM_INDEX_MIN:
            return finish(f"코스피 {idx:+.2f}% (기준 +{config.MOM_INDEX_MIN}% 미달) - 쉼")
        if tr["trend"] == "down":
            return finish(f"지수 하락 추세 ({tr['pct']}%) - 매수 대상 제외")
    if not w["items"]:
        return finish("스캔에서 포착된 후보 없음")

    data = daytrade._sector_rows(kis)
    if not data:
        return finish("대상 종목 없음 (시총 조건 미충족 또는 시세 조회 실패)")
    lead = daytrade._lead_sector(data)
    sig["sector"] = lead
    rows = {r["code"]: (sec, r) for sec, rs in data.items() for r in rs}

    cands = []
    for code, it in w["items"].items():
        pos = s["scalp_positions"].get(code)
        if code in done or (pos and pos.get("date") == _today()):
            continue  # 익절했거나 오늘 이미 매수한 종목
        if code not in rows:
            continue
        sec, r = rows[code]
        if config.SCALP_REQUIRE_MOMENTUM and sec != lead:
            continue
        if not _qualifies(r) or r["price"] < it.get("ref_price", 0):
            continue  # 추세가 죽었음
        cands.append(r)
    cands.sort(key=lambda r: r["vol_rate"], reverse=True)
    bought_today = sum(1 for p in s["scalp_positions"].values() if p.get("date") == _today())
    remain = config.SCALP_MAX_BUYS - bought_today
    if remain <= 0:
        return finish(f"오늘 매수 한도 {config.SCALP_MAX_BUYS}종목 소진")
    picks = cands[: min(config.PICK_COUNT, remain)]
    if not picks:
        return finish("조건을 만족하는 종목 없음 (추세 약화 또는 주도 섹터 아님)")

    bal = kis.balance()
    held_val = sum(p["qty"] * bal["holdings"].get(c, {}).get("price", 0)
                   for c, p in s["scalp_positions"].items() if p["qty"] > 0) + orders.pending_amount(s, "scalp")
    budget = min(bal["total"] * config.SCALP_RATIO - held_val, bal["cash"])
    if budget <= 0:
        return finish("초단타 예산 여유 없음")
    per = budget / len(picks)
    n = 0
    for r in picks:
        qty = int(per // r["price"])
        res = kis.order("buy", r["code"], qty) if qty > 0 else False
        if res:
            orders.on_buy(s, "scalp", res)
            sig["items"].append({"code": r["code"], "name": r["name"], "price": r["price"],
                                 "change_pct": r["change_pct"], "qty": qty, "vol_rate": r["vol_rate"]})
            n += 1
    finish(f"{lead} 주도 - {n}종목 매수")


# ---------------------------------------------------------------- 감시
def monitor(kis: KIS) -> None:
    """10:00~15:14 1분 간격 - 손절/본전/익절 및 손실 지속 알림."""
    now = dt.datetime.now()
    if not (dt.time(10, 0) <= now.time() < dt.time(15, 15)):
        return
    s = st.load()
    todo = {c: p for c, p in s["scalp_positions"].items() if p.get("date") == _today() and p["qty"] > 0}
    if not todo:
        return
    done = _done_codes(s)
    changed = False
    held = None if config.DRY_RUN else kis.balance()["holdings"]  # 지정가 매수 체결 확인용
    for code, p in todo.items():
        hq = None
        if held is not None:
            hq = held.get(code, {}).get("qty", 0)
            if hq <= 0:
                continue  # 아직 체결 전
        try:
            px = kis.price(code)["price"]
        except Exception as e:
            log.warning("초단타 감시 시세 조회 실패 %s: %s", code, e)
            continue
        name = config.STOCK_NAMES.get(code, code)
        pnl = (px / p["entry"] - 1 - config.MOM_ROUNDTRIP_COST) * 100
        steps, base = p["steps"], p["base_qty"]

        if pnl <= config.BE_ARM_PCT and not p["was_neg"]:
            p["was_neg"] = True  # -2% 이하 경험 -> 본전 회복 매도 활성
            changed = True
        # 손실 상태로 30분 이상 경과 시 알림 (1회)
        if pnl < 0 and not p["loss_alerted"]:
            held_min = (now - dt.datetime.fromisoformat(p["time"])).total_seconds() / 60
            if held_min >= config.SCALP_LOSS_ALERT_MIN:
                notify.send(f"⏰ 초단타 {name}({code}) 매수 후 {held_min:.0f}분째 손실 중 (수익률 {pnl:+.2f}%)")
                p["loss_alerted"] = True
                changed = True

        sells = []  # (단계, 수량)
        if pnl <= config.SCALP_STOP_PCT:
            sells.append(("stop", p["qty"]))
        else:
            if p["was_neg"] and pnl >= 0 and "be" not in steps:
                sells.append(("be", max(1, round(base * config.SCALP_BREAKEVEN_RATIO))))
            for thr, ratio in config.SCALP_TAKE_PROFIT:
                if pnl >= thr and f"tp{thr}" not in steps:
                    sells.append((f"tp{thr}", max(1, round(base * ratio))))
        for step, want in sells:
            qty = min(p["qty"], want)
            if hq is not None:
                qty = min(qty, hq)
            if qty <= 0:
                break
            if orders.track_sell(s, "scalp", kis.order("sell", code, qty)):
                p["qty"] -= qty
                if hq is not None:
                    hq -= qty
                steps.append(step)
                changed = True
                if step.startswith("tp") and code not in done:
                    done.append(code)  # 익절 종목은 오늘 재매수 금지
                log.info("초단타 %s 매도 %s x%s (수익률 %+.2f%%)", step, code, qty, pnl)
                notify.send(f"⚡ 초단타 {step} 매도 {name}({code}) x{qty} (수익률 {pnl:+.2f}%)")
            else:
                notify.send(f"⚠️ 초단타 {step} 매도 주문 실패 {name}({code})")
                break
    if changed:
        st.save(s)


# ---------------------------------------------------------------- 정리
def close(kis: KIS) -> None:
    """15:15 - 초단타 잔량(전일 이월분 포함) 전량 시장가 정리."""
    s = st.load()
    live = {c: p for c, p in s["scalp_positions"].items() if p["qty"] > 0}
    if not live:
        return
    held = kis.balance()["holdings"]
    for code, p in live.items():
        qty = min(p["qty"], held.get(code, {}).get("qty", 0))
        if qty > 0:
            if orders.track_sell(s, "scalp", kis.order("sell", code, qty)):
                p["qty"] -= qty
                p["steps"].append("close")
                log.info("초단타 장마감 정리 %s x%s", code, qty)
            else:
                notify.send(f"⚠️ 초단타 15:15 정리 매도 실패 {code} x{qty}")
        else:
            p["qty"] = 0
    st.save(s)


# ---------------------------------------------------------------- 대시보드용 TOP 5
def candidates(kis: KIS, n: int = 5) -> dict:
    """유니버스 전체를 초단타 조건으로 평가해 가까운 순 상위 n개."""
    data = daytrade._sector_rows(kis)
    if not data:
        return {"error": "대상 종목 없음 (시총 조건 미충족 또는 시세 조회 실패)"}
    lead = daytrade._lead_sector(data)
    s = st.load()
    w = s.get("scalp_watch") or {}
    items = w.get("items", {}) if w.get("date") == _today() else {}
    d = s.get("scalp_done") or {}
    done = d.get("codes", []) if d.get("date") == _today() else []
    out = []
    for sec, rows in data.items():
        for r in rows:
            c_chg = config.SCALP_MIN_CHANGE <= r["change_pct"] < config.MAX_CHANGE_PCT
            c_vol = r["vol_rate"] >= config.SCALP_VOL_RATE
            c_sec = sec == lead
            ref = items.get(r["code"], {}).get("ref_price")
            c_trend = None if ref is None else r["price"] >= ref
            met = int(c_chg) + int(c_vol) + int(c_sec) + int(bool(c_trend))
            out.append({"code": r["code"], "name": r["name"], "sector": sec, "price": r["price"],
                        "change_pct": r["change_pct"], "vol_rate": r["vol_rate"],
                        "c_chg": c_chg, "c_vol": c_vol, "c_sec": c_sec, "c_trend": c_trend,
                        "watched": r["code"] in items, "done": r["code"] in done, "met": met})
    out.sort(key=lambda x: (x["met"], x["watched"], x["vol_rate"], x["change_pct"]), reverse=True)
    top = out[:n]
    idx = kis.index_price("0001")["change_pct"]
    tr = daytrade.index_trend(kis)
    mkt_ok = idx >= config.MOM_INDEX_MIN and tr["trend"] != "down"
    req = config.SCALP_REQUIRE_MOMENTUM
    for x in top:
        x["pick"] = bool(x["c_chg"] and x["c_vol"] and (x["c_sec"] or not req)
                         and x["c_trend"] and (mkt_ok or not req) and not x["done"])
    return {"lead": lead, "index_pct": idx, "trend": tr, "market_ok": mkt_ok,
            "watch_count": len(items), "done": done, "items": top}
