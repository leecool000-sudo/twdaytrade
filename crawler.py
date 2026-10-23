"""CRAWLER:資料層。模擬行情(SimFeed)與證交所即時行情(TwseMisFeed)、處置/注意股名單、新聞標題。
TwseMisFeed 與各 OpenAPI 端點需要網路,請在你自己的電腦上執行並自行確認欄位仍然有效。"""
import math
import os
import random
import time as _time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from models import Quote, round_tick, tick_size

HEADERS = {"User-Agent": "Mozilla/5.0"}


# ───────────── 名單 / 新聞(需網路) ─────────────
def load_universe_twse(top_n=60):
    """取前一交易日成交值前N名的上市個股(排除ETF與非4碼代號)。"""
    import requests
    rows = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
                        headers=HEADERS, timeout=15).json()
    out = []
    for x in rows:
        code = x.get("Code", "")
        if len(code) != 4 or not code.isdigit() or code.startswith("00"):
            continue
        try:
            val = float(str(x.get("TradeValue", "0")).replace(",", ""))
        except ValueError:
            continue
        out.append((val, code, x.get("Name", "")))
    out.sort(reverse=True)
    return [(c, n, "tse") for _, c, n in out[:top_n]]


def load_restricted(manual_file="restricted.txt"):
    """回傳 {'punish':set, 'notice':set, 'manual':set}。
    manual_file 放你自己的黑名單(例如券商軟體顯示「禁現沖」的代號),一行一檔。"""
    out = {"punish": set(), "notice": set(), "manual": set()}
    try:
        import requests
        endpoints = (("punish", "https://openapi.twse.com.tw/v1/announcement/punish"),
                     ("notice", "https://openapi.twse.com.tw/v1/announcement/notice"))
        for key, url in endpoints:
            try:
                for row in requests.get(url, headers=HEADERS, timeout=15).json():
                    for k in ("Code", "證券代號", "公司代號"):
                        if row.get(k):
                            out[key].add(str(row[k]).strip())
            except Exception as e:
                print(f"[CRAWLER] 警告:{key} 名單抓取失敗:{e}")
    except ImportError:
        pass
    if os.path.exists(manual_file):
        out["manual"] = {l.strip() for l in open(manual_file) if l.strip() and not l.startswith("#")}
    return out


def fetch_headlines(code, name, n=8):
    import requests
    q = urllib.parse.quote(f"{name} {code} when:1d")
    url = f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    try:
        root = ET.fromstring(requests.get(url, headers=HEADERS, timeout=10).content)
        return [i.findtext("title") for i in root.iter("item")][:n]
    except Exception:
        return []


# ───────────── 即時行情:證交所 MIS ─────────────
class TwseMisFeed:
    """證交所基本市況報導網站(非官方保證的API,有頻率限制,輪詢間隔請 >= 5 秒)。"""
    URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"

    def __init__(self, universe):
        import requests
        self.universe = universe                      # [(code, name, 'tse'|'otc')]
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.s.get("https://mis.twse.com.tw/stock/index.jsp", timeout=10)  # 取得cookie

    @staticmethod
    def _f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    def _first(self, s):
        for p in (s or "").split("_"):
            v = self._f(p)
            if v:
                return v
        return None

    def snapshot(self, now):
        quotes = {}
        names = {c: n for c, n, _ in self.universe}
        for i in range(0, len(self.universe), 40):
            chunk = self.universe[i:i + 40]
            ex_ch = "|".join(f"{ex}_{c}.tw" for c, _, ex in chunk)
            try:
                r = self.s.get(self.URL, params={"ex_ch": ex_ch, "json": "1", "delay": "0",
                                                 "_": int(_time.time() * 1000)}, timeout=10).json()
            except Exception as e:
                print(f"[CRAWLER] 行情抓取失敗:{e}")
                continue
            for m in r.get("msgArray", []):
                last, y = self._f(m.get("z")), self._f(m.get("y"))
                if last is None or y is None:
                    continue                                   # 尚未成交
                c = m["c"]
                quotes[c] = Quote(c, names.get(c, m.get("n", c)), now, last, y,
                                  self._f(m.get("o")) or last, self._f(m.get("h")) or last,
                                  self._f(m.get("l")) or last, self._f(m.get("v")) or 0.0,
                                  self._first(m.get("b")), self._first(m.get("a")),
                                  self._f(m.get("u")) or round_tick(y * 1.1, "down"),
                                  self._f(m.get("w")) or round_tick(y * 0.9, "up"))
        return quotes


# ───────────── 模擬行情(離線測試用) ─────────────
class SimFeed:
    """隨機漫步 + 幾檔「真突破」與「假突破」,用來驗證流程,不代表真實市場。"""
    def __init__(self, seed=7, n=40, interval=10):
        self.rng = random.Random(seed)
        self.interval = interval
        self.stocks = {}
        prices = [38, 58, 72, 128, 165, 240, 380, 520, 860, 1250]
        for i in range(n):
            code = str(2000 + i * 13)
            prev = self.rng.choice(prices)
            self.stocks[code] = dict(name=f"模擬{code}", prev=prev, px=float(prev), vol=0.0,
                                     hi=prev, lo=prev, op=None, kind="noise",
                                     turn=self.rng.uniform(2e6, 1.2e7), t_start=0, t_len=0)
        movers = self.rng.sample(list(self.stocks), 8)
        for j, c in enumerate(movers):
            s = self.stocks[c]
            s["kind"] = "real" if j % 2 == 0 else "fake"
            s["t_start"] = self.rng.randint(80, 400)           # 第幾步開始(開盤後約13~67分)
            s["t_len"] = self.rng.randint(120, 300)
            s["prev"] = s["px"] = float(self.rng.choice([165, 240, 380, 520]))
            s["hi"] = s["lo"] = s["prev"]
            s["turn"] *= 2.5
        self.step = 0
        self.punish = {movers[1]}                               # 其中一檔當作處置股,測試VET否決

    def snapshot(self, now):
        self.step += 1
        out = {}
        for c, s in self.stocks.items():
            sig = 0.0004
            drift = 0.0
            mult = 1.0
            k = self.step - s["t_start"]
            if s["kind"] != "noise" and 0 <= k < s["t_len"]:
                mult = 3.0
                if s["kind"] == "real":
                    drift = 0.00028
                else:
                    drift = 0.00045 if k < s["t_len"] * 0.35 else -0.00060   # 假突破:先拉後殺
            ret = self.rng.gauss(drift, sig)
            p = max(1.0, s["px"] * (1 + ret))
            lu, ld = round_tick(s["prev"] * 1.1, "down"), round_tick(s["prev"] * 0.9, "up")
            px = min(max(round_tick(p), ld), lu)
            s["px"] = px
            s["op"] = s["op"] or px
            s["hi"], s["lo"] = max(s["hi"], px), min(s["lo"], px)
            lots = s["turn"] * mult * self.rng.uniform(0.5, 1.5) / (px * 1000)
            s["vol"] += lots
            tk = tick_size(px)
            bid = round_tick(px, "down")
            out[c] = Quote(c, s["name"], now, px, s["prev"], s["op"], s["hi"], s["lo"], s["vol"],
                           bid, round(bid + tk, 2), lu, ld)
        return out

    def restricted(self):
        return {"punish": set(self.punish), "notice": set(), "manual": set()}
