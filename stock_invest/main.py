import datetime as dt
import logging
import sys

import holidays
from apscheduler.schedulers.blocking import BlockingScheduler

from . import config
from . import daytrade
from . import fear_strategy
from . import notify
from . import orders
from . import breakout
from . import fishing
from . import report
from . import scalp
from .kis_client import KIS

TZ = "Asia/Seoul"
KR_HOLIDAYS = holidays.KR()

# (함수, 표시 이름, 시, 분[, cron 덮어쓰기]) — 표시는 시:분, 실제 실행은 덮어쓰기가 있으면 그 규칙 (함수 안에서 시간대를 다시 거름)
EVERY10 = {"hour": "9-15", "minute": "*/10"}
JOBS = [
    (daytrade.sell_fallback, "전일 모멘텀 잔여 정리", 9, 5),
    (report.daily_status, "10시 현황 알림", 10, 0),
    (daytrade.momentum_buy, "모멘텀 매수 (09:30~14:50, 10분 간격)", 9, 30, EVERY10),
    (scalp.scan, "눌림목 후보 스캔 (09:05 / 09:30 / 10:00)", 9, 5),
    (scalp.buy, "눌림목 매수 (09:30~14:50, 10분 간격)", 9, 30, EVERY10),
    (daytrade.box_sell, "박스권 상단 분할 매도", 15, 0),
    (fear_strategy.run, "VIX 공포지수 매매 (09:30~15:20, 10분 간격, 하루 1회)", 9, 30, EVERY10),
    (daytrade.box_buy, "박스권 하단 매수 (09:30~15:20, 10분 간격, 하루 1회)", 9, 30, EVERY10),
    (scalp.close, "눌림목 잔량 정리", 15, 15),
    (daytrade.momentum_close, "모멘텀 장마감 청산", 15, 15),
    (fishing.scan_fishing, "FISHING 오전 급락 스캔 (09:05~12:00, 5분 간격)", 9, 5, {"hour": "9-12", "minute": "*/5"}),
    (fishing.buy_fishing, "FISHING 반등 매수 (09:05~14:30, 1분 간격)", 9, 5, {"hour": "9-14", "minute": "*"}),
    (fishing.close_fishing, "FISHING 잔량 정리", 15, 15),
    (breakout.buy_breakout, "돌파매매 매수 (09:30~15:20, 10분 간격)", 9, 30, EVERY10),
    (daytrade.momentum_sell_evening, "모멘텀 NXT 매도 (15:15 실패분 보조)", 19, 30),
]


def setup_logging():
    config.LOG_DIR.mkdir(exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO, format=fmt,
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(config.LOG_DIR / "trade.log", encoding="utf-8")],
    )


def guarded(fn, kis, lock=None):
    def wrapper():
        today = dt.date.today()
        if today.weekday() >= 5 or today in KR_HOLIDAYS:
            return
        try:
            if lock:
                with lock:
                    fn(kis)
            else:
                fn(kis)
        except Exception as e:
            logging.getLogger("main").exception("%s 실행 중 오류", fn.__name__)
            notify.send(f"❌ 오류: {fn.__name__}\n{type(e).__name__}: {e}")
    wrapper.__name__ = fn.__name__
    return wrapper


def build_scheduler(kis, sched_cls=BlockingScheduler, lock=None):
    sched = sched_cls(timezone=TZ)
    for fn, _label, h, m, *rest in JOBS:
        when = rest[0] if rest else {"hour": h, "minute": m}
        sched.add_job(guarded(fn, kis, lock), "cron", day_of_week="mon-fri", **when,
                      misfire_grace_time=120, id=fn.__name__)
    # 매수 체결 반영 / 미체결 취소: 매 분 09:00~15:59 (15:35 이후 남은 주문은 만료 처리)
    sched.add_job(guarded(orders.sync, kis, lock), "cron", day_of_week="mon-fri",
                  hour="9-15", minute="*", misfire_grace_time=30, id="orders_sync")
    # NXT 매도 체결 확인 (애프터마켓 20:00 종료 후)
    sched.add_job(guarded(orders.sync, kis, lock), "cron", day_of_week="mon-fri", hour=20, minute=5,
                  misfire_grace_time=600, id="orders_sync_nxt")
    # 박스권 손절 감시: 매 분 (함수 내부에서 09:05~15:19 만 동작)
    sched.add_job(guarded(daytrade.box_monitor, kis, lock), "cron", day_of_week="mon-fri",
                  hour="9-15", minute="*", misfire_grace_time=30, id="box_monitor")
    # 같은 작업의 추가 실행 시각 (JOBS 의 첫 시각 외)
    for fn, jid, hour, minute in ((scalp.scan, "scalp_scan_2", 9, 30), (scalp.scan, "scalp_scan_3", 10, 0),
                                  ):
        sched.add_job(guarded(fn, kis, lock), "cron", day_of_week="mon-fri", hour=hour, minute=minute,
                      misfire_grace_time=120, id=jid)
    # FISHING 매도 감시: 매 분 (함수 내부에서 09:05~15:14 만 동작)
    sched.add_job(guarded(fishing.monitor, kis, lock), "cron", day_of_week="mon-fri",
                  hour="9-15", minute="*", misfire_grace_time=30, id="fish_monitor")
    # 돌파매매 손절/익절 감시: 매 분 (함수 내부에서 09:05~15:19 만 동작)
    sched.add_job(guarded(breakout.monitor, kis, lock), "cron", day_of_week="mon-fri",
                  hour="9-15", minute="*", misfire_grace_time=30, id="break_monitor")
    # 눌림목(단타) 손실/익절 감시: 매 분 (함수 내부에서 09:30~15:14 만 동작)
    sched.add_job(guarded(scalp.monitor, kis, lock), "cron", day_of_week="mon-fri",
                  hour="9-15", minute="*", misfire_grace_time=30, id="scalp_monitor")
    # 모멘텀 손절/익절 감시: 매 분 (함수 내부에서 09:31~15:19 만 동작)
    sched.add_job(guarded(daytrade.momentum_monitor, kis, lock), "cron", day_of_week="mon-fri",
                  hour="9-15", minute="*", misfire_grace_time=30, id="momentum_monitor")
    return sched


def main():
    setup_logging()
    log = logging.getLogger("main")
    log.info("시작: MODE=%s DRY_RUN=%s", config.MODE, config.DRY_RUN)
    sched = build_scheduler(KIS())
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("종료")


if __name__ == "__main__":
    main()
