"""影片裡的分工 → 台股當沖版:
CHIEF 決策(不持倉) / SCAN 掃動能 / VET 風險否決 / SOCIAL 新聞LLM / SIZE 部位 / RISK 出場 / FILLS 見 broker.py"""
import json
import math
import os
import re
from datetime import datetime, timedelta
from broker import cost_per_share
from models import Decision, round_tick, tick_size


def clip(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


# ───────────── SCAN:找出正在動的股票 ─────────────
class ScanAgent:
    """開盤區間突破(ORB) + 量能放大 + 站上VWAP + 漲幅。只做多。"""
    def __init__(self, cfg): self.cfg = cfg

    def evaluate(self, q, tape, now):
        cfg = self.cfg
        open_dt = now.replace(hour=9, minute=0, second=0, microsecond=0)
        or_end = open_dt + timedelta(minutes=cfg.orb_minutes)
        if now < or_end or not tape.rows or tape.rows[0][0] > open_dt + timedelta(minutes=3):
            return None                                   # 區間未形成,或程式開太晚缺資料
        or_rows = [r for r in tape.rows if r[0] < or_end]
        or_high = max(r[1] for r in or_rows)
        vwap = tape.vwap
        elapsed_min = max(1.0, (now - open_dt).total_seconds() / 60)
        recent = tape.since(now - timedelta(minutes=5))
        recent_v = sum(r[2] for r in recent)
        recent_val = sum(r[1] * r[2] for r in recent)
        vol_ratio = recent_v / (tape.v / elapsed_min * 5) if tape.v else 0
        chg = q.last / q.prev_close - 1

        breakout = 1.0 if (q.last > or_high and q.last >= q.high * 0.998) else \
            0.5 if q.last >= or_high * 0.999 else 0.0
        volr = clip((vol_ratio - 1) / 2)
        trend = clip((q.last / vwap - 1) / 0.01) if vwap else 0
        mom = clip(chg / 0.04) if chg > 0 else 0
        score = 0.35 * breakout + 0.25 * volr + 0.20 * trend + 0.20 * mom
        return dict(score=round(score, 3), or_high=or_high, vwap=vwap, vol_ratio=round(vol_ratio, 2),
                    chg=chg, recent_value=recent_val,
                    why=f"ORB{'✓' if breakout == 1 else '~' if breakout else '✗'} 量比{vol_ratio:.1f} "
                        f"VWAP{(q.last / vwap - 1) * 100:+.2f}% 漲幅{chg * 100:+.1f}%" if vwap else "no vwap")


# ───────────── VET:這檔能不能碰(任何一項不過就否決) ─────────────
class VetAgent:
    def __init__(self, cfg, restricted): self.cfg, self.restricted = cfg, restricted

    def evaluate(self, q, feat):
        cfg, r = self.cfg, self.restricted
        bad = []
        if q.code in r["punish"]: bad.append("處置股")
        if q.code in r["manual"]: bad.append("黑名單/禁現沖")
        if cfg.avoid_notice and q.code in r["notice"]: bad.append("注意股")
        if not q.bid or not q.ask: bad.append("無雙邊報價")
        else:
            if (q.ask - q.bid) / q.last > cfg.max_spread_pct: bad.append("價差過大")
        tk = tick_size(q.last)
        if tk / q.last > cfg.max_tick_pct: bad.append(f"跳動單位占{tk / q.last * 100:.2f}%,成本吃光利潤")
        if q.last >= q.limit_up - cfg.limit_buffer_ticks * tk: bad.append("接近/鎖漲停")
        if feat["chg"] > cfg.max_chase_pct: bad.append("漲幅過大不追")
        if feat["recent_value"] < cfg.min_recent_value: bad.append("近5分鐘成交值不足")
        if feat["vol_ratio"] < cfg.min_vol_ratio: bad.append("未放量")
        return (len(bad) == 0), ("通過" if not bad else "、".join(bad))


# ───────────── SOCIAL:新聞標題 → LLM 判斷利多/利空(可選) ─────────────
class SocialAgent:
    def __init__(self, cfg):
        self.cfg, self.cache, self.client = cfg, {}, None
        if cfg.use_llm and os.getenv("ANTHROPIC_API_KEY"):
            try:
                import anthropic
                self.client = anthropic.Anthropic()
            except ImportError:
                pass

    def evaluate(self, q):
        if q.code in self.cache:
            return self.cache[q.code]
        res = (0.5, "LLM/新聞未啟用,中性")
        if self.client:
            from crawler import fetch_headlines
            titles = fetch_headlines(q.code, q.name)
            if not titles:
                res = (0.5, "查無近日新聞,中性")
            else:
                try:
                    msg = self.client.messages.create(
                        model=self.cfg.llm_model, max_tokens=300,
                        system="你是台股當沖風控助理。根據標題判斷今日對該股的消息面:0=重大利空/疑似出貨或炒作警訊,"
                               "0.5=中性,1=明確利多。只回JSON:{\"score\":數字,\"reason\":\"20字內\"}",
                        messages=[{"role": "user", "content": f"{q.name}({q.code})\n" + "\n".join(titles)}])
                    j = json.loads(re.search(r"\{.*\}", msg.content[0].text, re.S).group())
                    res = (clip(float(j["score"])), str(j.get("reason", "")))
                except Exception as e:
                    res = (0.5, f"LLM失敗,中性({type(e).__name__})")
        self.cache[q.code] = res
        return res


# ───────────── SIZE:算股數(純數學,不交給LLM) ─────────────
class SizeAgent:
    def __init__(self, cfg): self.cfg = cfg

    def evaluate(self, q, equity, cash, open_positions):
        cfg = self.cfg
        entry = q.ask + cfg.slippage_ticks * tick_size(q.ask)
        tk = tick_size(entry)
        stop = round_tick(entry - max(cfg.stop_pct * entry, cfg.min_stop_ticks * tk), "down")
        tp = round_tick(entry * (1 + cfg.tp_pct), "up")
        cost = cost_per_share(entry, cfg)
        net_reward, net_risk = (tp - entry) - cost, (entry - stop) + cost
        rr = net_reward / net_risk if net_risk > 0 else 0
        if rr < cfg.min_net_rr:
            return None, f"扣成本後報酬風險比{rr:.2f}<{cfg.min_net_rr}"
        shares = math.floor(cfg.capital * cfg.risk_per_trade / net_risk)
        shares = min(shares, math.floor(cfg.capital * cfg.max_position_pct / entry),
                     math.floor(cash / (entry * 1.003)))
        shares = shares // 1000 * 1000 if shares >= 1000 else (shares if cfg.allow_odd_lot else 0)
        if shares <= 0:
            return None, "資金/風險預算不足以買進最小單位"
        return dict(shares=shares, stop=stop, tp=tp, rr=round(rr, 2)), f"{shares}股 停損{stop} 停利{tp} RR{rr:.2f}"


# ───────────── RISK:持倉管理與出場 ─────────────
class RiskAgent:
    def __init__(self, cfg):
        self.cfg, self.halted, self.consec_losses = cfg, False, 0

    def check_exit(self, p, q, now):
        cfg = self.cfg
        p.hwm = max(p.hwm, q.last)
        if p.hwm >= p.entry_px * (1 + cfg.breakeven_at_pct):          # 保本 + 移動停利
            p.stop = max(p.stop, round_tick(p.entry_px * 1.003, "up"),
                         round_tick(p.hwm * (1 - cfg.trail_pct), "down"))
        if now.time() >= cfg.flat_time: return "尾盤強制平倉"
        if q.last <= p.stop: return "停損/移動停利"
        if q.last >= p.tp: return "達停利目標"
        held = (now - p.entry_ts).total_seconds() / 60
        if held >= cfg.max_hold_min and q.last / p.entry_px - 1 < cfg.time_stop_min_gain:
            return "時間停損"
        return None

    def on_close(self, pnl):
        self.consec_losses = self.consec_losses + 1 if pnl < 0 else 0
        if self.consec_losses >= self.cfg.max_consecutive_losses:
            self.halted = True

    def check_circuit(self, equity):
        if equity - self.cfg.capital <= -self.cfg.capital * self.cfg.daily_loss_limit:
            self.halted = True
            return True
        return False


# ───────────── CHIEF:最後拍板,自己不持倉 ─────────────
class ChiefAgent:
    def __init__(self, cfg, risk: RiskAgent): self.cfg, self.risk, self.trades_by_code = cfg, risk, {}

    def decide(self, q, scan, vet_ok, vet_msg, social, n_pos, held, now, lost_codes):
        cfg = self.cfg
        if self.risk.halted: return Decision("SKIP", q.code, 0, "熔斷:今日停止開新倉")
        if not (cfg.entry_start <= now.time() < cfg.entry_end): return Decision("SKIP", q.code, 0, "非進場時段")
        if q.code in held: return Decision("SKIP", q.code, 0, "已持有")
        if n_pos >= cfg.max_positions: return Decision("SKIP", q.code, 0, "持股檔數已滿")
        if self.trades_by_code.get(q.code, 0) >= cfg.max_trades_per_code: return Decision("SKIP", q.code, 0, "該股今日交易次數已達上限")
        if q.code in lost_codes: return Decision("SKIP", q.code, 0, "該股今日已虧損出場,不再進")
        if not vet_ok: return Decision("SKIP", q.code, scan["score"], f"VET否決:{vet_msg}")
        s_score, s_msg = social
        if s_score < cfg.social_veto_below: return Decision("SKIP", q.code, scan["score"], f"SOCIAL否決:{s_msg}")
        total = scan["score"] + 0.2 * (s_score - 0.5)
        if total < cfg.min_scan_score: return Decision("SKIP", q.code, total, f"分數{total:.2f}不足")
        return Decision("BUY", q.code, total, f"SCAN {scan['why']} | SOCIAL {s_score:.2f} {s_msg}")
