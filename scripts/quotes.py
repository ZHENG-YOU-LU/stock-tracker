#!/usr/bin/env python3
"""抓證交所盤中報價（只供網頁「⚡ 立即更新」使用，不會自動推播任何訊息）。"""
import concurrent.futures
import http.cookiejar
import json
import ssl
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

MIS = "https://mis.twse.com.tw/stock/"
UA = "Mozilla/5.0 (compatible; stock-tracker/1.0)"


def num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def make_opener():
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT  # 證交所憑證在嚴格模式下可能驗證失敗
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), urllib.request.HTTPSHandler(context=ctx))
    op.addheaders = [("User-Agent", UA), ("Accept-Language", "zh-TW")]
    return op


def first_price(field):
    for p in str(field or "").split("_"):
        v = num(p)
        if v:
            return v
    return None


def fetch_mis(codes):
    op = make_opener()
    op.open(MIS + "index.jsp", timeout=8).read()  # 先取得 session cookie
    out = {}
    for i in range(0, len(codes), 40):
        chunk = codes[i:i + 40]
        qs = urllib.parse.urlencode({
            "ex_ch": "|".join(f"tse_{c}.tw" for c in chunk),
            "json": "1", "delay": "0", "_": int(time.time() * 1000),
        })
        req = urllib.request.Request(MIS + "api/getStockInfo.jsp?" + qs, headers={"Referer": MIS + "index.jsp"})
        data = json.load(op.open(req, timeout=8))
        for m in data.get("msgArray", []):
            price = num(m.get("z"))
            if price is None:  # 尚未成交時，用最佳買賣價中間值估計
                bid, ask = first_price(m.get("b")), first_price(m.get("a"))
                price = (bid + ask) / 2 if bid and ask else None
            if price:
                out[m.get("c")] = {"price": price, "prev": num(m.get("y")), "date": m.get("d"), "time": m.get("t"), "src": "mis"}
        if i + 40 < len(codes):
            time.sleep(1)
    return out


def fetch_yahoo_one(code):
    """備援：Yahoo 奇摩股市（台股報價通常延遲約 20 分鐘）。"""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.TW?interval=5m&range=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        meta = json.load(r)["chart"]["result"][0]["meta"]
    price, prev = meta.get("regularMarketPrice"), meta.get("chartPreviousClose")
    if not price:
        raise ValueError("沒有報價")
    t = datetime.fromtimestamp(meta.get("regularMarketTime") or time.time(), timezone(timedelta(hours=8)))
    return {"price": float(price), "prev": float(prev) if prev else None,
            "date": t.strftime("%Y%m%d"), "time": t.strftime("%H:%M:%S"), "src": "yahoo"}


def fetch_quotes(codes):
    """先試證交所（較即時），失敗或缺漏的再用 Yahoo 補。全部失敗才丟出例外，並附上原因。"""
    errors, out = [], {}
    try:
        out = fetch_mis(codes)
    except Exception as e:
        errors.append(f"證交所 {type(e).__name__}: {e}")
    missing = [c for c in codes if c not in out]
    if missing:
        def safe(c):
            try:
                return c, fetch_yahoo_one(c), None
            except Exception as e:
                return c, None, f"{type(e).__name__}: {e}"
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            for c, v, err in ex.map(safe, missing):
                if v:
                    out[c] = v
                elif not any(x.startswith("Yahoo") for x in errors):
                    errors.append(f"Yahoo {err}")
    if not out:
        raise RuntimeError("；".join(errors) or "沒有取得任何報價")
    return out
