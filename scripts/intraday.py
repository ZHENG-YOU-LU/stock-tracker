#!/usr/bin/env python3
"""盤中監控：每隔幾分鐘檢查「追蹤中」股票的盤中報價，觸及條件就用 Telegram 推播。
不會改動 state.json（狀態以收盤判定），只在 data/alerts.json 記錄今天已通知過什麼，避免重複轟炸。
用法：python scripts/intraday.py            正常執行（非盤中自動略過）
      python scripts/intraday.py --test     只送一則 Telegram 測試訊息
      python scripts/intraday.py --force    忽略時間與休市判斷（測試用）"""
import http.cookiejar
import json
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
STATE_F = Path("data/state.json")
ALERT_F = Path("data/alerts.json")
MIS = "https://mis.twse.com.tw/stock/"
UA = "Mozilla/5.0 (compatible; stock-tracker/1.0)"

NEAR_STOP = 0.02   # 距停損 2% 內提醒
MOVE_PCT = 5.0     # 單日漲跌幅超過 5% 提醒
OPEN_MIN, CLOSE_MIN = 9 * 60, 13 * 60 + 35


def num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def send_telegram(text):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        raise RuntimeError("未設定 TELEGRAM_TOKEN / TELEGRAM_CHAT_ID")
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000]}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


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


def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_alerts(date, sent):
    ALERT_F.parent.mkdir(exist_ok=True)
    ALERT_F.write_text(json.dumps({"date": date, "sent": sorted(sent)}, ensure_ascii=False), encoding="utf-8")


def main():
    if "--test" in sys.argv:
        send_telegram("✅ 台股追蹤：Telegram 測試成功，之後盤中提醒會送到這裡。")
        print("測試訊息已送出")
        return
    force = "--force" in sys.argv
    now = datetime.now(TZ)
    minutes = now.hour * 60 + now.minute
    if not force and (now.weekday() >= 5 or not OPEN_MIN <= minutes <= CLOSE_MIN):
        print("非盤中時間，略過")
        return

    state = load(STATE_F, {})
    active = [t for t in state.get("tracking", []) if t.get("status") == "追蹤中"]
    if not active:
        print("沒有追蹤中的股票")
        return

    today = now.strftime("%Y-%m-%d")
    alerts = load(ALERT_F, {})
    sent = set(alerts.get("sent", [])) if alerts.get("date") == today else set()

    try:
        quotes = fetch_quotes(sorted({t["code"] for t in active}))
    except Exception as e:
        print(f"::warning::盤中報價取得失敗：{e}")
        if "error" not in sent:  # 每天最多提醒一次，避免洗版
            try:
                send_telegram(f"⚠️ 盤中報價抓取失敗，今天的盤中提醒可能不會運作。\n{e}")
                sent.add("error")
                save_alerts(today, sent)
            except Exception as e2:
                print(f"::warning::Telegram 也失敗：{e2}")
        return
    if not quotes:
        print("沒有取得任何報價")
        return
    dates = {q["date"] for q in quotes.values() if q.get("date")}
    if not force and dates and today.replace("-", "") not in dates:
        print("今日沒有盤中資料（可能休市）")
        return

    lines, keys = [], []
    for t in active:
        q = quotes.get(t["code"])
        if not q:
            continue
        p = q["price"]
        day = (p / q["prev"] - 1) * 100 if q.get("prev") else None
        since = (p / t["basePrice"] - 1) * 100
        info = f"{t['code']} {t['name']} 現價 {p:.2f}（自推薦 {since:+.1f}%" + (f"，今日 {day:+.1f}%" if day is not None else "") + "）"
        found = []
        if p <= t["stop"]:
            found.append(("stop", f"⛔ 跌破停損 {t['stop']}｜{info}"))
        elif p <= t["stop"] * (1 + NEAR_STOP):
            found.append(("near", f"⚠️ 接近停損 {t['stop']}（還差 {(p / t['stop'] - 1) * 100:.1f}%）｜{info}"))
        if p >= t["target"]:
            found.append(("target", f"🎯 達到目標 {t['target']}｜{info}"))
        if day is not None and abs(day) >= MOVE_PCT:
            found.append(("up" if day > 0 else "down", f"📣 單日大幅{'上漲' if day > 0 else '下跌'} {day:+.1f}%｜{info}"))
        for k, text in found:
            key = f"{t['code']}:{k}"
            if key not in sent:
                keys.append(key)
                lines.append(text)

    if not lines:
        print(f"{now:%H:%M} 檢查 {len(active)} 檔，沒有新提醒")
        return
    msg = f"📊 盤中提醒 {now:%H:%M}\n\n" + "\n\n".join(lines) + "\n\n盤中價僅供參考，追蹤狀態以收盤價判定。"
    send_telegram(msg)  # 失敗會丟出例外，不標記為已通知，下一輪會重試
    sent.update(keys)
    save_alerts(today, sent)
    print(f"已推播 {len(lines)} 則提醒")


if __name__ == "__main__":
    main()
