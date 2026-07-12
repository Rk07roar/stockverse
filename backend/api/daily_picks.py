"""
StockVest — api/daily_picks.py
AI-powered daily stock picks using the existing signals + ML engine.
Picks top 5 BUY signals each morning with plain-English reasoning.
Email delivery via Gmail SMTP (free).

How to schedule this to run every morning at 9 AM:
  Option A (Windows Task Scheduler):
    - Action: python C:\path\to\run_daily_picks.py
    - Trigger: daily at 09:00

  Option B (APScheduler — add to main.py):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_daily_picks_email, 'cron', hour=9, minute=0)
    scheduler.start()
"""
import asyncio
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from fastapi import APIRouter
from data.fetcher import DataFetcher
from data.cache import Cache

logger = logging.getLogger(__name__)
router = APIRouter()

GMAIL_USER     = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASS = os.getenv("GMAIL_APP_PASSWORD", "")


# ── Reuse signal scoring from api/signals.py ────────────────────
def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0)    for d in deltas[-period:]]
    losses = [abs(min(d,0)) for d in deltas[-period:]]
    ag, al = sum(gains)/period, sum(losses)/period
    return round(100 - (100 / (1 + ag/al)), 1) if al else 100.0

def _ema(closes, period):
    if len(closes) < period: return []
    k = 2 / (period + 1)
    e = [sum(closes[:period]) / period]
    for p in closes[period:]:
        e.append(p * k + e[-1] * (1-k))
    return e

def _score(closes, volumes, price):
    score = 0; reasons = []
    rsi   = _rsi(closes)
    if rsi < 30:  score += 2; reasons.append(f"RSI {rsi} — deeply oversold")
    elif rsi < 40: score += 1; reasons.append(f"RSI {rsi} — oversold")
    elif rsi > 70: score -= 2; reasons.append(f"RSI {rsi} — overbought")
    e20, e50 = _ema(closes, 20), _ema(closes, 50)
    if e20 and e50:
        if price > e20[-1] > e50[-1]: score += 1; reasons.append("Healthy uptrend: Price > EMA20 > EMA50")
        if len(e20) >= 2 and len(e50) >= 2:
            if e20[-1] > e50[-1] and e20[-2] <= e50[-2]: score += 2; reasons.append("Golden Cross just formed")
    if len(volumes) >= 20:
        avg = sum(volumes[-20:]) / 20
        if avg > 0 and volumes[-1] / avg > 1.5 and closes[-1] > closes[-2]:
            score += 1; reasons.append(f"Volume {volumes[-1]/avg:.1f}× average — institutional buying")
    return score, rsi, reasons


async def _analyze(sym, name, price, change_pct):
    try:
        def _fetch():
            import yfinance as yf, math
            for sfx in [".NS", ".BO"]:
                df = yf.Ticker(f"{sym}{sfx}").history(period="6mo", auto_adjust=True).dropna(subset=["Close"])
                if len(df) >= 50:
                    cls = [float(v) for v in df["Close"]]
                    if any(math.isnan(c) for c in cls): continue
                    vols = [int(v) if not math.isnan(float(v)) else 0 for v in df["Volume"]]
                    return cls, vols
            return None, None
        loop = asyncio.get_running_loop()
        closes, volumes = await loop.run_in_executor(None, _fetch)
        if not closes: return None
        sc, rsi, reasons = _score(closes, volumes or [], price)
        return {"sym": sym, "name": name, "price": price, "change_pct": change_pct,
                "score": sc, "rsi": rsi, "reasons": reasons[:3]}
    except Exception:
        return None


async def _get_top_picks(n: int = 5) -> list:
    """Run signals engine on Nifty100 and return top N BUY picks."""
    cache_key = "daily_picks:top"
    cached = await Cache.get(cache_key)
    if cached:
        return cached

    stocks     = await DataFetcher.get_all_stocks(sort="ml_desc")
    candidates = [s for s in stocks if s.get("real_data")][:40]
    results    = await asyncio.gather(
        *[_analyze(s["sym"], s["name"], s["price"], s["change_pct"]) for s in candidates[:25]],
        return_exceptions=True
    )
    picks = sorted(
        [r for r in results if isinstance(r, dict) and r["score"] >= 2],
        key=lambda x: x["score"], reverse=True
    )[:n]

    await Cache.set(cache_key, picks, ttl=3600)   # cache 1 hour
    return picks


def _picks_email_html(picks: list, date_str: str) -> str:
    signal_labels = {4: ("STRONG BUY", "#00d4aa"), 3: ("STRONG BUY", "#00d4aa"),
                     2: ("BUY", "#69f0ae"), 1: ("WATCH", "#ffab40")}
    rows = ""
    for i, p in enumerate(picks, 1):
        label, col = signal_labels.get(p["score"], ("WATCH", "#ffab40"))
        chg_col    = "#00d4aa" if p["change_pct"] >= 0 else "#ff4757"
        chg_sign   = "+" if p["change_pct"] >= 0 else ""
        reasons_li = "".join(f'<li style="margin-bottom:4px;color:#8896a8;font-size:12px">{r}</li>'
                             for r in p["reasons"])
        rows += f"""
        <div style="background:#151a22;border-radius:10px;padding:16px;margin-bottom:12px;
                    border-left:3px solid {col}">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px">
            <div>
              <span style="font-size:11px;color:#4a5568;font-weight:600">#{i}</span>
              <span style="font-size:18px;font-weight:800;color:#e8edf5;margin-left:8px">{p["sym"]}</span>
              <span style="font-size:12px;color:#8896a8;margin-left:8px">{p["name"]}</span>
            </div>
            <span style="background:{col}22;color:{col};padding:3px 10px;border-radius:20px;
                         font-size:11px;font-weight:700">{label}</span>
          </div>
          <div style="display:flex;gap:20px;margin-bottom:10px">
            <div>
              <div style="font-size:10px;color:#4a5568;margin-bottom:2px">PRICE</div>
              <div style="font-size:16px;font-weight:700;font-family:monospace">₹{p["price"]:,.2f}</div>
            </div>
            <div>
              <div style="font-size:10px;color:#4a5568;margin-bottom:2px">TODAY</div>
              <div style="font-size:16px;font-weight:700;color:{chg_col};font-family:monospace">
                {chg_sign}{p["change_pct"]:.2f}%</div>
            </div>
            <div>
              <div style="font-size:10px;color:#4a5568;margin-bottom:2px">RSI</div>
              <div style="font-size:16px;font-weight:700;font-family:monospace">{p["rsi"]}</div>
            </div>
            <div>
              <div style="font-size:10px;color:#4a5568;margin-bottom:2px">SIGNAL SCORE</div>
              <div style="font-size:16px;font-weight:700;color:{col};font-family:monospace">
                {p["score"]}/10</div>
            </div>
          </div>
          <ul style="margin:0;padding-left:16px">{reasons_li}</ul>
        </div>"""

    return f"""
<div style="font-family:sans-serif;background:#0a0c10;color:#e8edf5;padding:28px;
            max-width:600px;margin:auto;border-radius:14px">
  <div style="text-align:center;margin-bottom:24px">
    <h1 style="color:#00d4aa;margin:0;font-size:24px">📈 StockVest Daily Picks</h1>
    <p style="color:#4a5568;margin:6px 0 0;font-size:13px">{date_str} • AI-powered signals</p>
  </div>
  <p style="color:#8896a8;font-size:13px;margin-bottom:20px;line-height:1.6">
    Today's top picks are selected by our ML model and multi-indicator signal engine
    (RSI, MACD, EMA crossovers, volume analysis). These are ideas, not advice — always
    do your own research.
  </p>
  {rows}
  <div style="border-top:1px solid #1e2a38;padding-top:16px;margin-top:8px">
    <p style="color:#4a5568;font-size:11px;margin:0;line-height:1.8">
      ⚠️ <strong style="color:#8896a8">Disclaimer:</strong> StockVest picks are algorithmic
      signals, not financial advice. Markets involve risk. Past signal performance does not
      guarantee future results.<br><br>
      Sent daily at 9 AM IST by StockVest
    </p>
  </div>
</div>"""


def _send_email_sync(to: str, subject: str, html: str):
    if not GMAIL_USER or not GMAIL_APP_PASS:
        logger.warning("Gmail not configured — skipping email")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = GMAIL_USER
        msg["To"]      = to
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_APP_PASS)
            smtp.sendmail(GMAIL_USER, to, msg.as_string())
        logger.info(f"Daily picks email sent → {to}")
        return True
    except Exception as e:
        logger.error(f"Daily picks email failed: {e}")
        return False


async def send_daily_picks_email(recipients: list[str] = None):
    """
    Main function — call this at 9 AM daily.
    If recipients is None, logs the picks to console only.
    """
    picks    = await _get_top_picks(5)
    date_str = datetime.now().strftime("%A, %d %B %Y")
    html     = _picks_email_html(picks, date_str)

    if recipients:
        loop = asyncio.get_running_loop()
        for email in recipients:
            subject = f"📈 StockVest Daily Picks — {date_str} — {len(picks)} BUY signals"
            await loop.run_in_executor(None, _send_email_sync, email, subject, html)

    return picks


# ── API Endpoints ─────────────────────────────────────────────────
@router.get("/", summary="Today's top AI stock picks (BUY signals)")
async def get_daily_picks():
    """Returns today's top 5 picks. Cached 1 hour."""
    picks = await _get_top_picks(5)
    return {
        "date":  datetime.now().strftime("%Y-%m-%d"),
        "count": len(picks),
        "picks": picks,
    }


@router.post("/send-email", summary="Send daily picks email to a specific address")
async def trigger_email(to: str):
    """Manually trigger the daily picks email. Use this to test before scheduling."""
    if not GMAIL_USER or not GMAIL_APP_PASS:
        from fastapi import HTTPException
        raise HTTPException(503,
            "Gmail not configured. Set GMAIL_USER and GMAIL_APP_PASSWORD environment variables.")
    picks = await send_daily_picks_email([to])
    return {"sent_to": to, "picks_count": len(picks), "message": "Email sent successfully"}
