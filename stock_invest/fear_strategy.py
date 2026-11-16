"""공포지수 전략 (계좌의 40%): VIX 급등 시 커버드콜 ETF 분할 매수, VIX 안정 시 10%씩 분할 매도."""
import datetime as dt
import logging

from . import config
from . import market_data
from . import orders
from . import state as st
from .kis_client import KIS

log = logging.getLogger("fear")


def run(kis: KIS) -> None:
    code = config.COVERED_CALL_CODE
    if not code:
        log.error("COVERED_CALL_CODE 가 설정되지 않았습니다 (.env)")
        return

    vix = market_data.vix()
    log.info("VIX=%s", vix)
    if vix is None:
        log.error("VIX 조회 실패 - 오늘은 매매하지 않음")
        return

    s = st.load()
    f = s["fear"]
    today = dt.date.today().isoformat()
    bal = kis.balance()
    # 같은 ETF 를 박스권 전략도 보유하므로, 계좌 보유 수량에서 박스권 몫을 뺀 것만 공포지수 몫으로 본다
    held_qty = max(0, bal["holdings"].get(code, {}).get("qty", 0) - s["box_positions"].get(code, {}).get("qty", 0))
    px = kis.price(code)["price"]
    budget = bal["total"] * st.alloc()["fear"]

    if vix >= config.VIX_BUY_MIN:
        if f["last_buy_date"] == today:
            return
        if not s.get("enabled", {}).get("fear", True):
            log.info("공포지수 전략 OFF - 매수 신호지만 매수하지 않음")
            return
        room = budget - held_qty * px - orders.pending_amount(s, "fear")
        amount = min(budget * config.BUY_TRANCHE, room, bal["cash"])
        qty = int(amount // px)
        if qty <= 0:
            log.info("매수 여력 없음 (예산 여유 %s원)", int(room))
            return
        res = kis.order("buy", code, qty)
        if res:
            orders.on_buy(s, "fear", res)
            f.update({"sold_steps": [], "peak_qty": 0, "last_buy_date": today})
    elif held_qty > 0 and vix <= config.VIX_SELL_LADDER[0]:
        if f["last_sell_date"] == today:
            return
        step = next((t for t in config.VIX_SELL_LADDER if vix <= t and t not in f["sold_steps"]), None)
        if step is None:
            return
        if f["peak_qty"] == 0:
            f["peak_qty"] = held_qty
        qty = min(held_qty, max(1, int(f["peak_qty"] * config.SELL_STEP_RATIO)))
        if orders.track_sell(s, "fear", kis.order("sell", code, qty)):
            f["sold_steps"].append(step)
            f["last_sell_date"] = today
            log.info("분할 매도 단계 VIX<=%s (VIX=%.2f)", step, vix)
    st.save(s)
