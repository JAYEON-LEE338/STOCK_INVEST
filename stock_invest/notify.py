"""텔레그램 알림. TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 없으면 로그만 남긴다."""
import logging

import requests

from . import config

log = logging.getLogger("notify")


def send(text: str) -> bool:
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        log.info("알림(텔레그램 미설정): %s", text.splitlines()[0])
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text[:4000]},
            timeout=8,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        # 예외 메시지에는 토큰이 들어간 URL 이 포함되므로 종류만 기록한다
        log.warning("텔레그램 전송 실패: %s", type(e).__name__)
        return False
