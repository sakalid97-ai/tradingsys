import json
import urllib.request

URLS = [
    "https://query1.finance.yahoo.com/v8/finance/chart/SPY?range=5d&interval=1d",
    "https://query2.finance.yahoo.com/v8/finance/chart/SPY?range=5d&interval=1d",
]

for url in URLS:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = json.load(r)
        res = payload["chart"]["result"][0]
        meta = res["meta"]
        quote = res["indicators"]["quote"][0]
        print("OK", url)
        print("   symbol     :", meta.get("symbol"))
        print("   exchangeTz :", meta.get("exchangeTimezoneName"))
        print("   bars       :", len(res["timestamp"]))
        print("   last close :", quote["close"][-1])
        break
    except Exception as exc:  # noqa: BLE001
        print("FAIL", url)
        print("   ", type(exc).__name__, exc)
