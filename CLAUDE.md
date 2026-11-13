# STOCK_INVEST

한국투자증권 Open API 기반 주식 자동매매 프로그램 (Python).

## 투자 전략
전략의 기준 문서는 아래 파일이다. 코드를 작성하거나 수정할 때 반드시 이 내용을 따른다.

@doc/STRATEGY.md

## 개발 규칙
- 언어: Python 3.10+
- 기본 모드는 모의투자(`MODE=mock`) + `DRY_RUN=true`. 실전 전환은 사용자가 명시적으로 요청할 때만 한다.
- App Key/Secret/계좌번호는 `.env`에서만 읽고 코드/로그/문서에 쓰지 않는다. `.env`는 Git에 올리지 않는다.
- 전략을 변경하면 코드보다 먼저 `doc/STRATEGY.md`를 갱신한다.
- API TR ID, 파라미터 등 불확실한 항목은 KIS Developers 문서(apiportal.koreainvestment.com)로 확인한다.

## 폴더 구조
- 루트: `app.py`(대시보드+스케줄러 진입점), `.env`, `CLAUDE.md`, `requirements.txt`
- `stock_invest/`: 소스 패키지 (config, kis_client, daytrade, fear_strategy, box 등, `ui.html` 포함)
- `doc/`: 전략 문서 등 (`CLAUDE.md` 외 md 파일)
- `service/`: 윈도우 서비스(`NATURE_STOCK`) 등록/제거 스크립트 (관리자 PowerShell 에서 실행)
- 보조 실행: `python -m stock_invest.check`, `python -m stock_invest.telegram_setup`, `python -m stock_invest.main`(스케줄러만)
