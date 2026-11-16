"""돌파매매 일봉 백테스트.  실행: python -m stock_invest.backtest [--period 2y] [--detail]

규칙 (doc/STRATEGY.md 2.4 와 동일한 가격·거래량·청산 규칙, 외국인 조건은 과거 데이터가 없어 제외):
- 진입: 종가 > 직전 BREAK_DAYS 거래일 최고가 이고 거래량 ≥ 전일 × BREAK_VOL_RATE% 인 날 종가에 매수 (15:20 종가 단일가 가정)
- R = 직전 ATR_DAYS 일 ATR% × BREAK_ATR_MULT, [BREAK_R_MIN, BREAK_R_MAX] 로 제한
- 청산 (다음 날부터, 하루 안에서는 손절을 먼저 본다 = 보수적):
  -1R 손절 전량 (시가가 더 낮으면 시가) / 매수 후 BREAK_FAIL_DAYS 거래일 안에 종가 < 돌파 기준선이면 종가에 전량 /
  +TP_R·R 에서 TP_RATIO 익절 / 익절 후 최고가 - BREAK_TRAIL_R·R 이탈 시 나머지 전량
- 수익률은 왕복 수수료·세금(MOM_ROUNDTRIP_COST) 차감. 같은 종목은 보유 중이면 새 신호를 무시한다.
- 데이터는 yfinance(.KS/.KQ). 유니버스는 현재 종목이라 생존 편향이 있다.
"""
import argparse
import sys

from . import config


def _history(code: str, period: str):
    import yfinance as yf
    for suf in (".KS", ".KQ"):
        try:
            h = yf.Ticker(code + suf).history(period=period, auto_adjust=False)
            if len(h) > 60 and h["Volume"].sum() > 0:
                return h
        except Exception:
            pass
    return None


def simulate(h, code: str) -> list:
    o, hi, lo, c, v = (h[k].tolist() for k in ("Open", "High", "Low", "Close", "Volume"))
    dates = [str(d.date()) for d in h.index]
    n, N, A = len(c), config.BREAK_DAYS, config.ATR_DAYS
    trades, t = [], max(N, A + 1)
    cost = config.MOM_ROUNDTRIP_COST
    while t < n - 1:
        upper = max(hi[t - N:t])
        if not (c[t] > upper and v[t - 1] > 0 and v[t] >= v[t - 1] * config.BREAK_VOL_RATE / 100):
            t += 1
            continue
        trs = [max(hi[i] - lo[i], abs(hi[i] - c[i - 1]), abs(lo[i] - c[i - 1])) for i in range(t - A, t)]
        rp = max(config.BREAK_R_MIN, min(config.BREAK_R_MAX, sum(trs) / A / c[t - 1] * 100 * config.BREAK_ATR_MULT)) / 100
        entry, stop = c[t], c[t] * (1 - rp)
        left, pnl, peak, tp_done, exit_i, how = 1.0, 0.0, c[t], False, None, ""
        for i in range(t + 1, n):
            # 1) 손절 (시가 갭 포함)
            if lo[i] <= stop:
                px = min(o[i], stop)
                pnl += left * (px / entry - 1)
                left, exit_i, how = 0.0, i, "stop"
                break
            # 2) 익절
            if not tp_done and hi[i] >= entry * (1 + config.TP_R * rp):
                pnl += config.TP_RATIO * (config.TP_R * rp)
                left -= config.TP_RATIO
                tp_done = True
            peak = max(peak, hi[i])
            # 3) 추적 손절 (익절 이후)
            if tp_done and lo[i] <= peak * (1 - config.BREAK_TRAIL_R * rp):
                px = min(o[i], peak * (1 - config.BREAK_TRAIL_R * rp))
                pnl += left * (px / entry - 1)
                left, exit_i, how = 0.0, i, "trail"
                break
            # 4) 돌파 실패
            if i - t <= config.BREAK_FAIL_DAYS and c[i] < upper:
                pnl += left * (c[i] / entry - 1)
                left, exit_i, how = 0.0, i, "fail"
                break
        if left > 0:  # 기간 끝까지 보유 → 마지막 종가로 평가
            pnl += left * (c[-1] / entry - 1)
            exit_i, how = n - 1, "open"
        net = pnl - cost
        trades.append({"code": code, "date": dates[t], "exit": dates[exit_i], "days": exit_i - t,
                       "ret": net * 100, "r": net / rp, "rp": rp * 100, "how": how, "tp": tp_done})
        t = exit_i + 1
    return trades


def report(trades: list) -> str:
    if not trades:
        return "신호 없음"
    n = len(trades)
    wins = [x for x in trades if x["ret"] > 0]
    losses = [x for x in trades if x["ret"] <= 0]
    gp, gl = sum(x["ret"] for x in wins), -sum(x["ret"] for x in losses)
    lines = [
        f"거래 {n}건 | 승률 {len(wins) / n * 100:.1f}% | 평균 보유 {sum(x['days'] for x in trades) / n:.1f}일",
        f"평균 이익 {gp / len(wins) if wins else 0:+.2f}% | 평균 손실 {-gl / len(losses) if losses else 0:+.2f}% | "
        f"손익비(PF) {gp / gl if gl else float('inf'):.2f}",
        f"기대값 {sum(x['ret'] for x in trades) / n:+.2f}%/거래 = {sum(x['r'] for x in trades) / n:+.2f}R/거래 | 합계 {sum(x['ret'] for x in trades):+.1f}%",
        "청산 사유: " + ", ".join(f"{k} {sum(1 for x in trades if x['how'] == k)}건" for k in ("stop", "fail", "trail", "open")),
    ]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="돌파매매 일봉 백테스트")
    ap.add_argument("--period", default="2y")
    ap.add_argument("--detail", action="store_true", help="거래 목록 출력")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    allt = []
    print(f"돌파매매 백테스트 ({a.period}, 외국인 조건 제외) — 기준: {config.BREAK_DAYS}일 최고가 돌파, 거래량 {config.BREAK_VOL_RATE:.0f}%, "
          f"R=ATR×{config.BREAK_ATR_MULT}, 익절 {config.TP_R}R×{config.TP_RATIO:.0%}, 추적 {config.BREAK_TRAIL_R}R, 실패 {config.BREAK_FAIL_DAYS}일\n")
    for sector, codes in config.UNIVERSE.items():
        for code in codes:
            h = _history(code, a.period)
            if h is None:
                print(f"  {config.STOCK_NAMES.get(code, code)}: 데이터 없음")
                continue
            tr = simulate(h, code)
            allt += tr
            avg = sum(x["ret"] for x in tr) / len(tr) if tr else 0
            print(f"  [{sector}] {config.STOCK_NAMES.get(code, code)}: {len(tr)}건, 평균 {avg:+.2f}%")
    print("\n=== 전체 ===")
    print(report(allt))
    if a.detail:
        for x in sorted(allt, key=lambda x: x["date"]):
            print(f"{x['date']}→{x['exit']} {config.STOCK_NAMES.get(x['code'], x['code'])} {x['ret']:+.2f}% ({x['r']:+.2f}R, {x['how']})")
    return allt


if __name__ == "__main__":
    main()
