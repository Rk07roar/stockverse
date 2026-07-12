"""
StockVest — api/alerts.py
Price, ML-score, and volume spike alerts with free notifications.
Notification channels:
  - Email via Gmail SMTP (free — needs GMAIL_USER + GMAIL_APP_PASSWORD env vars)
  - Telegram Bot (free — needs TELEGRAM_BOT_TOKEN env var)

How to set up Gmail:
  1. Enable 2FA on your Gmail account
  2. Go to myaccount.google.com → Security → App Passwords
  3. Create an app password for "Mail"
  4. Set GMAIL_USER=you@gmail.com and GMAIL_APP_PASSWORD=xxxx in your .env

How to set up Telegram (free):
  1. Message @BotFather on Telegram → /newbot
  2. Copy the token → set TELEGRAM_BOT_TOKEN=xxx in .env
  3. User sends /start to your bot → get their chat_id from webhook or @userinfobot
"""
import asyncio
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import aiosqlite

from db import DB_PATH
from utils.auth import decode_token
from data.fetcher import DataFetcher

logger  = logging.getLogger(__name__)
router  = APIRouter()
_bearer = HTTPBearer(auto_error=False)

GMAIL_USER       = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASS   = os.getenv("GMAIL_APP_PASSWORD", "")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")


# ── Models ──────────────────────────────────────────────────────
class AlertCreate(BaseModel):
    symbol:           str
    condition:        str    # above | below | ml_above | ml_below | volume_spike
    value:            float
    note:             Optional[str] = ""
    notify_email:     Optional[str] = ""
    telegram_chat_id: Optional[str] = ""


# ── Auth ─────────────────────────────────────────────────────────
async def _current_user(creds: HTTPAuthorizationCredentials = Depends(_bearer)) -> str:
    if not creds:
        return "guest"
    payload = decode_token(creds.credentials)
    return (payload or {}).get("sub", "guest")


# ── Email ─────────────────────────────────────────────────────────
def _email_html(symbol: str, condition: str, target: float, current: float) -> str:
    col = "#00d4aa" if "above" in condition or "buy" in condition else "#ff4757"
    return f"""
<div style="font-family:sans-serif;background:#0a0c10;color:#e8edf5;
            padding:28px;border-radius:14px;max-width:520px;margin:auto">
  <h2 style="color:{col};margin:0 0 8px;font-size:20px">⚡ StockVest Alert Triggered</h2>
  <p style="color:#8896a8;margin:0 0 20px;font-size:14px">
    Your alert for <strong style="color:#e8edf5">{symbol}</strong> has fired.
  </p>
  <div style="background:#151a22;border-radius:10px;padding:18px;margin-bottom:20px">
    <table style="width:100%;border-collapse:collapse">
      <tr><td style="color:#8896a8;padding:6px 0;font-size:13px">Condition</td>
          <td style="text-align:right;font-weight:600;font-size:13px">
            {condition.replace("_"," ").title()}</td></tr>
      <tr><td style="color:#8896a8;padding:6px 0;font-size:13px">Target</td>
          <td style="text-align:right;font-weight:600;font-size:13px">
            ₹{target:,.2f}</td></tr>
      <tr><td style="color:#8896a8;padding:8px 0 0;font-size:13px">Current</td>
          <td style="text-align:right;font-weight:800;font-size:22px;color:{col}">
            ₹{current:,.2f}</td></tr>
    </table>
  </div>
  <p style="color:#4a5568;font-size:11px;margin:0">
    Sent by StockVest • This alert has been marked as triggered and won't fire again
    until you reset it.
  </p>
</div>"""


def _send_email_sync(to: str, subject: str, html: str):
    if not GMAIL_USER or not GMAIL_APP_PASS or not to:
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = GMAIL_USER
        msg["To"]      = to
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_APP_PASS)
            smtp.sendmail(GMAIL_USER, to, msg.as_string())
        logger.info(f"Alert email sent → {to}")
    except Exception as e:
        logger.warning(f"Email send failed: {e}")


# ── Telegram ──────────────────────────────────────────────────────
async def _send_telegram(chat_id: str, text: str):
    if not TELEGRAM_TOKEN or not chat_id:
        return
    try:
        import httpx
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        async with httpx.AsyncClient(timeout=8) as c:
            await c.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")


# ── Alert checking engine (called by background task in main.py) ──
async def check_all_alerts():
    """
    Check every untriggered alert against current live prices.
    Call this every 2 minutes via APScheduler or FastAPI background task.
    """
    try:
        stocks    = await DataFetcher.get_all_stocks(sort="ml_desc")
        price_map = {s["sym"]: s for s in stocks}

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT * FROM alerts WHERE triggered=0"
            )).fetchall()

            for row in rows:
                alert    = dict(row)
                sym      = alert["symbol"]
                stock    = price_map.get(sym)
                if not stock:
                    continue

                price     = stock.get("price") or 0
                ml_score  = stock.get("ml_score") or 0
                volume    = stock.get("volume") or 0
                avg_vol   = stock.get("avg_volume") or 1
                cond      = alert["condition"]
                # DB column is "target" (legacy); fall back to "value" if somehow renamed
                target    = alert.get("target") or alert.get("value", 0)
                triggered = False
                current   = price

                if   cond == "above"        and price    >= target:  triggered = True
                elif cond == "below"        and price    <= target:  triggered = True
                elif cond == "ml_above"     and ml_score >= target:  triggered = True; current = ml_score
                elif cond == "ml_below"     and ml_score <= target:  triggered = True; current = ml_score
                elif cond == "volume_spike" and (volume / avg_vol)  >= target:
                    triggered = True; current = round(volume / avg_vol, 1)

                if not triggered:
                    continue

                # Mark triggered
                await db.execute("UPDATE alerts SET triggered=1 WHERE id=?", (alert["id"],))
                await db.commit()
                logger.info(f"Alert triggered → {sym} {cond} {target} (current={current})")

                # Send email
                email = alert.get("notify_email", "")
                if email:
                    html    = _email_html(sym, cond, target, current)
                    subject = f"⚡ StockVest: {sym} {cond.replace('_',' ')} ₹{target:,.0f}"
                    loop    = asyncio.get_running_loop()
                    await loop.run_in_executor(None, _send_email_sync, email, subject, html)

                # Send Telegram
                tg = alert.get("telegram_chat_id", "")
                if tg:
                    msg = (f"⚡ <b>StockVest Alert</b>\n"
                           f"<b>{sym}</b>: {cond.replace('_',' ')} target {target}\n"
                           f"Current value: <b>{current}</b>")
                    await _send_telegram(tg, msg)

    except Exception as e:
        logger.error(f"Alert check error: {e}")


# ── API Endpoints ─────────────────────────────────────────────────
@router.get("/", summary="List alerts for current user")
async def list_alerts(user: str = Depends(_current_user)):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM alerts WHERE user_id=? ORDER BY created_at DESC", (user,)
        )).fetchall()
    alerts = []
    for r in rows:
        d = dict(r)
        # DB column may be "target" (old schema) — expose as "value" so frontend stays consistent
        if "target" in d and "value" not in d:
            d["value"] = d["target"]
        alerts.append(d)
    return {"alerts": alerts}


@router.post("/", summary="Create a new alert")
async def create_alert(req: AlertCreate, user: str = Depends(_current_user)):
    valid = {"above", "below", "ml_above", "ml_below", "volume_spike"}
    if req.condition not in valid:
        raise HTTPException(400, f"condition must be one of: {', '.join(sorted(valid))}")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        # Use "target" to match the live DB schema (old schema used "target" not "value")
        cur = await db.execute(
            """INSERT INTO alerts
               (user_id, symbol, condition, target, triggered, created_at, notify_email, telegram_chat_id, note)
               VALUES (?,?,?,?,0,?,?,?,?)""",
            (user, req.symbol.upper().strip(), req.condition, req.value,
             now, req.notify_email or "", req.telegram_chat_id or "", req.note or "")
        )
        await db.commit()
    return {"id": cur.lastrowid, "message": "Alert created successfully"}


@router.delete("/{alert_id}", summary="Delete an alert")
async def delete_alert(alert_id: int, user: str = Depends(_current_user)):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM alerts WHERE id=? AND user_id=?", (alert_id, user))
        await db.commit()
    return {"message": "Alert deleted"}


@router.post("/reset/{alert_id}", summary="Reset a triggered alert back to active")
async def reset_alert(alert_id: int, user: str = Depends(_current_user)):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE alerts SET triggered=0 WHERE id=? AND user_id=?", (alert_id, user)
        )
        await db.commit()
    return {"message": "Alert reset to active"}


@router.post("/test-email", summary="Send a test alert email to verify config")
async def test_email(to: str, user: str = Depends(_current_user)):
    if not GMAIL_USER or not GMAIL_APP_PASS:
        raise HTTPException(503, "Gmail not configured. Set GMAIL_USER and GMAIL_APP_PASSWORD env vars.")
    html = _email_html("RELIANCE", "above", 2500, 2543.75)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _send_email_sync, to,
                               "⚡ StockVest Test Alert — Email is working!", html)
    return {"message": f"Test email sent to {to}"}
