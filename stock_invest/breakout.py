"""돌파매매 (총평가금액의 BREAK_RATIO, 기본 0%)

매수(09:30~15:20, 10분 간격, 15:20 은 종가 단일가): 외국인 3개월 순매수 금액 누적 > 0(시총 대비) 이고 최근 20일 순매수 > 0
  + 20일 박스권(최고가) 돌파 + 전일 대비 거래량 +30% 이상. 종가 확인으로 가짜 돌파를 거른다.
매도(1분 감시): R = ATR×2.0. -1R 전량 손절, 돌파 실패(3거래일 안에 기준선 아래 마감) 전량,
  +2R 50% 익절, 이후 최고가-1R 추적 손절. 오버나이트 가능.
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

log = logging.getLogger("breakout")

_UPPER = {"date": "", "v": {}}  # {code: 20일 박스 상단} 하루 1회 계산
_FOREIGN = {"date": "", "v": {}}  # {code: (충족 여부, 상세)} 하루 1회 계산


def _today() -> str:
    return dt.date.today().isoformat()


def _quantile(vals: list, q: float) -> float:
    v = sorted(vals)
    if q >= 1:
        return float(v[-1])
    pos = (len(v) - 1) * q
    lo = int(pos)
    return v[lo] + (v[min(lo + 1, len(v) - 1)] - v[lo]) * (pos - lo)


def _trading_days_since(date_str: str) -> int:
    """매수일 다음 날부터 오늘까지의 평일 수 (휴장일은 근사)."""
    d0 = dt.date.fromisoformat(date_str)
    n, d = 0, d0
    while d < dt.date.today():
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def box_upper(kis: KIS, code: str):
    """최근 BREAK_DAYS 거래일(오늘 제외) 고가의 BREAK_UPPER_Q 분위수. 데이터 부족하면 None."""
    if _UPPER["date"] != _today():
        _UPPER["date"], _UPPER["v"] = _today(), {}
    if code in _UPPER["v"]:
        return _UPPER["v"][code]
    up = None
    try:
        rows = kis.daily_ohlc(code, config.BREAK_DAYS)
        if len(rows) >= config.BREAK_DAYS:
            up = _quantile([x["high"] for x in rows], config.BREAK_UPPER_Q)
        else:
            log.warning("일봉 부족 %s: %s개 (필요 %s)", code, len(rows), config.BREAK_DAYS)
    except Exception as e:
        log.warning("일봉 조회 실패 %s: %s", code, e)
    _UPPER["v"][code] = up
    return up


def foreign_ok(kis: KIS, code: str, mcap: int):
    """(충족 여부, 상세) - 3개월 외국인 순매수 금액 누적 > 0 (시총 대비 BREAK_FOREIGN_MIN_PCT% 이상)
    이고 누적이 증가 중(최근 N일 순매수 금액 합 > 0). 데이터 부족/오류면 (False, ...)."""
    if _FOREIGN["date"] != _today():
        _FOREIGN["date"], _FOREIGN["v"] = _today(), {}
    if code in _FOREIGN["v"]:
        return _FOREIGN["v"][code]
    try:
        rows = kis.foreign_netbuy(code, config.BREAK_FOREIGN_DAYS)  # 최신순, 오늘 제외, amt=백만원
        n = config.BREAK_FOREIGN_RECENT
        if len(rows) < n * 2:
            res = (False, {"error": f"외국인 데이터 부족 ({len(rows)}일)"})
        else:
            total = sum(x["amt"] for x in rows)
            recent = sum(x["amt"] for x in rows[:n])
            ratio = total / (mcap * 100) * 100 if mcap else 0.0  # 시총(억원)→백만원 환산 후 %
            ok = total > 0 and recent > 0 and ratio >= config.BREAK_FOREIGN_MIN_PCT
            res = (ok, {"total": total, "recent": recent, "ratio_pct": round(ratio, 3)})
    except Exception as e:
        log.warning("외국인 순매수 조회 실패 %s: %s", code, e)
        res = (False, {"error": str(e)})
    _FOREIGN["v"][code] = res
    return res


def _evaluate(kis: KIS, r: dict, with_foreign: bool = True) -> dict:
    """한 종목의 돌파 조건 평가. 비싼 조회(외국인)는 앞 조건을 통과한 경우에만 한다."""
    hot = r["change_pct"] >= config.MAX_CHANGE_PCT
    up = box_upper(kis, r["code"])
    c_vol = r["vol_rate"] >= config.BREAK_VOL_RATE
    c_break = up is not None and r["price"] > up
    c_for, detail = None, None
    if with_foreign and c_vol and c_break and not hot:
        c_for, detail = foreign_ok(kis, r["code"], r.get("mcap", 0))
    return {"code": r["code"], "name": r["name"], "price": r["price"], "change_pct": r["change_pct"],
            "vol_rate": r["vol_rate"], "upper": up, "c_vol": c_vol, "c_break": c_break, "c_for": c_for,
            "foreign": detail, "hot": hot}


def _all(c: dict) -> bool:
    return bool(c["c_vol"] and c["c_break"] and c["c_for"] and not c["hot"])


def _done_codes(s: dict) -> list:
    d = s.get("break_done") or {}
    if d.get("date") != _today():
        d = {"date": _today(), "codes": []}
    s["break_done"] = d
    return d["codes"]


# ---------------------------------------------------------------- 매수 (15:20 종가 단일가)
def buy_breakout(kis: KIS) -> None:
    """09:30~15:20 (10분 간격) - 돌파 조건을 만족하면 매수 (하루 최대 BREAK_MAX_BUYS종목, 15:20 은 종가 단일가)."""
    now = dt.datetime.now().time()
    if not (dt.time(9, 30) <= now < dt.time(15, 25)):
        return
    if not st.is_on("break"):
        log.info("돌파매매 OFF - 매수 판단 건너뜀")
        return
    s = st.load()
    sig = {"date": _today(), "time": dt.datetime.now().strftime("%H:%M"), "items": [], "result": ""}

    def finish(msg):
        sig["result"] = msg
        log.info("돌파매매 판단: %s", msg)
        s["signals"]["break"] = sig
        st.save(s)

    ratio = st.alloc()["break"]
    if ratio <= 0:
        return finish("돌파매매 투자 비중 0% - 대시보드에서 비중을 설정하세요")
    data = daytrade._sector_rows(kis)
    if not data:
        return finish("대상 종목 없음 (시총 조건 미충족 또는 시세 조회 실패)")

    done = _done_codes(s)
    held = {c for c, p in s["break_positions"].items() if p["qty"] > 0}
    opened_today = sum(1 for p in s["break_positions"].values() if p.get("date") == _today())
    remain = config.BREAK_MAX_BUYS - opened_today
    if remain <= 0:
        return finish(f"오늘 매수 한도 {config.BREAK_MAX_BUYS}종목 소진")

    cands = []
    for rows in data.values():
        for r in rows:
            if r["code"] in held or r["code"] in done:
                continue
            c = _evaluate(kis, r)
            if _all(c):
                cands.append(c)
    cands.sort(key=lambda c: c["vol_rate"], reverse=True)
    picks = cands[:remain]
    if not picks:
        return finish("돌파 조건을 모두 만족한 종목 없음")

    bal = kis.balance()
    held_val = sum(p["qty"] * bal["holdings"].get(c, {}).get("price", 0)
                   for c, p in s["break_positions"].items() if p["qty"] > 0) + orders.pending_amount(s, "break")
    budget = min(bal["total"] * ratio - held_val, bal["cash"])
    if budget <= 0:
        return finish("돌파매매 예산 여유 없음")
    per = budget / len(picks)
    n = 0
    for c in picks:
        rp = risk.r_pct(kis, c["code"], config.BREAK_ATR_MULT, config.BREAK_R_MIN, config.BREAK_R_MAX)
        qty = risk.size_qty(bal["total"], c["price"], rp, per)
        res = kis.order("buy", c["code"], qty, mode="chase") if qty > 0 else False
        if res:
            orders.on_buy(s, "break", res)
            s["break_positions"][c["code"]].update(r_pct=round(rp, 3), level=c["upper"])
            sig["items"].append({"code": c["code"], "name": c["name"], "price": c["price"], "change_pct": c["change_pct"],
                                 "qty": qty, "vol_rate": c["vol_rate"], "r_pct": round(rp, 2)})
            n += 1
    finish(f"돌파 종목 {n}개 매수" + (" (종가 단일가)" if now >= dt.time(15, 20) else ""))


# ---------------------------------------------------------------- 감시
def monitor(kis: KIS) -> None:
    """09:05~15:19 1분 간격 - R 기준 손절, 돌파 실패, 익절, 추적 손절."""
    now = dt.datetime.now().time()
    if not (dt.time(9, 5) <= now < dt.time(15, 20)):
        return
    s = st.load()
    live = {c: p for c, p in s["break_positions"].items() if p["qty"] > 0 and p.get("entry")}
    if not live:
        return
    done = _done_codes(s)
    held = None if config.DRY_RUN else kis.balance()["holdings"]
    changed = False
    for code, p in live.items():
        hq = None
        if held is not None:
            hq = held.get(code, {}).get("qty", 0)
            if hq <= 0:
                continue  # 아직 체결 전
        try:
            px = kis.price(code)["price"]
        except Exception as e:
            log.warning("돌파매매 감시 시세 조회 실패 %s: %s", code, e)
            continue
        name = config.STOCK_NAMES.get(code, code)
        entry, rp = p["entry"], p.get("r_pct") or config.BREAK_R_MAX
        pnl = (px / entry - 1 - config.MOM_ROUNDTRIP_COST) * 100
        steps, base = p["steps"], p["base_qty"]
        if px > p.get("high", 0):
            p["high"] = px
            changed = True
        sells = []
        failed = (now >= dt.time(15, 15) and p.get("level") and px < p["level"]
                  and p.get("date") != _today() and _trading_days_since(p["date"]) <= config.BREAK_FAIL_DAYS)
        if pnl <= -rp:
            sells.append(("stop", p["qty"]))
        elif failed:
            sells.append(("fail", p["qty"]))
        else:
            if pnl >= config.TP_R * rp and "tp" not in steps:
                sells.append(("tp", max(1, round(base * config.TP_RATIO))))
            if "tp" in steps and p.get("high") and px <= p["high"] * (1 - config.BREAK_TRAIL_R * rp / 100):
                sells.append(("trail", p["qty"]))
        for step, want in sells:
            qty = min(p["qty"], want)
            if hq is not None:
                qty = min(qty, hq)
            if qty <= 0:
                break
            if orders.track_sell(s, "break", kis.order("sell", code, qty), entry, step):
                p["qty"] -= qty
                if hq is not None:
                    hq -= qty
                steps.append(step)
                changed = True
                if step == "tp" and code not in done:
                    done.append(code)
                log.info("돌파매매 %s 매도 %s x%s (수익률 %+.2f%%, R %.2f%%)", step, code, qty, pnl, rp)
                notify.send(f"🚀 돌파매매 {step} 매도 {name}({code}) x{qty} (수익률 {pnl:+.2f}%)")
            else:
                notify.send(f"⚠️ 돌파매매 {step} 매도 주문 실패 {name}({code})")
                break
    if changed:
        st.save(s)


# ---------------------------------------------------------------- 대시보드용 TOP 5
def candidates(kis: KIS, n: int = 5) -> dict:
    """유니버스를 돌파 조건으로 평가해 가까운 순 상위 n개. 외국인 조건은 앞 조건을 통과한 종목만 조회한다."""
    data = daytrade._sector_rows(kis)
    if not data:
        return {"error": "대상 종목 없음 (시총 조건 미충족 또는 시세 조회 실패)"}
    s = st.load()
    d = s.get("break_done") or {}
    done = d.get("codes", []) if d.get("date") == _today() else []
    held = {c for c, p in s["break_positions"].items() if p["qty"] > 0}
    out = []
    for sec, rows in data.items():
        for r in rows:
            c = _evaluate(kis, r)
            c["sector"] = sec
            c["met"] = int(c["c_vol"]) + int(c["c_break"]) + int(bool(c["c_for"]))
            c["held"], c["done"] = c["code"] in held, c["code"] in done
            out.append(c)
    # 충족 조건 수 -> 20일 상단 대비 근접도 -> 거래량 비율
    out.sort(key=lambda c: (c["met"], (c["price"] / c["upper"]) if c["upper"] else 0, c["vol_rate"]), reverse=True)
    top = out[:n]
    for c in top:
        c["pick"] = bool(_all(c) and not c["held"] and not c["done"])
    return {"items": top, "ratio": st.alloc()["break"]}
