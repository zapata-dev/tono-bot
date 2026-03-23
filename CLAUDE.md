# CLAUDE.md - Tono-Bot AI Assistant Guide

## Project Overview

**Tono-Bot** is a WhatsApp-based chatbot for **Tractos y Max**, a commercial vehicle (truck) dealership in Tlalnepantla, Mexico. The bot's core philosophy is **"DESTRABAR" (unblock)** - removing barriers and answering customer questions to encourage dealership visits, rather than hard selling.

### Key Capabilities
- WhatsApp customer support via Evolution API
- AI-powered conversations with Dual LLM (Gemini primary + OpenAI fallback)
- Vehicle inventory management with Google Sheets integration
- Lead generation and Monday.com CRM integration
- Audio message transcription (Whisper API)
- Image analysis via Vision API (Gemini/OpenAI)
- Human handoff detection
- Conversation memory with Supabase (PostgreSQL) persistence
- Photo carousel for vehicles
- PDF sending (fichas técnicas + corridas financieras) from financing.json
- Message accumulation (debouncing) for rapid messages
- Facebook/Instagram referral tracking (CTWA)

## Repository Structure

```
/home/user/tono-bot/
├── Dockerfile                      # Root Docker config (used by Render)
├── CLAUDE.md                       # This file
├── .env.example                    # Environment variables template
├── docs/
│   ├── BUSINESS_LOGIC.md           # Business logic & funnel rules
│   ├── MANUAL_INVENTARIO.md        # Inventory management guide
│   ├── GUIA_COTIZACIONES.md        # Financing & PDF guide
│   ├── CRM_OPERATIONS.md           # CRM operations manual
│   ├── AI_ARCHITECTURE.md          # AI architecture & concurrency
│   └── RUNBOOK.md                  # Troubleshooting & operations
└── tono-bot/                       # Main application directory
    ├── .dockerignore               # Excludes .env, .db, .git from Docker builds
    ├── Dockerfile                  # Alternative Docker config
    ├── requirements.txt            # Python dependencies
    ├── src/
    │   ├── main.py                 # FastAPI entry point, webhooks, debouncing, vision (~1520 lines)
    │   ├── conversation_logic.py   # Dual LLM handler, prompts, PDF detection (~1960 lines)
    │   ├── inventory_service.py    # Vehicle inventory from CSV/Google Sheets (~110 lines)
    │   ├── memory_store.py         # Supabase session persistence (~80 lines)
    │   └── monday_service.py       # Monday.com CRM integration, funnel V2 (~800 lines)
    └── data/
        ├── inventory.csv           # Vehicle catalog (8 models)
        └── financing.json          # Financing data & PDF URLs (6 models)
```

## Tech Stack

| Component | Technology |
|-----------|------------|
| Framework | FastAPI 0.115.0 |
| Server | Uvicorn 0.30.6 |
| Runtime | Python 3.11 |
| HTTP Client | httpx 0.27.2 (async) |
| Database | Supabase (PostgreSQL) via supabase-py 2.13.0 |
| AI (Primary) | Google Gemini via OpenAI SDK (gemini-2.5-flash-lite) |
| AI (Fallback) | OpenAI API (gpt-4o-mini) |
| AI (Audio) | OpenAI Whisper API |
| AI (Vision) | Gemini Vision / OpenAI Vision |
| WhatsApp | Evolution API |
| CRM | Monday.com GraphQL API |
| Data | Pandas 2.2.3, Google Sheets CSV |
| Config | Pydantic Settings 2.6.1 |
| Monitoring | Sentry SDK 2.19.2 (opt-in via SENTRY_DSN) |
| Timezone | pytz (America/Mexico_City) |

> **Note:** A single OpenAI SDK (`openai==1.59.7`) handles both Gemini (via `base_url`) and OpenAI (native) clients.

## Environment Variables

### Required
```bash
EVOLUTION_API_URL        # Evolution API endpoint
EVOLUTION_API_KEY        # Evolution API authentication
OPENAI_API_KEY           # OpenAI API key (fallback LLM + Whisper)
GEMINI_API_KEY           # Google Gemini API key (primary LLM)
SUPABASE_URL             # Supabase project URL (https://xxxx.supabase.co)
SUPABASE_KEY             # Supabase anon/service_role key
```

### Optional (with defaults)
```bash
# --- LLM Configuration ---
LLM_PRIMARY="gemini"                   # Primary LLM provider ("gemini" or "openai")
OPENAI_MODEL="gemini-2.5-flash-lite"   # Primary model name (Gemini)
OPENAI_FALLBACK_MODEL="gpt-4o-mini"    # Fallback model name (OpenAI)

# --- WhatsApp & Bot ---
EVO_INSTANCE="Maximo Cervantes 2"     # WhatsApp instance name
OWNER_PHONE=""                         # Owner's phone for alerts
MESSAGE_ACCUMULATION_SECONDS=8.0       # Debounce window for rapid messages

# --- Data Sources ---
SHEET_CSV_URL=""                       # Google Sheets CSV URL for inventory
INVENTORY_REFRESH_SECONDS=300          # Inventory cache TTL

# --- Handoff ---
TEAM_NUMBERS=""                        # Comma-separated handoff numbers
AUTO_REACTIVATE_MINUTES=60             # Bot silence duration after human detection
HUMAN_DETECTION_WINDOW_SECONDS=3       # Time window for human detection

# --- Logging ---
LOG_WEBHOOK_PAYLOAD=true               # Enable/disable webhook payload logging
LOG_WEBHOOK_PAYLOAD_MAX_CHARS=6000     # Truncate webhook logs at N chars

# --- Monitoring ---
SENTRY_DSN=""                          # Sentry DSN (opt-in, enables error tracking + 10% tracing)

# --- Monday.com ---
MONDAY_API_KEY=""                      # Monday.com API key
MONDAY_BOARD_ID=""                     # Monday.com board ID (Leads Bot Adrian: 18396811838)
MONDAY_DEDUPE_COLUMN_ID=""             # Monday.com dedup column (text_mkzw7xjz)
MONDAY_LAST_MSG_ID_COLUMN_ID=""        # Monday.com message tracking column (text_mkzwndf)
MONDAY_PHONE_COLUMN_ID=""              # Monday.com phone column (phone_mkzwh34a)
MONDAY_STAGE_COLUMN_ID=""              # Monday.com funnel stage column - STATUS type (status)
MONDAY_VEHICLE_COLUMN_ID=""            # Monday.com vehicle dropdown column (dropdown_mm0gq48r)
MONDAY_PAYMENT_COLUMN_ID=""            # Monday.com payment status column (color_mm0gbjea)
MONDAY_APPOINTMENT_COLUMN_ID=""        # Monday.com appointment date column (date_mm0grgky)
MONDAY_APPOINTMENT_TIME_COLUMN_ID=""   # Monday.com appointment time/hour column (hour_mm0hfk47)
MONDAY_CMV_COLUMN_ID=""                # Monday.com CMV checkbox column (boolean_mm0g2zf3)
MONDAY_SOURCE_COLUMN_ID=""             # Monday.com lead source/origin column - STATUS type (color_mm0wb2gm)
MONDAY_CHANNEL_COLUMN_ID=""            # Monday.com channel column - STATUS type (color_mm0wf5zn)
MONDAY_SOURCE_TYPE_COLUMN_ID=""        # Monday.com source type column - STATUS type (color_mm0w1mtn)
MONDAY_AD_ID_COLUMN_ID=""              # Monday.com Ad ID column - TEXT type (text_mm0wcdmz)
MONDAY_CTWA_CLID_COLUMN_ID=""         # Monday.com CTWA Click ID column - TEXT type (text_mm0wwwg1)
MONDAY_CAMPAIGN_NAME_COLUMN_ID=""      # Monday.com Campaign Name column - TEXT type (text_mm0w77pn)
MONDAY_ADSET_NAME_COLUMN_ID=""         # Monday.com Ad Set Name column - TEXT type (text_mm0wtebg)
MONDAY_AD_NAME_COLUMN_ID=""            # Monday.com Ad Name column - TEXT type (text_mm0wtpwb)
MONDAY_TRACKING_ID_COLUMN_ID=""        # Monday.com Tracking ID column on Leads board - TEXT type
MONDAY_ADS_BOARD_ID=""                 # Monday.com Anuncios board ID
MONDAY_ADS_TRACKING_COLUMN_ID=""       # Monday.com Tracking ID column on Anuncios board - TEXT type
MONDAY_LEADS_CONNECT_ADS_COLUMN_ID=""  # Monday.com Connect Boards column linking Leads → Anuncios
```

## Sales Funnel System (V2)

The bot automatically tracks leads through a 10-stage sales funnel in Monday.com:

| Stage | Trigger | Who moves it |
|-------|---------|--------------|
| `1er Contacto` | First message from client | Bot (auto) |
| `Intención` | Specific vehicle model mentioned | Bot (auto) |
| `Cotización` | PDF (ficha técnica/corrida) sent | Bot (auto) |
| `Cita Programada` | Appointment date confirmed | Bot (auto) |
| `Sin Interes` | Client expresses disinterest | Bot (auto) |
| `Cita Atendida` | Client showed up | Human (manual) |
| `Cita No Atendida` | Client didn't show | Human (manual) |
| `Venta Cerrada` | Sale completed | Human (manual) |
| `Financiamiento en Gestión` | Financing in process | Human (manual) |
| `Venta Caida` | Sale fell through | Human (manual) |

### V2 Funnel Rules
1. **Only advances, never regresses** - Stage hierarchy enforced in code (`STAGE_HIERARCHY`)
2. Lead is created on **1er Contacto** (first interaction)
3. Stage updates automatically as conversation progresses
4. Notes are added at each stage transition with relevant details
5. **Terminal states** (`Venta Cerrada`, `Venta Caida`, `Sin Interes`) → new item created for next cycle
6. **Sin Interes** can override any stage (explicit disinterest from client)

### V2 Dedicated Columns
| Column | Type | Monday ID | Populated by |
|--------|------|-----------|-------------|
| Vehículo de Interés | Dropdown | `dropdown_mm0gq48r` | Bot (auto-detected from conversation) |
| Esquema de Pago | Status | `color_mm0gbjea` | Bot (Contado/Financiamiento/Por definir) |
| Agenda Citas (Día) | Date | `date_mm0grgky` | Bot (solo fecha, sin hora) |
| Hora Cita | Hour | `hour_mm0hfk47` | Bot (hora parseada de la cita) |
| Confirmación CMV | Checkbox | `boolean_mm0g2zf3` | Human (manual) |
| Origen Lead | Status | `color_mm0wb2gm` | Bot (auto-detected from CTWA/referral) |
| Canal | Status | `color_mm0wf5zn` | Bot (Facebook/Instagram/Directo) |
| Tipo Origen | Status | `color_mm0w1mtn` | Bot (Ad/Post/Directo) |
| Ad ID | Text | `text_mm0wcdmz` | Bot (Cloud API only: `referral.source_id`; NOT available in Baileys) |
| CTWA CLID | Text | `text_mm0wwwg1` | Bot (Cloud API: `referral.ctwa_clid`; Baileys: decoded `conversionData`) |
| Campaign Name | Text | `text_mm0w77pn` | Future (Meta Marketing API batch enrichment from Ad ID) |
| Ad Set Name | Text | `text_mm0wtebg` | Future (Meta Marketing API batch enrichment from Ad ID) |
| Ad Name | Text | `text_mm0wtpwb` | Future (Meta Marketing API batch enrichment from Ad ID) |

### Vehicle Dropdown Labels
`Tunland E5`, `ESTA 6x4 11.8`, `ESTA 6x4 X13`, `Miler`, `Toano Panel`, `Tunland G7`, `Tunland G9`, `Cascadia`

### Payment Status Labels
`De Contado`, `Financiamiento`, `Por definir`

### Lead Source Labels (Auto-detected)
`Facebook Ad`, `Facebook Post`, `Instagram Ad`, `Instagram Post`, `Facebook`, `Instagram`, `Directo`

### Channel Labels (Canal)
`Facebook`, `Instagram`, `Directo`

### Source Type Labels (Tipo Origen)
`Ad`, `Post`, `Directo`

### Monday.com Board Setup
- **Board**: "Leads Tractos y Max" (ID: `18396811838`)
- **Estado column** (STATUS, ID: `status`): Labels listed in funnel table above
- **Groups**: Auto-created by month (e.g., "FEBRERO 2026")
- Set all `MONDAY_*` env vars in Render (see Environment Variables section)

## Ad Attribution System (V3 - Tracking ID)

Since Baileys/Evolution API does NOT provide Meta Ads metadata reliably, the bot uses an internal Tracking ID system for ad attribution.

### Tracking ID Format
`<MODEL_CODE>-<CAMPAIGN_TYPE><NUMBER>` — Embedded in pre-filled WhatsApp ad messages.

### Model Code Map
| Code | Vehicle |
|------|---------|
| `TG7` | Tunland G7 |
| `TG9` | Tunland G9 |
| `TE5` | Tunland E5 |
| `ML` | Miler |
| `TP` | Toano Panel |
| `E11` | ESTA 6x4 11.8 |
| `EX` | ESTA 6x4 X13 |
| `CA` | Cascadia |

### Campaign Type Codes
| Code | Type | Description |
|------|------|-------------|
| `A` | Anuncio | Regular Facebook/Instagram ad |
| `SU` | Mejor Precio | Mejor Propuesta / Precio especial |
| `LQ` | Liquidación | Liquidación / Precio especial |
| `PR` | Promoción | Promoción especial |
| `EV` | Evento | Evento / Open House |

Examples: `TG9-A1` (Tunland G9, Ad #1), `CA-SU1` (Cascadia, Mejor Precio #1), `ML-LQ2` (Miler, Liquidación #2)

### How It Works
1. Facebook/Instagram ad has pre-filled message: "Hola, me interesa TG9-A1" or "Hola CA-SU1"
2. Bot detects pattern `[A-Z][A-Z0-9]{1,3}-(A|SU|LQ|PR|EV)\d{1,3}` in first message
3. Model auto-resolved → `last_interest` set to vehicle label
4. Campaign type resolved → context includes type (e.g., "Mejor Precio de Cascadia")
5. Tracking ID stripped from message before GPT processing
6. GPT receives tracking context: "Este cliente llegó por Mejor Precio de Cascadia"
7. Lead created in Monday.com with tracking_id column populated
8. Lead linked to Anuncio item via Connect Boards column
9. Owner alert includes tracking ID

### Monday.com Anuncios Board
Separate board for cataloging active ads:
- **Tracking ID** (Text, unique key): `TG9-A1`
- **Modelo** (Dropdown): Same labels as Leads board vehicle dropdown
- **Campaign Name** (Text): Facebook campaign name
- **Canal** (Status): Facebook / Instagram
- **Activo** (Checkbox): Whether ad is currently running

### Leads Board New Columns
- **Tracking ID** (Text): Stores detected tracking code
- **Anuncio** (Connect Boards): Links to Anuncios board item

### Tracking ID vs CTWA Referral
- Tracking ID works with **Baileys** (no Meta API needed)
- CTWA referral requires Meta Cloud API (partially available with Baileys `conversionData`)
- Both systems coexist: if a lead has CTWA data AND a tracking ID, both are stored
- If only tracking ID exists (no CTWA), `referral_source` is set to `"Ad Tracking: TG9-A1"`

## Key Architecture Patterns

### 1. Dual LLM with Smart Fallback
- **Gemini** (primary): Via OpenAI SDK with `base_url=generativelanguage.googleapis.com/v1beta/openai/`
- **OpenAI** (fallback): Native OpenAI SDK
- **AUTO-SWITCH at startup**: Smoke test (DNS + TCP + HTTPS + API call) against Gemini; if unreachable, OpenAI becomes primary automatically
- **Per-request fallback** (`_llm_call_with_fallback()`): 2 quick retries per provider (1s, 2s backoff), then switches to secondary
- **IPv4 forced** on Render (Gemini sometimes fails with IPv6)

### 2. Message Accumulation (Debouncing)
- Groups rapid messages from same client into single LLM call (`MESSAGE_ACCUMULATION_SECONDS=8.0`)
- `pending_messages[jid]`: accumulates user messages per JID
- `pending_message_tasks[jid]`: async timer per user
- **Drain loop**: Re-processes messages that arrive during bot thinking
- 2+ messages combined as: `"msg1 | msg2 | msg3"`
- **Per-JID lock** (`asyncio.Lock()`) prevents race conditions

### 3. Async Everything
All I/O operations use async/await:
- `httpx.AsyncClient` for HTTP requests
- `aiosqlite` for database operations
- `AsyncOpenAI` for both Gemini and OpenAI calls
- Background task processing for webhook responses

### 4. Global State Management
`GlobalState` class in `main.py` manages runtime state:
- HTTP client connection
- Inventory service instance
- SQLite memory store
- Deduplication sets (BoundedOrderedSet with FIFO eviction)
- User silencing for human handoff
- Pending messages and accumulation timers
- Pending referrals (`Dict[str, Dict[str, str]]`)
- Per-JID processing locks

### 5. Error Handling
- Exponential backoff retry (2 attempts per provider) for transient failures
- Graceful degradation with default messages on API errors
- Rate limit handling (429 responses)
- Sanitized logging (no API keys in logs)

### 6. Image Analysis (Vision)
- `_handle_image_analysis()` in main.py downloads image from Evolution API (base64)
- Sends to OpenAI Vision API for analysis
- Result injected into conversation as: `"[El cliente envió una foto que muestra: ...]"`
- Bot responds contextually using the image description

### 7. Smart Context Injection (Token Optimization)
- **Turn 1-2**: Full inventory injected into GPT context
- **Turn 3+**: Only focused inventory (matched model) to save tokens
- **Financing data**: Only injected when user message contains financing keywords
- Saves ~2000 tokens per call vs always including full inventory

### 8. Facebook Referral Tracking (CTWA)
Automatic detection of leads arriving from Facebook/Instagram ads:
- **Baileys mode**: Extracts `contextInfo.conversionSource`, `entryPointConversionSource`, `entryPointConversionApp`
- **Cloud API mode**: Extracts `referral` object with `source_url`, `source_id`, `ctwa_clid`, `headline`, etc.
- Referral extraction runs **before** the `fromMe` filter (Baileys sends `conversionSource` on outgoing `fromMe=true` messages)
- Referral data captured on first message and persisted in session context (`referral_source`, `referral_data`)
- Stored in `GlobalState.pending_referrals` until persisted to SQLite
- Source label auto-populated in Monday.com "Origen Lead" column
- Referral details included in Monday.com lead creation notes and owner alerts
- Known Baileys limitation: `remoteJid` may arrive in `@lid` format (Evolution API issue #2267)
- Known Baileys limitation: `source_id` (Ad ID) is NOT available — only Cloud API provides it via `referral.source_id`
- Campaign Name, Ad Set Name, Ad Name require Meta Marketing API batch enrichment (future feature)

### 10. Error Monitoring (Sentry)
- **Opt-in**: Only active if `SENTRY_DSN` env var is set (empty = disabled, zero impact)
- **FastAPI integration**: Auto-captures unhandled exceptions with full stack traces
- **Trace sampling**: 10% of requests sampled for performance monitoring
- **Init**: Conditional at startup in `main.py`, before any request processing

### 9. Human Detection
Multi-layer heuristics to detect when a human agent takes over:
- Emoji presence in messages
- Specific human phrases
- Typing patterns and timestamps
- Message ID tracking

## Code Conventions

### Language
- Code comments: Spanish/English mix
- Variable names: Often Spanish (reflecting business domain)
- Commit messages: English
- Logging: English with emoji prefixes for visual scanning

### Style
- Type hints throughout (`Optional`, `Dict`, `List`, `Tuple`, etc.)
- Private functions prefixed with `_` (e.g., `_extract_name_from_text`)
- Pydantic models for configuration validation
- No global module variables (inject via function parameters)
- Defensive programming with null checks

### Bot Personality
- Name: "Adrian Jimenez"
- Max 2 sentences per response
- No emojis in bot messages
- Professional but natural tone
- Spanish language responses

## Development Commands

### Run Locally
```bash
cd tono-bot
pip install -r requirements.txt
uvicorn src.main:app --host 0.0.0.0 --port 8080 --reload
```

### Docker Build
```bash
docker build -t tono-bot .
docker run -p 8080:8080 --env-file .env tono-bot
```

### Health Check
```bash
curl http://localhost:8080/health
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check with bot metrics |
| `/webhook` | POST | Evolution API webhook receiver |

## Key Files Reference

### main.py (Entry Point)
- `Settings` class: Pydantic configuration at line 26
- `BoundedOrderedSet`: FIFO eviction set at line 71
- `GlobalState`: Runtime state at line 92
- `lifespan()`: Startup/shutdown lifecycle
- `process_webhook()`: Main webhook handler
- Human detection logic with multiple heuristics
- Audio transcription pipeline

### conversation_logic.py (GPT Handler)
- `SYSTEM_PROMPT`: Bot personality and rules at line 38
- `handle_message()`: Main conversation entry point
- Turn tracking to prevent repetitive greetings
- Interest extraction from conversation
- Lead generation with JSON parsing
- Photo carousel state management

### inventory_service.py (Inventory)
- Dual-source loading (local CSV or Google Sheets)
- 300-second refresh caching
- Semantic formatting for GPT context

### memory_store.py (Persistence)
- Supabase (PostgreSQL) wrapper via supabase-py
- Phone-keyed session storage
- Upsert logic for state + context JSONB

### monday_service.py (CRM - V2)
- GraphQL mutations for lead creation/update
- Phone-based deduplication
- V2 stage hierarchy (only-forward progression)
- Terminal state detection (creates new item for new sales cycle)
- Vehicle dropdown resolution (`VEHICLE_DROPDOWN_MAP`)
- Payment label resolution (`resolve_payment_to_label`)
- Appointment date parsing to ISO (`resolve_appointment_to_iso`)
- Monthly group auto-assignment
- Retry logic with backoff

## Important Implementation Notes

1. **Webhook ACK**: Return 200 immediately, process in background to prevent retries

2. **Deduplication**: BoundedOrderedSet with 4000-8000 item limits prevents memory bloat

3. **Token Efficiency**: Conversation history truncated to ~4000 chars for GPT context

4. **Photo Carousel**: State tracked in context (`photo_index`, `photo_model`)

5. **Lead Generation**: Requires NAME + MODEL + CONFIRMED APPOINTMENT for CRM entry

6. **Human Typing Delay**: 5-10 second random delay simulates human response time

7. **Rate Limiting**: Respect 429 responses with exponential backoff

8. **Facebook Referral Tracking**: CTWA referral data extracted from first message webhook, persisted in session context, and sent to Monday.com source column. Supports both Baileys (`contextInfo`) and Cloud API (`referral` object) formats.

9. **Ad Tracking ID (V3)**: Internal attribution system detecting `<MODEL_CODE>-<CAMPAIGN_TYPE><NUMBER>` pattern (e.g., `TG9-A1`, `CA-SU1`, `ML-LQ2`) in first message. Campaign types: A (Anuncio), SU (Mejor Precio), LQ (Liquidación), PR (Promoción), EV (Evento). Auto-resolves vehicle model and campaign type, strips code before GPT, persists in context, populates Monday.com Tracking ID column, and connects lead to Anuncios board.

## Testing

No formal test suite currently. Manual testing via:
- Direct WhatsApp messages to bot instance
- `/health` endpoint monitoring
- Log inspection for errors

## Deployment

Deployed on **Render** PaaS:
- Port: 8080
- Dockerfile at repo root
- Environment variables configured in Render dashboard
- Session data persisted in Supabase (PostgreSQL, external)
- **Docker hardening**:
  - Non-root user (`appuser`) — limits blast radius if container is compromised
  - `HEALTHCHECK` against `/health` (30s interval, 5s timeout, 10s start period)
  - `.dockerignore` excludes `.env`, `*.db`, `.git`, `__pycache__/`, `*.md` from builds

## Recent Development Focus

Based on commit history:
- Conversation quality (semantic summaries, anti-repetition)
- Async migration (httpx, aiosqlite, AsyncOpenAI)
- Token optimization for GPT context
- Infrastructure reliability (retry logic, error handling)
- Dependency injection (no module-level globals)
- Docker hardening (non-root user, HEALTHCHECK, .dockerignore)
- Sentry error monitoring integration (opt-in)
- Cascadia/Freightliner vehicle synonym mapping

## Common Issues & Solutions

| Issue | Solution |
|-------|----------|
| Bot responds to its own messages | Message ID deduplication in `processed_message_ids` |
| Repeated greetings | Turn count tracking, only greet on turn 1 |
| Google Sheets 403 | Check `SHEET_CSV_URL` is public CSV export link |
| Monday.com duplicates | Phone normalization and dedup search before create |
| Memory bloat | BoundedOrderedSet with FIFO eviction limits |
