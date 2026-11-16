"""단타 전략 (계좌의 40%)

1) 박스권: 코스피가 박스 하단에 가까우면 15:20 종가 매수, 상단 70% 근접부터 25%씩 매도.
2) 모멘텀: 10:01 코스피 +1% & 주도 섹터 & 지수 하락 추세 아님 & 장 초반 30분 고가 돌파 → 상대 강도 상위 매수.
   R(ATR) 기준 손절/익절/추적 손절, 15:15 정규장 청산 (NXT 19:30 / 다음날 09:05 는 보조).
"""
import datetime as dt
import logging
import time

from . import box as boxmod
from . import config
from . import notify
from . import orders
from . import risk
from . import state as st
from .kis_client import KIS, tick_size

log = logging.getLogger("daytrade")


def _today() -> str:
    return dt.date.today().isoformat()


def _sector_rows(kis: KIS) -> dict:
    """유니버스 시세. 시가총액 하한을 통과한 종목만 담는다."""
    data = {}
    for sector, codes in config.UNIVERSE.items():
        rows = []
        for c in codes:
            try:
                r = kis.price(c)
            except Exception as e:
                log.warning("시세 조회 실패 %s: %s", c, e)
                continue
            if r["mcap"] < config.MIN_MARKET_CAP:
                continue
            rows.append(r)
        if rows:
            data[sector] = rows
    return data


def sector_scores(data: dict) -> dict:
    """섹터별 등락률 중앙값 (한 종목 급등에 섹터 전체가 끌려가지 않도록 평균 대신 중앙값)."""
    out = {}
    for sec, rows in data.items():
        v = sorted(r["change_pct"] for r in rows)
        n = len(v)
        out[sec] = v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2
    return out


def _lead_sector(data: dict) -> str:
    med = sector_scores(data)
    lead = max(med, key=med.get)
    log.info("섹터 등락률 중앙값: %s -> 주도 섹터: %s", {k: round(v, 2) for k, v in med.items()}, lead)
    return lead


def _save_signal(kind: str, sig: dict) -> None:
    s = st.load()
    s["signals"][kind] = sig
    st.save(s)


# ---------------------------------------------------------------- 박스권
def box_buy(kis: KIS) -> None:
    """15:20 - 코스피가 박스 하단에 가까우면 종가 단일가에 매수 (매수 2호가 지정가)."""
    if not st.is_on("box"):
        log.info("박스권 전략 OFF - 매수 판단 건너뜀")
        return
    b = boxmod.get(kis)
    sig = {"date": _today(), "pos": round(b["pos"], 3), "items": [], "result": ""}
    if not b["valid"]:
        sig["result"] = f"박스 폭 {b['width']:.1%} 로 너무 좁음 - 매수 안 함"
    elif not (config.BOX_MIN_POS <= b["pos"] <= config.BOX_BUY_POS):
        sig["result"] = f"박스 내 위치 {b['pos']:.0%} - 매수 구간(≤{config.BOX_BUY_POS:.0%}) 아님"
    elif not config.COVERED_CALL_CODE:
        sig["result"] = "COVERED_CALL_CODE 미설정 (.env) - 매수 안 함"
    else:
        r = kis.price(config.COVERED_CALL_CODE)  # 박스권 매수 대상: KODEX 200 커버드콜 액티브 (공포지수와 동일)
        s = st.load()
        bal = kis.balance()
        held_val = sum(min(p["qty"], bal["holdings"].get(c, {}).get("qty", 0)) * bal["holdings"].get(c, {}).get("price", 0)
                       for c, p in s["box_positions"].items()) + orders.pending_amount(s, "box")
        budget = min(bal["total"] * st.alloc()["box"] - held_val, bal["cash"])
        qty = int(budget // r["price"]) if budget > 0 else 0
        res = kis.order("buy", r["code"], qty) if qty > 0 else False
        if res:
            orders.on_buy(s, "box", res)
            st.save(s)
            sig["items"] = [{"code": r["code"], "name": r["name"], "qty": qty, "price": r["price"]}]
            sig["result"] = f"박스 하단 근접 ({b['pos']:.0%}) - {r['name']} {qty}주 매수"
        else:
            sig["result"] = "예산 여유 없음 또는 주문 실패"
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
        if p["qty"] <= 0:
            continue  # 매수 체결 대기 중
        acct = held.get(code, {}).get("qty", 0)
        if acct <= 0 and not config.DRY_RUN:
            del s["box_positions"][code]  # 계좌에 없음 (수동 매도 등)
            continue
        h = p["qty"] if config.DRY_RUN else min(p["qty"], acct)  # 박스권 몫만 매도
        new = [t for t in ladder if b["pos"] >= t and t not in p["sold_steps"]]
        if not new:
            continue
        if len(p["sold_steps"]) + len(new) >= len(ladder):
            qty = h  # 마지막 단계는 전량
        else:
            qty = min(h, max(1, round(p["peak_qty"] * config.BOX_SELL_RATIO * len(new))))
        if orders.track_sell(s, "box", kis.order("sell", code, qty), p.get("entry") or held.get(code, {}).get("avg"), "ladder"):
            p["sold_steps"] += new
            p["qty"] -= qty
            log.info("박스권 분할 매도 %s x%s (박스 위치 %.0f%%, 단계 %s)", code, qty, b["pos"] * 100, new)
            if qty >= h:
                del s["box_positions"][code]
    st.save(s)


def box_monitor(kis: KIS) -> None:
    """09:05~15:19 1분 간격 - 박스권 보유 종목 평균 매수가 대비 BOX_STOP_PCT 이하면 전량 손절."""
    now = dt.datetime.now().time()
    if not (dt.time(9, 5) <= now < dt.time(15, 20)):
        return
    s = st.load()
    live = {c: p for c, p in s["box_positions"].items() if p["qty"] > 0}
    if not live:
        return
    held = kis.balance()["holdings"]
    changed = False
    for code, p in live.items():
        h = held.get(code, {})
        entry = p.get("entry") or h.get("avg")  # 체결 기록이 없으면 계좌 매입 평균가
        qty = p["qty"] if config.DRY_RUN else min(p["qty"], h.get("qty", 0))
        if not entry or qty <= 0:
            continue
        try:
            px = kis.price(code)["price"]
        except Exception as e:
            log.warning("박스권 감시 시세 조회 실패 %s: %s", code, e)
            continue
        pnl = (px / entry - 1 - config.MOM_ROUNDTRIP_COST) * 100
        if pnl > config.BOX_STOP_PCT:
            continue
        name = config.STOCK_NAMES.get(code, code)
        if orders.track_sell(s, "box", kis.order("sell", code, qty), entry, "stop"):
            log.warning("박스권 손절 %s x%s (수익률 %+.2f%%)", code, qty, pnl)
            notify.send(f"🛑 박스권 손절 {name}({code}) {qty}주 (수익률 {pnl:+.2f}%)")
            p["qty"] -= qty
            if p["qty"] <= 0:
                del s["box_positions"][code]
            changed = True
        else:
            notify.send(f"⚠️ 박스권 손절 매도 주문 실패 {name}({code})")
    if changed:
        st.save(s)


# ---------------------------------------------------------------- 모멘텀
# 지수 추세: KIS 업종 1분봉으로 최근 N분 변화율 판단 (앱 재시작과 무관, 30초 캐시)
_TREND = {"t": 0.0, "v": None}


def index_trend(kis: KIS) -> dict:
    """최근 MOM_TREND_MIN 분 코스피 변화율(%)과 추세(up/down/flat). 조회 실패·데이터 부족이면 'unknown'."""
    if _TREND["v"] is not None and time.time() - _TREND["t"] < 30:
        return _TREND["v"]
    v = {"trend": "unknown", "pct": None}
    try:
        bars = kis.index_minutes("0001")  # 최신순
        if bars:
            now_t, now_v = bars[0]
            past = next((x for t, x in bars if t <= now_t - dt.timedelta(minutes=config.MOM_TREND_MIN)), None)
            if past:
                pct = (now_v / past - 1) * 100
                tr = "down" if pct <= -config.MOM_TREND_PCT else "up" if pct >= config.MOM_TREND_PCT else "flat"
                v = {"trend": tr, "pct": round(pct, 2)}
    except Exception as e:
        log.warning("지수 분봉 조회 실패: %s", e)
    _TREND.update(t=time.time(), v=v)
    return v


def _momentum_entry(kis: KIS, kind: str) -> bool:
    """kind: 'am'(10:01) / 'pm'(오후 재매수, MOM_PM_ENABLED 일 때만). 매수가 이뤄졌으면 True."""
    pm = kind == "pm"
    sig_key = "momentum_pm" if pm else "momentum"
    if not st.is_on("mom"):
        log.info("모멘텀 전략 OFF - 매수 판단 건너뜀")
        return False
    idx = kis.index_price("0001")["change_pct"]
    tr = index_trend(kis)
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
        return done("대상 종목 없음 (시총 조건 미충족 또는 시세 조회 실패)")
    lead = _lead_sector(data)
    sig["sector"] = lead

    s = st.load()
    cands = []
    for r in data[lead]:
        if not (0 < r["change_pct"] < config.MAX_CHANGE_PCT):
            continue
        try:
            orh = kis.or_high(r["code"], config.MOM_OR_MIN)  # 장 초반 30분 고가
        except Exception as e:
            log.warning("초반 고가 조회 실패 %s: %s", r["code"], e)
            continue
        if orh <= 0 or r["price"] <= orh:
            continue  # 초반 고가를 돌파하지 못함
        cands.append({**r, "or_high": orh, "rs": round(r["change_pct"] - idx, 2)})
    cands.sort(key=lambda r: r["rs"], reverse=True)  # 상대 강도 순
    picks = cands[: config.PICK_COUNT]
    if not picks:
        return done(f"주도 섹터({lead})에 장 초반 30분 고가를 돌파한 상승 종목 없음")

    bal = kis.balance()
    # 모멘텀·단타 공용 예산: 모멘텀 + 단타가 쓴 금액(보유 + 미체결)을 모두 차감
    held_val = sum(p["qty"] * bal["holdings"].get(c, {}).get("price", 0)
                   for c, p in s["mom_positions"].items() if p.get("date") == _today()) + orders.pending_amount(s, "mom") \
        + sum(p["qty"] * bal["holdings"].get(c, {}).get("price", 0)
              for c, p in s["scalp_positions"].items() if p["qty"] > 0) + orders.pending_amount(s, "scalp")
    budget = min(bal["total"] * st.alloc()["momscalp"] - held_val, bal["cash"])
    if budget <= 0:
        return done("모멘텀 예산 여유 없음")
    per = budget / len(picks)
    n_bought = 0
    for r in picks:
        rp = risk.r_pct(kis, r["code"], config.MOM_ATR_MULT, config.MOM_R_MIN, config.MOM_R_MAX)
        qty = risk.size_qty(bal["total"], r["price"], rp, per)
        res = kis.order("buy", r["code"], qty, mode="chase") if qty > 0 else False
        if res:
            orders.on_buy(s, "mom", res)
            s["mom_positions"][r["code"]].update(r_pct=round(rp, 3))
            sig["items"].append({"code": r["code"], "name": r["name"], "price": r["price"],
                                 "change_pct": r["change_pct"], "qty": qty, "rs": r["rs"], "r_pct": round(rp, 2)})
            n_bought += 1
    st.save(s)
    return done(f"{lead} 주도 - {n_bought}종목 매수" + (" (오후 재매수)" if pm else ""), n_bought > 0)


def momentum_buy(kis: KIS) -> None:
    """10:01 - 코스피 +1% & 주도 섹터 & 지수 하락 추세 아님 & 장 초반 30분 고가 돌파 → 상대 강도 상위 매수."""
    _momentum_entry(kis, "am")


def momentum_buy_pm(kis: KIS) -> None:
    """14:00~14:50 오후 재매수 - 폐지(MOM_PM_ENABLED=False). 켜면 지수 상승 추세일 때 하루 1회."""
    if not config.MOM_PM_ENABLED:
        return
    now = dt.datetime.now().time()
    if not (dt.time(14, 0) <= now < dt.time(15, 0)):
        return
    sp = st.load()["signals"].get("momentum_pm") or {}
    if sp.get("items") and sp.get("date") == _today():
        return  # 오늘 오후 매수 완료
    _momentum_entry(kis, "pm")


def momentum_monitor(kis: KIS) -> None:
    """10:02~15:19 - 모멘텀 보유 종목 손절/본전/익절/추적 손절 (1분 간격, R 기준)."""
    now = dt.datetime.now().time()
    if not (dt.time(10, 2) <= now < dt.time(15, 20)):
        return
    s = st.load()
    todo = {c: p for c, p in s["mom_positions"].items()
            if p.get("date") == _today() and p.get("entry") and p["qty"] > 0 and not p.get("sold_order")}
    if not todo:
        return
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
            log.warning("모멘텀 감시 시세 조회 실패 %s: %s", code, e)
            continue
        entry = p["entry"]
        rp = p.get("r_pct") or config.MOM_R_MAX
        pnl = (px / entry - 1 - config.MOM_ROUNDTRIP_COST) * 100
        if px > p.get("high", 0):
            p["high"] = px
            changed = True
        steps = p.setdefault("steps", [])
        base = p.get("base_qty", p["qty"])
        sells = []  # (단계, 비율)
        if pnl <= -rp and "stop" not in steps:
            sells.append(("stop", config.MOM_STOP_RATIO))
        if "stop" in steps and "stop2" not in steps and p.get("stop_at"):
            waited = (dt.datetime.now() - dt.datetime.fromisoformat(p["stop_at"])).total_seconds()
            if waited >= config.MOM_STOP2_WAIT_MIN * 60:
                if pnl <= -rp:
                    sells.append(("stop2", 1.0))
                else:
                    steps.append("stop2")  # 회복했으므로 2차 손절 판단 종료
                    changed = True
        if pnl <= config.BE_ARM_PCT and not p.get("was_neg"):
            p["was_neg"] = True  # -2% 이하 경험 -> 본전 회복 매도 활성
            changed = True
        if pnl >= 0 and p.get("was_neg") and "breakeven" not in steps:
            sells.append(("breakeven", config.MOM_BREAKEVEN_RATIO))
        if pnl >= config.TP_R * rp and "tp" not in steps:
            sells.append(("tp", config.TP_RATIO))
        if "tp" in steps and p.get("high") and px <= p["high"] * (1 - config.MOM_TRAIL_R * rp / 100):
            sells.append(("trail", 1.0))
        for name, ratio in sells:
            qty = p["qty"] if ratio >= 1 else min(p["qty"], max(1, round(base * ratio)))
            if hq is not None:
                qty = min(qty, hq)
            if qty <= 0:
                break
            if orders.track_sell(s, "mom", kis.order("sell", code, qty), entry, name):
                p["qty"] -= qty
                if hq is not None:
                    hq -= qty
                steps.append(name)
                if name == "stop":
                    p["stop_at"] = dt.datetime.now().isoformat(timespec="seconds")
                changed = True
                log.info("모멘텀 %s 매도 %s x%s (수익률 %+.2f%%, R %.2f%%)", name, code, qty, pnl, rp)
                notify.send(f"📉 모멘텀 {name} 매도 {config.STOCK_NAMES.get(code, code)}({code}) x{qty} (수익률 {pnl:+.2f}%)")
            else:
                log.error("모멘텀 %s 매도 주문 실패 %s", name, code)
                notify.send(f"⚠️ 모멘텀 {name} 매도 주문 실패 {code}")
                break
    if changed:
        st.save(s)


def momentum_close(kis: KIS) -> None:
    """15:15 - 모멘텀 남은 수량을 정규장에서 전량 시장가 청산 (MOM_EXIT_CLOSE). 실패분은 19:30 NXT / 다음날 09:05 보조."""
    if not config.MOM_EXIT_CLOSE:
        return
    s = st.load()
    live = {c: p for c, p in s["mom_positions"].items() if p["qty"] > 0 and p.get("date") == _today()
            and not p.get("sold_order")}
    if not live:
        return
    held = None if config.DRY_RUN else kis.balance()["holdings"]
    for code, p in live.items():
        qty = p["qty"] if held is None else min(p["qty"], held.get(code, {}).get("qty", 0))
        if qty <= 0:
            continue
        if orders.track_sell(s, "mom", kis.order("sell", code, qty), p.get("entry"), "close"):
            p["qty"] -= qty
            p.setdefault("steps", []).append("close")
            log.info("모멘텀 장마감 청산 %s x%s", code, qty)
        else:
            notify.send(f"⚠️ 모멘텀 15:15 청산 실패 {code} x{qty} - 19:30 NXT 에서 다시 시도합니다")
    st.save(s)


def momentum_sell_evening(kis: KIS) -> None:
    """19:30 - 모멘텀 종목을 NXT 애프터마켓에서 지정가 매도."""
    s = st.load()
    held = None if config.DRY_RUN else kis.balance()["holdings"]
    for code, p in s["mom_positions"].items():
        if p.get("sold_order"):
            continue
        if held is not None:
            p["qty"] = min(p["qty"], held.get(code, {}).get("qty", 0))  # 실제 보유 수량만
            if p["qty"] <= 0:
                continue
        try:
            px = kis.price(code, market=config.NXT_PRICE_MARKET)["price"]
            limit = px - tick_size(px) * config.SELL_TICKS_BELOW
            if orders.track_sell(s, "mom", kis.order("sell", code, p["qty"], price=limit,
                                                     exchange=config.NXT_ORDER_EXCHANGE), p.get("entry"), "nxt"):
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
            if not orders.track_sell(s, "mom", kis.order("sell", code, qty), p.get("entry"), "fallback"):
                notify.send(f"⚠️ 09:05 모멘텀 잔여 정리 매도 실패 {code} x{qty}")
        del s["mom_positions"][code]
    st.save(s)
