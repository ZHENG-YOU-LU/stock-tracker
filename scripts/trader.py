#!/usr/bin/env python3
"""模擬投資人（紙上交易，不會真的下單）。
每個交易日收盤後由 update.py 呼叫一次：用前一個交易日的推薦，依「明日買進計畫」在隔天開盤買進，
再用當天的開高低收判斷停損、停利、時間停損，並記錄每筆交易的原因，方便事後觀察與檢討。"""
import json
import math
from pathlib import Path

# ---- 資金與風險參數 ----
INITIAL_CAPITAL = 1_000_000
RISK_PER_TRADE = 0.01      # 每筆最多虧總資產的 1%（停損時）
RISK_WEAK = 0.005          # 大盤偏弱時風險減半
MAX_POSITIONS = 8          # 最多同時持有幾檔
MAX_POS_PCT = 0.15         # 單檔最多佔總資產 15%
CASH_RESERVE = 0.10        # 至少保留 10% 現金，不滿倉
# ---- 交易規則 ----
STOP_RATIO = 0.93          # 停損：買價 −7%
TARGET_RATIO = 1.15        # 停利：買價 +15%
MAX_HOLD_DAYS = 20         # 持有滿幾個交易日就出場
TRAIL_ACTIVATE = 1.08      # 收盤高點超過買價 +8% 後啟動移動停損
TRAIL_RATIO = 0.93         # 移動停損 = 持有期間收盤高點 −7%（只會往上調）
# ---- 交易成本 ----
FEE_RATE = 0.001425        # 手續費（買賣各一次，未計折扣，偏保守）
FEE_MIN = 1
TAX_RATE = 0.003           # 證交稅（賣出）


def tick(p):
    for limit, t in ((10, 0.01), (50, 0.05), (100, 0.1), (500, 0.5), (1000, 1)):
        if p < limit:
            return t
    return 5


def round_tick(p, up=False):
    t = tick(p)
    n = math.ceil(p / t - 1e-9) if up else math.floor(p / t + 1e-9)
    return round(n * t, 2)


def fee(value):
    return max(FEE_MIN, round(value * FEE_RATE))


def tax(value):
    return round(value * TAX_RATE)


def new_portfolio(capital=INITIAL_CAPITAL):
    return {
        "version": 1, "initialCapital": float(capital), "cash": float(capital),
        "startDate": None, "lastDate": None,
        "positions": [], "trades": [], "equity": [], "journal": [],
        "params": {
            "riskPerTrade": RISK_PER_TRADE, "riskWeak": RISK_WEAK, "maxPositions": MAX_POSITIONS,
            "maxPosPct": MAX_POS_PCT, "cashReserve": CASH_RESERVE, "stop": STOP_RATIO, "target": TARGET_RATIO, "maxHold": MAX_HOLD_DAYS,
            "trailActivate": TRAIL_ACTIVATE, "trailRatio": TRAIL_RATIO, "feeRate": FEE_RATE, "taxRate": TAX_RATE,
        },
    }


def load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save(pf, path):
    Path(path).parent.mkdir(exist_ok=True)
    Path(path).write_text(json.dumps(pf, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def n(v):
    return f"{v:,.0f}"


def step(pf, state, stocks, date, prev_date, weak):
    """處理一個交易日。回傳 True 代表有處理（需要存檔），同一天重複呼叫不會重複交易。"""
    if pf.get("lastDate") == date:
        return False
    if not pf.get("startDate"):
        pf["startDate"] = date
    counts = {"buy": 0, "sell": 0, "skip": 0}

    def mark_equity():
        return pf["cash"] + sum(p["shares"] * p.get("last", p["entryPrice"]) for p in pf["positions"])

    # ---------- 1) 開盤買進：前一個交易日的推薦 ----------
    equity_open = mark_equity()
    recs = [r for r in state.get("recommendations", [])
            if prev_date and r.get("date") == prev_date and r.get("source") != "manual"
            and r.get("scoreVer") == 2 and r.get("plan")]
    done = {t.get("recId") for t in pf["trades"] if t["type"] in ("buy", "skip")}
    held = {p["code"] for p in pf["positions"]}

    def skip(r, why):
        counts["skip"] += 1
        pf["trades"].append({"date": date, "type": "skip", "code": r["code"], "name": r["name"], "recId": r["id"],
                             "score": r.get("score"), "reason": why})

    for r in sorted(recs, key=lambda x: -(x.get("score") or 0)):
        if r["id"] in done or r["code"] in held:
            continue
        s, plan = stocks.get(r["code"]), r["plan"]
        op = s.get("open") if s else None
        if not op:
            skip(r, "當日沒有開盤價（可能停牌），放棄買進")
        elif op > plan["cap"]:
            skip(r, f"開盤 {op} 高於可接受上限 {plan['cap']}（開高超過 1.5%），不追價")
        elif op <= plan["stop"]:
            skip(r, f"開盤 {op} 已跌破停損價 {plan['stop']}，不接刀")
        elif len(pf["positions"]) >= MAX_POSITIONS:
            skip(r, f"持股已達上限 {MAX_POSITIONS} 檔，放棄買進")
        else:
            stop = round_tick(op * STOP_RATIO, up=True)
            target = round_tick(op * TARGET_RATIO)
            risk_pct = RISK_WEAK if weak else RISK_PER_TRADE
            budget = equity_open * risk_pct
            shares = int(budget // (op - stop))
            shares = min(shares, int(equity_open * MAX_POS_PCT // op), int(max(0, pf["cash"] - equity_open * CASH_RESERVE) // (op * (1 + FEE_RATE))))
            if shares < 1:
                skip(r, "可用資金不足（需保留 10% 現金）或風險預算買不到 1 股，放棄買進")
                continue
            value = shares * op
            f = fee(value)
            pf["cash"] -= value + f
            pf["positions"].append({
                "code": r["code"], "name": r["name"], "industry": r.get("industry", ""), "recId": r["id"],
                "score": r.get("score"), "entryDate": date, "entryPrice": op, "shares": shares,
                "stop": stop, "initStop": stop, "target": target, "high": op, "days": 0, "last": op, "buyFee": f,
                "trailing": False,
            })
            held.add(r["code"])
            counts["buy"] += 1
            why = (f"評分 {r.get('score', 0):.0f}，開盤 {op} ≤ 可接受上限 {plan['cap']}，依計畫買進。"
                   f"風險預算 {risk_pct * 100:.1f}%（約 NT$ {n(budget)}），停損 {stop}（−7%）、目標 {target}（+15%）"
                   f" → 買 {shares:,} 股，投入 NT$ {n(value)}" + ("。大盤偏弱，風險預算減半" if weak else ""))
            pf["trades"].append({"date": date, "type": "buy", "code": r["code"], "name": r["name"], "recId": r["id"],
                                 "price": op, "shares": shares, "fee": f, "score": r.get("score"), "reason": why})

    # ---------- 2) 盤中出場判斷（含今天剛買進的）與收盤後調整 ----------
    for p in list(pf["positions"]):
        if p["entryDate"] != date:
            p["days"] += 1
        s = stocks.get(p["code"])
        if not s or not s.get("close"):
            continue
        cl = s["close"]
        op, hi, lo = s.get("open") or cl, s.get("high") or cl, s.get("low") or cl
        px, why = None, None
        if op <= p["stop"]:
            px, why = op, f"跳空開低：開盤 {op} ≤ 停損 {p['stop']}，以開盤價出場"
        elif lo <= p["stop"]:
            px, why = p["stop"], f"觸及{'移動' if p['trailing'] else ''}停損：最低 {lo} ≤ 停損 {p['stop']}，以停損價出場"
        elif op >= p["target"]:
            px, why = op, f"跳空開高：開盤 {op} ≥ 目標 {p['target']}，以開盤價停利"
        elif hi >= p["target"]:
            px, why = p["target"], f"觸及目標：最高 {hi} ≥ 目標 {p['target']}，以目標價停利"
        elif p["days"] >= MAX_HOLD_DAYS:
            px, why = cl, f"持有滿 {MAX_HOLD_DAYS} 個交易日仍未觸及停損或目標，時間停損，以收盤價出場"
        if px is not None:
            value = p["shares"] * px
            f, t = fee(value), tax(value)
            proceeds = value - f - t
            cost = p["entryPrice"] * p["shares"] + p["buyFee"]
            pnl = proceeds - cost
            pf["cash"] += proceeds
            pf["positions"].remove(p)
            counts["sell"] += 1
            pf["trades"].append({
                "date": date, "type": "sell", "code": p["code"], "name": p["name"], "recId": p["recId"],
                "price": px, "shares": p["shares"], "fee": f, "tax": t, "entryDate": p["entryDate"],
                "entryPrice": p["entryPrice"], "days": p["days"], "pnl": round(pnl), "pnlPct": round(pnl / cost * 100, 2),
                "reason": why + f"。含手續費與證交稅，損益 NT$ {pnl:+,.0f}（{pnl / cost * 100:+.2f}%）",
            })
            continue
        p["last"] = cl
        p["high"] = max(p["high"], cl)
        if p["high"] >= p["entryPrice"] * TRAIL_ACTIVATE:  # 賺超過 8% 後，停損跟著收盤高點往上移
            new_stop = round_tick(p["high"] * TRAIL_RATIO, up=True)
            if new_stop > p["stop"]:
                p["stop"], p["trailing"] = new_stop, True

    # ---------- 3) 總資產與手記 ----------
    eq = mark_equity()
    pf["equity"].append({"date": date, "equity": round(eq), "cash": round(pf["cash"]), "n": len(pf["positions"])})
    ret = (eq / pf["initialCapital"] - 1) * 100
    pf["journal"].append({"date": date, "text": (
        f"大盤{'偏弱（風險減半）' if weak else '正常'}｜買進 {counts['buy']} 檔、賣出 {counts['sell']} 檔、放棄 {counts['skip']} 檔｜"
        f"持股 {len(pf['positions'])} 檔、現金 {pf['cash'] / eq * 100:.0f}%｜總資產 NT$ {n(eq)}（{ret:+.2f}%）")})
    pf["lastDate"] = date
    return True
