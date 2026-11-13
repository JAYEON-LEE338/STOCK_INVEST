"""연결 점검: 읽기 전용 API만 호출한다 (주문 없음)."""
import logging

import config
import market_data
from kis_client import KIS

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def step(name, fn):
    try:
        r = fn()
        print(f"[OK]   {name}: {r}")
        return r
    except Exception as e:
        print(f"[FAIL] {name}: {e}")
        return None


print(f"MODE={config.MODE} DRY_RUN={config.DRY_RUN} 계좌={config.ACCOUNT_NO[:4]}****-{config.ACCOUNT_PROD}")
kis = step("토큰 발급", lambda: KIS())
if kis:
    step("현재가(삼성전자)", lambda: kis.price("005930"))
    step("일봉 종가(삼성전자)", lambda: kis.daily_closes("005930", 5))
    step("코스피 등락률", lambda: kis.index_change("0001"))
    if config.COVERED_CALL_CODE:
        step(f"현재가(커버드콜 {config.COVERED_CALL_CODE})", lambda: kis.price(config.COVERED_CALL_CODE))
    step("NXT 현재가(삼성전자)", lambda: kis.price("005930", market=config.NXT_PRICE_MARKET))
    b = step("잔고", lambda: kis.balance())
step("VIX", market_data.vix)
