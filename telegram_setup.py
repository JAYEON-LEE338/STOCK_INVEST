"""텔레그램 설정 도우미.
1) @BotFather 에서 봇을 만들고 토큰을 .env 의 TELEGRAM_BOT_TOKEN 에 넣는다.
2) 만든 봇에게 텔레그램에서 아무 메시지나 보낸다.
3) python telegram_setup.py  -> chat_id 를 찾아 출력하고 테스트 메시지를 보낸다.
"""
import requests

import config

if not config.TELEGRAM_BOT_TOKEN:
    raise SystemExit(".env 에 TELEGRAM_BOT_TOKEN 을 먼저 입력하세요.")

base = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
updates = requests.get(f"{base}/getUpdates", timeout=10).json().get("result", [])
chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "") for u in updates if "message" in u}
if not chats:
    raise SystemExit("메시지가 없습니다. 봇에게 먼저 아무 메시지나 보낸 뒤 다시 실행하세요.")
for cid, name in chats.items():
    print(f"chat_id={cid} ({name})")
cid = config.TELEGRAM_CHAT_ID or next(iter(chats))
r = requests.post(f"{base}/sendMessage", json={"chat_id": cid, "text": "STOCK_INVEST 알림 테스트 ✅"}, timeout=10)
print("테스트 전송:", "성공" if r.ok else f"실패 {r.status_code}")
print("→ .env 의 TELEGRAM_CHAT_ID 에 위 chat_id 를 넣으세요.")
