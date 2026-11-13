import datetime as dt
import logging
import sys

import holidays
from apscheduler.schedulers.blocking import BlockingScheduler

import config
import daytrade
import fear_strategy
import notify
import report
from kis_client import KIS

TZ = "Asia/Seoul"
KR_HOLIDAYS = holidays.KR()

# (함수, 표시 이름, 시, 분)
JOBS = [
    (daytrade.sell_fallback, "전일 모멘텀 잔여 정리", 9, 5),
    (report.daily_status, "10시 현황 알림", 10, 0),
    (daytrade.momentum_buy, "모멘텀 매수 (10시 기준)", 10, 1),
    (daytrade.momentum_buy_pm, "모멘텀 오후 재매수 (14:00~14:50, 10분 간격)", 14, 0),
    (daytrade.box_sell, "박스권 상단 분할 매도", 15, 0),
    (fear_strategy.run, "VIX 공포지수 매매", 15, 10),
    (daytrade.box_buy, "박스권 하단 종가 매수", 15, 20),
    (daytrade.momentum_sell_evening, "모멘텀 NXT 매도", 19, 30),
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
    for fn, _label, h, m in JOBS:
        sched.add_job(guarded(fn, kis, lock), "cron", day_of_week="mon-fri", hour=h, minute=m,
                      misfire_grace_time=120, id=fn.__name__)
    sched.add_job(guarded(daytrade.momentum_buy_pm, kis, lock), "cron", day_of_week="mon-fri", hour=14,
                  minute="10,20,30,40,50", misfire_grace_time=120, id="momentum_buy_pm_more")
    # 모멘텀 손절/익절 감시: 매 분 (함수 내부에서 10:02~15:19 만 동작)
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
