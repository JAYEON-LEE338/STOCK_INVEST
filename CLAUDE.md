# STOCK_INVEST

한국투자증권 Open API 기반 주식 자동매매 프로그램 (Python).

## 투자 전략
전략의 기준 문서는 아래 파일이다. 코드를 작성하거나 수정할 때 반드시 이 내용을 따른다.

@STRATEGY.md

## 개발 규칙
- 언어: Python 3.10+
- 기본 모드는 모의투자(`MODE=mock`) + `DRY_RUN=true`. 실전 전환은 사용자가 명시적으로 요청할 때만 한다.
- App Key/Secret/계좌번호는 `.env`에서만 읽고 코드/로그/문서에 쓰지 않는다. `.env`는 Git에 올리지 않는다.
- 전략을 변경하면 코드보다 먼저 `STRATEGY.md`를 갱신한다.
- API TR ID, 파라미터 등 불확실한 항목은 KIS Developers 문서(apiportal.koreainvestment.com)로 확인한다.
