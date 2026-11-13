"""코스피 박스권 산정. 상단/하단은 최근 N거래일 고가/저가의 백분위로 잡는다."""
import datetime as dt
import logging
import time

import config
import state as st

log = logging.getLogger("box")
_cache = {"t": 0.0, "v": None}


def _levels() -> dict:
    if _cache["v"] and time.time() - _cache["t"] < 1800:
        return _cache["v"]
    import yfinance as yf

    h = yf.Ticker("^KS11").history(period="1y")
    today = dt.date.today()
    h = h[[d < today for d in h.index.date]].tail(config.BOX_DAYS)  # 장중 미완성 봉 제외
    if len(h) < 20:
        raise RuntimeError("코스피 일봉 데이터 부족")
    v = {
        "upper": float(h["High"].quantile(config.BOX_UPPER_Q)),
        "lower": float(h["Low"].quantile(config.BOX_LOWER_Q)),
        "days": len(h),
        "from": str(h.index[0].date()),
        "to": str(h.index[-1].date()),
    }
    _cache.update(t=time.time(), v=v)
    return v


def get(kis) -> dict:
    manual = st.load().get("box_manual")
    if manual:
        lv = {"upper": float(manual["upper"]), "lower": float(manual["lower"]), "source": "manual"}
    else:
        lv = {**_levels(), "source": "auto"}
    cur = kis.index_price("0001")["price"]
    span = lv["upper"] - lv["lower"]
    width = span / lv["lower"]
    return {
        **lv,
        "current": cur,
        "pos": (cur - lv["lower"]) / span,  # 0=하단, 1=상단
        "width": width,
        "valid": width >= config.BOX_MIN_WIDTH,
        "buy_pos": config.BOX_BUY_POS,
        "min_pos": config.BOX_MIN_POS,
        "ladder": config.BOX_SELL_LADDER,
    }
