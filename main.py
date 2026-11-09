"""MONEY ENGINE Telegram webhook v2.

Receives signed JSON notifications; it NEVER sends broker orders.
No real or demo trading connection is implemented in this service.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool


app = FastAPI(title="MONEY ENGINE Telegram Notifier", version="2.0.0")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
DB_PATH = os.getenv("EVENT_DB_PATH", "/tmp/money-engine-telegram-events.sqlite3")
MAX_BODY_BYTES = 64 * 1024
LEASE_SECONDS = 120

EVENTS = {
    "PREATTENTION", "BUY", "SELL", "SIGNAL_CONFIRMED",
    "ORDER_SUBMITTED", "ORDER_FILLED", "ORDER_REJECTED",
    "TP", "SL", "TIMEOUT", "INVALIDATED",
    "MANAGEMENT", "PAUSE", "RECOVERY", "REDUCED", "KILL",
    "SYSTEM_STATUS", "DATA_INTEGRITY", "REGIME_CHANGE",
    "CONNECTION_LOST", "CONNECTION_RESTORED",
}
TRADE_SIGNALS = {"BUY", "SELL", "SIGNAL_CONFIRMED"}
ORDER_EVENTS = {"ORDER_SUBMITTED", "ORDER_FILLED", "ORDER_REJECTED"}
EXIT_EVENTS = {"TP", "SL", "TIMEOUT", "INVALIDATED"}
SERVICE_EVENTS = EVENTS - TRADE_SIGNALS - ORDER_EVENTS - EXIT_EVENTS - {"PREATTENTION", "MANAGEMENT"}
DISALLOWED = {"UNKNOWN", "N/A", "N\\A", "NOT AVAILABLE", "TBD", "NULL"}


class ContractError(ValueError):
    """Malformed or incomplete event; never invent missing trading data."""


@dataclass(frozen=True)
class Prepared:
    event_id: str
    dedupe_key: str
    fingerprint: str
    message: str


def clean_text(data: dict, field: str, *, required: bool = False, limit: int = 240) -> str | None:
    value = data.get(field)
    if value is None:
        if required:
            raise ContractError(f"Missing required field: {field}")
        return None
    if not isinstance(value, str):
        raise ContractError(f"{field} must be a string")
    result = value.strip()
    if not result:
        raise ContractError(f"{field} cannot be blank")
    if len(result) > limit or result.upper() in DISALLOWED:
        raise ContractError(f"Invalid value for {field}")
    return result


def number(data: dict, field: str, *, required: bool = False, positive: bool = False) -> Decimal | None:
    value = data.get(field)
    if value is None:
        if required:
            raise ContractError(f"Missing required numeric field: {field}")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ContractError(f"{field} must be numeric")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        raise ContractError(f"{field} must be numeric") from None
    if not parsed.is_finite() or (positive and parsed <= 0):
        raise ContractError(f"Invalid numeric value for {field}")
    return parsed


def alias_number(data: dict, primary: str, alias: str, *, required: bool = False, positive: bool = False) -> Decimal | None:
    a = number(data, primary, positive=positive)
    b = number(data, alias, positive=positive)
    if a is not None and b is not None and a != b:
        raise ContractError(f"Conflicting fields: {primary} / {alias}")
    result = a if a is not None else b
    if required and result is None:
        raise ContractError(f"Missing required numeric field: {primary}")
    return result


def utc_time(value: str, field: str) -> datetime:
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ContractError(f"Invalid timestamp: {field}") from None
    if d.tzinfo is None or d.utcoffset() != timezone.utc.utcoffset(d):
        raise ContractError(f"{field} must use UTC with timezone")
    return d.astimezone(timezone.utc)


def show_number(value: Decimal) -> str:
    return format(value, "f")


def show_price(value: Decimal, digits: int | None) -> str:
    """Show broker price precision without silently rounding real levels."""
    if digits is None:
        return show_number(value)
    rendered = format(value, f".{digits}f")
    if Decimal(rendered) != value:
        raise ContractError(f"Price precision exceeds {digits} decimal places")
    return rendered


def safe(text: str) -> str:
    return html.escape(text, quote=True)


def line(label: str, value: str) -> str:
    return f"<b>{safe(label)}:</b> {safe(value)}"


def validate_geometry(side: str, entry: Decimal, stop: Decimal, take: Decimal) -> Decimal:
    if side == "BUY" and not (stop < entry < take):
        raise ContractError("BUY requires stopLoss < entry < takeProfit")
    if side == "SELL" and not (take < entry < stop):
        raise ContractError("SELL requires takeProfit < entry < stopLoss")
    return abs(take - entry) / abs(entry - stop)


def prepare(data: dict) -> Prepared:
    event_id = clean_text(data, "eventId", required=True, limit=128)
    schema = clean_text(data, "schemaVersion", required=True, limit=80)
    if schema != "2":
        raise ContractError("Unsupported schemaVersion (expected 2)")
    strategy_version = clean_text(data, "strategyVersion", required=True)
    account = clean_text(data, "accountAlias", required=True)
    event = clean_text(data, "event", required=True)
    if event not in EVENTS:
        raise ContractError("Unsupported event")
    reason = clean_text(data, "reason", required=True, limit=500)
    timestamp = utc_time(clean_text(data, "timestampUTC", required=True), "timestampUTC")
    symbol = clean_text(data, "symbol", limit=32)
    timeframe = clean_text(data, "timeframe", limit=16)
    side = clean_text(data, "side", limit=8)
    test_flag = data.get("testOnly", False)
    if type(test_flag) is not bool:
        raise ContractError("testOnly must be a boolean")
    # Keep compatibility with the existing local staging smoke test.
    test_only = test_flag or (account == "TEST-NO-TRADE" and strategy_version.startswith("STAGING-"))
    configured_digits = data.get("priceDigits")
    if configured_digits is None:
        price_digits = 5 if symbol == "EURUSD" else None
    elif type(configured_digits) is not int or not 0 <= configured_digits <= 10:
        raise ContractError("priceDigits must be an integer from 0 to 10")
    else:
        price_digits = configured_digits
    if side is not None and side not in {"BUY", "SELL", "NONE"}:
        raise ContractError("Invalid side")
    if event in {"PREATTENTION", "BUY", "SELL", "SIGNAL_CONFIRMED", "ORDER_SUBMITTED", "ORDER_FILLED"}:
        if symbol is None or timeframe is None or side not in {"BUY", "SELL"}:
            raise ContractError("This event requires symbol, timeframe, and BUY/SELL side")
        if timeframe != "M5":
            raise ContractError("MONEY ENGINE trading notifications require M5")
    if event in {"BUY", "SELL"} and side != event:
        raise ContractError("BUY/SELL event and side must match")

    # Reject placeholders even in optional supplied fields. Don't show made-up zero prices.
    for field in ("regime", "module", "payoffProfile", "riskState", "guardScope",
                  "signalId", "tradeId", "sourceHash", "parameterHash", "action", "status",
                  "parentCore", "variationTag", "engineState", "signalState"):
        clean_text(data, field)

    entry = alias_number(data, "referenceEntry", "entry", positive=True)
    stop = number(data, "stopLoss", positive=True)
    take = number(data, "takeProfit", positive=True)
    fill = number(data, "actualFill", positive=True)
    current = number(data, "currentPrice", positive=True)
    rr = alias_number(data, "riskReward", "RR", positive=True)
    risk_pct = number(data, "riskPct", positive=True)
    number(data, "monetaryRisk", positive=True)
    number(data, "positionSize", positive=True)
    number(data, "RSI")
    number(data, "ADX")
    number(data, "resultR")
    number(data, "resultMoney")
    number(data, "newStopLoss", positive=True)
    number(data, "newTakeProfit", positive=True)
    seconds = number(data, "secondsToClose")
    if data.get("RSI") is not None and not 0 <= number(data, "RSI") <= 100:
        raise ContractError("RSI out of range")
    if data.get("ADX") is not None and not 0 <= number(data, "ADX") <= 100:
        raise ContractError("ADX out of range")

    bar_close = clean_text(data, "barCloseUTC")
    close_at = utc_time(bar_close, "barCloseUTC") if bar_close else None
    if event in TRADE_SIGNALS:
        for field in ("signalId", "regime", "module", "payoffProfile", "sourceHash", "parameterHash"):
            clean_text(data, field, required=True)
        if data.get("signalState") != "VALID":
            raise ContractError("Confirmed signal requires signalState=VALID")
        if data.get("riskState") not in {"NORMAL", "REDUCED"}:
            raise ContractError("Signal blocked by riskState")
        if data.get("engineState") in {"PAUSE", "KILL", "BLOCK"}:
            raise ContractError("Signal blocked by engineState")
        if data.get("barClosed") is not True or close_at is None or close_at > timestamp:
            raise ContractError("Confirmed signal requires a closed M5 bar")
        if None in (entry, stop, take, rr, risk_pct):
            raise ContractError("Confirmed signal requires entry, SL, TP, RR and riskPct")
        geometry_rr = validate_geometry(side, entry, stop, take)
        if abs(geometry_rr - rr) > Decimal("0.03"):
            raise ContractError("riskReward inconsistent with price geometry")

    if event == "PREATTENTION":
        for field in ("signalId", "regime", "module"):
            clean_text(data, field, required=True)
        if data.get("signalState") != "VALID" or data.get("riskState") not in {"NORMAL", "REDUCED"}:
            raise ContractError("PREATTENTION blocked by signal/risk state")
        if data.get("engineState") in {"PAUSE", "KILL", "BLOCK", "TRANSITION"} or data.get("activeTrade") is True:
            raise ContractError("PREATTENTION prohibited in blocked/active state")
        if close_at is None or close_at <= timestamp or close_at.minute % 5 or close_at.second:
            raise ContractError("PREATTENTION requires a future M5 barCloseUTC")
        if seconds is None or not 0 < seconds <= 120:
            raise ContractError("Invalid secondsToClose")
        if abs(Decimal(str((close_at - timestamp).total_seconds())) - seconds) > Decimal("10"):
            raise ContractError("secondsToClose disagrees with timestamps")
        if None in (entry, stop, take):
            raise ContractError("PREATTENTION requires potential entry, stop and take")
        validate_geometry(side, entry, stop, take)
        if data.get("tradeId"):
            raise ContractError("PREATTENTION must not contain tradeId")

    if event in ORDER_EVENTS | EXIT_EVENTS | {"MANAGEMENT"}:
        clean_text(data, "tradeId", required=True)
        if symbol is None:
            raise ContractError("Trade event requires symbol")
    if event in {"ORDER_FILLED", "ORDER_SUBMITTED"}:
        if fill is None and entry is None:
            raise ContractError("Order event requires referenceEntry or actualFill")
        if event == "ORDER_FILLED":
            if side not in {"BUY", "SELL"} or fill is None or stop is None or take is None:
                raise ContractError("ORDER_FILLED requires side, fill, SL and TP")
            validate_geometry(side, fill, stop, take)
    if event in EXIT_EVENTS:
        number(data, "resultR", required=True)
    if event == "MANAGEMENT":
        action = clean_text(data, "action", required=True)
        if action not in {"MOVE_STOP", "MOVE_TAKE", "CLOSE", "KEEP"}:
            raise ContractError("Invalid management action")
        if action == "MOVE_STOP" and data.get("newStopLoss") is None:
            raise ContractError("MOVE_STOP requires newStopLoss")
        if action == "MOVE_TAKE" and data.get("newTakeProfit") is None:
            raise ContractError("MOVE_TAKE requires newTakeProfit")
    if event in {"PAUSE", "RECOVERY", "REDUCED"}:
        clean_text(data, "guardScope", required=True)

    title_side = side if side in {"BUY", "SELL"} else ""
    subject = safe(symbol or "MONEY ENGINE")
    titles = {
        "PREATTENTION": f"🟡 {subject} {title_side} — PREATTENTION",
        "BUY": f"🟢 {subject} BUY — ПОДТВЕРЖДЁННЫЙ СИГНАЛ",
        "SELL": f"🔴 {subject} SELL — ПОДТВЕРЖДЁННЫЙ СИГНАЛ",
        "SIGNAL_CONFIRMED": f"🟢 {subject} {title_side} — СИГНАЛ ПОДТВЕРЖДЁН",
        "ORDER_SUBMITTED": f"📤 {subject} — ЗАЯВКА ОТПРАВЛЕНА",
        "ORDER_FILLED": f"✅ {subject} — СДЕЛКА ИСПОЛНЕНА",
        "ORDER_REJECTED": f"⚠️ {subject} — ЗАЯВКА ОТКЛОНЕНА",
        "TP": f"🎯 {subject} — TAKE PROFIT",
        "SL": f"🛑 {subject} — STOP LOSS",
        "TIMEOUT": f"⌛ {subject} — TIMEOUT",
        "INVALIDATED": f"⚠️ {subject} — INVALIDATED",
        "MANAGEMENT": f"🟣 {subject} — MANAGEMENT",
        "PAUSE": f"🔴 {subject} — PAUSE",
        "RECOVERY": f"🟠 {subject} — RECOVERY CONFIRMED",
        "REDUCED": f"🟠 {subject} — REDUCED RISK",
        "KILL": "⛔ MONEY ENGINE — KILL SWITCH",
        "SYSTEM_STATUS": "ℹ️ MONEY ENGINE — SYSTEM STATUS",
        "DATA_INTEGRITY": "⚠️ MONEY ENGINE — DATA INTEGRITY",
        "REGIME_CHANGE": f"🔄 {subject} — REGIME CHANGE",
        "CONNECTION_LOST": "⚠️ MONEY ENGINE — CONNECTION LOST",
        "CONNECTION_RESTORED": "✅ MONEY ENGINE — CONNECTION RESTORED",
    }
    display_title = titles[event]
    if test_only:
        display_title = f"🧪 ТЕСТ — {subject} {title_side or event} — НЕ ТОРГОВАТЬ"
    rows = [f"<b>{display_title}</b>", line("Счёт", account)]
    if test_only:
        rows.insert(1, "<b>СИНТЕТИЧЕСКАЯ ПРОВЕРКА. ЭТО НЕ ТОРГОВЫЙ СИГНАЛ.</b>")
    if timeframe:
        rows.append(line("Таймфрейм", timeframe))
    if side in {"BUY", "SELL"} and event not in {"BUY", "SELL", "PREATTENTION", "SIGNAL_CONFIRMED"}:
        rows.append(line("Направление", side))
    for field, label in (
        ("regime", "Режим"), ("module", "Модуль"),
        ("payoffProfile", "Payoff Profile"), ("riskState", "Состояние риска"),
        ("guardScope", "Уровень защиты"), ("action", "Действие"), ("status", "Статус"),
        ("signalId", "Signal ID"), ("tradeId", "Trade ID"),
    ):
        val = data.get(field)
        if val is not None and val != "NONE":
            rows.append(line(label, str(val)))

    price_labels = {
        "Возможный Entry", "Entry", "Цена исполнения",
        "Возможный SL", "Stop Loss", "Возможный TP", "Take Profit",
        "Новый Stop Loss", "Новый Take Profit", "Текущая цена",
    }
    for value, label in (
        (entry, "Возможный Entry" if event == "PREATTENTION" else "Entry"),
        (fill, "Цена исполнения"), (stop, "Возможный SL" if event == "PREATTENTION" else "Stop Loss"),
        (take, "Возможный TP" if event == "PREATTENTION" else "Take Profit"),
        (number(data, "newStopLoss", positive=True), "Новый Stop Loss"),
        (number(data, "newTakeProfit", positive=True), "Новый Take Profit"),
        (rr, "Risk/Reward"), (risk_pct, "Риск, %"),
        (number(data, "monetaryRisk", positive=True), "Риск, деньги"),
        (number(data, "positionSize", positive=True), "Объём"),
        (current, "Текущая цена"),
        (number(data, "RSI"), "RSI"), (number(data, "ADX"), "ADX"),
        (number(data, "resultR"), "Результат, R"),
        (number(data, "resultMoney"), "Результат, деньги"),
        (seconds, "Секунд до закрытия"),
    ):
        if value is not None:
            rendered = show_price(value, price_digits) if label in price_labels else show_number(value)
            rows.append(line(label, ("1:" if label == "Risk/Reward" else "") + rendered))

    rows.append(line("Причина", reason))
    rows.append(line("Время UTC", timestamp.isoformat().replace("+00:00", "Z")))
    if test_only:
        rows.append("<b>ТЕСТОВОЕ СОБЫТИЕ. Реальный сигнал не формировался; сделка не открывалась.</b>")
    elif event == "PREATTENTION":
        rows.append("<b>НЕ ВХОДИТЬ. Потенциальный сигнал может исчезнуть к закрытию M5.</b>")
    elif event in TRADE_SIGNALS:
        rows.append("<b>Сигнал подтверждён; исполнение заявки ещё не подтверждено.</b>")
    elif event == "RECOVERY":
        rows.append("<b>На текущей свече не входить; ждать новый отдельный VALID-сигнал.</b>")
    elif event == "KILL":
        rows.append("<b>Новые входы запрещены. Требуется ручная проверка.</b>")
    elif event == "PAUSE":
        rows.append("<b>Новые входы заблокированы на указанном уровне.</b>")

    # Secrets are excluded from stored fingerprints, logs and rendered messages.
    hashed_payload = {k: v for k, v in data.items() if k != "secret"}
    fingerprint = hashlib.sha256(json.dumps(hashed_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    if event == "PREATTENTION":
        dedupe_key = f"PREATTENTION:{account}:{symbol}:M5:{close_at.isoformat()}"
    elif event in {"SYSTEM_STATUS", "CONNECTION_LOST", "CONNECTION_RESTORED", "DATA_INTEGRITY"}:
        dedupe_key = f"{event}:{account}:{symbol}:{reason}:{int(timestamp.timestamp()) // 600}"
    else:
        dedupe_key = f"EVENT:{event_id}"
    return Prepared(event_id=event_id, dedupe_key=dedupe_key, fingerprint=fingerprint, message="\n".join(rows))


def db_connection() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.execute("""CREATE TABLE IF NOT EXISTS deliveries (
        event_id TEXT PRIMARY KEY,
        dedupe_key TEXT NOT NULL UNIQUE,
        fingerprint TEXT NOT NULL,
        state TEXT NOT NULL,
        lease_until INTEGER NOT NULL,
        telegram_message_id TEXT,
        updated_at INTEGER NOT NULL
    )""")
    return connection


def reserve(item: Prepared) -> str:
    now = int(time.time())
    with db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT event_id, fingerprint, state, lease_until FROM deliveries WHERE event_id = ? OR dedupe_key = ?",
            (item.event_id, item.dedupe_key),
        ).fetchone()
        if row:
            old_id, old_fp, state, lease = row
            if old_id == item.event_id and old_fp != item.fingerprint:
                return "conflict"
            if state == "sent":
                return "duplicate"
            if state == "sending" and lease > now:
                return "in_progress"
            db.execute(
                "UPDATE deliveries SET state='sending', lease_until=?, updated_at=? WHERE event_id=?",
                (now + LEASE_SECONDS, now, old_id),
            )
            return "accepted"
        db.execute(
            "INSERT INTO deliveries(event_id,dedupe_key,fingerprint,state,lease_until,updated_at) VALUES(?,?,?,?,?,?)",
            (item.event_id, item.dedupe_key, item.fingerprint, "sending", now + LEASE_SECONDS, now),
        )
        return "accepted"


def mark(item: Prepared, state: str, telegram_message_id: str | None = None) -> None:
    with db_connection() as db:
        db.execute(
            "UPDATE deliveries SET state=?, telegram_message_id=?, lease_until=0, updated_at=? "
            "WHERE event_id=? OR dedupe_key=?",
            (state, telegram_message_id, int(time.time()), item.event_id, item.dedupe_key),
        )


def send(message: str) -> str:
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Telegram credentials are not configured")
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=(3, 12),
    )
    response.raise_for_status()
    result = response.json()
    if result.get("ok") is not True:
        raise RuntimeError("Telegram returned a delivery failure")
    return str(result.get("result", {}).get("message_id", ""))


@app.get("/")
def health_check():
    return {"status": "ok", "service": "money-engine-telegram-v2"}


@app.post("/webhook")
async def webhook(request: Request):
    if not WEBHOOK_SECRET or WEBHOOK_SECRET == "CHANGE_ME_SECRET":
        raise HTTPException(status_code=503, detail="Webhook authentication is not configured")
    if "application/json" not in request.headers.get("content-type", "").lower():
        raise HTTPException(status_code=415, detail="JSON required")
    if request.headers.get("content-length", "").isdigit() and int(request.headers["content-length"]) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON") from None
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Object JSON required")
    secret = data.get("secret")
    if not isinstance(secret, str) or not hmac.compare_digest(secret, WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        item = prepare(data)
    except ContractError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    try:
        outcome = await run_in_threadpool(reserve, item)
    except (OSError, sqlite3.Error):
        raise HTTPException(status_code=503, detail="Delivery ledger unavailable") from None
    if outcome == "duplicate":
        return {"status": "duplicate", "eventId": item.event_id}
    if outcome == "in_progress":
        return {"status": "in_progress", "eventId": item.event_id}
    if outcome == "conflict":
        raise HTTPException(status_code=409, detail="eventId reused with different payload")
    try:
        telegram_id = await run_in_threadpool(send, item.message)
        await run_in_threadpool(mark, item, "sent", telegram_id)
    except Exception:
        await run_in_threadpool(mark, item, "failed")
        raise HTTPException(status_code=503, detail="Telegram delivery failed; retry same eventId") from None
    return {"status": "sent", "eventId": item.event_id}
