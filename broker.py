"""FILLS:下單與成交。PaperBroker 為紙上交易(含手續費、證交稅、滑價)。"""
from datetime import datetime
from models import Position, Quote, round_tick, tick_size


def fee(value, cfg):
    return max(cfg.min_fee, round(value * cfg.fee_rate * cfg.fee_discount))


def cost_per_share(price, cfg):
    """每股來回成本估計(手續費+證交稅+兩次滑價),用於SIZE評估報酬風險比。"""
    return price * (2 * cfg.fee_rate * cfg.fee_discount + cfg.day_trade_tax) \
        + 2 * cfg.slippage_ticks * tick_size(price)


class PaperBroker:
    def __init__(self, cfg):
        self.cfg = cfg
        self.cash = cfg.capital
        self.pos = {}            # code -> Position
        self.trades = []         # 已平倉交易紀錄
        self.fees = 0.0
        self.tax = 0.0

    def buy(self, q: Quote, shares, stop, tp, now):
        cfg = self.cfg
        px = round_tick(q.ask + cfg.slippage_ticks * tick_size(q.ask), "up")
        px = min(px, q.limit_up)
        value = px * shares
        f = fee(value, cfg)
        if value + f > self.cash or shares <= 0:
            return None
        self.cash -= value + f
        self.fees += f
        self.pos[q.code] = Position(q.code, q.name, shares, px, now, f, stop, tp, px)
        return self.pos[q.code]

    def sell(self, code, q: Quote, now, reason):
        cfg = self.cfg
        p = self.pos.pop(code)
        px = round_tick((q.bid or q.last) - cfg.slippage_ticks * tick_size(q.bid or q.last), "down")
        px = max(px, q.limit_down)
        value = px * p.shares
        f, t = fee(value, cfg), round(value * cfg.day_trade_tax)
        self.cash += value - f - t
        self.fees += f
        self.tax += t
        pnl = (value - f - t) - (p.entry_px * p.shares + p.entry_fee)
        rec = dict(code=code, name=p.name, shares=p.shares, entry_ts=p.entry_ts.strftime("%H:%M:%S"),
                   exit_ts=now.strftime("%H:%M:%S"), entry_px=p.entry_px, exit_px=px,
                   fee=p.entry_fee + f, tax=t, pnl=round(pnl, 1), reason=reason)
        self.trades.append(rec)
        return rec

    def equity(self, quotes):
        return self.cash + sum(p.shares * ((quotes[c].bid or quotes[c].last) if c in quotes else p.entry_px)
                               for c, p in self.pos.items())


class LiveBroker:
    """實盤券商介面(刻意不實作)。
    接上實盤前請先:1) 紙上交易穩定獲利數週 2) 查券商API文件(永豐 Shioaji、富邦 Neo、元大等)
    3) 實作 buy/sell,下單用限價單,並處理部分成交、委託失敗、斷線時的『全數平倉』保護。"""
    def buy(self, *a, **k): raise NotImplementedError
    def sell(self, *a, **k): raise NotImplementedError
