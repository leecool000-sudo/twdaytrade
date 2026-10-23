import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


def tick_size(p: float) -> float:
    """台股升降單位(股票)。"""
    if p < 10: return 0.01
    if p < 50: return 0.05
    if p < 100: return 0.1
    if p < 500: return 0.5
    if p < 1000: return 1.0
    return 5.0


def round_tick(p: float, mode: str = "nearest") -> float:
    t = tick_size(p)
    n = p / t
    n = math.ceil(n - 1e-9) if mode == "up" else math.floor(n + 1e-9) if mode == "down" else round(n)
    return round(n * t, 2)


@dataclass
class Quote:
    code: str
    name: str
    ts: datetime
    last: float
    prev_close: float
    open: float
    high: float
    low: float
    volume: float            # 當日累計成交量(張)
    bid: Optional[float]
    ask: Optional[float]
    limit_up: float
    limit_down: float


class Tape:
    """逐筆快照累積:計算 VWAP、近N分鐘量能。"""
    def __init__(self):
        self.rows = []       # (ts, last, shares_traded_in_step)
        self.pv = 0.0
        self.v = 0.0
        self._last_vol = None

    def add(self, q: Quote):
        dv = 0 if self._last_vol is None else max(0, q.volume - self._last_vol)
        self._last_vol = q.volume
        sh = dv * 1000
        self.pv += q.last * sh
        self.v += sh
        self.rows.append((q.ts, q.last, sh))

    @property
    def vwap(self):
        return self.pv / self.v if self.v else None

    def since(self, ts):
        return [r for r in self.rows if r[0] >= ts]


@dataclass
class Position:
    code: str
    name: str
    shares: int
    entry_px: float
    entry_ts: datetime
    entry_fee: float
    stop: float
    tp: float
    hwm: float


@dataclass
class Decision:
    action: str              # "BUY" | "SKIP"
    code: str
    score: float
    reason: str
