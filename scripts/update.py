#!/usr/bin/env python3
"""每日收盤後：抓證交所資料 → 更新追蹤快照 → 篩選新推薦 → 寄 Email。
只使用 Python 標準函式庫。資料寫入 data/state.json 與 data/prices.json。"""
import hashlib
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

BASE = "https://openapi.twse.com.tw/v1/exchangeReport/"
DATA = Path("data")
STATE_F = DATA / "state.json"
PRICES_F = DATA / "prices.json"
TZ = timezone(timedelta(hours=8))

# ---- 可調整參數 ----
MIN_PRICE = 10          # 排除低價股
MIN_VALUE = 1e8         # 成交值至少 1 億（流動性）
THRESHOLD = 70          # 達到幾分才推薦
MAX_NEW = 5             # 每天最多新增幾檔
STOP_RATIO = 0.93       # 停損：-7%
TARGET_RATIO = 1.15     # 達標：+15%
MAX_DAYS = 20           # 追蹤滿幾個交易日自動結案
COOLDOWN_DAYS = 10      # 同一檔股票多久內不重複推薦
HISTORY_KEEP = 30       # 每檔保留幾天歷史


def get(path):
    req = urllib.request.Request(
        BASE + path,
        headers={"accept": "application/json", "User-Agent": "stock-tracker/1.0"},
    )
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if i == 2:
                raise
            time.sleep(3 * (i + 1))


def num(v):
    try:
        return float(str(v).replace(",", "").replace("+", "").strip())
    except ValueError:
        return None


def trade_date(rows):
    d = str(rows[0].get("Date") or "").strip()
    if re.fullmatch(r"\d{7}", d):
        return f"{int(d[:3]) + 1911}-{d[3:5]}-{d[5:7]}"
    if re.fullmatch(r"\d{8}", d):
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    return datetime.now(TZ).strftime("%Y-%m-%d")


def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def score(s, hist, ma, rank):
    pts, why = 0, []
    if ma and s["close"] > ma:
        ext = s["close"] / ma - 1
        if ext <= 0.10:
            pts += 30
            why.append(f"收盤價 {s['close']:.2f} 站上月均價 {ma:.2f}（+{ext * 100:.1f}%）")
    prev = s["close"] - s["chg"]
    if prev > 0:
        d = s["chg"] / prev
        if 0.005 <= d <= 0.05:
            pts += 20
            why.append(f"當日漲幅 {d * 100:.1f}%")
    vols = [h[2] for h in hist[-5:] if h[2]]
    if len(vols) >= 3 and s["vol"]:
        avg = sum(vols) / len(vols)
        if avg > 0 and s["vol"] >= 1.5 * avg:
            pts += 30
            why.append(f"成交量為近 {len(vols)} 日均量的 {s['vol'] / avg:.1f} 倍")
    if rank <= 100:
        pts += 20
        why.append(f"成交值排名第 {rank}")
    return pts, why


def send_mail(subject, body):
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    to = os.environ.get("MAIL_TO")
    if not (user and pw and to):
        print("未設定 SMTP_USER / SMTP_PASS / MAIL_TO，略過寄信")
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(body)
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or "465")
    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as s:
        s.login(user, pw)
        s.send_message(msg)
    print("已寄出通知")


def send_telegram(text):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("未設定 TELEGRAM_TOKEN / TELEGRAM_CHAT_ID，略過 Telegram")
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000]}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()
    print("已送出 Telegram 通知")


def main():
    DATA.mkdir(exist_ok=True)
    rows = get("STOCK_DAY_ALL")
    try:
        avg_rows = get("STOCK_DAY_AVG_ALL")
    except Exception as e:
        print("月均價取得失敗：", e)
        avg_rows = []
    ma_map = {r.get("Code"): num(r.get("MonthlyAveragePrice")) for r in avg_rows}

    stocks = {}
    for r in rows:
        code = str(r.get("Code", ""))
        if not re.fullmatch(r"[1-9]\d{3}", code):
            continue
        close, vol, val, chg = (num(r.get(k)) for k in ("ClosingPrice", "TradeVolume", "TradeValue", "Change"))
        if not close or close <= 0:
            continue
        stocks[code] = {"name": r.get("Name", ""), "close": close, "vol": vol or 0, "val": val or 0, "chg": chg or 0}
    if len(stocks) < 500:
        sys.exit(f"資料異常：只取得 {len(stocks)} 檔，中止更新")

    state = load(STATE_F, {"recommendations": [], "tracking": [], "lastDate": None, "signature": None})
    prices = load(PRICES_F, {})

    sig = hashlib.md5(json.dumps(sorted((c, s["close"], s["vol"]) for c, s in stocks.items())).encode()).hexdigest()
    if sig == state.get("signature"):
        print("資料與上次相同（可能是休市日），不更新")
        return
    date = trade_date(rows)
    now_ms = int(time.time() * 1000)
    events = []

    # 1) 更新追蹤中的快照，並檢查停損／達標／到期
    for t in state["tracking"]:
        if t["status"] != "追蹤中" or (t["snapshots"] and t["snapshots"][-1]["date"] == date):
            continue
        s = stocks.get(t["code"])
        if not s:
            continue
        t["snapshots"].append({"date": date, "price": s["close"], "at": now_ms})
        ret = (s["close"] / t["basePrice"] - 1) * 100
        new = None
        if s["close"] <= t["stop"]:
            new, why = "已失效", f"收盤 {s['close']:.2f} 跌破停損 {t['stop']:.2f}"
        elif s["close"] >= t["target"]:
            new, why = "已達標", f"收盤 {s['close']:.2f} 達到目標 {t['target']:.2f}"
        elif len(t["snapshots"]) >= MAX_DAYS:
            new, why = "已結案", f"已追蹤滿 {MAX_DAYS} 個交易日"
        if new:
            t["status"] = new
            events.append(f"【{new}】{t['code']} {t['name']}：{why}，報酬率 {ret:+.2f}%")

    # 2) 篩選新推薦
    liquid = sorted(
        ((c, s) for c, s in stocks.items() if s["close"] >= MIN_PRICE and s["val"] >= MIN_VALUE),
        key=lambda x: x[1]["val"],
        reverse=True,
    )
    active = {t["code"] for t in state["tracking"] if t["status"] == "追蹤中"}
    last_rec = {}
    for r in state["recommendations"]:
        last_rec[r["code"]] = max(last_rec.get(r["code"], ""), r["date"])
    today = datetime.fromisoformat(date)

    picks = []
    for rank, (code, s) in enumerate(liquid, 1):
        if code in active:
            continue
        if code in last_rec and (today - datetime.fromisoformat(last_rec[code])).days < COOLDOWN_DAYS:
            continue
        pts, why = score(s, prices.get(code, []), ma_map.get(code), rank)
        if pts >= THRESHOLD:
            picks.append((pts, s["val"], code, s, why))
    picks.sort(key=lambda x: (-x[0], -x[1]))

    new_lines = []
    for pts, _, code, s, why in picks[:MAX_NEW]:
        rid = f"{date}-{code}"
        stop = round(s["close"] * STOP_RATIO, 2)
        target = round(s["close"] * TARGET_RATIO, 2)
        risk = f"收盤跌破 {stop}（-7%）視為失效；追蹤滿 {MAX_DAYS} 個交易日自動結案。量化規則篩選，非投資建議。"
        state["recommendations"].append({
            "id": rid, "code": code, "name": s["name"], "industry": "", "date": date,
            "price": s["close"], "score": pts, "reason": "；".join(why), "risk": risk,
        })
        state["tracking"].append({
            "id": "t-" + rid, "recId": rid, "code": code, "name": s["name"], "date": date,
            "basePrice": s["close"], "status": "追蹤中", "snapshots": [],
            "stop": stop, "target": target,
        })
        new_lines.append(f"{code} {s['name']}　參考價 {s['close']:.2f}　評分 {pts}\n  理由：{'；'.join(why)}\n  停損 {stop}／目標 {target}")

    # 3) 寫入歷史與狀態
    for code, s in stocks.items():
        h = prices.setdefault(code, [])
        if not h or h[-1][0] != date:
            h.append([date, s["close"], s["vol"]])
        prices[code] = h[-HISTORY_KEEP:]
    # 大盤基準：流動性足夠股票的等權重日報酬，累積成指數（首日 = 100），用來計算超額報酬
    rets = [max(-0.11, min(0.11, s["chg"] / (s["close"] - s["chg"]))) for _, s in liquid if s["close"] - s["chg"] > 0]
    mean = sum(rets) / len(rets) if rets else 0.0
    mk = state.get("market") or []
    if not mk or mk[-1]["date"] != date:
        mk.append({"date": date, "idx": 100.0 if not mk else round(mk[-1]["idx"] * (1 + mean), 4)})
    state["market"] = mk[-500:]
    state.update({
        "lastDate": date, "signature": sig,
        "updatedAt": datetime.now(TZ).isoformat(timespec="seconds"),
        "params": {"threshold": THRESHOLD, "stop": STOP_RATIO, "target": TARGET_RATIO, "maxDays": MAX_DAYS},
    })
    STATE_F.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    PRICES_F.write_text(json.dumps(prices, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{date}：新推薦 {len(new_lines)} 檔，觸發事件 {len(events)} 項")

    # 4) 寄信（有新推薦或觸發事件才寄）
    if new_lines or events:
        parts = [f"交易日：{date}", ""]
        if new_lines:
            parts += ["■ 今日新推薦", *new_lines, ""]
        if events:
            parts += ["■ 追蹤狀態變動", *events, ""]
        parts.append("本信由量化規則自動產生，僅供個人研究紀錄，不構成投資建議。")
        subject = f"[台股追蹤] {date} 新推薦 {len(new_lines)} 檔／狀態變動 {len(events)} 項"
        body = "\n".join(parts)
        for fn, args in ((send_mail, (subject, body)), (send_telegram, (subject + "\n\n" + body,))):
            try:
                fn(*args)
            except Exception as e:
                print(f"::warning::通知失敗（{fn.__name__}）：{e}")


if __name__ == "__main__":
    main()
