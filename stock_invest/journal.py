"""거래 기록(체결 단위)과 전략별 성과 집계.

파일: trades_{mode}.jsonl (한 줄에 체결 하나). 매도 기록에는 매수가(entry) 기준 실현 손익이 들어간다.
성과는 매도 체결 1건을 거래 1건으로 센다 (분할 매도는 여러 건).
"""
import datetime as dt
import json
import logging

from . import config

log = logging.getLogger("journal")
LABEL = {"fear": "공포지수", "box": "박스권", "mom": "모멘텀", "scalp": "단타", "break": "돌파매매"}


def _file():
    return config.BASE_DIR / f"trades_{config.MODE}.jsonl"


def record(strategy: str, side: str, code: str, qty: int, price: float, entry: float | None = None,
           note: str = "", dry: bool | None = None) -> None:
    if qty <= 0 or not price:
        return
    rec = {"time": dt.datetime.now().isoformat(timespec="seconds"), "strategy": strategy, "side": side,
           "code": code, "name": config.STOCK_NAMES.get(code, code), "qty": int(qty), "price": round(float(price), 2),
           "dry": config.DRY_RUN if dry is None else dry, "note": note}
    if side == "sell" and entry:
        pct = (price / entry - 1 - config.MOM_ROUNDTRIP_COST) * 100
        rec.update(entry=round(float(entry), 2), pnl_pct=round(pct, 3), pnl_won=round(qty * entry * pct / 100))
    try:
        with open(_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("거래 기록 실패: %s", e)


def load() -> list:
    out = []
    try:
        with open(_file(), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
    except FileNotFoundError:
        pass
    return out


def _summary(sells: list) -> dict:
    n = len(sells)
    wins = [t for t in sells if t["pnl_won"] > 0]
    losses = [t for t in sells if t["pnl_won"] <= 0]
    total = sum(t["pnl_won"] for t in sells)
    cum = peak = mdd = 0.0
    for t in sells:  # 시간순
        cum += t["pnl_won"]
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    today = dt.date.today().isoformat()
    return {
        "trades": n,
        "win_rate": round(len(wins) / n * 100, 1) if n else None,
        "avg_win_pct": round(sum(t["pnl_pct"] for t in wins) / len(wins), 2) if wins else None,
        "avg_loss_pct": round(sum(t["pnl_pct"] for t in losses) / len(losses), 2) if losses else None,
        "expectancy_won": round(total / n) if n else None,
        "total_won": round(total),
        "mdd_won": round(mdd),
        "today_won": round(sum(t["pnl_won"] for t in sells if t["time"][:10] == today)),
    }


def stats() -> dict:
    """{strategy: 요약, "all": 전체 요약, "buys": 매수 체결 수}."""
    rows = load()
    sells = sorted([t for t in rows if t["side"] == "sell" and "pnl_won" in t], key=lambda t: t["time"])
    out = {k: _summary([t for t in sells if t["strategy"] == k]) for k in LABEL}
    out["all"] = _summary(sells)
    out["buys"] = sum(1 for t in rows if t["side"] == "buy")
    out["first"] = rows[0]["time"][:10] if rows else None
    return out


def recent(n: int = 30) -> list:
    return load()[-n:][::-1]
