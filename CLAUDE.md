# CLAUDE.md - Tono-Bot AI Assistant Guide

## Project Overview

**Tono-Bot** is a WhatsApp-based chatbot for **Tractos y Max**, a commercial vehicle (truck) dealership in Tlalnepantla, Mexico. The bot's core philosophy is **"DESTRABAR" (unblock)** - removing barriers and answering customer questions to encourage dealership visits, rather than hard selling.

### Key Capabilities
- WhatsApp customer support via Evolution API
- AI-powered conversations using OpenAI GPT (gpt-4o-mini)
- Vehicle inventory management with Google Sheets integration
- Lead generation and Monday.com CRM integration
- Audio message transcription (Whisper API)
- Human handoff detection
- Conversation memory with SQLite persistence
- Photo carousel for vehicles

## Repository Structure

```
/home/user/tono-bot/
├── Dockerfile                      # Root Docker config (used by Render)
├── CLAUDE.md                       # This file
└── tono-bot/                       # Main application directory
    ├── Dockerfile                  # Alternative Docker config
    ├── requirements.txt            # Python dependencies
    ├── src/
    │   ├── main.py                 # FastAPI entry point, webhooks, state management (~770 lines)
    │   ├── conversation_logic.py   # GPT conversation handler, prompts (~880 lines)
    │   ├── inventory_service.py    # Vehicle inventory from CSV/Google Sheets (~90 lines)
    │   ├── memory_store.py         # SQLite session persistence (~60 lines)
    │   └── monday_service.py       # Monday.com CRM integration (~180 lines)
    └── data/
        └── inventory.csv           # Vehicle catalog (~37 items)
```

## Tech Stack

| Component | Technology |
|-----------|------------|
| Framework | FastAPI 0.115.0 |
| Server | Uvicorn 0.30.6 |
| Runtime | Python 3.11 |
| HTTP Client | httpx 0.27.2 (async) |
| Database | SQLite via aiosqlite 0.20.0 |
| AI | OpenAI API (gpt-4o-mini, Whisper) |
| WhatsApp | Evolution API |
| CRM | Monday.com GraphQL API |
| Data | Pandas 2.2.3, Google Sheets CSV |
| Config | Pydantic Settings 2.6.1 |
| Timezone | pytz (America/Mexico_City) |

## Environment Variables

### Required
```bash
EVOLUTION_API_URL        # Evolution API endpoint
EVOLUTION_API_KEY        # Evolution API authentication
OPENAI_API_KEY           # OpenAI API key
```

### Optional (with defaults)
```bash
EVO_INSTANCE="Maximo Cervantes 2"     # WhatsApp instance name
OPENAI_MODEL="gpt-4o-mini"            # GPT model to use
OWNER_PHONE=""                         # Owner's phone for alerts
SHEET_CSV_URL=""                       # Google Sheets CSV URL for inventory
INVENTORY_REFRESH_SECONDS=300          # Inventory cache TTL
SQLITE_PATH="/app/tono-bot/db/memory.db"  # SQLite database path
TEAM_NUMBERS=""                        # Comma-separated handoff numbers
AUTO_REACTIVATE_MINUTES=60             # Bot silence duration after human detection
HUMAN_DETECTION_WINDOW_SECONDS=3       # Time window for human detection
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
| Ad ID | Text | `text_mm0wcdmz` | Bot (Meta source_id from referral) |
| CTWA CLID | Text | `text_mm0wwwg1` | Bot (Click-to-WhatsApp click ID) |
| Campaign Name | Text | `text_mm0w77pn` | Future (Meta Marketing API batch enrichment) |
| Ad Set Name | Text | `text_mm0wtebg` | Future (Meta Marketing API batch enrichment) |
| Ad Name | Text | `text_mm0wtpwb` | Future (Meta Marketing API batch enrichment) |

### Vehicle Dropdown Labels
`Tunland E5`, `ESTA 6x4 11.8`, `ESTA 6x4 X13`, `Miler`, `Toano Panel`, `Tunland G7`, `Tunland G9`

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

## Key Architecture Patterns

### 1. Async Everything
All I/O operations use async/await:
- `httpx.AsyncClient` for HTTP requests
- `aiosqlite` for database operations
- `AsyncOpenAI` for GPT calls
- Background task processing for webhook responses

### 2. Global State Management
`GlobalState` class in `main.py` manages runtime state:
- HTTP client connection
- Inventory service instance
- SQLite memory store
- Deduplication sets (BoundedOrderedSet with FIFO eviction)
- User silencing for human handoff

### 3. Error Handling
- Exponential backoff retry (3 attempts) for transient failures
- Graceful degradation with default messages on API errors
- Rate limit handling (429 responses)
- Sanitized logging (no API keys in logs)

### 4. Facebook Referral Tracking (CTWA)
Automatic detection of leads arriving from Facebook/Instagram ads:
- **Baileys mode**: Extracts `contextInfo.conversionSource`, `entryPointConversionSource`, `entryPointConversionApp`
- **Cloud API mode**: Extracts `referral` object with `source_url`, `source_id`, `ctwa_clid`, `headline`, etc.
- Referral data captured on first message and persisted in session context (`referral_source`, `referral_data`)
- Stored in `GlobalState.pending_referrals` until persisted to SQLite
- Source label auto-populated in Monday.com "Origen Lead" column
- Referral details included in Monday.com lead creation notes and owner alerts
- Known Baileys limitation: `remoteJid` may arrive in `@lid` format (Evolution API issue #2267)

### 5. Human Detection
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
- SQLite async wrapper
- Phone-keyed session storage
- Upsert logic for state + context JSON

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
- SQLite database persisted in `/app/tono-bot/db/`

## Recent Development Focus

Based on commit history:
- Conversation quality (semantic summaries, anti-repetition)
- Async migration (httpx, aiosqlite, AsyncOpenAI)
- Token optimization for GPT context
- Infrastructure reliability (retry logic, error handling)
- Dependency injection (no module-level globals)

## Common Issues & Solutions

| Issue | Solution |
|-------|----------|
| Bot responds to its own messages | Message ID deduplication in `processed_message_ids` |
| Repeated greetings | Turn count tracking, only greet on turn 1 |
| Google Sheets 403 | Check `SHEET_CSV_URL` is public CSV export link |
| Monday.com duplicates | Phone normalization and dedup search before create |
| Memory bloat | BoundedOrderedSet with FIFO eviction limits |
