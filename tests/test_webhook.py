"""Offline tests: no real Telegram calls and no broker orders."""
import importlib
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def bot(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TEST_ONLY_NOT_REAL")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456")
    monkeypatch.setenv("WEBHOOK_SECRET", "TEST_SECRET")
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.sqlite3"))
    sys.modules.pop("main", None)
    module = importlib.import_module("main")
    return module, TestClient(module.app)


def payload(event="BUY", event_id="E-1"):
    close = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
    return {
        "secret": "TEST_SECRET",
        "schemaVersion": "2",
        "strategyVersion": "RESEARCH-TEST",
        "eventId": event_id,
        "event": event,
        "timestampUTC": (close + timedelta(seconds=1)).isoformat(),
        "barCloseUTC": close.isoformat(),
        "barClosed": True,
        "accountAlias": "DEMO",
        "symbol": "EURUSD",
        "timeframe": "M5",
        "side": "BUY",
        "signalState": "VALID",
        "engineState": "NORMAL",
        "riskState": "NORMAL",
        "signalId": "SIG-1",
        "regime": "STRONG_UP",
        "module": "PULLBACK",
        "payoffProfile": "BASE",
        "sourceHash": "SOURCE-1",
        "parameterHash": "PARAM-1",
        "referenceEntry": 1.1,
        "stopLoss": 1.099,
        "takeProfit": 1.1012,
        "riskReward": 1.2,
        "riskPct": 0.5,
        "RSI": 57.2,
        "reason": "VALID_CLOSED_M5",
    }


def test_buy_message_has_prices_and_no_placeholders(bot):
    module, _ = bot
    result = module.prepare(payload())
    assert "1.099" in result.message
    assert "1.1012" in result.message
    assert "UNKNOWN" not in result.message
    assert "N/A" not in result.message
    assert "XTB" not in result.message
    assert "исполнение заявки ещё не подтверждено" in result.message


@pytest.mark.parametrize("field", ["referenceEntry", "stopLoss", "takeProfit", "riskReward", "riskPct", "signalId"])
def test_missing_required_signal_rejected(bot, field):
    module, _ = bot
    p = payload()
    p.pop(field)
    with pytest.raises(module.ContractError):
        module.prepare(p)


def test_impossible_sl_tp_rejected(bot):
    module, _ = bot
    p = payload()
    p["stopLoss"] = 1.102
    with pytest.raises(module.ContractError):
        module.prepare(p)


def test_zero_price_and_nan_rejected(bot):
    module, _ = bot
    p = payload()
    p["referenceEntry"] = 0
    with pytest.raises(module.ContractError):
        module.prepare(p)
    p["referenceEntry"] = "NaN"
    with pytest.raises(module.ContractError):
        module.prepare(p)


def test_preattention_does_not_claim_trade_and_dedupes_one_bar(bot):
    module, _ = bot
    p = payload(event="PREATTENTION")
    p["timestampUTC"] = datetime(2026, 11, 9, 9, 59, tzinfo=timezone.utc).isoformat()
    p["secondsToClose"] = 60
    p["barClosed"] = False
    item = module.prepare(p)
    assert "НЕ ВХОДИТЬ" in item.message
    assert module.reserve(item) == "accepted"
    module.mark(item, "sent", "21")
    p["eventId"] = "E-SECOND"
    item2 = module.prepare(p)
    assert module.reserve(item2) == "duplicate"


def test_no_preattention_when_trade_open(bot):
    module, _ = bot
    p = payload(event="PREATTENTION")
    p["timestampUTC"] = datetime(2026, 11, 9, 9, 59, tzinfo=timezone.utc).isoformat()
    p["secondsToClose"] = 60
    p["barClosed"] = False
    p["activeTrade"] = True
    with pytest.raises(module.ContractError):
        module.prepare(p)


def test_duplicate_requests_only_one_delivery(bot, monkeypatch):
    module, client = bot
    sent = []
    monkeypatch.setattr(module, "send", lambda msg: sent.append(msg) or "101")
    p = payload()
    first = client.post("/webhook", json=p)
    second = client.post("/webhook", json=p)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "sent"
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "duplicate"
    assert len(sent) == 1


def test_same_id_modified_payload_is_conflict(bot, monkeypatch):
    module, client = bot
    monkeypatch.setattr(module, "send", lambda msg: "101")
    p = payload()
    assert client.post("/webhook", json=p).status_code == 200
    p["reason"] = "SOMETHING_ELSE"
    assert client.post("/webhook", json=p).status_code == 409


def test_wrong_secret_no_message(bot, monkeypatch):
    module, client = bot
    monkeypatch.setattr(module, "send", lambda msg: pytest.fail("Telegram must not be called"))
    p = payload()
    p["secret"] = "WRONG"
    assert client.post("/webhook", json=p).status_code == 403


def test_legacy_webhook_cannot_send_trade(bot, monkeypatch):
    module, client = bot
    monkeypatch.setattr(module, "send", lambda msg: pytest.fail("Telegram must not be called"))
    p = {"secret": "TEST_SECRET", "symbol": "EURUSD", "tf": "M5", "signal": "BUY"}
    assert client.post("/webhook", json=p).status_code == 422


def test_status_does_not_require_prices(bot):
    module, _ = bot
    p = payload(event="SYSTEM_STATUS")
    for field in ("referenceEntry", "stopLoss", "takeProfit", "riskReward", "riskPct", "signalId", "barCloseUTC"):
        p.pop(field, None)
    p["side"] = "NONE"
    result = module.prepare(p)
    assert "SYSTEM STATUS" in result.message
    assert "N/A" not in result.message


def test_markup_escaped(bot):
    module, _ = bot
    p = payload()
    p["reason"] = "<script>alert(1)</script>"
    result = module.prepare(p)
    assert "<script>" not in result.message
    assert "&lt;script&gt;" in result.message


def test_failed_delivery_can_retry(bot, monkeypatch):
    module, client = bot
    p = payload()
    calls = []
    def send(message):
        calls.append(message)
        if len(calls) == 1:
            raise RuntimeError("Telegram offline")
        return "400"
    monkeypatch.setattr(module, "send", send)
    assert client.post("/webhook", json=p).status_code == 503
    assert client.post("/webhook", json=p).status_code == 200
    assert len(calls) == 2
