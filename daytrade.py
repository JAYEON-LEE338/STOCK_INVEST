"""단타 전략 (계좌의 30%)

1) 박스권: 코스피가 박스 하단에 가까우면 15:20 종가 매수, 상단 70% 근접부터 25%씩 매도.
2) 모멘텀: 10시 기준 코스피 +1% 이상 & 주도 섹터이면 매수, 장중 손절/익절 감시,
   잔량은 당일 19:30 NXT 에서 매도.
"""
import datetime as dt
import logging

import box as boxmod
import config
import notify
import state as st
from kis_client import KIS, tick_size

log = logging.getLogger("daytrade")


def _today() -> str:
    return dt.date.today().isoformat()


def _sector_rows(kis: KIS) -> dict:
    data = {}
    for sector, codes in config.UNIVERSE.items():
        rows = []
        for c in codes:
            try:
                rows.append(kis.price(c))
            except Exception as e:
                log.warning("시세 조회 실패 %s: %s", c, e)
        if rows:
            data[sector] = rows
    return data


def _lead_sector(data: dict) -> str:
    avg = {s: sum(r["change_pct"] for r in rows) / len(rows) for s, rows in data.items()}
    lead = max(avg, key=avg.get)
    log.info("섹터 평균 등락률: %s -> 주도 섹터: %s", {k: round(v, 2) for k, v in avg.items()}, lead)
    return lead


def _save_signal(kind: str, sig: dict) -> None:
    s = st.load()
    s["signals"][kind] = sig
    st.save(s)


# ---------------------------------------------------------------- 박스권
def box_buy(kis: KIS) -> None:
    """15:20 - 코스피가 박스 하단에 가까우면 종가 단일가에 시장가 매수."""
    b = boxmod.get(kis)
    sig = {"date": _today(), "pos": round(b["pos"], 3), "items": [], "result": ""}
    if not b["valid"]:
        sig["result"] = f"박스 폭 {b['width']:.1%} 로 너무 좁음 - 매수 안 함"
    elif not (config.BOX_MIN_POS <= b["pos"] <= config.BOX_BUY_POS):
        sig["result"] = f"박스 내 위치 {b['pos']:.0%} - 매수 구간(≤{config.BOX_BUY_POS:.0%}) 아님"
    else:
        data = _sector_rows(kis)
        if not data:
            sig["result"] = "시세 조회 실패"
        else:
            lead = _lead_sector(data)
            sig["sector"] = lead
            rows = [r for r in data[lead] if r["change_pct"] < config.MAX_CHANGE_PCT]
            rows.sort(key=lambda r: r["value"], reverse=True)
            picks = rows[: config.PICK_COUNT]
            s = st.load()
            bal = kis.balance()
            held_val = sum(min(p["qty"], bal["holdings"].get(c, {}).get("qty", 0)) * bal["holdings"].get(c, {}).get("price", 0)
                           for c, p in s["box_positions"].items())
            budget = min(bal["total"] * config.DAY_RATIO * config.DAY_SPLIT_BOX - held_val, bal["cash"])
            if budget <= 0 or not picks:
                sig["result"] = "예산 여유 없음 또는 후보 없음"
            else:
                per = budget / len(picks)
                bought = []
                for r in picks:
                    qty = int(per // r["price"])
                    if qty > 0 and kis.order("buy", r["code"], qty):
                        p = s["box_positions"].setdefault(r["code"], {"qty": 0, "peak_qty": 0, "sold_steps": []})
                        p["qty"] += qty
                        p["peak_qty"], p["sold_steps"], p["date"] = p["qty"], [], _today()
                        bought.append({"code": r["code"], "name": r["name"], "qty": qty, "price": r["price"]})
                st.save(s)
                sig["items"] = bought
                sig["result"] = f"박스 하단 근접 ({b['pos']:.0%}) - {len(bought)}종목 매수"
    log.info("박스권 판단: %s", sig["result"])
    _save_signal("box", sig)


def box_sell(kis: KIS) -> None:
    """15:00 - 박스 상단 70%/80%/90%/100% 도달 시 단계마다 25%씩 매도."""
    s = st.load()
    if not s["box_positions"]:
        return
    b = boxmod.get(kis)
    if not b["valid"]:
        log.info("박스 폭 부족 - 박스권 매도 판단 보류")
        return
    held = kis.balance()["holdings"]
    ladder = config.BOX_SELL_LADDER
    for code, p in list(s["box_positions"].items()):
        h = held.get(code, {}).get("qty", 0)
        if h <= 0:
            del s["box_positions"][code]
            continue
        new = [t for t in ladder if b["pos"] >= t and t not in p["sold_steps"]]
        if not new:
            continue
        if len(p["sold_steps"]) + len(new) >= len(ladder):
            qty = h  # 마지막 단계는 전량
        else:
            qty = min(h, max(1, round(p["peak_qty"] * config.BOX_SELL_RATIO * len(new))))
        if kis.order("sell", code, qty):
            p["sold_steps"] += new
            log.info("박스권 분할 매도 %s x%s (박스 위치 %.0f%%, 단계 %s)", code, qty, b["pos"] * 100, new)
            if qty >= h:
                del s["box_positions"][code]
    st.save(s)


# ---------------------------------------------------------------- 모멘텀
def momentum_buy(kis: KIS) -> None:
    """10:01 - 코스피 +1% & 주도 섹터 종목 중 거래대금 상위를 매수."""
    idx = kis.index_price("0001")["change_pct"]
    sig = {"date": _today(), "index_pct": idx, "items": [], "result": ""}
    if idx < config.MOM_INDEX_MIN:
        sig["result"] = f"코스피 {idx:+.2f}% (기준 +{config.MOM_INDEX_MIN}% 미달) - 쉼"
        log.info("모멘텀 판단: %s", sig["result"])
        return _save_signal("momentum", sig)

    data = _sector_rows(kis)
    if not data:
        sig["result"] = "시세 조회 실패"
        return _save_signal("momentum", sig)
    lead = _lead_sector(data)
    sig["sector"] = lead

    cands = [r for r in data[lead] if 0 < r["change_pct"] < config.MAX_CHANGE_PCT]
    cands.sort(key=lambda r: r["value"], reverse=True)
    picks = cands[: config.PICK_COUNT]
    if not picks:
        sig["result"] = f"주도 섹터({lead})에 상승 중인 종목 없음"
        log.info("모멘텀 판단: %s", sig["result"])
        return _save_signal("momentum", sig)

    s = st.load()
    bal = kis.balance()
    budget = min(bal["total"] * config.DAY_RATIO * config.DAY_SPLIT_MOM, bal["cash"])
    per = budget / len(picks)
    n_bought = 0
    for r in picks:
        qty = int(per // r["price"])
        if qty > 0 and kis.order("buy", r["code"], qty):
            pos = s["mom_positions"].get(r["code"])
            if pos and pos.get("date") == _today():
                # 같은 날 추가 매수: 평균 매수가 갱신
                tot = pos["qty"] + qty
                pos["entry"] = (pos["entry"] * pos["qty"] + r["price"] * qty) / tot
                pos["base_qty"] += qty
                pos["qty"] = tot
            else:
                pos = {"qty": qty, "base_qty": qty, "entry": r["price"], "date": _today(),
                       "sold_order": False, "steps": [], "was_neg": False}
                s["mom_positions"][r["code"]] = pos
            sig["items"].append({"code": r["code"], "name": r["name"], "price": r["price"],
                                 "change_pct": r["change_pct"], "qty": qty})
            n_bought += 1
    st.save(s)
    sig["result"] = f"{lead} 주도 - {n_bought}종목 매수"
    log.info("모멘텀 판단: %s", sig["result"])
    _save_signal("momentum", sig)


def momentum_monitor(kis: KIS) -> None:
    """10:02~15:19 - 모멘텀 보유 종목 손절/본전/익절 감시 (1분 간격)."""
    now = dt.datetime.now().time()
    if not (dt.time(10, 2) <= now < dt.time(15, 20)):
        return
    s = st.load()
    todo = {c: p for c, p in s["mom_positions"].items()
            if p.get("date") == _today() and p.get("entry") and p["qty"] > 0 and not p.get("sold_order")}
    if not todo:
        return
    changed = False
    for code, p in todo.items():
        try:
            px = kis.price(code)["price"]
        except Exception as e:
            log.warning("모멘텀 감시 시세 조회 실패 %s: %s", code, e)
            continue
        pnl = (px / p["entry"] - 1 - config.MOM_ROUNDTRIP_COST) * 100
        steps = p.setdefault("steps", [])
        base = p.get("base_qty", p["qty"])
        sells = []  # (단계, 비율)
        if pnl <= config.MOM_STOP_PCT and "stop" not in steps:
            sells.append(("stop", config.MOM_STOP_RATIO))
        if "stop" in steps and "stop2" not in steps and p.get("stop_at"):
            waited = (dt.datetime.now() - dt.datetime.fromisoformat(p["stop_at"])).total_seconds()
            if waited >= config.MOM_STOP2_WAIT_MIN * 60:
                if pnl <= config.MOM_STOP_PCT:
                    sells.append(("stop2", config.MOM_STOP2_RATIO))
                else:
                    steps.append("stop2")  # 회복했으므로 2차 손절 판단 종료
                    changed = True
        if pnl < 0 and not p.get("was_neg"):
            p["was_neg"] = True
            changed = True
        if pnl >= 0 and p.get("was_neg") and "breakeven" not in steps:
            sells.append(("breakeven", config.MOM_BREAKEVEN_RATIO))
        for thr, ratio in config.MOM_TAKE_PROFIT:
            if pnl >= thr and f"tp{thr}" not in steps:
                sells.append((f"tp{thr}", ratio))
        for name, ratio in sells:
            qty = min(p["qty"], max(1, round(base * ratio)))
            if qty <= 0:
                break
            if kis.order("sell", code, qty):
                p["qty"] -= qty
                steps.append(name)
                if name == "stop":
                    p["stop_at"] = dt.datetime.now().isoformat(timespec="seconds")
                changed = True
                log.info("모멘텀 %s 매도 %s x%s (수익률 %+.2f%%)", name, code, qty, pnl)
                notify.send(f"📉 모멘텀 {name} 매도 {code} x{qty} (수익률 {pnl:+.2f}%)")
            else:
                log.error("모멘텀 %s 매도 주문 실패 %s", name, code)
                notify.send(f"⚠️ 모멘텀 {name} 매도 주문 실패 {code}")
                break
    if changed:
        st.save(s)


def momentum_sell_evening(kis: KIS) -> None:
    """19:30 - 모멘텀 종목을 NXT 애프터마켓에서 지정가 매도."""
    s = st.load()
    for code, p in s["mom_positions"].items():
        if p.get("sold_order"):
            continue
        try:
            px = kis.price(code, market=config.NXT_PRICE_MARKET)["price"]
            limit = px - tick_size(px) * config.SELL_TICKS_BELOW
            if kis.order("sell", code, p["qty"], price=limit, exchange=config.NXT_ORDER_EXCHANGE):
                p["sold_order"] = True
            else:
                log.error("NXT 매도 주문 실패 %s", code)
                notify.send(f"⚠️ NXT 매도 실패 {code}\n다음 영업일 09:05에 정규장에서 정리합니다.")
        except Exception as e:
            log.error("NXT 매도 실패 %s: %s (다음날 09:05 정리 예정)", code, e)
            notify.send(f"⚠️ NXT 매도 실패 {code}: {e}\n다음 영업일 09:05에 정규장에서 정리합니다.")
    st.save(s)


def sell_fallback(kis: KIS) -> None:
    """09:05 - 전일 모멘텀 종목 중 남은 수량을 정규장에서 시장가 정리."""
    s = st.load()
    stale = {c: p for c, p in s["mom_positions"].items() if p.get("date", "") < _today()}
    if not stale:
        return
    held = kis.balance()["holdings"]
    for code, p in stale.items():
        qty = min(p["qty"], held.get(code, {}).get("qty", 0))
        if qty > 0:
            log.warning("전일 모멘텀 잔여 종목 정리: %s x%s", code, qty)
            if not kis.order("sell", code, qty):
                notify.send(f"⚠️ 09:05 모멘텀 잔여 정리 매도 실패 {code} x{qty}")
        del s["mom_positions"][code]
    st.save(s)
