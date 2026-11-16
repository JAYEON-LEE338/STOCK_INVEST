import datetime as dt
import json
import logging
import time

import requests

from . import config
from . import notify

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
        self._load_token()

    # ---------- 일일 매수 사용액 (파일에 저장해 재시작해도 유지) ----------
    def _daily_used(self) -> int:
        try:
            d = json.loads(config.DAILY_FILE.read_text())
            return int(d["amount"]) if d.get("date") == dt.date.today().isoformat() else 0
        except Exception:
            return 0

    def _add_daily(self, amount: int) -> None:
        used = self._daily_used() + int(amount)
        config.DAILY_FILE.write_text(json.dumps({"date": dt.date.today().isoformat(), "amount": used}))

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
            "mcap": int(o.get("hts_avls") or 0),  # 시가총액(억원)
            "vol_rate": float(o.get("prdy_vrss_vol_rate") or 0),  # 전일 거래량 대비 누적 거래량(%)
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

    def index_minutes(self, index_code: str = "0001") -> list:
        """오늘 업종지수 1분봉 [(datetime, 지수)] 최신순 (최근 약 100분)."""
        j = self._request(
            "GET", "/uapi/domestic-stock/v1/quotations/inquire-time-indexchartprice", "FHKUP03500200",
            params={"FID_COND_MRKT_DIV_CODE": "U", "FID_ETC_CLS_CODE": "0", "FID_INPUT_ISCD": index_code,
                    "FID_INPUT_HOUR_1": "60", "FID_PW_DATA_INCU_YN": "Y"},
        )
        today = dt.date.today().strftime("%Y%m%d")
        out = []
        for x in j.get("output2", []):
            h = x.get("stck_cntg_hour", "")
            if x.get("stck_bsop_date") != today or not h.isdigit() or h == "888888":
                continue
            out.append((dt.datetime.strptime(today + h, "%Y%m%d%H%M%S"), float(x["bstp_nmix_prpr"])))
        out.sort(reverse=True)
        return out

    def index_change(self, index_code: str = "0001") -> float:
        return self.index_price(index_code)["change_pct"]

    def daily_ohlc(self, code: str, days: int = 40) -> list:
        """오늘 제외 일봉 [{date, high, low, close, volume}] (최신순). 약 100거래일까지."""
        end = dt.date.today()
        j = self._request(
            "GET", "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice", "FHKST03010100",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                    "FID_INPUT_DATE_1": (end - dt.timedelta(days=int(days * 1.6) + 10)).strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                    "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"},
        )
        today = end.strftime("%Y%m%d")
        return [{"date": x["stck_bsop_date"], "high": int(x["stck_hgpr"]), "low": int(x["stck_lwpr"]),
                 "close": int(x["stck_clpr"]), "volume": int(x["acml_vol"])}
                for x in j.get("output2", []) if x.get("stck_bsop_date") and x["stck_bsop_date"] != today][:days]

    def foreign_netbuy(self, code: str, days: int = 60) -> list:
        """외국인 일별 순매수 수량 (최신순). 한 번에 30거래일씩이라 날짜를 거슬러 올라가며 이어 받는다."""
        out, seen, end = [], set(), dt.date.today()
        for _ in range(5):
            j = self._request(
                "GET", "/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily", "FHPTJ04160001",
                params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                        "FID_INPUT_DATE_1": end.strftime("%Y%m%d"), "FID_ORG_ADJ_PRC": "", "FID_ETC_CLS_CODE": ""},
            )
            rows = [x for x in j.get("output2", []) if x.get("stck_bsop_date") and x["stck_bsop_date"] != dt.date.today().strftime("%Y%m%d")]
            new = [x for x in rows if x["stck_bsop_date"] not in seen]
            if not new:
                break
            for x in new:
                seen.add(x["stck_bsop_date"])
                out.append((x["stck_bsop_date"], int(x.get("frgn_ntby_qty") or 0)))
            if len(out) >= days:
                break
            oldest = min(x["stck_bsop_date"] for x in new)
            end = dt.datetime.strptime(oldest, "%Y%m%d").date() - dt.timedelta(days=1)
        out.sort(reverse=True)
        return [q for _, q in out[:days]]

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
            holdings[code] = {"qty": qty, "price": px, "name": name,
                              "avg": float(row.get("pchs_avg_pric") or 0)}
        s = j["output2"][0]
        return {"holdings": holdings, "cash": int(s["dnca_tot_amt"]),
                "total": int(s["tot_evlu_amt"]) - hidden_val}

    # ---------- 주문 ----------
    def order(self, side: str, code: str, qty: int, price: int = 0, exchange: str = "KRX"):
        """side: buy/sell. price=0 이면 시장가(매수는 매수 2호가 지정가로 바뀜). DRY_RUN 이면 로그만.
        성공 시 주문 정보 dict (truthy), 실패 시 False.
        {"odno", "orgno", "code", "side", "qty", "price", "dry"}"""
        if qty <= 0:
            return False
        if side == "buy" and price == 0:  # 매수는 시장가 금지: 매수 2호가 지정가
            try:
                price = self.bid_price(code, 2)
            except Exception as e:
                log.error("매수 2호가 조회 실패로 매수 취소 %s: %s", code, e)
                notify.send(f"⚠️ 매수 취소 {code}: 매수 2호가 조회 실패 ({e})")
                return False
        info = self.price(code)
        ref_price = price or info["price"]
        amount = ref_price * qty
        name = info.get("name") or code

        if side == "buy":
            if amount > config.MAX_ORDER_AMOUNT:
                log.error("1회 주문 한도 초과로 거부: %s %s주 (%s원)", code, qty, amount)
                return False
            if self._daily_used() + amount > config.MAX_DAILY_AMOUNT:
                log.error("일일 주문 한도 초과로 거부: %s", code)
                return False

        tag = f"[{'DRY' if config.DRY_RUN else config.MODE}] {side} {code} x{qty} @{price or '시장가'} ({exchange})"
        msg = (f"{'🟢 매수' if side == 'buy' else '🔴 매도'} {name}({code}) {qty}주 @ {price:,}원" if price
               else f"{'🟢 매수' if side == 'buy' else '🔴 매도'} {name}({code}) {qty}주 @ 시장가")
        msg += f"\n약 {amount:,}원 · {exchange}"

        if config.DRY_RUN:
            log.info(tag)
            if side == "buy":
                self._add_daily(amount)
            notify.send(msg + " · DRY_RUN(실제 주문 안 나감)")
            return {"odno": "DRY", "orgno": "", "code": code, "side": side, "qty": qty,
                    "price": ref_price, "dry": True, "exchange": exchange}

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
            self._add_daily(amount)
        out = j.get("output", {})
        log.info("%s 주문번호=%s", tag, out.get("ODNO"))
        notify.send(msg + f" · 주문번호 {out.get('ODNO')}")
        return {"odno": out.get("ODNO", ""), "orgno": out.get("KRX_FWDG_ORD_ORGNO", ""), "code": code,
                "side": side, "qty": qty, "price": ref_price, "dry": False, "exchange": exchange}

    def order_fills(self, date: str | None = None) -> dict:
        """당일 주문체결 내역 {주문번호: {code, side, ord_qty, ccld_qty, avg, rmn_qty, cancelled}}."""
        date = date or dt.date.today().strftime("%Y%m%d")
        tr = "VTTC0081R" if config.IS_MOCK else "TTTC0081R"
        out, fk, nk = {}, "", ""
        for _ in range(10):  # 연속조회
            j = self._request(
                "GET", "/uapi/domestic-stock/v1/trading/inquire-daily-ccld", tr,
                params={"CANO": config.ACCOUNT_NO, "ACNT_PRDT_CD": config.ACCOUNT_PROD,
                        "INQR_STRT_DT": date, "INQR_END_DT": date, "SLL_BUY_DVSN_CD": "00",
                        "INQR_DVSN": "00", "PDNO": "", "CCLD_DVSN": "00", "ORD_GNO_BRNO": "", "ODNO": "",
                        "INQR_DVSN_3": "00", "INQR_DVSN_1": "", "CTX_AREA_FK100": fk, "CTX_AREA_NK100": nk,
                        "EXCG_ID_DVSN_CD": "ALL"},
            )
            for r in j.get("output1", []):
                out[r.get("odno", "")] = {
                    "code": r.get("pdno", ""), "side": "sell" if r.get("sll_buy_dvsn_cd") == "01" else "buy",
                    "ord_qty": int(r.get("ord_qty") or 0), "ccld_qty": int(r.get("tot_ccld_qty") or 0),
                    "avg": float(r.get("avg_prvs") or 0), "rmn_qty": int(r.get("rmn_qty") or 0),
                    "cancelled": r.get("cncl_yn") == "Y"}
            fk, nk = (j.get("ctx_area_fk100") or "").strip(), (j.get("ctx_area_nk100") or "").strip()
            if not nk or not j.get("output1"):
                break
        return out

    def cancel(self, orgno: str, odno: str, exchange: str = "KRX") -> bool:
        """미체결 주문 잔량 전부 취소."""
        if config.DRY_RUN:
            log.info("[DRY] 주문 취소 %s", odno)
            return True
        tr = "VTTC0013U" if config.IS_MOCK else "TTTC0013U"
        body = {"CANO": config.ACCOUNT_NO, "ACNT_PRDT_CD": config.ACCOUNT_PROD,
                "KRX_FWDG_ORD_ORGNO": orgno, "ORGN_ODNO": odno, "ORD_DVSN": "00",
                "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": "0", "ORD_UNPR": "0",
                "QTY_ALL_ORD_YN": "Y", "EXCG_ID_DVSN_CD": exchange}
        self._request("POST", "/uapi/domestic-stock/v1/trading/order-rvsecncl", tr, body=body)
        log.info("주문 취소 접수 %s", odno)
        return True
