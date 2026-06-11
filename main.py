import os
import html
import requests
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "CHANGE_ME_SECRET")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

if not TELEGRAM_CHAT_ID:
    raise RuntimeError("TELEGRAM_CHAT_ID is missing")


def send_telegram_message(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    response = requests.post(url, json=payload, timeout=15)
    response.raise_for_status()


@app.get("/")
def health_check():
    return {"status": "ok", "service": "misha-trade-bot"}


@app.post("/webhook")
async def tradingview_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if data.get("secret") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    symbol = html.escape(str(data.get("symbol", "UNKNOWN")))
    tf = html.escape(str(data.get("tf", "UNKNOWN")))
    signal = html.escape(str(data.get("signal", "UNKNOWN")))
    score = html.escape(str(data.get("score", "N/A")))
    entry = html.escape(str(data.get("entry", "N/A")))
    sl = html.escape(str(data.get("sl", "N/A")))
    tp = html.escape(str(data.get("tp", "N/A")))
    rr = html.escape(str(data.get("rr", "N/A")))
    rsi = html.escape(str(data.get("rsi", "N/A")))
    h1 = html.escape(str(data.get("h1", "N/A")))
    reason = html.escape(str(data.get("reason", "")))

    emoji = "🟢" if signal == "BUY" else "🔴" if signal == "SELL" else "⚪"

    message = (
        f"{emoji} <b>{signal} SIGNAL</b>\n\n"
        f"<b>Symbol:</b> {symbol}\n"
        f"<b>Timeframe:</b> {tf}\n"
        f"<b>Score:</b> {score}\n"
        f"<b>H1 filter:</b> {h1}\n"
        f"<b>RSI:</b> {rsi}\n\n"
        f"<b>Entry:</b> {entry}\n"
        f"<b>Stop Loss:</b> {sl}\n"
        f"<b>Take Profit:</b> {tp}\n"
        f"<b>Risk/Reward:</b> 1:{rr}\n\n"
        f"<b>Reason:</b> {reason}\n\n"
        f"⚠️ Сверь цену в XTB перед входом. Не входи, если цена уже ушла далеко от Entry."
    )

    send_telegram_message(message)

    return {"status": "sent"}
