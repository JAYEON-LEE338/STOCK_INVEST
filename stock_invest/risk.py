"""위험 관리: ATR 기반 위험 단위(R)·투자 수량, 하루 손실 한도(킬 스위치), 종목당 노출 한도."""
import datetime as dt
import json
import logging

from . import config
from . import notify

log = logging.getLogger("risk")

_ATR = {"date": "", "v": {}}  # {code: (atr_won, atr_pct)} 하루 1회 계산


def atr(kis, code: str):
    """최근 ATR_DAYS 거래일 평균 실제 변동폭 (원, 전일 종가 대비 %). 데이터 부족/오류면 None."""
    today = dt.date.today().isoformat()
    if _ATR["date"] != today:
        _ATR["date"], _ATR["v"] = today, {}
    if code in _ATR["v"]:
        return _ATR["v"][code]
    res = None
    try:
        rows = kis.daily_ohlc(code, config.ATR_DAYS + 1)  # 최신순, 오늘 제외
        if len(rows) >= config.ATR_DAYS + 1:
            trs = []
            for i in range(config.ATR_DAYS):
                h, lo, prev_c = rows[i]["high"], rows[i]["low"], rows[i + 1]["close"]
                trs.append(max(h - lo, abs(h - prev_c), abs(lo - prev_c)))
            a = sum(trs) / len(trs)
            res = (a, a / rows[0]["close"] * 100)
    except Exception as e:
        log.warning("ATR 계산 실패 %s: %s", code, e)
    _ATR["v"][code] = res
    return res


def r_pct(kis, code: str, mult: float, lo: float, hi: float) -> float:
    """위험 단위 R(%) = ATR% × mult, [lo, hi] 범위로 제한. ATR 을 못 구하면 보수적으로 hi."""
    a = atr(kis, code)
    if not a:
        return hi
    return max(lo, min(hi, a[1] * mult))


def size_qty(total: float, price: float, rp: float, budget: float) -> int:
    """손절(1R) 시 계좌 손실이 RISK_PER_TRADE 가 되도록 하는 금액, 예산, 종목당 노출 한도 중 가장 작은 쪽의 수량."""
    if price <= 0 or rp <= 0 or budget <= 0:
        return 0
    by_risk = total * config.RISK_PER_TRADE / (rp / 100)
    by_cap = total * config.MAX_STOCK_PCT * 0.99  # 호가 차이 여유
    return int(min(budget, by_risk, by_cap) // price)


# ---------------------------------------------------------------- 킬 스위치 / 노출 한도
def _load() -> dict:
    try:
        return json.loads(config.RISK_FILE.read_text())
    except Exception:
        return {}


def _save(d: dict) -> None:
    try:
        config.RISK_FILE.write_text(json.dumps(d))
    except Exception as e:
        log.warning("위험 상태 저장 실패: %s", e)


def day_state(total: float) -> dict:
    """오늘의 시작 평가금액을 기록/조회하고 손실 한도 도달 여부를 계산."""
    today = dt.date.today().isoformat()
    d = _load()
    if d.get("date") != today or not d.get("start"):
        d = {"date": today, "start": float(total), "alerted": False}
        _save(d)
    chg = (total / d["start"] - 1) * 100 if d["start"] else 0.0
    d["change_pct"] = round(chg, 2)
    d["limit_pct"] = -config.DAILY_LOSS_LIMIT_PCT
    d["blocked"] = chg <= -config.DAILY_LOSS_LIMIT_PCT
    return d


def check_buy(bal: dict, code: str, amount: float):
    """매수 직전 검사. (허용 여부, 거부 사유)."""
    total = bal["total"]
    d = day_state(total)
    if d["blocked"]:
        if not d.get("alerted"):
            notify.send(f"🛑 하루 손실 한도 도달: 시작 {d['start']:,.0f}원 → 현재 {total:,.0f}원 ({d['change_pct']:+.2f}%)\n"
                        f"오늘 신규 매수를 모두 중단합니다. (보유 종목 손절·매도는 계속)")
            d["alerted"] = True
            _save({k: d[k] for k in ("date", "start", "alerted")})
        return False, f"하루 손실 한도 {-config.DAILY_LOSS_LIMIT_PCT}% 도달 ({d['change_pct']:+.2f}%)"
    if code != config.COVERED_CALL_CODE and total > 0:
        h = bal["holdings"].get(code, {})
        held_val = h.get("qty", 0) * h.get("price", 0)
        if held_val + amount > total * config.MAX_STOCK_PCT:
            return False, (f"종목당 노출 한도 {config.MAX_STOCK_PCT:.0%} 초과 "
                           f"(보유 {held_val:,.0f} + 주문 {amount:,.0f} > {total * config.MAX_STOCK_PCT:,.0f}원)")
    return True, ""
