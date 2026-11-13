import datetime as dt
import json
import logging
import time

import requests

import config
import notify

log = logging.getLogger("kis")


def tick_size(price: int, etf: bool = False) -> int:
    """호가단위. ETF는 2,000원 이상 5원."""
    if etf:
        return 1 if price < 2000 else 5
    for limit, tick in ((2000, 1), (5000, 5), (20000, 10), (50000, 50), (200000, 100), (500000, 500)):
        if price < limit:
            return tick
    return 1000


class KIS:
    def __init__(self):
        if not (config.APP_KEY and config.APP_SECRET and config.ACCOUNT_NO):
            raise RuntimeError(".env 에 KIS_APP_KEY / KIS_APP_SECRET / KIS_ACCOUNT_NO 를 설정하세요.")
        self.token = None
        self._last = 0.0
        self.daily_amount = 0
        self.daily_date = dt.date.today()
        self._load_token()

    # ---------- 인증 ----------
    def reload(self):
        """모의/실전 전환 후 새 모드의 토큰으로 교체."""
        self.token = None
        self._load_token()

    def _load_token(self):
        f = config.TOKEN_FILE
        if f.exists():
            try:
                d = json.loads(f.read_text())
                if dt.datetime.fromisoformat(d["expires"]) > dt.datetime.now() + dt.timedelta(minutes=10):
                    self.token = d["token"]
                    return
            except Exception:
                pass
        self._issue_token()

    def _issue_token(self):
        r = requests.post(
            f"{config.BASE_URL}/oauth2/tokenP",
            json={"grant_type": "client_credentials", "appkey": config.APP_KEY, "appsecret": config.APP_SECRET},
            timeout=10,
        )
        r.raise_for_status()
        j = r.json()
        self.token = j["access_token"]
        exp = dt.datetime.strptime(j["access_token_token_expired"], "%Y-%m-%d %H:%M:%S")
        config.TOKEN_FILE.write_text(json.dumps({"token": self.token, "expires": exp.isoformat()}))
        log.info("토큰 발급 완료 (만료 %s)", exp)

    # ---------- 공통 요청 ----------
    def _request(self, method, path, tr_id, params=None, body=None, retries=3):
        for attempt in range(retries):
            wait = config.MIN_CALL_INTERVAL - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            headers = {
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {self.token}",
                "appkey": config.APP_KEY,
                "appsecret": config.APP_SECRET,
                "tr_id": tr_id,
                "custtype": "P",
            }
            r = requests.request(method, config.BASE_URL + path, headers=headers,
                                 params=params, data=json.dumps(body) if body else None, timeout=10)
            try:
                j = r.json()
            except Exception:
                j = {}
            if j.get("rt_cd") == "0":
                return j
            msg_cd = j.get("msg_cd", "")
            if msg_cd == "EGW00201":  # 초당 호출 제한
                time.sleep(1)
                continue
            if msg_cd in ("EGW00123", "EGW00121"):  # 토큰 만료/오류
                self._issue_token()
                continue
            raise RuntimeError(f"KIS 오류 {path} [{msg_cd}] {j.get('msg1', r.text[:200])}")
        raise RuntimeError(f"KIS 재시도 초과: {path}")

    # ---------- 시세 ----------
    def price(self, code: str, market: str = "J") -> dict:
        j = self._request(
            "GET", "/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
            params={"FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": code},
        )
        o = j["output"]
        return {
            "code": code,
            "name": o.get("hts_kor_isnm") or config.STOCK_NAMES.get(code, code),
            "price": int(o["stck_prpr"]),
            "change_pct": float(o["prdy_ctrt"]),
            "value": int(o.get("acml_tr_pbmn") or 0),
        }

    def daily_closes(self, code: str, days: int = 10) -> list[int]:
        """최근 종가 (최신순)."""
        j = self._request(
            "GET", "/uapi/domestic-stock/v1/quotations/inquire-daily-price", "FHKST01010400",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                    "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "1"},
        )
        return [int(x["stck_clpr"]) for x in j["output"][:days]]

    def index_price(self, index_code: str = "0001") -> dict:
        """업종지수 현재값과 등락률(%). 0001=코스피, 1001=코스닥."""
        j = self._request(
            "GET", "/uapi/domestic-stock/v1/quotations/inquire-index-price", "FHPUP02100000",
            params={"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": index_code},
        )
        o = j["output"]
        return {"price": float(o["bstp_nmix_prpr"]), "change_pct": float(o["bstp_nmix_prdy_ctrt"])}

    def index_change(self, index_code: str = "0001") -> float:
        return self.index_price(index_code)["change_pct"]

    def daily_bars(self, code: str, days: int = 10) -> list[dict]:
        """일봉 (최신순): date(YYYYMMDD), close, volume."""
        j = self._request(
            "GET", "/uapi/domestic-stock/v1/quotations/inquire-daily-price", "FHKST01010400",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                    "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "1"},
        )
        return [{"date": x["stck_bsop_date"], "close": int(x["stck_clpr"]), "volume": int(x["acml_vol"])}
                for x in j["output"][:days]]

    def volume_until(self, code: str, date: str, hhmmss: str = "100000") -> tuple[int, int]:
        """해당일 정규장 09:01~hhmmss 1분봉 (봉 개수, 누적 거래량). date=YYYYMMDD."""
        j = self._request(
            "GET", "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice", "FHKST03010230",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code, "FID_INPUT_HOUR_1": hhmmss,
                    "FID_INPUT_DATE_1": date, "FID_PW_DATA_INCU_YN": "Y", "FID_FAKE_TICK_INCU_YN": ""},
        )
        bars = [x for x in j.get("output2", [])
                if x.get("stck_bsop_date") == date and "090000" < x["stck_cntg_hour"] <= hhmmss]
        return len(bars), sum(int(x["cntg_vol"]) for x in bars)

    # ---------- 계좌 ----------
    def balance(self) -> dict:
        tr = "VTTC8434R" if config.IS_MOCK else "TTTC8434R"
        j = self._request(
            "GET", "/uapi/domestic-stock/v1/trading/inquire-balance", tr,
            params={"CANO": config.ACCOUNT_NO, "ACNT_PRDT_CD": config.ACCOUNT_PROD,
                    "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01",
                    "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
                    "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""},
        )
        holdings, hidden_val = {}, 0
        for row in j.get("output1", []):
            qty = int(row["hldg_qty"])
            if qty <= 0:
                continue
            code, name, px = row["pdno"], row["prdt_name"], int(row["prpr"])
            if any(h == code or h in name for h in config.HIDDEN_HOLDINGS):
                hidden_val += qty * px  # 상장폐지 등 숨김 종목: 목록/예산 계산에서 제외
                continue
            holdings[code] = {"qty": qty, "price": px, "name": name}
        s = j["output2"][0]
        return {"holdings": holdings, "cash": int(s["dnca_tot_amt"]),
                "total": int(s["tot_evlu_amt"]) - hidden_val}

    # ---------- 주문 ----------
    def order(self, side: str, code: str, qty: int, price: int = 0, exchange: str = "KRX") -> bool:
        """side: buy/sell. price=0 이면 시장가. DRY_RUN 이면 로그만."""
        if qty <= 0:
            return False
        info = self.price(code)
        ref_price = price or info["price"]
        amount = ref_price * qty
        name = info.get("name") or code

        today = dt.date.today()
        if today != self.daily_date:
            self.daily_date, self.daily_amount = today, 0
        if side == "buy":
            if amount > config.MAX_ORDER_AMOUNT:
                log.error("1회 주문 한도 초과로 거부: %s %s주 (%s원)", code, qty, amount)
                return False
            if self.daily_amount + amount > config.MAX_DAILY_AMOUNT:
                log.error("일일 주문 한도 초과로 거부: %s", code)
                return False

        tag = f"[{'DRY' if config.DRY_RUN else config.MODE}] {side} {code} x{qty} @{price or '시장가'} ({exchange})"
        msg = (f"{'🟢 매수' if side == 'buy' else '🔴 매도'} {name}({code}) {qty}주 @ {price:,}원" if price
               else f"{'🟢 매수' if side == 'buy' else '🔴 매도'} {name}({code}) {qty}주 @ 시장가")
        msg += f"\n약 {amount:,}원 · {exchange}"

        if config.DRY_RUN:
            log.info(tag)
            if side == "buy":
                self.daily_amount += amount
            notify.send(msg + " · DRY_RUN(실제 주문 안 나감)")
            return True

        if config.IS_MOCK:
            tr = "VTTC0012U" if side == "buy" else "VTTC0011U"
        else:
            tr = "TTTC0012U" if side == "buy" else "TTTC0011U"
        body = {
            "CANO": config.ACCOUNT_NO, "ACNT_PRDT_CD": config.ACCOUNT_PROD,
            "PDNO": code, "ORD_DVSN": "01" if price == 0 else "00",
            "ORD_QTY": str(qty), "ORD_UNPR": str(price),
            "EXCG_ID_DVSN_CD": exchange,
        }
        j = self._request("POST", "/uapi/domestic-stock/v1/trading/order-cash", tr, body=body)
        if side == "buy":
            self.daily_amount += amount
        log.info("%s 주문번호=%s", tag, j.get("output", {}).get("ODNO"))
        notify.send(msg + f" · 주문번호 {j.get('output', {}).get('ODNO')}")
        return True
