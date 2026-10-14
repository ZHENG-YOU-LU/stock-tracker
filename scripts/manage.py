#!/usr/bin/env python3
"""由網頁透過 workflow_dispatch 觸發：add（新增自選股）/ remove（移除）/ refresh（更新即時價）。
參數從環境變數讀取（ACTION, CODE, COST, NOTE, NONCE），結果寫入 data/action_result.json 讓網頁讀取。"""
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import quotes
import update

TZ = timezone(timedelta(hours=8))
RESULT_F = Path("data/action_result.json")
LIVE_F = Path("data/live.json")


class Fail(Exception):
    pass


def write_result(ok, message, nonce, action, code=""):
    Path("data").mkdir(exist_ok=True)
    RESULT_F.write_text(json.dumps({
        "ok": ok, "message": message, "nonce": nonce, "action": action, "code": code,
        "at": datetime.now(TZ).isoformat(timespec="seconds"),
    }, ensure_ascii=False), encoding="utf-8")
    print(("OK  " if ok else "FAIL ") + message)


def load_state():
    state = update.load(update.STATE_F, {})
    state.setdefault("recommendations", [])
    state.setdefault("tracking", [])
    return state


def save_state(state):
    update.STATE_F.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def cmd_add(code, cost_text, note):
    if not re.fullmatch(r"[1-9]\d{3}", code):
        raise Fail("股票代號格式不正確，請輸入 4 碼上市股票代號")
    cost = None
    if cost_text:
        try:
            cost = float(cost_text)
        except ValueError:
            raise Fail("成本價必須是數字")
        if not 0 < cost < 100000:
            raise Fail("成本價必須大於 0")
    rows = update.get("STOCK_DAY_ALL")
    row = next((r for r in rows if str(r.get("Code")) == code), None)
    if not row:
        raise Fail(f"找不到上市股票 {code}（上櫃、興櫃與 ETF 暫不支援）")
    close = update.num(row.get("ClosingPrice"))
    if not close or close <= 0:
        raise Fail(f"{code} 目前沒有收盤價（可能停牌）")
    name = str(row.get("Name", ""))[:40]
    date = update.trade_date(rows)

    state = load_state()
    if any(t["code"] == code and t["status"] == "追蹤中" for t in state["tracking"]):
        raise Fail(f"{code} {name} 已經在追蹤中")

    base = cost or close
    stop = round(base * update.STOP_RATIO, 2)
    target = round(base * update.TARGET_RATIO, 2)
    rid = f"{date}-{code}-m{int(time.time())}"
    reason = "手動加入自選" + (f"：{note}" if note else "") + (f"（成本價 {base}）" if cost else "（以最近收盤價為起點）")
    state["recommendations"].append({
        "id": rid, "code": code, "name": name, "industry": "", "date": date, "price": base, "score": None,
        "reason": reason, "source": "manual",
        "risk": f"收盤跌破 {stop}（-7%）視為失效；追蹤滿 {update.MAX_DAYS} 個交易日自動結案。",
    })
    state["tracking"].append({
        "id": "t-" + rid, "recId": rid, "code": code, "name": name, "date": date, "basePrice": base,
        "status": "追蹤中", "source": "manual", "stop": stop, "target": target,
        "snapshots": [{"date": date, "price": close, "at": int(time.time() * 1000)}],
    })
    save_state(state)
    return f"已加入 {code} {name}（起點 {base}，停損 {stop}／目標 {target}）"


def cmd_remove(code):
    state = load_state()
    hit = None
    for t in state["tracking"]:
        if t["code"] == code and t["status"] == "追蹤中":
            hit = t
            break
    if not hit:
        raise Fail(f"找不到追蹤中的 {code}")
    if hit.get("source") == "manual":  # 自選股直接刪除，不影響規則績效
        state["tracking"].remove(hit)
        state["recommendations"] = [r for r in state["recommendations"] if r["id"] != hit["recId"]]
        msg = f"已移除自選股 {code} {hit['name']}"
    else:  # 規則推薦保留紀錄，改為結案
        hit["status"] = "已結案"
        msg = f"{code} {hit['name']} 已手動結案（紀錄保留）"
    save_state(state)
    return msg


def cmd_refresh():
    state = load_state()
    codes = sorted({t["code"] for t in state["tracking"] if t["status"] == "追蹤中"})
    if not codes:
        raise Fail("目前沒有追蹤中的股票")
    try:
        got = quotes.fetch_quotes(codes)
    except Exception as e:
        raise Fail(f"盤中報價來源暫時無法使用：{e}")
    if not got:
        raise Fail("沒有取得任何報價（可能尚未開盤或來源暫時無法使用）")
    LIVE_F.write_text(json.dumps({
        "updatedAt": datetime.now(TZ).isoformat(timespec="seconds"), "quotes": got,
    }, ensure_ascii=False), encoding="utf-8")
    return f"已更新 {len(got)}／{len(codes)} 檔的即時價"


def main():
    action = os.environ.get("ACTION", "").strip()
    code = os.environ.get("CODE", "").strip()
    cost = os.environ.get("COST", "").strip()
    note = re.sub(r"[\x00-\x1f]", " ", os.environ.get("NOTE", "")).strip()[:40]
    nonce = os.environ.get("NONCE", "").strip()[:32]
    try:
        if action == "add":
            msg = cmd_add(code, cost, note)
        elif action == "remove":
            msg = cmd_remove(code)
        elif action == "refresh":
            msg = cmd_refresh()
        else:
            raise Fail(f"未知的動作：{action}")
        write_result(True, msg, nonce, action, code)
    except Fail as e:
        write_result(False, str(e), nonce, action, code)
    except Exception as e:  # 非預期錯誤也要讓網頁看得到原因
        write_result(False, f"執行失敗：{e}", nonce, action, code)


if __name__ == "__main__":
    main()
