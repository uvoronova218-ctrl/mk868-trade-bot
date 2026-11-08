# MONEY ENGINE Telegram notifier v2 — candidate

**Status:** isolated feature branch. Do NOT merge/deploy to Render without an explicit owner decision and test evidence. No broker connection or trading commands.

## Existing system

- Repository: mk868-trade-bot
- Render service: mk868-trade-bot; current production reads original main branch
- Confirmed by user: health GET /, Telegram direct test, existing JSON webhook test
- Candidate branch: feature/money-engine-telegram-v2
- Older TradingView payloads intentionally **do not** match the v2 contract. A cutover without changing the producer will break old alerts.

## v2 delivery

HTTP POST /webhook, Content-Type: application/json. Authentication is a JSON \`secret\` matched in constant time to WEBHOOK_SECRET. Never place credentials in Git or chat. TLS only. Requires explicit v2 \`schemaVersion\`, \`eventId\`, \`strategyVersion\`, \`accountAlias\`, \`timestampUTC\` and nonempty \`reason\` per message. BUY/SELL require fully closed M5 evidence, a valid signal ID, risk state, usable prices, matching side and geometric RR; they do **not** prove order execution. ORDER_FILLED is a separate event. PREATTENTION represents preliminary geometry only, is never an order, and is suppressed for paused/blocked states and active trades.

Supported event families: PREATTENTION; BUY/SELL/SIGNAL_CONFIRMED; ORDER_SUBMITTED, ORDER_FILLED, ORDER_REJECTED; TP, SL, TIMEOUT, INVALIDATED; MANAGEMENT; PAUSE, RECOVERY, REDUCED, KILL, SYSTEM_STATUS, DATA_INTEGRITY, REGIME_CHANGE, CONNECTION_LOST, CONNECTION_RESTORED.

**Use canonical v2 camelCase keys, not the legacy** \`tf\`, \`signal\`, \`sl\`, \`tp\`, \`rr\`, \`h1\` keys. Prices may be omitted for service events only. No UNKNOWN, N/A, zero price or fabricated values in trade notifications. This is a **candidate interface**; compare Stage 16 output with HYBRID M5 and approved MONEY ENGINE spec before final release.

### Example — research/demo signal, not a real trade

\`\`\`json
{
  "secret":"<WEBHOOK_SECRET FROM ENV>",
  "schemaVersion":"2",
  "strategyVersion":"RESEARCH-TEST",
  "eventId":"TEST-20261109-001",
  "event":"BUY",
  "timestampUTC":"2026-11-09T10:00:01Z",
  "barCloseUTC":"2026-11-09T10:00:00Z",
  "barClosed":true,
  "accountAlias":"IC-TRADING-DEMO",
  "symbol":"EURUSD",
  "timeframe":"M5",
  "side":"BUY",
  "signalState":"VALID",
  "engineState":"NORMAL",
  "riskState":"NORMAL",
  "signalId":"TEST-SIGNAL-001",
  "regime":"STRONG_UP",
  "module":"PULLBACK",
  "payoffProfile":"BASE",
  "sourceHash":"TEST-SOURCE",
  "parameterHash":"TEST-PARAM",
  "referenceEntry":1.10000,
  "stopLoss":1.09900,
  "takeProfit":1.10120,
  "riskReward":1.2,
  "riskPct":0.5,
  "RSI":57.2,
  "reason":"TEST_ONLY_NO_TRADE"
}
\`\`\`

**Example numbers are synthetic and for JSON validation only. Do not use for live signals.**

### Environment

\`\`\`text
TELEGRAM_BOT_TOKEN  # required, secret
TELEGRAM_CHAT_ID    # required, secret
WEBHOOK_SECRET      # required, high-entropy; do not use CHANGE_ME_SECRET
EVENT_DB_PATH       # SQLite path on PERSISTENT storage for dedupe across restarts
\`\`\`

Default EVENT_DB_PATH is \`/tmp/money-engine-telegram-events.sqlite3\` and is **NOT durable on Render**. Do not claim persistent deduplication without mounting persistent storage or replacing SQLite with a managed persistent database. Render service should only be switched after verifying persistence and access controls.

Limitations: Telegram sendMessage is not transactional with local SQLite. If the network send succeeds but the process fails before marking delivered, a retry may send a second notification; true end-to-end exactly-once delivery is **not guaranteed**. With a shared-volume SQLite backend and multiple replicas, review leader locking and transaction timing. Pending/failed states may be retried by producer with the exact same eventId. Pre-attention suppression keys by account/symbol/M5 bar end.

Health GET / only proves the HTTP service is responding; it does not prove database durability, credentials, Telegram availability, or brokerage access. No inbound Telegram commands are implemented; they need separate owner-approved authorization later.

### Local offline tests

\`\`\`bash
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
\`\`\`

Tests monkeypatch Telegram sending; no real token and no orders. A passing local test does not certify Render compatibility or production readiness.
