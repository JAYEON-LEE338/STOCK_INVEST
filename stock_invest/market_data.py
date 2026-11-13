import logging
import time

import requests

log = logging.getLogger("market")

FNG_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"


def fear_greed() -> float | None:
    """CNN 공포탐욕지수(0~100). 비공식 엔드포인트라 실패 시 None."""
    try:
        r = requests.get(FNG_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        r.raise_for_status()
        return float(r.json()["fear_and_greed"]["score"])
    except Exception as e:
        log.warning("공포탐욕지수 조회 실패: %s", e)
        return None


def vix() -> float | None:
    try:
        import yfinance as yf

        hist = yf.Ticker("^VIX").history(period="5d")
        return float(hist["Close"].dropna().iloc[-1])
    except Exception as e:
        log.warning("VIX 조회 실패: %s", e)
        return None


US_INDICES = {"S&P 500": "^GSPC", "다우": "^DJI", "나스닥": "^IXIC"}
_US = {"t": 0.0, "v": None}


def us_indices() -> dict:
    """미국 지수 {이름: {price, change_pct}} (직전 종가 대비). yfinance, 5분 캐시. 실패한 지수는 error."""
    if _US["v"] is not None and time.time() - _US["t"] < 300:
        return _US["v"]
    import yfinance as yf

    out = {}
    for name, sym in US_INDICES.items():
        try:
            c = yf.Ticker(sym).history(period="5d")["Close"].dropna()
            out[name] = {"price": float(c.iloc[-1]), "change_pct": float((c.iloc[-1] / c.iloc[-2] - 1) * 100)}
        except Exception as e:
            log.warning("%s 조회 실패: %s", name, e)
            out[name] = {"error": str(e)}
    _US.update(t=time.time(), v=out)
    return out
