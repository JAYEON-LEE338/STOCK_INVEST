import os
from pathlib import Path

from dotenv import load_dotenv

PKG_DIR = Path(__file__).resolve().parent
BASE_DIR = PKG_DIR.parent  # 프로젝트 루트 (.env, state, logs 위치)
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
    global MIN_CALL_INTERVAL, TOKEN_FILE, STATE_FILE, DAILY_FILE
    mode = "live" if mode == "live" else "mock"
    MODE, IS_MOCK = mode, mode != "live"
    APP_KEY, APP_SECRET, acct = _creds(mode)
    ACCOUNT_NO, _, ACCOUNT_PROD = acct.partition("-")
    ACCOUNT_PROD = ACCOUNT_PROD or "01"
    BASE_URL = ("https://openapivts.koreainvestment.com:29443" if IS_MOCK
                else "https://openapi.koreainvestment.com:9443")
    MIN_CALL_INTERVAL = 0.6 if IS_MOCK else 0.08  # 초당 호출 제한 대응
    TOKEN_FILE = BASE_DIR / f".token_{mode}.json"
    DAILY_FILE = BASE_DIR / f".daily_{mode}.json"  # 일일 매수 금액 사용액
    # 모의/실전 상태(보유 전략 기록 등)가 섞이지 않게 분리. 모의는 기존 파일 유지.
    STATE_FILE = BASE_DIR / ("state.json" if IS_MOCK else "state_live.json")


set_mode("live" if _ENV_MODE == "live" else "mock")
LOG_DIR = BASE_DIR / "logs"

# 대시보드/전략에서 숨길 보유 종목 (상장폐지 등). 종목명 일부 또는 종목코드, 콤마 구분.
HIDDEN_HOLDINGS = [h.strip() for h in os.getenv("HIDDEN_HOLDINGS", "파나케이아").split(",") if h.strip()]

# ---- 자산 배분 ----
DAY_RATIO = 0.40  # 박스권 + 모멘텀 (50:50)
SCALP_RATIO = 0.20  # 초단타
FEAR_RATIO = 0.40

# ---- 단타 ----
UNIVERSE = {  # 코스피/코스닥 혼합. 종목 구성은 임의 선정이므로 필요에 맞게 조정
    "반도체": ["005930", "000660", "000990"],
    "반도체소부장": ["042700", "058470", "403870", "240810", "039030", "036930", "319660"],
    "2차전지": ["373220", "006400", "003670", "247540", "086520", "066970", "096770"],
    "로봇": ["277810", "454910", "108490", "058610", "090360"],
    "AI전력": ["267260", "010120", "298040", "062040", "006260", "033100"],
    "신재생에너지": ["009830", "010060", "112610", "034020"],
}
# 시세 API가 종목명을 주지 않을 때 쓰는 이름표
STOCK_NAMES = {
    "005930": "삼성전자", "000660": "SK하이닉스", "000990": "DB하이텍",
    "042700": "한미반도체", "058470": "리노공업", "403870": "HPSP", "240810": "원익IPS",
    "039030": "이오테크닉스", "036930": "주성엔지니어링", "319660": "피에스케이",
    "373220": "LG에너지솔루션", "006400": "삼성SDI", "003670": "포스코퓨처엠", "247540": "에코프로비엠",
    "086520": "에코프로", "066970": "엘앤에프", "096770": "SK이노베이션",
    "277810": "레인보우로보틱스", "454910": "두산로보틱스", "108490": "로보티즈", "058610": "에스피지",
    "090360": "로보스타",
    "267260": "HD현대일렉트릭", "010120": "LS ELECTRIC", "298040": "효성중공업", "062040": "산일전기",
    "006260": "LS", "033100": "제룡전기",
    "009830": "한화솔루션", "010060": "OCI홀딩스", "112610": "씨에스윈드", "034020": "두산에너빌리티",
    "0219E0": "KODEX 200 커버드콜 액티브",
}
MIN_MARKET_CAP = 5000  # 시가총액 하한 (억원)
PICK_COUNT = 2
MAX_CHANGE_PCT = 20.0  # 이 이상 등락률은 과열로 제외
DAY_SPLIT_BOX = 0.5  # 단타 예산 중 박스권 전략 몫
DAY_SPLIT_MOM = 0.5  # 단타 예산 중 모멘텀 전략 몫
SELL_TICKS_BELOW = 2

# 박스권 전략 (코스피 기준)
BOX_DAYS = 20  # 박스 산정 기간(거래일)
BOX_UPPER_Q = 0.95  # 상단 = 고가의 95백분위
BOX_LOWER_Q = 0.05  # 하단 = 저가의 5백분위
BOX_MIN_WIDTH = 0.05  # (상단-하단)/하단 이 이보다 좁으면 박스 아님
BOX_BUY_POS = 0.20  # 박스 내 위치 20% 이하(하단 근접)면 종가 매수
BOX_MIN_POS = -0.10  # 하단 이탈이 이보다 크면(-10%) 매수 안 함
BOX_SELL_LADDER = [0.70, 0.80, 0.90, 1.00]  # 상단 근접 단계마다
BOX_SELL_RATIO = 0.25  # 25%씩 매도
BOX_STOP_PCT = -5.0  # 평균 매수가 대비 이 수익률 이하면 전량 손절

# 모멘텀 전략 (10시 기준)
MOM_INDEX_MIN = 1.0  # 코스피 등락률 +1% 이상
# 초단타 전략 (총평가금액의 SCALP_RATIO)
SCALP_MIN_CHANGE = 5.0  # 전일 대비 +5% 이상
SCALP_VOL_RATE = 50.0  # 전일 거래량 대비 누적 거래량 비율(%) 이상이면 급증
SCALP_REQUIRE_MOMENTUM = True  # 모멘텀 매수 조건(코스피 +1%, 주도 섹터, 지수 하락 추세 아님)도 요구
SCALP_MAX_BUYS = 2  # 하루 최대 매수 종목 수
SCALP_STOP_PCT = -3.0  # 이 수익률 이하면 전량 매도
SCALP_BREAKEVEN_RATIO = 0.50  # 마이너스였다가 본전 회복 시 매도 비율
SCALP_TAKE_PROFIT = [(3.0, 0.30), (6.0, 0.30), (9.0, 0.30)]  # (수익률 %, 매도 비율)
SCALP_LOSS_ALERT_MIN = 30  # 매수 후 이 시간(분) 지나도 손실이면 알림
MOM_TREND_MIN = 30  # 지수 추세 판단 기간(분)
MOM_TREND_PCT = 0.2  # 이 % 이상 내리면 하락, 오르면 상승 추세
MOM_PM_INDEX_MIN = 1.0  # 오후 재매수 시 코스피 등락률 기준(%)
BE_ARM_PCT = -2.0  # 이 수익률 이하까지 내려간 적이 있어야 본전 회복 매도가 켜짐 (모멘텀·초단타)
SELL_ALERT_MIN = 3  # 정규장 매도 주문이 이 시간(분) 넘게 미체결이면 알림
BUY_CANCEL_MIN = 10  # 모멘텀·초단타 매수 주문이 이 시간(분) 안에 다 체결되지 않으면 잔량 취소
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
