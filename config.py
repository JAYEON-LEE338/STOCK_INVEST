import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "y")


# ---- 접속 ----
_ENV_MODE = os.getenv("MODE", "mock").lower()
DRY_RUN = _bool("DRY_RUN", True)


def _creds(mode: str) -> tuple[str, str, str]:
    """모드별 키. KIS_MOCK_* / KIS_LIVE_* 를 우선 쓰고, 없으면 .env 의 MODE 와 같은 모드에 한해 KIS_* 를 쓴다."""
    p = f"KIS_{mode.upper()}_"
    fb = mode == _ENV_MODE
    key = os.getenv(p + "APP_KEY") or (os.getenv("KIS_APP_KEY", "") if fb else "")
    sec = os.getenv(p + "APP_SECRET") or (os.getenv("KIS_APP_SECRET", "") if fb else "")
    acct = os.getenv(p + "ACCOUNT_NO") or (os.getenv("KIS_ACCOUNT_NO", "") if fb else "")
    return key, sec, acct


def has_creds(mode: str) -> bool:
    return all(_creds(mode))


def set_mode(mode: str) -> None:
    """모의/실전 전환. 이후 모든 모듈이 config.X 로 읽으므로 즉시 반영된다."""
    global MODE, IS_MOCK, APP_KEY, APP_SECRET, ACCOUNT_NO, ACCOUNT_PROD, BASE_URL
    global MIN_CALL_INTERVAL, TOKEN_FILE, STATE_FILE
    mode = "live" if mode == "live" else "mock"
    MODE, IS_MOCK = mode, mode != "live"
    APP_KEY, APP_SECRET, acct = _creds(mode)
    ACCOUNT_NO, _, ACCOUNT_PROD = acct.partition("-")
    ACCOUNT_PROD = ACCOUNT_PROD or "01"
    BASE_URL = ("https://openapivts.koreainvestment.com:29443" if IS_MOCK
                else "https://openapi.koreainvestment.com:9443")
    MIN_CALL_INTERVAL = 0.6 if IS_MOCK else 0.08  # 초당 호출 제한 대응
    TOKEN_FILE = BASE_DIR / f".token_{mode}.json"
    # 모의/실전 상태(보유 전략 기록 등)가 섞이지 않게 분리. 모의는 기존 파일 유지.
    STATE_FILE = BASE_DIR / ("state.json" if IS_MOCK else "state_live.json")


set_mode("live" if _ENV_MODE == "live" else "mock")
LOG_DIR = BASE_DIR / "logs"

# 대시보드/전략에서 숨길 보유 종목 (상장폐지 등). 종목명 일부 또는 종목코드, 콤마 구분.
HIDDEN_HOLDINGS = [h.strip() for h in os.getenv("HIDDEN_HOLDINGS", "파나케이아").split(",") if h.strip()]

# ---- 자산 배분 ----
DAY_RATIO = 0.30
FEAR_RATIO = 0.70

# ---- 단타 ----
UNIVERSE = {
    "반도체": ["005930", "000660", "042700", "058470", "403870", "240810", "039030"],
    "2차전지": ["373220", "006400", "003670", "247540", "086520", "066970", "096770"],
}
PICK_COUNT = 2
MAX_CHANGE_PCT = 20.0  # 이 이상 등락률은 과열로 제외
DAY_SPLIT_BOX = 0.5  # 단타 예산 중 박스권 전략 몫
DAY_SPLIT_MOM = 0.5  # 단타 예산 중 모멘텀 전략 몫
SELL_TICKS_BELOW = 2

# 박스권 전략 (코스피 기준)
BOX_DAYS = 60  # 박스 산정 기간(거래일)
BOX_UPPER_Q = 0.95  # 상단 = 고가의 95백분위
BOX_LOWER_Q = 0.05  # 하단 = 저가의 5백분위
BOX_MIN_WIDTH = 0.05  # (상단-하단)/하단 이 이보다 좁으면 박스 아님
BOX_BUY_POS = 0.20  # 박스 내 위치 20% 이하(하단 근접)면 종가 매수
BOX_MIN_POS = -0.10  # 하단 이탈이 이보다 크면(-10%) 매수 안 함
BOX_SELL_LADDER = [0.70, 0.80, 0.90, 1.00]  # 상단 근접 단계마다
BOX_SELL_RATIO = 0.25  # 25%씩 매도

# 모멘텀 전략 (10시 기준)
MOM_INDEX_MIN = 1.0  # 코스피 등락률 +1% 이상
MOM_ROUNDTRIP_COST = 0.0025  # 왕복 수수료+세금 (수익률 계산 시 차감)
MOM_STOP_PCT = -3.0  # 수익률 -3% 이하면 손절
MOM_STOP_RATIO = 0.50  # 손절 시 매도 비율 (최초 수량 기준)
MOM_STOP2_WAIT_MIN = 5  # 1차 손절 후 이 시간(분)이 지나서도 -3% 이하면 2차 손절
MOM_STOP2_RATIO = 0.50  # 2차 손절 매도 비율 (최초 수량 기준)
MOM_BREAKEVEN_RATIO = 0.30  # 마이너스였다가 본전 회복 시 매도 비율
MOM_TAKE_PROFIT = [(3.0, 0.30), (6.0, 0.30)]  # (수익률 %, 매도 비율)
NXT_PRICE_MARKET = os.getenv("NXT_PRICE_MARKET", "NX")
NXT_ORDER_EXCHANGE = os.getenv("NXT_ORDER_EXCHANGE", "NXT")

# ---- 공포지수 ----
COVERED_CALL_CODE = os.getenv("COVERED_CALL_CODE", "")
VIX_BUY_MIN = 28
BUY_TRANCHE = 0.25  # 공포 계좌 예산 대비 1회 매수 비중
# VIX가 이 값 이하로 내려갈 때마다 1단계씩(하루 1회) 매도
VIX_SELL_LADDER = [15, 14.5, 14, 13.5, 13, 12.5, 12, 11.5, 11, 10.5]
SELL_STEP_RATIO = 0.10

# ---- 대시보드 접속 ----
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
# 콤마로 구분한 바인딩 주소. 127.0.0.1 외 주소는 DASHBOARD_PASSWORD 가 있어야 열린다.
DASHBOARD_HOSTS = [h.strip() for h in os.getenv("DASHBOARD_HOSTS", "127.0.0.1").split(",") if h.strip()]

# ---- 알림 (텔레그램) ----
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ---- 안전장치 ----
MAX_ORDER_AMOUNT = int(os.getenv("MAX_ORDER_AMOUNT", "3000000"))
MAX_DAILY_AMOUNT = int(os.getenv("MAX_DAILY_AMOUNT", "10000000"))
