"""Notifier:手動下單模式的 Discord 通知。系統只發訊號,不連任何券商 API。
沒設定 DISCORD_WEBHOOK_URL 時只印在 log,不會報錯。
環境變數:
  DISCORD_WEBHOOK_URL  必填才會推送。網址等同密碼,只放在 GitHub Secrets / 本機環境變數。
  DISCORD_MENTION      選填,格式 <@你的使用者ID>,買進/賣出/警示時會 @ 你以觸發手機推播。
每則訊息開頭都會加上來源標籤:[模擬](人造行情,訊號不能下單)或 [實盤行情](真實行情的紙上訊號)。
"""
import os

from models import round_tick, tick_size

CHASE_TICKS = 2          # 「最高可追價」= 建議限價往上幾檔;超過就放棄(停損不變,追越高風險越大)


class Notifier:
    def __init__(self, url=None, mention="", tag=""):
        self.url, self.mention, self.tag = url, mention, tag

    @classmethod
    def from_env(cls, tag=""):
        url = os.getenv("DISCORD_WEBHOOK_URL", "").strip() or None
        n = cls(url, os.getenv("DISCORD_MENTION", "").strip(), tag)
        print("[NOTIFY] Discord 通知已啟用" if url else "[NOTIFY] 未設定 DISCORD_WEBHOOK_URL,只印 log 不推送")
        return n

    # ── 底層送出 ─────────────────────────────
    def _send(self, text, ping=False):
        if not self.url:
            return
        if ping and self.mention:
            text = f"{self.mention} {text}"
        if self.tag:                                # 標籤放最前面,一眼分辨模擬或實盤行情
            text = f"{self.tag} {text}"
        try:
            import requests
            r = requests.post(self.url, json={"content": text[:1900]}, timeout=10)
            if r.status_code >= 300:
                print(f"[NOTIFY] Discord 回應 {r.status_code}")
        except Exception as e:                      # 通知失敗不能影響交易流程
            print(f"[NOTIFY] 推送失敗:{type(e).__name__}")

    # ── 各種訊息 ─────────────────────────────
    def info(self, msg):
        self._send(f"✅ {msg}")

    def alert(self, msg):
        self._send(f"⚠️ {msg}", ping=True)

    def buy(self, q, p, plan, d, now):
        top = min(round_tick(p.entry_px + CHASE_TICKS * tick_size(p.entry_px), "up"), q.limit_up)
        self._send(
            f"🟢 買進訊號 {q.code} {q.name} ({now:%H:%M:%S})\n"
            f"限價 {p.entry_px} | 最高可追價 {top} | 股數 {p.shares}\n"
            f"停損 {p.stop} | 停利 {p.tp} | 報酬風險比 {plan['rr']}\n"
            f"依據:{d.reason}\n"
            f"買進後請立刻在券商設好停損觸價單。", ping=True)

    def stop_update(self, p, q, now):
        self._send(f"🟡 停損上移 {p.code} {p.name} → {p.stop} (現價 {q.last}, {now:%H:%M:%S})")

    def exit(self, rec, q, now):
        self._send(
            f"🔴 賣出訊號 {rec['code']} {rec['name']} ({now:%H:%M:%S})\n"
            f"原因:{rec['reason']} | 參考賣價 {rec['exit_px']}\n"
            f"紙上損益 {rec['pnl']:+,.0f}(沒跟單的訊號請忽略)", ping=True)

    def summary(self, s):
        self._send(
            f"📊 收盤結算(紙上)\n筆數 {s['n']} | 勝率 {s['win']:.0f}% | 淨損益 {s['net']:+,.0f}\n"
            f"手續費 {s['fees']:,.0f} | 證交稅 {s['tax']:,.0f} | 最大回撤 {s['mdd']:,.0f}")
