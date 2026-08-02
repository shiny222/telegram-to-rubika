import json
import requests

with open("config.json", encoding="utf-8") as f:
    config = json.load(f)

TOKEN = config["RUBIKA_TOKEN"]
URL = f"https://botapi.rubika.ir/v3/{TOKEN}/sendMessage"

payload = {
    "chat_id": config.get("RUBIKA_DEFAULT_CHAT_ID", ""),
    "text": "تست مستقیم"
}

try:
    r = requests.post(URL, json=payload, timeout=20)
    print("status:", r.status_code)
    print("headers:", dict(r.headers))
    print("text:", r.text[:1000])
except Exception as e:
    print("ERROR:", repr(e))
