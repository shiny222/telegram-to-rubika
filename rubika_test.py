import requests
import json

TOKEN = "HIDBJ0HUNSXPDLQPUJHFHUNDQIEQLBXEQCKNDNOFOPFBFDRCEQMISNMTHINEYTVM"
URL = f"https://botapi.rubika.ir/v3/{TOKEN}/sendMessage"

payload = {
    "chat_id": "c0DMQdZ0c8fe9dd53f324ca1f93ac249",
    "text": "تست مستقیم"
}

try:
    r = requests.post(URL, json=payload, timeout=20)
    print("status:", r.status_code)
    print("headers:", dict(r.headers))
    print("text:", r.text[:1000])
except Exception as e:
    print("ERROR:", repr(e))