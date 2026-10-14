#!/usr/bin/env python3
"""抓證交所盤中報價（只供網頁「⚡ 立即更新」使用，不會自動推播任何訊息）。"""
import http.cookiejar
import json
import ssl
import time
import urllib.parse
import urllib.request

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


def fetch_quotes(codes):
    op = make_opener()
    op.open(MIS + "index.jsp", timeout=20).read()  # 先取得 session cookie
    out = {}
    for i in range(0, len(codes), 40):
        chunk = codes[i:i + 40]
        qs = urllib.parse.urlencode({
            "ex_ch": "|".join(f"tse_{c}.tw" for c in chunk),
            "json": "1", "delay": "0", "_": int(time.time() * 1000),
        })
        req = urllib.request.Request(MIS + "api/getStockInfo.jsp?" + qs, headers={"Referer": MIS + "index.jsp"})
        data = json.load(op.open(req, timeout=20))
        for m in data.get("msgArray", []):
            price = num(m.get("z"))
            if price is None:  # 尚未成交時，用最佳買賣價中間值估計
                bid, ask = first_price(m.get("b")), first_price(m.get("a"))
                price = (bid + ask) / 2 if bid and ask else None
            if price:
                out[m.get("c")] = {"price": price, "prev": num(m.get("y")), "date": m.get("d"), "time": m.get("t")}
        if i + 40 < len(codes):
            time.sleep(1)
    return out
