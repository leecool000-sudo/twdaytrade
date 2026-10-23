"""所有可調參數集中在這裡。預設值偏保守，請先用模擬/紙上交易驗證後再調整。"""
from dataclasses import dataclass
from datetime import time


@dataclass
class Config:
    # ── 資金與成本 ─────────────────────────────
    capital: float = 1_000_000        # 本金(TWD)
    fee_rate: float = 0.001425        # 手續費牌價,買賣各一次
    fee_discount: float = 0.6         # 券商折扣(6折=0.6),請改成你自己的
    min_fee: float = 20               # 最低手續費(整股常見20元)
    day_trade_tax: float = 0.0015     # 現股當沖證交稅(賣出時,減半優惠)
    slippage_ticks: int = 0           # 額外滑價檔數。本系統已用『買在賣一、賣在買一』跨價差成交;
                                      # 建議用 1 做壓力測試,看策略扛不扛得住

    # ── 交易時段 ───────────────────────────────
    entry_start: time = time(9, 20)   # 開盤區間(ORB)形成後才進場
    entry_end: time = time(12, 30)    # 此後不再開新倉
    flat_time: time = time(13, 15)    # 強制全數平倉(避開13:25起的收盤試撮)
    market_close: time = time(13, 30)

    # ── SCAN 參數 ──────────────────────────────
    orb_minutes: int = 15             # 開盤區間分鐘數
    min_scan_score: float = 0.60
    top_k: int = 3                    # 每輪最多評估幾檔候選

    # ── VET 參數 ───────────────────────────────
    avoid_notice: bool = True         # 注意股一併迴避
    max_chase_pct: float = 0.07       # 漲幅超過此值不追
    limit_buffer_ticks: int = 3       # 離漲停幾檔內不進場
    max_spread_pct: float = 0.003     # 買賣價差上限
    max_tick_pct: float = 0.004       # 一檔跳動占股價比例上限(太高=成本吃光利潤)
    min_recent_value: float = 8_000_000  # 近5分鐘成交值下限(流動性)
    min_vol_ratio: float = 1.5        # 近5分鐘量能 ÷ 當日平均5分鐘量,未放量不進場

    # ── SOCIAL(新聞/LLM)──────────────────────
    use_llm: bool = True              # 有 ANTHROPIC_API_KEY 才會真的啟用
    llm_model: str = "claude-sonnet-5-5"
    social_veto_below: float = 0.30

    # ── SIZE ───────────────────────────────────
    risk_per_trade: float = 0.003     # 單筆最大虧損 = 本金的0.3%
    max_position_pct: float = 0.20    # 單檔部位上限(占本金)
    max_positions: int = 3
    stop_pct: float = 0.007           # 停損距離
    tp_pct: float = 0.018             # 停利距離
    min_stop_ticks: int = 3
    min_net_rr: float = 1.1           # 扣掉成本後的最低報酬風險比
    allow_odd_lot: bool = True        # 允許盤中零股(不足1000股時)

    # ── RISK ───────────────────────────────────
    breakeven_at_pct: float = 0.009   # 浮盈達此值,停損上移到保本
    trail_pct: float = 0.005          # 之後以最高價回落此比例移動停利
    max_hold_min: int = 45            # 時間停損
    time_stop_min_gain: float = 0.003
    daily_loss_limit: float = 0.015   # 當日虧損達本金1.5%:全平並停止交易
    max_consecutive_losses: int = 3
    max_trades_per_code: int = 2
