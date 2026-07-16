"""
StockVest — api/daily_picks.py
AI-powered daily stock picks using the real hybrid ML + technical scoring engine
(ml/scoring.py — same engine that powers the ML Engine panel: GradientBoosting model
blended with a 6-factor technical composite: RSI, MACD, momentum, Bollinger, volume,
MA crossover). Picks the top BUY-signal stocks each morning with plain-English reasoning.
Email delivery via Gmail SMTP (free).

Candidates are drawn from real-data, liquid stocks (sorted by traded volume) rather
than by raw change_pct — that avoids surfacing thinly-traded stocks whose "today's
change" figure can be corrupted by bad prev-close data upstream.

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
from ml.scoring import score_batch

logger = logging.getLogger(__name__)
router = APIRouter()

GMAIL_USER     = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASS = os.getenv("GMAIL_APP_PASSWORD", "")

_CANDIDATE_POOL   = 40   # how many liquid, real-data stocks to run the ML/technical engine over
_MIN_BUY_SCORE    = 65   # ml/scoring.py: BUY signal starts at 75, but we allow a slightly
                         # wider "watch-worthy" floor so a thin market still returns a few picks


def _reasons_from_score(r: dict) -> list:
    """Build plain-English reasons from a real ml/scoring.compute_score() result."""
    reasons = []
    comps = r.get("components", {})
    rsi = r.get("rsi")
    if rsi is not None:
        if rsi < 30:  reasons.append(f"RSI {rsi} — deeply oversold")
        elif rsi < 40: reasons.append(f"RSI {rsi} — oversold")
        elif rsi > 70: reasons.append(f"RSI {rsi} — overbought, caution")

    if r.get("golden_cross"):
        reasons.append("Golden Cross: MA50 above MA200 — bullish trend structure")

    macd = comps.get("macd", {})
    if macd.get("score", 0) >= 70:
        reasons.append("MACD trending bullish")

    vol = comps.get("volume", {})
    if vol.get("ratio", 1) > 1.5:
        reasons.append(f"Volume {vol['ratio']}× average — strong conviction")

    ret20 = r.get("ret20d")
    if ret20 is not None and ret20 > 5:
        reasons.append(f"+{ret20}% over 20 days — positive momentum")

    if r.get("ml_blend"):
        reasons.append(f"ML model confidence {r.get('ml_model')}/100")

    if not reasons:
        reasons.append(f"Composite technical score {r['score']}/100")
    return reasons


async def _get_top_picks(n: int = 5) -> list:
    """
    Run the real hybrid ML + technical scoring engine (ml/scoring.py) over the most
    liquid real-data stocks and return the top N by score. Same engine backing the
    ML Engine panel and single-stock ML score endpoint — not a separate/fake metric.
    """
    cache_key = "daily_picks:top"
    cached = await Cache.get(cache_key)
    if cached:
        return cached

    stocks     = await DataFetcher.get_all_stocks(sort="vol_desc")
    candidates = [s for s in stocks if s.get("real_data") and s.get("volume", 0) > 0][:_CANDIDATE_POOL]
    if not candidates:
        return []

    scores = await score_batch([s["sym"] for s in candidates])

    scored = []
    for s in candidates:
        r = scores.get(s["sym"]) or {}
        if not r.get("real_data") or r.get("score", 0) < _MIN_BUY_SCORE:
            continue
        scored.append({
            "sym": s["sym"], "name": s["name"], "price": s["price"],
            "change_pct": s["change_pct"], "score": r["score"], "signal": r.get("signal", "BUY"),
            "rsi": r.get("rsi"), "reasons": _reasons_from_score(r)[:3],
        })

    picks = sorted(scored, key=lambda x: x["score"], reverse=True)[:n]

    await Cache.set(cache_key, picks, ttl=3600)   # cache 1 hour
    return picks


def _label_for_score(score: int) -> tuple:
    if score >= 85: return ("STRONG BUY", "#00d4aa")
    if score >= 75: return ("BUY", "#69f0ae")
    return ("WATCH", "#ffab40")


def _picks_email_html(picks: list, date_str: str) -> str:
    rows = ""
    for i, p in enumerate(picks, 1):
        label, col = _label_for_score(p["score"])
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
                {p["score"]}/100</div>
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
