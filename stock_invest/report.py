"""10시 현황 알림."""
import datetime as dt
import logging

from . import box as boxmod
from . import config
from . import market_data
from . import notify
from . import state as st
from .kis_client import KIS

log = logging.getLogger("report")


def build(kis: KIS) -> str:
    mode = "실전" if config.MODE == "live" else "모의투자"
    lines = [f"📊 {dt.date.today()} 현황 ({mode}{' · DRY_RUN' if config.DRY_RUN else ' · 실주문'})"]
    s = st.load()

    try:
        bal = kis.balance()
        lines.append(f"총평가 {bal['total']:,}원 / 예수금 {bal['cash']:,}원")
        tags = {}
        if config.COVERED_CALL_CODE:
            tags[config.COVERED_CALL_CODE] = "공포"
        tags.update({c: "박스권" for c in s["box_positions"]})
        tags.update({c: "모멘텀" for c in s["mom_positions"]})
        held = [f"· {h['name']}({c}) {h['qty']}주 {h['qty'] * h['price']:,}원 [{tags.get(c, '-')}]"
                for c, h in bal["holdings"].items()]
        lines.append("보유 종목:\n" + "\n".join(held) if held else "보유 종목 없음")
    except Exception as e:
        lines.append(f"잔고 조회 실패: {e}")

    try:
        idx = kis.index_price("0001")
        lines.append(f"코스피 {idx['price']:,.2f} ({idx['change_pct']:+.2f}%)")
    except Exception as e:
        lines.append(f"코스피 조회 실패: {e}")

    try:
        b = boxmod.get(kis)
        lines.append(f"박스권 {b['lower']:,.0f}~{b['upper']:,.0f} · 현재 위치 {b['pos']:.0%}"
                     f"{'' if b['valid'] else ' (박스 폭 부족)'}")
    except Exception as e:
        lines.append(f"박스권 계산 실패: {e}")

    vix = market_data.vix()
    if vix is not None:
        state = "매수 신호" if vix >= config.VIX_BUY_MIN else "매도 구간" if vix <= config.VIX_SELL_LADDER[0] else "관망"
        lines.append(f"VIX {vix:.2f} ({state})")
    return "\n".join(lines)


def daily_status(kis: KIS) -> None:
    text = build(kis)
    log.info("현황 알림 전송\n%s", text)
    notify.send(text)
