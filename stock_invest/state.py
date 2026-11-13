import json

from . import config

_DEFAULT = {
    "box_positions": {},  # {code: {qty, peak_qty, sold_steps, date}} 박스권 전략 보유
    "mom_positions": {},  # {code: {qty, base_qty, entry, date, sold_order, steps, was_neg}} 모멘텀 전략 보유 (당일 NXT 매도)
    "box_manual": None,  # {lower, upper} 직접 설정한 박스권. None 이면 자동 산정
    "signals": {"box": {}, "momentum": {}},  # UI 표시용 최근 판단 결과
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
