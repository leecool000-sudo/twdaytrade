#!/usr/bin/env python3
"""
台股「明日再被列注意股就進處置」候選名單 → 推播 Discord

邏輯（證交所／櫃買中心「公布或通知注意交易資訊暨處置作業要點」）：
  處置條件（任一）：
    A. 連續 3 個營業日 達第一款
    B. 連續 5 個營業日 達第一～八款
    C. 最近 10 個營業日內 有 6 日 達第一～八款
    D. 最近 30 個營業日內 有 12 日 達第一～八款
  「差一天」= 假設明天(T+1)再被列注意，就會滿足上面任一條：
    A1. 今天、昨天都是第一款（已連 2 日）
    B1. 今天往回已連 4 日注意
    C1. 最近 9 個營業日(含今天)已 5 次
    D1. 最近 29 個營業日(含今天)已 11 次
  排除：已在處置中、或今天已公告處置者；最近一次處置公告日之前的注意次數不再計入。

用法：
  export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
  python tw_disposal_watch.py              # 今天
  python tw_disposal_watch.py --date 2026-11-13 --dry-run
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field

import requests

TZ = dt.timezone(dt.timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (tw-disposal-watch)", "Accept": "application/json"}
LOOKBACK_TRADING_DAYS = 30
C1_GAIN6 = 32.0   # 第一款主要門檻：6 日累積漲幅 32%（另有 25%+價差等條件，未納入估算）
STOCK_CODE_RE = re.compile(r"^\d{4}$")          # 只看普通股；要含 ETF 改成 r"^(\d{4}|00\d{2,4}[A-Z]?)$"

# ───────────────────────── 共用工具 ─────────────────────────

def get_json(url: str, params: dict | None = None, retries: int = 4) -> dict | list:
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5 * (2 ** i))  # 證交所限流時回空白頁，退避 5/10/20/40 秒
    raise RuntimeError(f"GET 失敗 {url} {params}: {last}")


def parse_date(s: str) -> dt.date | None:
    """支援 115/11/13、1151113、2026/11/13、20261113、2026-11-13。"""
    s = str(s).strip()
    m = re.match(r"^(\d{2,4})[/\-.]?(\d{1,2})[/\-.]?(\d{1,2})", s)
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    if y < 1911:
        y += 1911
    try:
        return dt.date(y, mo, d)
    except ValueError:
        return None


def tables_of(payload) -> list[tuple[list[str], list[list]]]:
    """把 TWSE rwd / TPEx www 兩種 JSON 統一成 [(fields, rows)]。"""
    out = []
    if isinstance(payload, dict):
        if "fields" in payload and "data" in payload:
            out.append((payload["fields"], payload["data"]))
        for t in payload.get("tables", []) or []:
            if t.get("fields") and t.get("data") is not None:
                out.append((t["fields"], t["data"]))
    return out


def col(fields: list[str], *keys: str) -> int | None:
    for k in keys:
        for i, f in enumerate(fields):
            if k in str(f):
                return i
    return None


CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def cn2int(s: str) -> int:
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + CN_NUM.get(s[1:], 0)
    if "十" in s:
        a, b = s.split("十")
        return CN_NUM.get(a, 0) * 10 + (CN_NUM.get(b, 0) if b else 0)
    return CN_NUM.get(s, 0)


def classify(text: str) -> tuple[bool, bool]:
    """回傳 (是否第一款, 是否計入處置次數[第一～八款])。"""
    t = re.sub(r"\s", "", str(text))
    nums = [cn2int(x) for x in re.findall(r"第([一二三四五六七八九十\d]+)款", t)]
    # 過濾掉「第四條第一項」這種，只留款
    if nums:
        return (1 in nums, any(1 <= n <= 8 for n in nums))
    # 證交所注意股多半只寫數據描述、不寫款次 → 用關鍵字判斷
    if "當日沖銷" in t or "當沖" in t:
        return (False, False)  # 當沖占比過高不在第一～八款，只影響處置天數
    is_c1 = ("漲跌百分比" in t or "漲幅" in t) and not any(k in t for k in ("差幅", "全體", "同類", "週轉", "成交量", "本益比", "淨值比", "券資比", "借券"))
    return (is_c1, True)


# ───────────────────────── 資料抓取 ─────────────────────────

def trading_days(end: dt.date, n: int) -> list[dt.date]:
    """用證交所 FMTQIK（每月市場成交資訊）取得實際交易日。"""
    days: set[dt.date] = set()
    cur = end.replace(day=1)
    for _ in range(4):
        try:
            p = get_json("https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK",
                         {"date": cur.strftime("%Y%m%d"), "response": "json"})
            for _, rows in tables_of(p):
                for r in rows:
                    d = parse_date(r[0])
                    if d and d <= end:
                        days.add(d)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 交易日曆 {cur:%Y-%m} 取得失敗：{e}", file=sys.stderr)
        cur = (cur - dt.timedelta(days=1)).replace(day=1)
        time.sleep(1)
    if not days:  # 退而求其次：平日
        print("[warn] 改用平日當交易日（遇假日可能誤差）", file=sys.stderr)
        d = end
        while len(days) < n + 10:
            if d.weekday() < 5:
                days.add(d)
            d -= dt.timedelta(days=1)
    return sorted(days)[-n:]


@dataclass
class Notice:
    market: str
    code: str
    name: str
    date: dt.date
    text: str
    close: float | None
    c1: bool
    counted: bool


def fetch_twse_notices(start: dt.date, end: dt.date) -> list[Notice]:
    p = get_json("https://www.twse.com.tw/rwd/zh/announcement/notice", {
        "querytype": "1", "stockNo": "", "selectType": "", "sortKind": "STKNO",
        "startDate": start.strftime("%Y%m%d"), "endDate": end.strftime("%Y%m%d"), "response": "json"})
    return _parse_notices(p, "上市")


def fetch_tpex_notices(start: dt.date, end: dt.date) -> list[Notice]:
    p = get_json("https://www.tpex.org.tw/www/zh-tw/bulletin/attention", {
        "startDate": start.strftime("%Y/%m/%d"), "endDate": end.strftime("%Y/%m/%d"), "response": "json"})
    return _parse_notices(p, "上櫃")


def _parse_notices(payload, market: str) -> list[Notice]:
    out = []
    for fields, rows in tables_of(payload):
        ic = col(fields, "證券代號", "代號")
        iname = col(fields, "證券名稱", "名稱")
        idate = col(fields, "公告日期", "日期")
        itext = col(fields, "注意交易資訊", "公布注意", "原因", "注意")
        iclose = col(fields, "收盤價")
        if None in (ic, idate, itext):
            continue
        for r in rows:
            code = str(r[ic]).strip()
            if not STOCK_CODE_RE.match(code):
                continue
            d = parse_date(r[idate])
            if not d:
                continue
            text = str(r[itext])
            c1, counted = classify(text)
            close = None
            if iclose is not None:
                try:
                    close = float(str(r[iclose]).replace(",", ""))
                except ValueError:
                    pass
            out.append(Notice(market, code, str(r[iname]).strip() if iname is not None else "",
                              d, re.sub(r"<[^>]+>", "", text), close, c1, counted))
    return out


@dataclass
class Disposal:
    code: str
    announce: dt.date
    start: dt.date | None
    end: dt.date | None


def _parse_disposals(payload) -> list[Disposal]:
    out = []
    for fields, rows in tables_of(payload):
        ic = col(fields, "證券代號", "代號")
        ia = col(fields, "公布日期", "公告日期", "日期")
        ip = col(fields, "處置起迄", "處置期間", "起迄")
        if ic is None or ia is None:
            continue
        for r in rows:
            code = str(r[ic]).strip()
            a = parse_date(r[ia])
            s = e = None
            src = str(r[ip]) if ip is not None else " ".join(map(str, r))
            m = re.search(r"(\d{2,4}/\d{1,2}/\d{1,2})\s*[~～至-]\s*(\d{2,4}/\d{1,2}/\d{1,2})", src)
            if m:
                s, e = parse_date(m.group(1)), parse_date(m.group(2))
            if a:
                out.append(Disposal(code, a, s, e))
    return out


def fetch_disposals(start: dt.date, end: dt.date) -> list[Disposal]:
    res = []
    try:
        res += _parse_disposals(get_json("https://www.twse.com.tw/rwd/zh/announcement/punish", {
            "querytype": "3", "startDate": start.strftime("%Y%m%d"), "endDate": end.strftime("%Y%m%d"), "response": "json"}))
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 上市處置清單失敗：{e}", file=sys.stderr)
    try:
        res += _parse_disposals(get_json("https://www.tpex.org.tw/www/zh-tw/bulletin/disposal", {
            "startDate": start.strftime("%Y/%m/%d"), "endDate": end.strftime("%Y/%m/%d"), "response": "json"}))
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 上櫃處置清單失敗：{e}", file=sys.stderr)
    return res


# ───────────────────────── 核心判斷 ─────────────────────────

@dataclass
class Candidate:
    market: str
    code: str
    name: str
    reasons: list[str] = field(default_factory=list)
    today_text: str = ""
    close: float | None = None
    streak_c1: int = 0
    streak_all: int = 0
    cnt10: int = 0
    cnt30: int = 0
    price: dict = field(default_factory=dict)


def screen(notices: list[Notice], disposals: list[Disposal], days: list[dt.date]) -> list[Candidate]:
    today = days[-1]
    idx = {d: i for i, d in enumerate(days)}

    # 最近一次處置公告日：之前的注意次數不再累計；處置中 / 今日公告處置者排除
    last_disp: dict[str, dt.date] = {}
    excluded: set[str] = set()
    for d in disposals:
        last_disp[d.code] = max(last_disp.get(d.code, d.announce), d.announce)
        if d.announce >= today or (d.end and d.end > today):
            excluded.add(d.code)

    by_code: dict[str, dict[dt.date, Notice]] = defaultdict(dict)
    meta: dict[str, Notice] = {}
    for n in notices:
        if n.date not in idx:
            continue
        if n.code in last_disp and n.date <= last_disp[n.code]:
            continue
        prev = by_code[n.code].get(n.date)
        if prev:  # 同日多款 → 合併
            prev.c1 |= n.c1
            prev.counted |= n.counted
            prev.text += "；" + n.text
        else:
            by_code[n.code][n.date] = n
        meta[n.code] = n

    out = []
    for code, m in by_code.items():
        if code in excluded:
            continue
        c1 = [bool(m.get(d) and m[d].c1) for d in days]
        cnt = [bool(m.get(d) and m[d].counted) for d in days]

        def streak(arr):
            s = 0
            for v in reversed(arr):
                if not v:
                    break
                s += 1
            return s

        s1, sa = streak(c1), streak(cnt)
        n9, n29 = sum(cnt[-9:]), sum(cnt[-29:])
        reasons = []
        if s1 >= 2:
            reasons.append(f"第一款已連 {s1} 日 → 明日第一款即「連 3 日」")
        if sa >= 4:
            reasons.append(f"已連續 {sa} 日注意 → 明日即「連 5 日」")
        if n9 >= 5:
            reasons.append(f"近 9 日已 {n9} 次 → 明日即「10 日 6 次」")
        if n29 >= 11:
            reasons.append(f"近 29 日已 {n29} 次 → 明日即「30 日 12 次」")
        if not reasons:
            continue
        info = meta[code]
        tn = m.get(today)
        out.append(Candidate(info.market, code, info.name, reasons,
                             tn.text if tn else "（今日未列注意）",
                             tn.close if tn else None, s1, sa, n9, n29))
    # 第一款連 2 日的最優先（最常是「隔天拉一根」的型態）
    out.sort(key=lambda c: (-(c.streak_c1 >= 2), -c.streak_all, -c.cnt10, c.code))
    return out


# ───────────────────────── 價格輔助（官方個股日成交） ─────────────────────────

def daily_bars(c: "Candidate", end: dt.date, months: int = 3) -> list[tuple[dt.date, float, float]]:
    """回傳 [(日期, 最高, 收盤)]，資料來自證交所 STOCK_DAY / 櫃買 tradingStock。"""
    bars = {}
    cur = end.replace(day=1)
    for _ in range(months):
        try:
            if c.market == "上市":
                p = get_json("https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY",
                             {"date": cur.strftime("%Y%m%d"), "stockNo": c.code, "response": "json"})
            else:
                p = get_json("https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock",
                             {"code": c.code, "date": cur.strftime("%Y/%m/%d"), "response": "json"})
            for _, rows in tables_of(p):
                for r in rows:
                    d = parse_date(r[0])
                    try:
                        hi, cl = float(str(r[4]).replace(",", "")), float(str(r[6]).replace(",", ""))
                    except (ValueError, IndexError):
                        continue
                    if d and d <= end:
                        bars[d] = (d, hi, cl)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {c.code} {cur:%Y-%m} 價格失敗：{e}", file=sys.stderr)
        cur = (cur - dt.timedelta(days=1)).replace(day=1)
        time.sleep(2.0)  # 證交所有頻率限制（約每 5 秒 3 次），別太快
    return [bars[k] for k in sorted(bars)]


def enrich_prices(cands: list[Candidate], end: dt.date) -> None:
    for attempt in range(2):  # 第二輪補抓第一輪失敗的
        todo = [c for c in cands if not c.price]
        if not todo:
            return
        if attempt:
            print(f"[info] 補抓 {len(todo)} 檔價格", file=sys.stderr)
            time.sleep(15)
        for c in todo:
            _enrich_one(c, end)


def _enrich_one(c: "Candidate", end: dt.date) -> None:
        b = daily_bars(c, end)
        if len(b) < 7 or b[-1][0] != end:
            return
        closes = [x[2] for x in b]
        highs = [x[1] for x in b]
        last, prev = closes[-1], closes[-2]
        # 官方「最近六個營業日累積漲跌幅」= 每日漲跌幅「加總」（非複利），已用實際公告驗證
        pct = [(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, len(closes))]
        hi60 = max(highs[-60:])
        c.price = {
            "close": last,
            "chg": (last / prev - 1) * 100,
            "gain6": sum(pct[-6:]),
            "from_hi20": (last / max(highs[-20:]) - 1) * 100,
            "at_hi60": last >= hi60 * 0.98,
            "sum5": sum(pct[-5:]),
            "need_c1": C1_GAIN6 - sum(pct[-5:]),   # 明天的 6 日 = 最近 5 日 + 明天
        }


# ───────────────────────── TradingView ─────────────────────────

def tv_symbol(c: Candidate) -> str:
    return f"{'TWSE' if c.market == '上市' else 'TPEX'}:{c.code}"


def tv_url(c: Candidate) -> str:
    return f"https://www.tradingview.com/chart/?symbol={tv_symbol(c).replace(':', '%3A')}"


def tv_watchlist(cands: list[Candidate], target: dt.date) -> str:
    """TradingView 觀察清單匯入格式：逗號分隔，### 開頭為分區名稱。"""
    return ",".join([f"###處置候選_{target:%m%d}"] + [tv_symbol(c) for c in cands])


# ───────────────────────── Discord ─────────────────────────

def fmt(c: Candidate) -> str:
    p = c.price
    t = c.today_text
    arrow = "📉跌幅觸發 " if re.search(r"跌幅達", t) and not re.search(r"漲幅達", t) else ""
    head = f"{arrow}[**{c.code} {c.name}**](<{tv_url(c)}>)（{c.market}）"
    if p:
        hi = "｜🔺近60日高點" if p["at_hi60"] else ""
        head += (f"\n收 {p['close']:.2f}（{p['chg']:+.2f}%）｜6日 {p['gain6']:+.1f}%｜"
                 f"距20日高 {p['from_hi20']:+.1f}%{hi}")
        if arrow:  # 跌幅觸發：看明天再跌多少會達第一款
            need = -C1_GAIN6 - p["sum5"]
            tag = "已達標" if need >= 0 else ("跌停內可達" if need >= -10 else "需跌停以上")
            head += f"\n明日漲跌 ≤ {need:+.1f}% 即達第一款(6日-32%)（{tag}，估算）"
        else:
            need = p["need_c1"]
            tag = "已達標" if need <= 0 else ("⚡漲停內可達" if need <= 10 else "需漲停以上")
            head += f"\n明日漲跌 ≥ {need:+.1f}% 即達第一款(6日+32%)（{tag}，估算）"
    elif c.close:
        head += f"\n收 {c.close:.2f}"
    body = "\n".join(f"• {r}" for r in c.reasons)
    txt = t[:110] + ("…" if len(t) > 110 else "")
    return f"{head}\n{body}\n> 今日：{txt}"


def push_discord(webhook: str, title: str, blocks: list[str],
                 attachment: tuple[str, bytes] | None = None) -> None:
    msgs, cur = [], title
    for b in blocks:
        if len(cur) + len(b) + 2 > 1900:
            msgs.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + b
    if cur:
        msgs.append(cur)
    for i, m in enumerate(msgs):
        # flags=4：不展開連結預覽，避免每個 TradingView 連結都跑出一大張卡片
        payload = {"content": m, "allowed_mentions": {"parse": []}, "flags": 4}
        last = i == len(msgs) - 1
        for _ in range(3):
            if last and attachment:   # 附件放在最後一則
                r = requests.post(webhook, data={"payload_json": json.dumps(payload)},
                                  files={"files[0]": (attachment[0], attachment[1], "text/plain")}, timeout=30)
            else:
                r = requests.post(webhook, json=payload, timeout=20)
            if r.status_code != 429:
                break
            time.sleep(float(r.json().get("retry_after", 2)))
        r.raise_for_status()
        time.sleep(0.6)


# ───────────────────────── main ─────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD，預設今天（台北時間）")
    ap.add_argument("--dry-run", action="store_true", help="只印出，不推 Discord")
    ap.add_argument("--no-price", action="store_true", help="不抓價格")
    ap.add_argument("--json", help="另存結果 JSON 路徑")
    a = ap.parse_args()

    target = dt.date.fromisoformat(a.date) if a.date else dt.datetime.now(TZ).date()
    days = trading_days(target, LOOKBACK_TRADING_DAYS)
    if not days or days[-1] != target:
        print(f"{target} 非交易日（或資料尚未更新），結束。")
        return 0
    start = days[0]

    notices = []
    for fn, label in ((fetch_twse_notices, "上市"), (fetch_tpex_notices, "上櫃")):
        try:
            got = fn(start, target)
            print(f"[info] {label}注意股紀錄 {len(got)} 筆")
            notices += got
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {label}注意股失敗：{e}", file=sys.stderr)
    if not any(n.date == target for n in notices):
        print("[warn] 今日注意股資料為空，可能尚未公告；稍後再跑。", file=sys.stderr)

    disposals = fetch_disposals(start - dt.timedelta(days=45), target)
    cands = screen(notices, disposals, days)
    if cands and not a.no_price:
        enrich_prices(cands, target)

    title = (f"📢 **{target:%Y/%m/%d} 明日再注意即處置｜共 {len(cands)} 檔**\n"
             f"_條件：連3日第一款／連5日／10日6次／30日12次 差一天_")
    blocks = [fmt(c) for c in cands] or ["今日沒有符合條件的個股。"]

    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump([c.__dict__ for c in cands], f, ensure_ascii=False, indent=2, default=str)

    tv_name = f"tradingview_{target:%Y%m%d}.txt"
    tv_text = tv_watchlist(cands, target)
    os.makedirs("output", exist_ok=True)
    with open(os.path.join("output", tv_name), "w", encoding="utf-8") as f:
        f.write(tv_text)
    if cands:
        blocks.append(f"📎 附件 `{tv_name}`：TradingView 觀察清單 →「匯入清單」即可")

    print(title)
    for b in blocks:
        print("\n" + b)

    hook = os.getenv("DISCORD_WEBHOOK_URL")
    if a.dry_run:
        return 0
    if not hook:
        print("[error] 未設定 DISCORD_WEBHOOK_URL", file=sys.stderr)
        return 1
    push_discord(hook, title, blocks, (tv_name, tv_text.encode("utf-8")) if cands else None)
    print("[info] 已推播 Discord")
    return 0


if __name__ == "__main__":
    sys.exit(main())
