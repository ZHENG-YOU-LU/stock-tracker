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
MAX_PER_INDUSTRY = 2    # 同一產業每天最多推薦幾檔（分散風險）
SCORE_VERSION = 2       # 評分版本：2 = 0–100 連續分數
WEAK_THRESHOLD_ADD = 5  # 大盤偏弱時，推薦門檻加幾分
WEAK_MAX_NEW = 3        # 大盤偏弱時，每天最多推薦幾檔
STOP_RATIO = 0.93       # 停損：-7%
TARGET_RATIO = 1.15     # 達標：+15%
MAX_DAYS = 20           # 追蹤滿幾個交易日自動結案
COOLDOWN_DAYS = 10      # 同一檔股票多久內不重複推薦
HISTORY_KEEP = 30       # 每檔保留幾天歷史


def get(path):
    req = urllib.request.Request(
        path if path.startswith("http") else BASE + path,
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


IND_NAMES = {
    "01": "水泥", "02": "食品", "03": "塑膠", "04": "紡織纖維", "05": "電機機械", "06": "電器電纜",
    "08": "玻璃陶瓷", "09": "造紙", "10": "鋼鐵", "11": "橡膠", "12": "汽車", "14": "建材營造",
    "15": "航運", "16": "觀光餐旅", "17": "金融保險", "18": "貿易百貨", "19": "綜合", "20": "其他",
    "21": "化學", "22": "生技醫療", "23": "油電燃氣", "24": "半導體", "25": "電腦及週邊",
    "26": "光電", "27": "通信網路", "28": "電子零組件", "29": "電子通路", "30": "資訊服務",
    "31": "其他電子", "32": "文化創意", "33": "農業科技", "34": "電子商務", "35": "綠能環保",
    "36": "數位雲端", "37": "運動休閒", "38": "居家生活",
}


def fetch_industries():
    """代號 → 產業名稱。失敗時回傳空字典（產業欄留空、不做產業分散，其餘照常運作）。"""
    try:
        rows = get("https://openapi.twse.com.tw/v1/opendata/t187ap03_L")
    except Exception as e:
        print("產業資料取得失敗：", e)
        return {}
    out = {}
    for r in rows:
        code = str(r.get("公司代號", "")).strip()
        ind = str(r.get("產業別", "")).strip()
        if code and ind:
            out[code] = IND_NAMES.get(ind.zfill(2), ind) if ind.isdigit() else ind
    return out


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


def clamp(x):
    return max(0.0, min(1.0, x))


def score(s, hist, ma, rank):
    """0–100 連續分數。趨勢 30、當日動能 20、量能 30、流動性 20。
    量能歷史不足 3 天時，量能項不計入，其餘按比例換算成 100 分，讓不同日子的分數可以比較。"""
    got, avail, why = 0.0, 30 + 20 + 20, []

    # 趨勢：站上月均價 3% 內逐步加分，超過 6% 視為過熱開始扣分，12% 以上得 0
    if ma and s["close"] > ma:
        ext = s["close"] / ma - 1
        pts = 30 * (clamp(ext / 0.03) if ext <= 0.06 else clamp((0.12 - ext) / 0.06))
        why.append(f"趨勢 {pts:.0f}/30：收盤 {s['close']:.2f} 高於月均價 {ma:.2f}（{ext * 100:+.1f}%）")
    else:
        pts = 0
        why.append("趨勢 0/30：未站上月均價" if ma else "趨勢 0/30：無月均價資料")
    got += pts

    # 動能：當日漲幅 2.5% 最高分，0% 或 5% 以上得 0
    prev = s["close"] - s["chg"]
    d = s["chg"] / prev if prev > 0 else 0
    pts = 20 * clamp(1 - abs(d - 0.025) / 0.025)
    got += pts
    why.append(f"當日動能 {pts:.0f}/20：漲幅 {d * 100:+.1f}%")

    # 量能：今日量為近 5 日均量的 1 倍得 0 分、2 倍以上滿分
    vols = [h[2] for h in hist[-5:] if h[2]]
    if len(vols) >= 3 and s["vol"]:
        avg = sum(vols) / len(vols)
        ratio = s["vol"] / avg if avg > 0 else 0
        pts = 30 * clamp(ratio - 1)
        got += pts
        avail += 30
        why.append(f"量能 {pts:.0f}/30：為近 {len(vols)} 日均量的 {ratio:.1f} 倍")
    else:
        why.append(f"量能：歷史資料不足（{len(vols)}/3 日），本項未計入")

    # 流動性：成交值排名第 1 滿分，排名 300 名以外得 0
    pts = 20 * clamp(1 - (rank - 1) / 300)
    got += pts
    why.append(f"流動性 {pts:.0f}/20：成交值排名第 {rank}")
    return round(got / avail * 100, 1), why


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
    force = os.environ.get("FORCE", "").lower() in ("1", "true", "yes") or "--force" in sys.argv
    if sig == state.get("signature") and not force:
        print("資料與上次相同（可能是休市日），不更新")
        return
    date = trade_date(rows)
    now_ms = int(time.time() * 1000)
    events = []
    if force:
        # 強制重算：把「當日由規則產生」的推薦與追蹤整批移除，再用目前規則重新產生。自選股保留，不會新舊混在一起。
        drop = {r["id"] for r in state["recommendations"] if r["date"] == date and r.get("source") != "manual"}
        state["recommendations"] = [r for r in state["recommendations"] if r["id"] not in drop]
        state["tracking"] = [t for t in state["tracking"] if t["recId"] not in drop]
        print(f"強制重算 {date}：已移除當日 {len(drop)} 筆規則推薦")

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
    # 大盤基準：流動性足夠股票的等權重日報酬，累積成指數（首日 = 100），用來算超額報酬與判斷大盤強弱
    rets = [max(-0.11, min(0.11, s["chg"] / (s["close"] - s["chg"]))) for _, s in liquid if s["close"] - s["chg"] > 0]
    mean = sum(rets) / len(rets) if rets else 0.0
    mk = state.get("market") or []
    if not mk or mk[-1]["date"] != date:
        mk.append({"date": date, "idx": 100.0 if not mk else round(mk[-1]["idx"] * (1 + mean), 4)})
    state["market"] = mk[-500:]
    last5 = [x["idx"] for x in state["market"][-5:]]
    weak = len(last5) >= 5 and state["market"][-1]["idx"] < sum(last5) / 5  # 低於近 5 日均線 = 偏弱
    threshold = THRESHOLD + (WEAK_THRESHOLD_ADD if weak else 0)
    max_new = WEAK_MAX_NEW if weak else MAX_NEW
    print(f"大盤狀態：{'偏弱，門檻 ' + str(threshold) + '、最多 ' + str(max_new) + ' 檔' if weak else '正常'}")

    active = {t["code"] for t in state["tracking"] if t["status"] == "追蹤中"}
    last_rec = {}
    for r in state["recommendations"]:
        last_rec[r["code"]] = max(last_rec.get(r["code"], ""), r["date"])
    today = datetime.fromisoformat(date)
    industries = fetch_industries()

    picks = []
    for rank, (code, s) in enumerate(liquid, 1):
        if code in active:
            continue
        if code in last_rec and (today - datetime.fromisoformat(last_rec[code])).days < COOLDOWN_DAYS:
            continue
        pts, why = score(s, prices.get(code, []), ma_map.get(code), rank)
        if pts >= threshold:
            picks.append((pts, s["val"], code, s, why))
    picks.sort(key=lambda x: (-x[0], -x[1]))
    chosen, per_ind = [], {}
    for pick in picks:
        ind = industries.get(pick[2], "")
        if ind and per_ind.get(ind, 0) >= MAX_PER_INDUSTRY:
            continue
        per_ind[ind] = per_ind.get(ind, 0) + 1
        chosen.append(pick)
        if len(chosen) >= max_new:
            break

    new_lines = []
    for pts, _, code, s, why in chosen:
        rid = f"{date}-{code}"
        stop = round(s["close"] * STOP_RATIO, 2)
        target = round(s["close"] * TARGET_RATIO, 2)
        risk = f"收盤跌破 {stop}（-7%）視為失效；追蹤滿 {MAX_DAYS} 個交易日自動結案。量化規則篩選，非投資建議。"
        state["recommendations"].append({
            "id": rid, "code": code, "name": s["name"], "industry": industries.get(code, ""), "date": date,
            "price": s["close"], "score": pts, "scoreVer": SCORE_VERSION, "regime": "weak" if weak else "normal", "reason": "\n".join(why), "risk": risk,
        })
        state["tracking"].append({
            "id": "t-" + rid, "recId": rid, "code": code, "name": s["name"], "date": date,
            "basePrice": s["close"], "status": "追蹤中", "snapshots": [],
            "stop": stop, "target": target,
        })
        new_lines.append(f"{code} {s['name']}" + (f"（{industries[code]}）" if industries.get(code) else "") + f"　參考價 {s['close']:.2f}　評分 {pts:.0f}\n  理由：{'；'.join(why)}\n  停損 {stop}／目標 {target}")

    # 3) 寫入歷史與狀態
    for code, s in stocks.items():
        h = prices.setdefault(code, [])
        if not h or h[-1][0] != date:
            h.append([date, s["close"], s["vol"]])
        prices[code] = h[-HISTORY_KEEP:]
    state.update({
        "lastDate": date, "signature": sig,
        "updatedAt": datetime.now(TZ).isoformat(timespec="seconds"),
        "params": {"threshold": threshold, "baseThreshold": THRESHOLD, "weakMarket": weak, "maxNew": max_new, "stop": STOP_RATIO, "target": TARGET_RATIO, "maxDays": MAX_DAYS,
                   "scoreVer": SCORE_VERSION, "maxPerIndustry": MAX_PER_INDUSTRY},
    })
    STATE_F.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    PRICES_F.write_text(json.dumps(prices, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{date}：新推薦 {len(new_lines)} 檔，觸發事件 {len(events)} 項")

    # 4) 寄信（有新推薦或觸發事件才寄）
    if (new_lines or events) and not force:
        parts = [f"交易日：{date}", ""]
        if new_lines:
            parts += ["■ 今日新推薦", *new_lines, ""]
        if events:
            parts += ["■ 追蹤狀態變動", *events, ""]
        parts.append("本信由量化規則自動產生，僅供個人研究紀錄，不構成投資建議。")
        subject = f"[台股追蹤] {date} 新推薦 {len(new_lines)} 檔／狀態變動 {len(events)} 項"
        body = "\n".join(parts)
        try:
            send_mail(subject, body)
        except Exception as e:
            print(f"::warning::寄信失敗：{e}")


if __name__ == "__main__":
    main()
