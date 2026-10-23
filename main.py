"""台股當沖多代理人系統(紙上交易)。
用法:
  python main.py --mode sim  --seed 7           # 離線模擬行情,驗證流程
  python main.py --mode live --top 60           # 證交所即時行情 + 紙上交易(開盤前啟動)
"""
import argparse
import csv
import os
import time as _time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from agents import ChiefAgent, RiskAgent, ScanAgent, SizeAgent, SocialAgent, VetAgent
from broker import PaperBroker
from config import Config
from crawler import SimFeed, TwseMisFeed, load_restricted, load_universe_twse
from notifier import Notifier
from models import Tape


def log(now, tag, msg):
    print(f"[{now:%H:%M:%S}] {tag:<7}▶ {msg}")


def run(cfg, feed, restricted, clock, sleeper=None, out_dir="out", notifier=None):
    notifier = notifier or Notifier.from_env()
    scan, vet, social = ScanAgent(cfg), VetAgent(cfg, restricted), SocialAgent(cfg)
    size, risk = SizeAgent(cfg), RiskAgent(cfg)
    chief, broker = ChiefAgent(cfg, risk), PaperBroker(cfg)
    tapes, lost_codes, curve = {}, set(), []
    last_skip = {}
    stale, prev_vol = 0, None
    notified_stop = {}
    notifier.info("系統啟動:開始監控(手動下單模式,系統只發訊號)")

    def close(code, q, now, reason):
        rec = broker.sell(code, q, now, reason)
        risk.on_close(rec["pnl"])
        if rec["pnl"] < 0: lost_codes.add(code)
        log(now, "RISK", f"平倉 {code} {rec['name']} {reason} 損益{rec['pnl']:+,.0f}")
        notifier.exit(rec, q, now)

    for now in clock:
        quotes = feed.snapshot(now)
        if now.time() >= dtime(9, 5):                                   # 休市日/行情被擋 → 自動中止
            tv = sum(q.volume for q in quotes.values())
            stale = stale + 1 if (not quotes or tv == prev_vol) else 0
            prev_vol = tv
            if stale >= 12:
                log(now, "CRAWLER", "連續12次無新行情:可能休市日,或行情來源擋掉了此IP。本日中止。")
                notifier.alert("連續12次無新行情,系統已中止(休市日或行情被擋)。若今天有開盤,請自行盯盤!")
                break
        for c, q in quotes.items():
            tapes.setdefault(c, Tape()).add(q)

        # 1) RISK 先處理持倉
        for code in list(broker.pos):
            q = quotes.get(code)
            if q is None: continue
            reason = risk.check_exit(broker.pos[code], q, now)
            if reason:
                close(code, q, now, reason); notified_stop.pop(code, None)
            else:                                    # 停損累計上移 ≥ 0.4% 才通知,避免手動交易被洗版
                p = broker.pos[code]
                if p.stop - notified_stop.get(code, p.stop) >= 0.004 * p.entry_px:
                    notifier.stop_update(p, q, now); notified_stop[code] = p.stop
        eq = broker.equity(quotes)
        curve.append(eq)
        if not risk.halted and risk.check_circuit(eq):
            log(now, "RISK", f"觸發單日虧損熔斷(權益{eq:,.0f}),全部平倉並停止交易")
            notifier.alert("觸發單日虧損熔斷:系統將不再發出買進訊號,建議你也停手。")
            for code in list(broker.pos):
                if code in quotes: close(code, quotes[code], now, "單日熔斷")

        # 2) SCAN → VET → SOCIAL → CHIEF → SIZE → FILLS
        if cfg.entry_start <= now.time() < cfg.entry_end and not risk.halted:
            cands = []
            for c, q in quotes.items():
                f = scan.evaluate(q, tapes[c], now)
                if f and f["score"] >= cfg.min_scan_score: cands.append((f["score"], c, f))
            for _, c, f in sorted(cands, reverse=True)[:cfg.top_k]:
                q = quotes[c]
                ok, vmsg = vet.evaluate(q, f)
                soc = social.evaluate(q) if ok else (0.5, "")
                d = chief.decide(q, f, ok, vmsg, soc, len(broker.pos), broker.pos, now, lost_codes)
                if d.action != "BUY":
                    if last_skip.get(c) != d.reason:                     # 避免洗版
                        last_skip[c] = d.reason
                        log(now, "CHIEF", f"略過 {c} {q.name}:{d.reason}")
                    continue
                plan, smsg = size.evaluate(q, eq, broker.cash, len(broker.pos))
                if plan is None:
                    if last_skip.get(("size", c)) != smsg:
                        last_skip[("size", c)] = smsg
                        log(now, "SIZE", f"{c} 不下單:{smsg}")
                    continue
                p = broker.buy(q, plan["shares"], plan["stop"], plan["tp"], now)
                if p:
                    chief.trades_by_code[c] = chief.trades_by_code.get(c, 0) + 1
                    log(now, "CHIEF", f"買進 {c} {q.name} 分數{d.score:.2f} | {d.reason}")
                    log(now, "FILLS", f"成交 {p.shares}股 @ {p.entry_px} | {smsg}")
                    notifier.buy(q, p, plan, d, now); notified_stop[c] = p.stop

    for code in list(broker.pos):                                        # 保險:收盤後仍有部位
        if code in quotes: close(code, quotes[code], now, "收盤保險平倉")
    stats = report(cfg, broker, curve, out_dir)
    notifier.summary(stats)
    return broker


def report(cfg, broker, curve, out_dir):
    t = broker.trades
    wins = [x["pnl"] for x in t if x["pnl"] > 0]
    loss = [x["pnl"] for x in t if x["pnl"] <= 0]
    net = sum(x["pnl"] for x in t)
    peak, mdd = curve[0] if curve else cfg.capital, 0
    for e in curve:
        peak = max(peak, e); mdd = max(mdd, peak - e)
    print("\n═══ 今日結算(紙上交易)═══")
    print(f"交易筆數 {len(t)} | 勝率 {len(wins) / len(t) * 100 if t else 0:.0f}% | "
          f"淨損益 {net:+,.0f} ({net / cfg.capital * 100:+.2f}%)")
    print(f"手續費 {broker.fees:,.0f} | 證交稅 {broker.tax:,.0f} | 最大回撤 {mdd:,.0f}"
          + (f" | 獲利因子 {sum(wins) / -sum(loss):.2f}" if loss and sum(loss) < 0 else ""))
    os.makedirs(out_dir, exist_ok=True)
    if t:
        path = os.path.join(out_dir, f"trades_{datetime.now():%Y%m%d_%H%M%S}.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=t[0].keys()); w.writeheader(); w.writerows(t)
        print(f"交易明細已存:{path}")
    return dict(n=len(t), win=len(wins) / len(t) * 100 if t else 0, net=net,
                fees=broker.fees, tax=broker.tax, mdd=mdd)


def sim_clock(interval):
    d = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    end = d.replace(hour=13, minute=30)
    while d <= end:
        yield d
        d += timedelta(seconds=interval)


def live_clock(interval, cfg):
    tz = ZoneInfo("Asia/Taipei")
    while True:
        now = datetime.now(tz).replace(tzinfo=None)
        if now.time() > cfg.market_close: return
        if now.time() >= datetime.strptime("09:00", "%H:%M").time():
            yield now
        _time.sleep(interval)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["sim", "live"], default="sim")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--top", type=int, default=60)
    ap.add_argument("--interval", type=int, default=None)
    a = ap.parse_args()
    cfg = Config()
    if a.mode == "sim":
        iv = a.interval or 10
        feed = SimFeed(seed=a.seed, interval=iv)
        run(cfg, feed, feed.restricted(), sim_clock(iv), notifier=Notifier.from_env("[模擬]"))
    else:
        iv = a.interval or 5                                              # 證交所MIS有頻率限制
        uni = load_universe_twse(a.top)
        print(f"[CRAWLER] 觀察名單 {len(uni)} 檔;處置/注意股名單載入中…")
        run(cfg, TwseMisFeed(uni), load_restricted(), live_clock(iv, cfg),
            notifier=Notifier.from_env("[實盤行情]"))
