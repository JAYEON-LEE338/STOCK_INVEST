"""웹 대시보드 + 스케줄러. 실행: python app.py -> http://127.0.0.1:8765

외부(Tailscale) 접속: .env 의 DASHBOARD_HOSTS 에 Tailscale IP 를 넣고 DASHBOARD_PASSWORD 를 설정한다.
- 로컬(127.0.0.1)에서 온 요청은 로그인 없이 허용, 그 외는 비밀번호 로그인 필요.
- 로그인 5회 실패 시 해당 IP 5분 차단 + 텔레그램 알림.
"""
import datetime as dt
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from apscheduler.schedulers.background import BackgroundScheduler

import box as boxmod
import config
import daytrade
import main as core
import market_data
import notify
import state as st
from kis_client import KIS

PORT = 8765
LOCK = threading.RLock()  # KIS 호출/잡 실행 직렬화
CACHE = {"t": 0.0, "data": None}
CACHE_SEC = 15
SESSIONS: dict[str, float] = {}  # {토큰: 만료시각}
SESSION_SEC = 12 * 3600
FAILS: dict[str, list] = {}  # {ip: [실패횟수, 차단해제시각]}
log = logging.getLogger("app")

LOGIN_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>로그인</title>
<style>:root{--bg:#f4f5f7;--card:#fff;--text:#1d2330;--line:#d1d5db;--accent:#2563eb;--bad:#b91c1c;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#0f1218;--card:#181c25;--text:#e5e7eb;--line:#2a303c;--accent:#60a5fa;--bad:#f87171;color-scheme:dark}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);font:14px -apple-system,"Segoe UI","Malgun Gothic",sans-serif;padding:16px}
form{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px;width:100%;max-width:320px;display:grid;gap:12px}
h1{font-size:18px;margin:0}input{padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text);font:inherit}
button{padding:10px;border:0;border-radius:8px;background:var(--accent);color:#fff;font:inherit;cursor:pointer}.err{color:var(--bad);font-size:13px}</style></head>
<body><form method="post" action="/login"><h1>STOCK_INVEST</h1>
<input type="password" name="password" placeholder="비밀번호" autofocus required>
<button>로그인</button><div class="err">{{ERR}}</div></form></body></html>"""


PW_FILE = config.BASE_DIR / ".dashboard_pw.json"  # 대시보드에서 바꾼 비밀번호(해시). 있으면 .env 값보다 우선


def _hash_pw(pw: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000).hex()


def pw_configured() -> bool:
    return PW_FILE.exists() or bool(config.DASHBOARD_PASSWORD)


def check_pw(pw: str) -> bool:
    if PW_FILE.exists():
        try:
            d = json.loads(PW_FILE.read_text())
            return hmac.compare_digest(_hash_pw(pw, bytes.fromhex(d["salt"])), d["hash"])
        except Exception:
            return False
    return bool(config.DASHBOARD_PASSWORD) and hmac.compare_digest(pw.encode(), config.DASHBOARD_PASSWORD.encode())


def set_pw(pw: str) -> None:
    salt = secrets.token_bytes(16)
    PW_FILE.write_text(json.dumps({"salt": salt.hex(), "hash": _hash_pw(pw, salt)}))


def safe(fn):
    try:
        return fn()
    except Exception as e:
        return {"error": str(e)}


def tail_log(n=80):
    f = config.LOG_DIR / "trade.log"
    if not f.exists():
        return []
    try:
        return f.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except Exception:
        return []


def collect(kis, sched):
    s = st.load()
    d = {
        "now": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": config.MODE, "dry_run": config.DRY_RUN,
        "day_ratio": config.DAY_RATIO, "fear_ratio": config.FEAR_RATIO,
        "vix_buy": config.VIX_BUY_MIN, "vix_sell_ladder": config.VIX_SELL_LADDER,
        "covered_call_code": config.COVERED_CALL_CODE,
    }
    with LOCK:
        bal = safe(kis.balance)
        d["balance"] = bal
        if "error" not in bal:
            d["budgets"] = {"day": int(bal["total"] * config.DAY_RATIO), "fear": int(bal["total"] * config.FEAR_RATIO)}
        d["vix"] = safe(market_data.vix)
        d["kospi"] = safe(lambda: kis.index_change("0001"))
        d["cc"] = safe(lambda: kis.price(config.COVERED_CALL_CODE)) if config.COVERED_CALL_CODE else None
        d["box"] = safe(lambda: boxmod.get(kis))
    d["mom_cfg"] = {
        "budget_ratio": config.DAY_RATIO * config.DAY_SPLIT_MOM, "pick": config.PICK_COUNT,
        "index_min": config.MOM_INDEX_MIN, "max_change": config.MAX_CHANGE_PCT,
        "cost": config.MOM_ROUNDTRIP_COST * 100,
        "stop_pct": config.MOM_STOP_PCT, "stop_ratio": config.MOM_STOP_RATIO,
        "stop2_wait": config.MOM_STOP2_WAIT_MIN, "stop2_ratio": config.MOM_STOP2_RATIO,
        "be_ratio": config.MOM_BREAKEVEN_RATIO, "tp": config.MOM_TAKE_PROFIT,
        "nxt_ticks": config.SELL_TICKS_BELOW, "trend_min": config.MOM_TREND_MIN,
        "trend_pct": config.MOM_TREND_PCT, "pm_index_min": config.MOM_PM_INDEX_MIN,
    }
    d["signals"] = s.get("signals", {})
    d["box_positions"] = s.get("box_positions", {})
    d["mom_positions"] = s.get("mom_positions", {})
    d["fear"] = s.get("fear", {})
    jobs = {j.id: j for j in sched.get_jobs()}
    d["jobs"] = []
    for fn, label, h, m in core.JOBS:
        j = jobs.get(fn.__name__)
        nxt = j.next_run_time.strftime("%m-%d %H:%M") if j and j.next_run_time else "-"
        d["jobs"].append({"id": fn.__name__, "label": label, "time": f"{h:02d}:{m:02d}", "next": nxt})
    d["logs"] = tail_log()
    return d


MOM_CAND = {"t": 0.0, "data": None}


def mom_candidates(kis, n=5):
    """모멘텀 선정 조건에 가까운 종목 상위 n개. 조건: 주도 섹터 / 상승 중 / 과열 아님, 순위는 거래대금."""
    data = daytrade._sector_rows(kis)
    if not data:
        return {"error": "시세 조회 실패"}
    avg = {s: sum(r["change_pct"] for r in rows) / len(rows) for s, rows in data.items()}
    lead = max(avg, key=avg.get)
    out = []
    for sector, rows in data.items():
        for r in rows:
            c_sector = sector == lead
            c_up = r["change_pct"] > 0
            c_hot = r["change_pct"] < config.MAX_CHANGE_PCT
            out.append({"code": r["code"], "name": r["name"], "sector": sector, "price": r["price"],
                        "change_pct": r["change_pct"], "value": r["value"],
                        "c_sector": c_sector, "c_up": c_up, "c_hot": c_hot,
                        "met": int(c_sector) + int(c_up) + int(c_hot)})
    # 충족 조건 수 -> 주도 섹터 우선 -> 거래대금 순
    out.sort(key=lambda x: (x["met"], x["c_sector"], x["value"]), reverse=True)
    top = out[:n]
    trend = daytrade.index_trend()
    for i, x in enumerate(top):
        x["pick"] = (x["met"] == 3 and trend["trend"] != "down"
                     and sum(1 for y in top[:i] if y["met"] == 3) < config.PICK_COUNT)
    return {"lead": lead, "sector_avg": {k: round(v, 2) for k, v in avg.items()},
            "index_pct": safe(lambda: kis.index_change("0001")), "trend": trend, "items": top}


def make_handler(kis, sched, allowed_hosts):
    job_map = {fn.__name__: fn for fn, *_ in core.JOBS}
    html = (config.BASE_DIR / "ui.html").read_bytes()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # 접속 로그 생략
            pass

        # ---------- 공통 ----------
        def _local(self):
            return self.client_address[0] in ("127.0.0.1", "::1")

        def _host_ok(self):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in allowed_hosts

        def _authed(self):
            if self._local():
                return True
            if not pw_configured():
                return False
            tok = SimpleCookie(self.headers.get("Cookie") or "").get("sid")
            exp = SESSIONS.get(tok.value) if tok else None
            return bool(exp and exp > time.time())

        def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, to, headers=None):
            self._send(302, b"", "text/plain", {"Location": to, **(headers or {})})

        def _login_page(self, code=200, err=""):
            self._send(code, LOGIN_HTML.replace("{{ERR}}", err).encode(), "text/html; charset=utf-8")

        # ---------- 로그인 ----------
        def _do_login(self):
            ip = self.client_address[0]
            n_fail, until = FAILS.get(ip, [0, 0])
            if until > time.time():
                return self._login_page(429, "로그인 시도가 너무 많습니다. 잠시 후 다시 시도하세요.")
            n = min(int(self.headers.get("Content-Length") or 0), 2048)
            pw = parse_qs(self.rfile.read(n).decode("utf-8", "replace")).get("password", [""])[0]
            if check_pw(pw):
                FAILS.pop(ip, None)
                now = time.time()
                for k in [k for k, v in SESSIONS.items() if v < now]:
                    del SESSIONS[k]
                tok = secrets.token_urlsafe(32)
                SESSIONS[tok] = now + SESSION_SEC
                log.info("대시보드 로그인 성공: %s", ip)
                return self._redirect("/", {"Set-Cookie": f"sid={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SEC}"})
            n_fail += 1
            FAILS[ip] = [n_fail, time.time() + 300 if n_fail >= 5 else 0]
            log.warning("대시보드 로그인 실패 %s회: %s", n_fail, ip)
            if n_fail == 5:
                notify.send(f"⚠️ 대시보드 로그인 5회 실패 ({ip}) - 5분간 차단")
            time.sleep(1)
            self._login_page(401, "비밀번호가 올바르지 않습니다.")

        # ---------- GET ----------
        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            path, _, qs = self.path.partition("?")
            if path == "/login":
                return self._login_page()
            if path == "/logout":
                tok = SimpleCookie(self.headers.get("Cookie") or "").get("sid")
                if tok:
                    SESSIONS.pop(tok.value, None)
                return self._redirect("/login", {"Set-Cookie": "sid=; Max-Age=0; Path=/"})
            if not self._authed():
                if path.startswith("/api/"):
                    return self._send(401, b'{"error":"login required"}')
                return self._redirect("/login")
            if path == "/":
                return self._send(200, html, "text/html; charset=utf-8")
            if path == "/api/status":
                if "force" in qs or time.time() - CACHE["t"] > CACHE_SEC or CACHE["data"] is None:
                    CACHE["data"] = collect(kis, sched)
                    CACHE["t"] = time.time()
                data = dict(CACHE["data"])
                data["remote"] = not self._local()
                return self._send(200, json.dumps(data, ensure_ascii=False).encode())
            if path == "/api/mom_candidates":
                if time.time() - MOM_CAND["t"] > 30 or MOM_CAND["data"] is None:
                    with LOCK:
                        MOM_CAND["data"] = safe(lambda: mom_candidates(kis))
                    MOM_CAND["t"] = time.time()
                return self._send(200, json.dumps(MOM_CAND["data"], ensure_ascii=False).encode())
            self._send(404, b"not found", "text/plain")

        # ---------- POST ----------
        def do_POST(self):
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            if self.path == "/login":
                return self._do_login()
            if not self._authed() or self.headers.get("X-Requested-With") != "dashboard":
                return self._send(403, b"forbidden", "text/plain")

            if self.path.startswith("/api/run/"):
                name = self.path.rsplit("/", 1)[-1]
                fn = job_map.get(name)
                if not fn:
                    return self._send(404, b'{"error":"unknown job"}')

                def run():
                    with LOCK:
                        try:
                            log.info("수동 실행: %s", name)
                            fn(kis)
                        except Exception:
                            log.exception("수동 실행 오류: %s", name)
                        CACHE["t"] = 0.0

                threading.Thread(target=run, daemon=True).start()
                return self._send(200, b'{"started":true}')

            if self.path == "/api/dry_run":
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    req = json.loads(self.rfile.read(n) or b"{}")
                    want = bool(req.get("dry_run"))
                    if not want and req.get("confirm") != "실주문":
                        raise ValueError("실주문 전환은 확인 문구가 필요합니다")
                    with LOCK:  # 실행 중인 작업이 끝난 뒤 전환
                        config.DRY_RUN = want
                        CACHE["t"] = 0.0
                    msg = "DRY_RUN 켬 - 주문 중단" if want else "⚠️ 실주문 켬 - 지금부터 실제 주문이 나갑니다"
                    log.warning("웹에서 설정 변경(%s): %s", self.client_address[0], msg)
                    notify.send(msg)
                    return self._send(200, json.dumps({"dry_run": config.DRY_RUN}).encode())
                except Exception as e:
                    return self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode())

            if self.path == "/api/password":
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    req = json.loads(self.rfile.read(min(n, 2048)) or b"{}")
                    cur, new = str(req.get("current", "")), str(req.get("new", ""))
                    ip = self.client_address[0]
                    n_fail, until = FAILS.get(ip, [0, 0])
                    if until > time.time():
                        raise ValueError("시도가 너무 많습니다. 잠시 후 다시 시도하세요")
                    if pw_configured() and not check_pw(cur):
                        FAILS[ip] = [n_fail + 1, time.time() + 300 if n_fail + 1 >= 5 else 0]
                        time.sleep(1)
                        raise ValueError("현재 비밀번호가 올바르지 않습니다")
                    if len(new) < 4:
                        raise ValueError("새 비밀번호는 4자 이상이어야 합니다")
                    set_pw(new)
                    keep = SimpleCookie(self.headers.get("Cookie") or "").get("sid")
                    for k in [k for k in SESSIONS if not keep or k != keep.value]:
                        del SESSIONS[k]  # 다른 접속은 로그아웃
                    log.warning("대시보드 비밀번호 변경: %s", ip)
                    notify.send("🔑 대시보드 비밀번호가 변경되었습니다")
                    return self._send(200, b'{"ok":true}')
                except Exception as e:
                    return self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode())

            if self.path == "/api/mode":
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    req = json.loads(self.rfile.read(n) or b"{}")
                    want = "live" if req.get("mode") == "live" else "mock"
                    if want == "live" and req.get("confirm") != "실전":
                        raise ValueError("실전 전환은 확인 문구가 필요합니다")
                    if want == config.MODE:
                        raise ValueError("이미 해당 모드입니다")
                    if not config.has_creds(want):
                        raise ValueError(f"{want} 용 App Key/Secret/계좌번호가 .env 에 없습니다 "
                                         f"(KIS_{want.upper()}_APP_KEY / _APP_SECRET / _ACCOUNT_NO)")
                    with LOCK:  # 실행 중인 작업이 끝난 뒤 전환
                        prev = config.MODE
                        config.DRY_RUN = True  # 전환 직후에는 항상 DRY_RUN 으로 시작
                        config.set_mode(want)
                        try:
                            kis.reload()
                        except Exception:
                            config.set_mode(prev)
                            kis.reload()
                            raise
                        CACHE["t"] = 0.0
                    msg = f"⚠️ {'실전' if want == 'live' else '모의투자'} 모드로 전환 (DRY_RUN 켜짐)"
                    log.warning("웹에서 모드 전환(%s): %s", self.client_address[0], msg)
                    notify.send(msg)
                    return self._send(200, json.dumps({"mode": config.MODE}).encode())
                except Exception as e:
                    return self._send(400, json.dumps({"error": f"모드 전환 실패: {e}"}, ensure_ascii=False).encode())

            if self.path == "/api/box":
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    req = json.loads(self.rfile.read(n) or b"{}")
                    with LOCK:
                        s = st.load()
                        if req.get("auto"):
                            s["box_manual"] = None
                        else:
                            lo, hi = float(req["lower"]), float(req["upper"])
                            if not (0 < lo < hi):
                                raise ValueError("하단은 0보다 크고 상단보다 작아야 합니다")
                            s["box_manual"] = {"lower": lo, "upper": hi}
                        st.save(s)
                        CACHE["t"] = 0.0
                    log.info("박스권 설정 변경: %s", s["box_manual"] or "자동 산정")
                    return self._send(200, b'{"ok":true}')
                except Exception as e:
                    return self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode())
            self._send(404, b"not found", "text/plain")

    return H


def main():
    core.setup_logging()
    log.info("대시보드 시작: MODE=%s DRY_RUN=%s", config.MODE, config.DRY_RUN)
    kis = KIS()
    sched = core.build_scheduler(kis, BackgroundScheduler, LOCK)
    sched.start()

    hosts = config.DASHBOARD_HOSTS
    handler = make_handler(kis, sched, set(hosts) | {"127.0.0.1", "localhost"})
    servers = []
    for h in hosts:
        if h not in ("127.0.0.1", "localhost") and not pw_configured():
            log.warning("DASHBOARD_PASSWORD 가 없어 %s 로는 열지 않습니다", h)
            continue
        try:
            servers.append(ThreadingHTTPServer((h, PORT), handler))
            print(f"  대시보드: http://{h}:{PORT}")
        except OSError as e:
            log.warning("바인딩 실패 %s: %s (Tailscale 이 켜져 있는지 확인)", h, e)
    if not servers:
        sched.shutdown(wait=False)
        raise SystemExit("열 수 있는 주소가 없습니다")
    print("  (종료: Ctrl+C)\n")

    for srv in servers[:-1]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        servers[-1].serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        sched.shutdown(wait=False)
        for srv in servers:
            srv.server_close()


if __name__ == "__main__":
    main()
