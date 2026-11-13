import logging

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
