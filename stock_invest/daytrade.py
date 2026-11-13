"""단타 전략 (계좌의 50%)

1) 박스권: 코스피가 박스 하단에 가까우면 15:20 종가 매수, 상단 70% 근접부터 25%씩 매도.
2) 모멘텀: 10시 기준 코스피 +1% 이상 & 주도 섹터 & 지수 하락 추세 아님이면 매수,
   14~15시 지수가 다시 상승 추세면 재매수, 장중 손절/익절 감시, 잔량은 당일 19:30 NXT 에서 매도.
"""
import datetime as dt
import logging

from . import box as boxmod
from . import config
from . import notify
from . import state as st
from .kis_client import KIS, tick_size

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
# 지수 추세: 매 분 코스피를 기록해 최근 N분 변화율로 판단 (메모리, 재시작 시 초기화)
_IDX = {"date": "", "s": []}


def record_index(kis: KIS) -> None:
    now = dt.datetime.now()
    if _IDX["date"] != _today():
        _IDX["date"], _IDX["s"] = _today(), []
    _IDX["s"].append((now, kis.index_price("0001")["price"]))
    del _IDX["s"][:-400]


def index_trend() -> dict:
    """최근 MOM_TREND_MIN 분 코스피 변화율(%)과 추세. 기록 부족하면 trend='unknown'."""
    s = _IDX["s"] if _IDX["date"] == _today() else []
    if len(s) < 2:
        return {"trend": "unknown", "pct": None}
    now_t, now_v = s[-1]
    past = [x for x in s if x[0] <= now_t - dt.timedelta(minutes=config.MOM_TREND_MIN)]
    if not past:
        return {"trend": "unknown", "pct": None}
    pct = (now_v / past[-1][1] - 1) * 100
    tr = "down" if pct <= -config.MOM_TREND_PCT else "up" if pct >= config.MOM_TREND_PCT else "flat"
    return {"trend": tr, "pct": round(pct, 2)}


def _momentum_entry(kis: KIS, kind: str) -> bool:
    """kind: 'am'(10:01) / 'pm'(14:00~14:50 재매수). 매수가 이뤄졌으면 True."""
    pm = kind == "pm"
    sig_key = "momentum_pm" if pm else "momentum"
    idx = kis.index_price("0001")["change_pct"]
    tr = index_trend()
    sig = {"date": _today(), "time": dt.datetime.now().strftime("%H:%M"), "index_pct": idx,
           "trend": tr, "items": [], "result": ""}
    need = config.MOM_PM_INDEX_MIN if pm else config.MOM_INDEX_MIN

    def done(msg, bought=False):
        sig["result"] = msg
        log.info("모멘텀(%s) 판단: %s", kind, msg)
        _save_signal(sig_key, sig)
        return bought

    if idx < need:
        return done(f"코스피 {idx:+.2f}% (기준 +{need}% 미달) - 쉼")
    if pm and tr["trend"] != "up":
        return done(f"지수 상승 추세 아님 ({tr['trend']}, {tr['pct']}%) - 재매수 안 함")
    if not pm and tr["trend"] == "down":
        return done(f"지수 하락 추세 ({tr['pct']}%) - 매수 대상 제외")

    data = _sector_rows(kis)
    if not data:
        return done("시세 조회 실패")
    lead = _lead_sector(data)
    sig["sector"] = lead

    s = st.load()
    cands = [r for r in data[lead] if 0 < r["change_pct"] < config.MAX_CHANGE_PCT]
    cands.sort(key=lambda r: r["value"], reverse=True)
    picks = cands[: config.PICK_COUNT]
    if not picks:
        return done(f"주도 섹터({lead})에 매수할 상승 종목 없음")

    bal = kis.balance()
    held_val = sum(p["qty"] * bal["holdings"].get(c, {}).get("price", 0)
                   for c, p in s["mom_positions"].items() if p.get("date") == _today())
    budget = min(bal["total"] * config.DAY_RATIO * config.DAY_SPLIT_MOM - held_val, bal["cash"])
    if budget <= 0:
        return done("모멘텀 예산 여유 없음")
    per = budget / len(picks)
    n_bought = 0
    for r in picks:
        qty = int(per // r["price"])
        if qty > 0 and kis.order("buy", r["code"], qty):
            pos = s["mom_positions"].get(r["code"])
            if pos and pos.get("date") == _today():
                tot = pos["qty"] + qty
                pos["entry"] = (pos["entry"] * pos["qty"] + r["price"] * qty) / tot
                pos["base_qty"] += qty
                pos["qty"] = tot
            else:
                s["mom_positions"][r["code"]] = {"qty": qty, "base_qty": qty, "entry": r["price"], "date": _today(),
                                                 "sold_order": False, "steps": [], "was_neg": False}
            sig["items"].append({"code": r["code"], "name": r["name"], "price": r["price"],
                                 "change_pct": r["change_pct"], "qty": qty})
            n_bought += 1
    st.save(s)
    return done(f"{lead} 주도 - {n_bought}종목 매수" + (" (오후 재매수)" if pm else ""), n_bought > 0)


def momentum_buy(kis: KIS) -> None:
    """10:01 - 코스피 +1% & 주도 섹터 & 지수 하락 추세 아님이면 거래대금 상위 매수."""
    _momentum_entry(kis, "am")


def momentum_buy_pm(kis: KIS) -> None:
    """14:00~14:50 (10분 간격) - 지수가 다시 상승 추세면 하루 1회 재매수."""
    now = dt.datetime.now().time()
    if not (dt.time(14, 0) <= now < dt.time(15, 0)):
        return
    if (st.load()["signals"].get("momentum_pm") or {}).get("items") and             st.load()["signals"]["momentum_pm"].get("date") == _today():
        return  # 오늘 오후 매수 완료
    _momentum_entry(kis, "pm")


def momentum_monitor(kis: KIS) -> None:
    """10:02~15:19 - 모멘텀 보유 종목 손절/본전/익절 감시 (1분 간격)."""
    now = dt.datetime.now().time()
    if dt.time(9, 0) <= now < dt.time(15, 20):
        try:
            record_index(kis)
        except Exception as e:
            log.warning("지수 기록 실패: %s", e)
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
