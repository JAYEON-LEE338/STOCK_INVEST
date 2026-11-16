import json

from . import config

_DEFAULT = {
    "box_positions": {},  # {code: {qty, peak_qty, sold_steps, date}} 박스권 전략 보유
    "mom_positions": {},  # {code: {qty, base_qty, entry, date, sold_order, steps, was_neg}} 모멘텀 전략 보유 (당일 NXT 매도)
    "scalp_positions": {},  # {code: {qty, base_qty, entry, date, time, steps, was_neg, loss_alerted}} 초단타 보유 (qty 0 도 당일 기록 유지)
    "scalp_watch": {"date": "", "items": {}},  # 스캔에서 포착한 후보 {code: {name, sector, ref_price, ...}}
    "scalp_done": {"date": "", "codes": []},  # 오늘 익절한 종목 (재매수 금지)
    "orders": {},  # {주문번호: {strategy, code, qty, price, orgno, date, time, applied, applied_cost, status}} 매수 체결 추적
    "box_manual": None,  # {lower, upper} 직접 설정한 박스권. None 이면 자동 산정
    "signals": {"box": {}, "momentum": {}},  # UI 표시용 최근 판단 결과
    "alloc": {},  # 대시보드에서 바꾼 전략별 투자 비중 {fear, box, mom, scalp} (총평가금액 대비 0~1). 비어 있으면 config 기본값
    "break_positions": {},  # {code: {qty, base_qty, entry, date, steps, level}} 돌파매매 보유 (여러 날 보유 가능)
    "break_done": {"date": "", "codes": []},  # 오늘 익절한 종목 (재매수 금지)
    "enabled": {"fear": True, "box": True, "mom": True, "scalp": True, "break": True},  # 전략별 신규 매수 ON/OFF
    "fear": {"sold_steps": [], "peak_qty": 0, "last_buy_date": "", "last_sell_date": ""},
}


def load() -> dict:
    if config.STATE_FILE.exists():
        try:
            d = json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
            for k, v in _DEFAULT.items():
                d.setdefault(k, json.loads(json.dumps(v)))
            return d
        except Exception:
            pass
    return json.loads(json.dumps(_DEFAULT))


def save(d: dict) -> None:
    config.STATE_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


STRATEGIES = ("fear", "box", "mom", "scalp", "break")


def is_on(name: str) -> bool:
    """전략 신규 매수 ON 여부 (기본 ON)."""
    return bool(load().get("enabled", {}).get(name, True))


def alloc() -> dict:
    """전략별 투자 비중 (총평가금액 대비 0~1). 모멘텀과 단타는 하나의 비중(momscalp)을 공유한다.
    반환: {fear, box, momscalp, break, mom, scalp}  (mom/scalp 는 momscalp 와 같은 값 — 같은 예산 풀)"""
    base = {"fear": config.FEAR_RATIO, "box": config.DAY_RATIO * config.DAY_SPLIT_BOX,
            "momscalp": config.DAY_RATIO * config.DAY_SPLIT_MOM + config.SCALP_RATIO, "break": config.BREAK_RATIO}
    saved = dict(load().get("alloc") or {})
    if "momscalp" not in saved and ("mom" in saved or "scalp" in saved):  # 예전 형식 호환
        saved["momscalp"] = float(saved.get("mom", 0)) + float(saved.get("scalp", 0))
    out = {k: float(saved.get(k, v)) for k, v in base.items()}
    out["mom"] = out["scalp"] = out["momscalp"]
    return out
