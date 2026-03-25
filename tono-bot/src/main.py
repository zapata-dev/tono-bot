import os
import json
import logging
import asyncio
import socket
import tempfile
import random
import time
import re
import base64
from contextlib import asynccontextmanager
from urllib.parse import quote
from collections import deque, OrderedDict
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, Request
from pydantic_settings import BaseSettings

# === IMPORTACIONES PROPIAS ===
from src.inventory_service import InventoryService
from src.campaign_service import CampaignService
from src.conversation_logic import (
    handle_message,
    client as gemini_client,
    openai_client,
    MODEL_NAME as LLM_MODEL_NAME,
    FALLBACK_MODEL,
    LLM_PRIMARY,
    set_llm_primary,
    _GEMINI_BASE_URL,
)
from src.memory_store import MemoryStore
from src.monday_service import monday_service, extract_tracking_id, strip_tracking_id


# === 1. CONFIGURACIÓN ROBUSTA (Pydantic) ===
class Settings(BaseSettings):
    # Obligatorias
    EVOLUTION_API_URL: str
    EVOLUTION_API_KEY: str

    # Opcionales / defaults
    EVO_INSTANCE: str = "Maximo Cervantes 2"
    OWNER_PHONE: Optional[str] = None
    SHEET_CSV_URL: Optional[str] = None
    CAMPAIGNS_CSV_URL: Optional[str] = None
    CAMPAIGNS_REFRESH_SECONDS: int = 300
    INVENTORY_REFRESH_SECONDS: int = 300

    # Logging del payload (evita logs gigantes)
    LOG_WEBHOOK_PAYLOAD: bool = True
    LOG_WEBHOOK_PAYLOAD_MAX_CHARS: int = 6000

    # Handoff
    TEAM_NUMBERS: str = ""
    AUTO_REACTIVATE_MINUTES: int = 60
    HUMAN_DETECTION_WINDOW_SECONDS: int = 3

    # Acumulación de mensajes rápidos
    MESSAGE_ACCUMULATION_SECONDS: float = 8.0  # Espera para acumular mensajes seguidos

    # Monitoreo
    SENTRY_DSN: str = ""  # Si se configura, habilita Sentry error tracking

    class Config:
        env_file = ".env"
        extra = "ignore"


try:
    settings = Settings()
except Exception as e:
    print(f"❌ FATAL: Error en configuración de variables de entorno: {e}")
    raise

# Sentry (error monitoring) — solo si SENTRY_DSN está configurado
if settings.SENTRY_DSN and settings.SENTRY_DSN.strip():
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=settings.SENTRY_DSN.strip(),
            traces_sample_rate=0.1,
            environment="production",
        )
    except Exception as e:
        print(f"⚠️ Sentry init failed (invalid DSN?): {e} — continuing without Sentry")

# Logs
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("BotTractos")


# Filtro para suprimir logs de health check de uvicorn
class HealthCheckFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "GET /health" in msg:
            return False
        return True


logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())


# === 2. ESTADO GLOBAL EN RAM ===
class BoundedOrderedSet:
    """Set con O(1) lookup y evicción FIFO al llegar al límite."""

    def __init__(self, maxlen: int):
        self._data: OrderedDict = OrderedDict()
        self._maxlen = maxlen

    def add(self, key):
        if key in self._data:
            return
        if len(self._data) >= self._maxlen:
            self._data.popitem(last=False)
        self._data[key] = None

    def __contains__(self, key):
        return key in self._data

    def __len__(self):
        return len(self._data)


class GlobalState:
    def __init__(self):
        self.http_client: Optional[httpx.AsyncClient] = None
        self.inventory: Optional[InventoryService] = None
        self.campaigns: Optional[CampaignService] = None
        self.store: Optional[MemoryStore] = None

        # dedupe RAM (O(1) lookup con evicción FIFO)
        self.processed_message_ids = BoundedOrderedSet(maxlen=4000)
        self.processed_lead_ids = BoundedOrderedSet(maxlen=8000)

        # Silencios (ahora soporta timestamp o bool)
        self.silenced_users: Dict[str, Any] = {}

        # 🆕 HANDOFF: Rastreo de mensajes del bot
        self.bot_sent_message_ids = BoundedOrderedSet(maxlen=2000)
        self.bot_sent_texts: Dict[str, deque] = {}
        self.last_bot_message_time: Dict[str, float] = {}

        # 🆕 ACUMULACIÓN DE MENSAJES: Agrupa mensajes rápidos del cliente
        self.pending_messages: Dict[str, List[str]] = {}  # jid -> [msg1, msg2, ...]
        self.pending_message_tasks: Dict[str, asyncio.Task] = {}  # jid -> task
        self.last_user_message_time: Dict[str, float] = {}  # jid -> timestamp

        # 📢 REFERRAL TRACKING: Datos de origen de Facebook/Instagram ads
        self.pending_referrals: Dict[str, Dict[str, str]] = {}  # jid -> referral_data

        # 🏷️ TRACKING ID: Internal ad attribution (Baileys workaround)
        self.pending_tracking_ids: Dict[str, Dict[str, Any]] = {}  # jid -> tracking_data

        # 🔒 LOCK PER JID: Evita procesamiento concurrente para el mismo usuario
        # Sin esto, dos lotes de mensajes pueden correr en paralelo, leer contexto
        # stale de Supabase, y el segundo sobreescribir el contexto del primero.
        self.processing_locks: Dict[str, asyncio.Lock] = {}


# === 3. LIFESPAN (INICIO/CIERRE) ===
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Iniciando BotTractos con sistema completo...")

    bot_state = GlobalState()

    # A) Cliente HTTP persistente (Evolution)
    bot_state.http_client = httpx.AsyncClient(
        base_url=settings.EVOLUTION_API_URL.rstrip("/"),
        headers={"apikey": settings.EVOLUTION_API_KEY, "Content-Type": "application/json"},
        timeout=30.0,
    )

    # B) Inventario
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    INVENTORY_PATH = os.path.join(BASE_DIR, "data", "inventory.csv")

    bot_state.inventory = InventoryService(
        INVENTORY_PATH,
        sheet_csv_url=settings.SHEET_CSV_URL,
        refresh_seconds=settings.INVENTORY_REFRESH_SECONDS,
    )

    try:
        await bot_state.inventory.load(force=True)
        count = len(getattr(bot_state.inventory, "items", []) or [])
        logger.info(f"✅ Inventario cargado: {count} items.")
    except Exception as e:
        logger.error(f"⚠️ Error cargando inventario inicial: {e}")

    # B2) Campañas
    bot_state.campaigns = CampaignService(
        csv_url=settings.CAMPAIGNS_CSV_URL,
        refresh_seconds=settings.CAMPAIGNS_REFRESH_SECONDS,
    )
    if not settings.CAMPAIGNS_CSV_URL:
        logger.warning(
            "⚠️ CAMPAIGNS_CSV_URL no configurado. Las campañas especiales (Mejor Precio, "
            "Liquidación, Promoción, Evento) NO tendrán instrucciones personalizadas. "
            "El bot caerá al modo genérico de captura de datos para tracking IDs especiales."
        )
    try:
        await bot_state.campaigns.load(force=True)
        active_count = len(bot_state.campaigns.get_active_campaigns())
        logger.info(f"📢 Campañas cargadas: {active_count} activas.")
    except Exception as e:
        logger.error(f"⚠️ Error cargando campañas iniciales: {e}")

    # C) Memoria
    _store = MemoryStore()
    try:
        await _store.init()
        bot_state.store = _store
        logger.info("✅ MemoryStore inicializado.")
    except Exception as e:
        bot_state.store = None
        logger.error(f"⚠️ Error iniciando MemoryStore: {e}")

    # D) Smoke test LLM: diagnóstico de red + conectividad
    logger.info(f"🔍 LLM config: primary={LLM_PRIMARY}, model={LLM_MODEL_NAME}, fallback={FALLBACK_MODEL}")
    logger.info(f"🔍 Gemini base_url={_GEMINI_BASE_URL}")

    # D.1) DNS diagnostic
    _gemini_host = "generativelanguage.googleapis.com"
    try:
        _addrs = socket.getaddrinfo(_gemini_host, 443)
        _ipv4 = [a for a in _addrs if a[0] == socket.AF_INET]
        _ipv6 = [a for a in _addrs if a[0] == socket.AF_INET6]
        logger.info(f"🔍 DNS {_gemini_host}: {len(_ipv4)} IPv4, {len(_ipv6)} IPv6")
        if _ipv4:
            logger.info(f"   IPv4: {_ipv4[0][4][0]}")
        if _ipv6:
            logger.info(f"   IPv6: {_ipv6[0][4][0]}")
    except Exception as e:
        logger.error(f"❌ DNS resolution failed for {_gemini_host}: {e}")

    # D.2) Raw TCP connectivity test
    _gemini_reachable = False
    try:
        _sock = socket.create_connection((_gemini_host, 443), timeout=5)
        _sock.close()
        logger.info(f"✅ TCP {_gemini_host}:443 alcanzable")
        _gemini_reachable = True
    except Exception as e:
        logger.warning(f"❌ TCP {_gemini_host}:443 NO alcanzable: {type(e).__name__}: {e}")

    # D.3) Raw HTTPS test (sin OpenAI SDK, httpx default)
    if _gemini_reachable:
        try:
            async with httpx.AsyncClient(timeout=10.0) as _test_client:
                _r = await _test_client.get(f"https://{_gemini_host}/")
                logger.info(f"✅ HTTPS GET {_gemini_host} → {_r.status_code}")
        except Exception as e:
            logger.warning(f"⚠️ HTTPS GET {_gemini_host} falló: {type(e).__name__}: {e}")

    # D.4) API smoke test con OpenAI SDK (usa IPv4 forzado)
    _gemini_smoke_ok = False
    _smoke_messages = [{"role": "user", "content": "Hola"}]
    try:
        _t0 = asyncio.get_event_loop().time()
        await gemini_client.chat.completions.create(
            model=LLM_MODEL_NAME, messages=_smoke_messages, max_tokens=5,
        )
        _elapsed = asyncio.get_event_loop().time() - _t0
        logger.info(f"✅ Smoke test Gemini OK ({_elapsed:.1f}s)")
        _gemini_smoke_ok = True
    except Exception as e:
        _elapsed = asyncio.get_event_loop().time() - _t0
        logger.warning(f"⚠️ Smoke test Gemini FALLÓ ({_elapsed:.1f}s): {type(e).__name__}: {e}")
        # Log causa raíz completa
        _cause = e.__cause__
        while _cause:
            logger.warning(f"   Caused by: {type(_cause).__name__}: {_cause}")
            _cause = getattr(_cause, "__cause__", None)

    # D.5) AUTO-SWITCH: Si Gemini falla, cambiar a OpenAI como primario
    if not _gemini_smoke_ok and LLM_PRIMARY == "gemini":
        set_llm_primary("openai")
        logger.warning(f"🔄 AUTO-SWITCH: Gemini inalcanzable → OpenAI ({FALLBACK_MODEL}) es ahora primario")
        logger.warning(f"   Gemini queda como fallback por si se recupera")
    elif _gemini_smoke_ok:
        logger.info(f"✅ Gemini confirmado como primario")

    # Inyectar estado en app para acceso desde endpoints
    app.state.bot = bot_state

    yield

    # D) Limpieza
    logger.info("🛑 Deteniendo aplicación...")
    if bot_state.store:
        await bot_state.store.close()
    if bot_state.http_client:
        await bot_state.http_client.aclose()
    logger.info("👋 Recursos liberados.")


app = FastAPI(lifespan=lifespan)


# === 4. UTILIDADES ===
def _clean_phone_or_jid(value: str) -> str:
    if not value:
        return ""
    return "".join([c for c in str(value) if c.isdigit()])


def _extract_user_message(msg_obj: Dict[str, Any]) -> Tuple[str, bool, bool]:
    """
    Extrae el texto del mensaje de Evolution.
    Retorna (texto, is_audio, is_image).
    """
    if not isinstance(msg_obj, dict):
        return "", False, False

    # 1. Mensaje de texto normal
    if "conversation" in msg_obj:
        return msg_obj.get("conversation") or "", False, False

    # 2. Mensaje de texto extendido (reply, link preview, etc)
    if "extendedTextMessage" in msg_obj:
        ext = msg_obj.get("extendedTextMessage") or {}
        text = ext.get("text") or ""

        # Extraer metadata de link preview si existe
        preview_parts: list[str] = []
        for field in ("title", "description", "canonicalUrl", "matchedText"):
            val = (ext.get(field) or "").strip()
            if val and val != text:
                preview_parts.append(val)
        if preview_parts:
            preview_ctx = " | ".join(preview_parts)
            text = f"{text}\n[Link preview: {preview_ctx}]" if text else f"[Link preview: {preview_ctx}]"
            logger.info(f"🔗 Link preview extraído: {preview_ctx[:120]}")

        return text, False, False

    # 3. Imagen (con o sin caption) → marcar como imagen para análisis con Vision
    if "imageMessage" in msg_obj:
        img = msg_obj.get("imageMessage") or {}
        caption = (img.get("caption") or "").strip()
        return caption, False, True

    # 4. AUDIO/NOTA DE VOZ (múltiples formatos de Evolution API)
    if "audioMessage" in msg_obj or "pttMessage" in msg_obj:
        return "", True, False

    # 5. Fallback: revisar messageType (algunas versiones de Evolution API)
    #    También verifica si hay mimetype de audio en cualquier sub-mensaje
    for k, v in msg_obj.items():
        if isinstance(v, dict):
            mimetype = v.get("mimetype", "")
            if "audio" in mimetype or "ogg" in mimetype:
                logger.info(f"🎤 Audio detectado vía mimetype en key '{k}': {mimetype}")
                return "", True, False

    # Log de keys no reconocidas para diagnóstico
    known_keys = {"conversation", "extendedTextMessage", "imageMessage", "audioMessage",
                  "pttMessage", "messageContextInfo", "senderKeyDistributionMessage"}
    unknown = set(msg_obj.keys()) - known_keys
    if unknown:
        logger.info(f"📋 Keys de mensaje no procesadas: {unknown}")

    return "", False, False


def _extract_referral_data(data: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """
    Extrae datos de referral/atribución de anuncios de Facebook (CTWA).

    Soporta dos formatos:
    1. Baileys (no oficial): contextInfo con conversionSource, entryPointConversionSource, etc.
    2. Cloud API (oficial): objeto referral con source_url, source_id, ctwa_clid, etc.

    Retorna dict con campos normalizados o None si no hay datos de referral.
    """
    if not isinstance(data, dict):
        return None

    msg_obj = data.get("message", {}) or {}
    referral: Optional[Dict[str, str]] = None

    # --- Cloud API: referral object at message level or data level ---
    ref_obj = data.get("referral") or msg_obj.get("referral")
    if isinstance(ref_obj, dict) and ref_obj:
        referral = {
            "source_type": ref_obj.get("source_type", ""),       # "ad" or "post"
            "source_id": ref_obj.get("source_id", ""),            # Ad ID / Post ID
            "source_url": ref_obj.get("source_url", ""),          # Facebook URL
            "headline": ref_obj.get("headline", ""),              # Ad title
            "body": ref_obj.get("body", ""),                      # Ad description
            "ctwa_clid": ref_obj.get("ctwa_clid", ""),            # Click-to-WhatsApp Click ID
            "media_type": ref_obj.get("media_type", ""),          # "image" or "video"
        }
        # Clean empty values
        referral = {k: v for k, v in referral.items() if v}
        if referral:
            referral["detection_method"] = "cloud_api_referral"
            logger.info(f"📢 REFERRAL (Cloud API): type={referral.get('source_type')} id={referral.get('source_id')} ctwa={referral.get('ctwa_clid', 'N/A')}")
            return referral

    # --- Baileys: contextInfo with conversion fields ---
    # contextInfo can be at the webhook event level (data["contextInfo"]) or inside
    # the message object (message["contextInfo"] / message["messageContextInfo"]).
    # Evolution API (Baileys) places it at the event level alongside "message".
    ctx_info = data.get("contextInfo") or msg_obj.get("messageContextInfo") or msg_obj.get("contextInfo") or {}
    if not isinstance(ctx_info, dict):
        ctx_info = {}

    # Also check inside extendedTextMessage.contextInfo
    if not ctx_info.get("conversionSource"):
        ext_msg = msg_obj.get("extendedTextMessage", {}) or {}
        ctx_info_alt = ext_msg.get("contextInfo", {}) or {}
        if isinstance(ctx_info_alt, dict) and ctx_info_alt.get("conversionSource"):
            ctx_info = ctx_info_alt

    conversion_source = (ctx_info.get("conversionSource") or "").strip()
    entry_point = (ctx_info.get("entryPointConversionSource") or "").strip()
    entry_app = (ctx_info.get("entryPointConversionApp") or "").strip()

    if conversion_source or entry_point:
        # Determine source_type: check entry_point first, then conversionSource
        combined = f"{entry_point} {conversion_source}".lower()
        if "ad" in combined:
            detected_type = "ad"
        elif "post" in combined:
            detected_type = "post"
        elif entry_point:
            detected_type = "unknown"
        else:
            detected_type = "unknown"

        referral = {
            "source_type": detected_type,
            "conversion_source": conversion_source,                  # e.g. "FB_Ads"
            "entry_point": entry_point,                              # e.g. "ctwa_ad"
            "entry_app": entry_app,                                  # e.g. "facebook", "instagram"
            "detection_method": "baileys_context_info",
        }

        # Decode conversionData byte array → CTWA click ID string
        conv_data_raw = ctx_info.get("conversionData")
        if isinstance(conv_data_raw, dict):
            try:
                max_idx = max(int(k) for k in conv_data_raw.keys())
                ctwa_clid = "".join(chr(int(conv_data_raw[str(i)])) for i in range(max_idx + 1))
                if ctwa_clid:
                    referral["ctwa_clid"] = ctwa_clid
            except (ValueError, KeyError, TypeError):
                pass
        elif isinstance(conv_data_raw, (bytes, bytearray)):
            ctwa_clid = conv_data_raw.decode("utf-8", errors="ignore")
            if ctwa_clid:
                referral["ctwa_clid"] = ctwa_clid

        # Extract additional Baileys fields if present
        for field in ("conversionDelaySeconds", "ctwaSignals", "ctwaPayload", "externalAdReply"):
            val = ctx_info.get(field)
            if val and val != {}:
                referral[field] = str(val) if not isinstance(val, str) else val

        referral = {k: v for k, v in referral.items() if v}
        if referral:
            logger.info(f"📢 REFERRAL (Baileys): source={conversion_source} entry={entry_point} app={entry_app} type={detected_type} ctwa={referral.get('ctwa_clid', 'N/A')[:30]}")
            return referral

    return None


def _build_referral_label(referral: Dict[str, str]) -> str:
    """
    Genera un label legible para el origen del lead.
    Ej: 'Facebook Ad', 'Facebook Post', 'Instagram Ad', etc.
    """
    if not referral:
        return "Directo"

    app = (referral.get("entry_app") or "").capitalize() or "Facebook"
    source_type = referral.get("source_type", "unknown")

    if source_type == "ad":
        return f"{app} Ad"
    elif source_type == "post":
        return f"{app} Post"
    else:
        conversion = referral.get("conversion_source", "")
        if "FB_Ads" in conversion or "ad" in conversion.lower():
            return f"{app} Ad"
        return f"{app}"


async def _ensure_inventory_loaded(bot_state: GlobalState) -> None:
    """
    Compatibilidad con distintas versiones de InventoryService.
    """
    inv = bot_state.inventory
    if not inv:
        return
    try:
        if hasattr(inv, "ensure_loaded"):
            await inv.ensure_loaded()
        else:
            await inv.load(force=False)
    except Exception as e:
        logger.error(f"⚠️ No se pudo refrescar inventario: {e}")


def _safe_log_payload(prefix: str, obj: Any) -> None:
    """
    Log controlado CON SANITIZACIÓN.
    """
    if not settings.LOG_WEBHOOK_PAYLOAD:
        return
    try:
        raw = json.dumps(obj, ensure_ascii=False)
        
        # 🔒 SANITIZAR información sensible
        raw = raw.replace(settings.EVOLUTION_API_KEY, "***REDACTED***")
        raw = re.sub(r'"apikey":\s*"[^"]*"', '"apikey": "***"', raw)
        raw = re.sub(r'"password":\s*"[^"]*"', '"password": "***"', raw)
        raw = re.sub(r'"token":\s*"[^"]*"', '"token": "***"', raw)
        
        if len(raw) > settings.LOG_WEBHOOK_PAYLOAD_MAX_CHARS:
            raw = raw[: settings.LOG_WEBHOOK_PAYLOAD_MAX_CHARS] + " ...[TRUNCATED]"
        logger.info(f"{prefix}{raw}")
    except Exception as e:
        logger.warning(f"⚠️ No se pudo loggear payload: {e}")


async def _evo_post(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    """POST a Evolution API con retry automático en 429 (rate limit)."""
    _MAX_RETRIES = 3
    for _attempt in range(_MAX_RETRIES):
        response = await client.post(url, **kwargs)
        if response.status_code == 429 and _attempt < _MAX_RETRIES - 1:
            retry_after = response.headers.get("retry-after")
            backoff = int(retry_after) if retry_after and retry_after.isdigit() else 2 ** (_attempt + 1)
            logger.warning(f"⚠️ Evolution 429 retry {_attempt + 1}/{_MAX_RETRIES} tras {backoff}s")
            await asyncio.sleep(backoff)
            continue
        return response
    return response


# === 5. DETECCIÓN DE MENSAJES HUMANOS ===
def _is_automated_greeting(text: str) -> bool:
    """
    Detecta mensajes automáticos de WhatsApp Business o sistemas externos (n8n, etc).
    Estos mensajes NO deben silenciar al bot.
    """
    if not text:
        return False

    text_lower = text.lower()

    # Patrones de mensajes de bienvenida automáticos
    automated_patterns = [
        # WhatsApp Business greeting messages
        ("bienvenido" in text_lower and "wa.me" in text_lower),
        ("catálogo" in text_lower and "wa.me" in text_lower),
        ("catalogo" in text_lower and "wa.me" in text_lower),
        # Links de catálogo de WhatsApp
        "wa.me/c/" in text_lower,
        # Mensajes de ausencia típicos
        ("no estamos disponibles" in text_lower),
        ("fuera de horario" in text_lower),
        ("te contactaremos" in text_lower and "pronto" in text_lower),
        # Mensajes de bienvenida genéricos sin contexto
        (text_lower.startswith("hola") and "bienvenido" in text_lower and len(text) < 200),
    ]

    if any(automated_patterns):
        logger.info(f"🤖 Mensaje automático detectado (NO silencia): '{text[:80]}...'")
        return True

    return False


def _is_bot_message(bot_state: GlobalState, remote_jid: str, msg_id: str, msg_text: str) -> bool:
    """
    Verifica si un mensaje saliente fue enviado por el bot (multicapa).
    """
    # CAPA 1: Verificar ID del mensaje
    if msg_id and msg_id in bot_state.bot_sent_message_ids:
        logger.debug(f"✓ Mensaje ID {msg_id[:20]}... es del bot")
        return True
    
    # CAPA 2: Verificar texto exacto reciente
    if remote_jid in bot_state.bot_sent_texts:
        recent_texts = bot_state.bot_sent_texts[remote_jid]
        if msg_text in recent_texts:
            logger.debug(f"✓ Texto coincide con cache del bot")
            return True
    
    # CAPA 3: Verificar timestamp (ventana temporal)
    last_bot_time = bot_state.last_bot_message_time.get(remote_jid, 0)
    time_diff = time.time() - last_bot_time
    
    if time_diff < settings.HUMAN_DETECTION_WINDOW_SECONDS:
        logger.debug(f"✓ Dentro de ventana temporal ({time_diff:.1f}s)")
        return True
    
    logger.debug(f"✗ NO es del bot (time_diff={time_diff:.1f}s)")
    return False


# === 6. DELAY HUMANO ALEATORIO ===
async def human_typing_delay():
    """Simula el tiempo que un humano tarda en escribir."""
    delay = random.uniform(5.0, 8.0)
    logger.info(f"⏳ Esperando {delay:.1f}s (delay humano)...")
    await asyncio.sleep(delay)


# === 6.5 PROCESAMIENTO DE MENSAJES ACUMULADOS ===
async def _process_accumulated_messages(bot_state: GlobalState, remote_jid: str):
    """
    Procesa todos los mensajes acumulados de un usuario como uno solo.
    Se ejecuta después de MESSAGE_ACCUMULATION_SECONDS sin nuevos mensajes.

    Usa un lock per-JID para evitar procesamiento concurrente del mismo usuario.
    Sin esto, si llegan mensajes rápidos, dos lotes pueden correr en paralelo,
    ambos leer contexto stale de Supabase, y el segundo sobreescribir el contexto
    correcto del primero (causando pérdida de modelo de interés, etc).
    """
    # Obtener o crear lock para este JID
    if remote_jid not in bot_state.processing_locks:
        bot_state.processing_locks[remote_jid] = asyncio.Lock()
    lock = bot_state.processing_locks[remote_jid]

    async with lock:
        # === DRAIN LOOP: Procesa todos los mensajes pendientes, incluyendo
        # los que llegan mientras el bot está pensando/respondiendo.
        # Sin esto, mensajes que llegan durante el procesamiento crean un
        # batch separado que no tiene contexto de la respuesta anterior,
        # causando respuestas duplicadas o preguntas ya respondidas. ===
        _drain_iteration = 0
        _MAX_DRAIN_ITERATIONS = 3  # Máximo de ciclos para evitar loops infinitos

        while _drain_iteration < _MAX_DRAIN_ITERATIONS:
            # Obtener y limpiar mensajes pendientes (DENTRO del lock)
            messages = bot_state.pending_messages.pop(remote_jid, [])
            bot_state.pending_message_tasks.pop(remote_jid, None)

            if not messages:
                if _drain_iteration == 0:
                    return  # Nada que procesar
                break  # No hay más mensajes nuevos, terminamos el drain

            _drain_iteration += 1

            # Combinar mensajes en uno solo
            if len(messages) == 1:
                combined_message = messages[0]
            else:
                combined_message = " | ".join(messages)
                logger.info(f"📦 Mensajes acumulados ({len(messages)}): '{combined_message[:100]}...'")

            # === Verificar silenciamiento ===
            if remote_jid in bot_state.silenced_users:
                silence_value = bot_state.silenced_users[remote_jid]
                if isinstance(silence_value, (int, float)):
                    if time.time() < silence_value:
                        mins_left = int((silence_value - time.time()) / 60)
                        logger.info(f"🤐 Bot silenciado en {remote_jid} ({mins_left} min restantes)")
                        return
                    else:
                        del bot_state.silenced_users[remote_jid]
                        logger.info(f"✅ Bot reactivado automáticamente en {remote_jid}")
                elif silence_value is True:
                    logger.info(f"🤐 Bot silenciado permanentemente en {remote_jid}")
                    return

            # === Comandos especiales ===
            if combined_message.lower() == "/silencio":
                bot_state.silenced_users[remote_jid] = True
                await send_evolution_message(bot_state, remote_jid, "Bot desactivado. Un asesor humano te atenderá en breve.")
                if settings.OWNER_PHONE:
                    clean_client = remote_jid.split("@")[0]
                    alerta = f"*HANDOFF ACTIVADO*\n\nEl chat con wa.me/{clean_client} ha sido pausado."
                    await send_evolution_message(bot_state, settings.OWNER_PHONE, alerta)
                return

            if combined_message.lower() == "/activar":
                bot_state.silenced_users.pop(remote_jid, None)
                await send_evolution_message(bot_state, remote_jid, "Bot activado de nuevo. ¿En qué te ayudo?")
                return

            # === Refrescar inventario ===
            await _ensure_inventory_loaded(bot_state)

            store = bot_state.store
            if not store:
                logger.error("❌ MemoryStore no inicializado.")
                return

            session = await store.get(remote_jid) or {"state": "start", "context": {}}
            state = session.get("state", "start")
            context = session.get("context", {}) or {}

            # === REFERRAL: Persistir datos de referral en contexto de sesión ===
            # Solo se guarda una vez (primer mensaje con datos de referral)
            if not context.get("referral_source"):
                pending_ref = bot_state.pending_referrals.pop(remote_jid, None)
                if pending_ref:
                    context["referral_source"] = _build_referral_label(pending_ref)
                    context["referral_data"] = pending_ref
                    logger.info(f"📢 Referral guardado en contexto: {context['referral_source']}")
            else:
                # Clean up pending referral if already persisted
                bot_state.pending_referrals.pop(remote_jid, None)

            # === TRACKING ID: Persistir datos de tracking en contexto de sesión ===
            # Si llega un nuevo tracking ID, reemplaza al anterior (permite testing
            # desde el mismo número y clientes que regresan por otro anuncio).
            pending_track = bot_state.pending_tracking_ids.pop(remote_jid, None)
            if pending_track:
                new_tid = pending_track["tracking_id"]
                old_tid = context.get("tracking_id")
                if old_tid != new_tid:
                    context["tracking_id"] = new_tid
                    context["tracking_data"] = pending_track
                    # Update vehicle interest from new tracking code
                    context["last_interest"] = pending_track["vehicle_label"]
                    logger.info(f"🏷️ Auto-interés desde tracking: {pending_track['vehicle_label']}")
                    # Update referral source
                    context["referral_source"] = f"Ad Tracking: {new_tid}"
                    logger.info(f"🏷️ Tracking ID {'actualizado' if old_tid else 'guardado'} en contexto: {new_tid} → {pending_track['vehicle_label']}")

            # === Auto-populate phone from WhatsApp JID ===
            # No need to ask the client for their phone — we already have it
            if not context.get("user_phone"):
                _jid_phone = remote_jid.split("@")[0] if "@" in remote_jid else ""
                if _jid_phone:
                    context["user_phone"] = _jid_phone

            # === Strip tracking ID from message before GPT ===
            # Prevents GPT from echoing the code in its response
            if context.get("tracking_id"):
                combined_message = strip_tracking_id(combined_message)

            # Delay humano
            await human_typing_delay()

            # === Procesar con IA ===
            try:
                result = await handle_message(combined_message, bot_state.inventory, state, context, campaign_service=bot_state.campaigns)
            except Exception as e:
                logger.error(f"❌ Error IA: {e}")
                result = {
                    "reply": "Dame un momento...",
                    "new_state": state,
                    "context": context,
                    "media_urls": [],
                    "lead_info": None
                }

            reply_text = (result.get("reply") or "").strip()
            media_urls = result.get("media_urls") or []
            lead_info = result.get("lead_info")
            pdf_info = result.get("pdf_info")
            location_link = result.get("location_link")

            # Guardar estado
            try:
                await store.upsert(
                    remote_jid,
                    str(result.get("new_state", state)),
                    dict(result.get("context", context)),
                )
            except Exception as e:
                logger.error(f"⚠️ Error guardando memoria: {e}")

            # === DRAIN CHECK: Antes de enviar la respuesta, verificar si
            # llegaron nuevos mensajes mientras procesábamos. Si sí, NO
            # enviar esta respuesta (está basada en contexto incompleto)
            # y procesar todo junto en la siguiente iteración. ===
            new_pending = bot_state.pending_messages.get(remote_jid, [])
            if new_pending and _drain_iteration < _MAX_DRAIN_ITERATIONS:
                logger.info(f"🔄 DRAIN: {len(new_pending)} mensajes nuevos llegaron durante procesamiento, re-procesando junto con contexto actualizado")
                # Re-read state from what we just saved (has updated context)
                # The new messages will be picked up in the next iteration
                continue

            # Verificar si hay que enviar un PDF
            if pdf_info:
                logger.info(f"📄 PDF info recibido: {pdf_info}")
                if pdf_info.get("pdf_url"):
                    # Enviar texto + PDF
                    logger.info(f"📤 Enviando PDF: {pdf_info.get('filename')} -> {remote_jid}")
                    await send_evolution_document(
                        bot_state,
                        remote_jid,
                        reply_text,
                        pdf_info.get("pdf_url"),
                        pdf_info.get("filename", "documento.pdf")
                    )
                else:
                    # PDF detectado pero no disponible - enviar solo texto
                    logger.info(f"📄 PDF detectado pero no disponible: {pdf_info}")
                    await send_evolution_message(bot_state, remote_jid, reply_text, media_urls)
            else:
                # Enviar respuesta normal (texto + fotos si las hay)
                await send_evolution_message(bot_state, remote_jid, reply_text, media_urls)

            # Send location link as a follow-up message if available
            if location_link:
                logger.info(f"📍 Enviando link de ubicación: {location_link}")
                await send_evolution_message(bot_state, remote_jid, location_link)

            # === FUNNEL TRACKING (V2) ===
            funnel_stage = result.get("funnel_stage", "1er Contacto")
            funnel_data = result.get("funnel_data", {})
            is_disinterest = result.get("is_disinterest", False)
            previous_stage = context.get("funnel_stage", "")

            # V2: Update Monday on any stage change, including 1er Contacto and new stages
            v2_stages = ("1er Contacto", "Intención", "Cotización", "Cita Programada", "Sin Interes")

            # Also update Monday when customer name is newly detected (even without stage change)
            previous_name = (context.get("user_name") or "").strip()
            current_name = (funnel_data.get("nombre") or "").strip()
            name_just_detected = bool(current_name and not previous_name)

            should_update_monday = (
                (funnel_stage in v2_stages and funnel_stage != previous_stage) or
                (name_just_detected and funnel_stage in v2_stages)
            )

            if should_update_monday:
                try:
                    # Use different dedupe key when it's a name-only update vs stage change
                    is_stage_change = (funnel_stage != previous_stage)
                    funnel_key = f"{remote_jid}|{funnel_stage}" if is_stage_change else f"{remote_jid}|name|{current_name}"
                    if funnel_key not in bot_state.processed_lead_ids:
                        bot_state.processed_lead_ids.add(funnel_key)

                        # Get referral source from context (persisted from first message)
                        result_context = result.get("context", context) or {}
                        referral_source = result_context.get("referral_source") or context.get("referral_source") or ""
                        referral_detail = result_context.get("referral_data") or context.get("referral_data") or {}

                        # Get tracking ID from context
                        tracking_id = result_context.get("tracking_id") or context.get("tracking_id") or ""
                        tracking_data = result_context.get("tracking_data") or context.get("tracking_data") or {}
                        if tracking_id:
                            logger.info(f"🏷️ MONDAY lead_data incluye tracking_id='{tracking_id}'")
                        else:
                            logger.info(f"🏷️ MONDAY lead_data SIN tracking_id (result_ctx={bool(result_context.get('tracking_id'))}, ctx={bool(context.get('tracking_id'))})")

                        lead_data = {
                            "telefono": remote_jid.split("@")[0],
                            "external_id": f"accumulated_{int(time.time())}",
                            "nombre": funnel_data.get("nombre") or "Lead sin nombre",
                            "interes": funnel_data.get("interes") or "",
                            "cita": funnel_data.get("cita"),
                            "pago": funnel_data.get("pago"),
                            "referral_source": referral_source,
                            "referral_data": referral_detail,
                            "tracking_id": tracking_id,
                            "tracking_data": tracking_data,
                        }

                        # Build referral note suffix
                        referral_note = ""
                        if referral_source:
                            referral_note = f"\n📢 Origen: {referral_source}"
                            if referral_detail.get("headline"):
                                referral_note += f" - {referral_detail['headline']}"
                            if referral_detail.get("source_id"):
                                referral_note += f" (ID: {referral_detail['source_id']})"

                        # Build tracking ID note suffix
                        tracking_note = ""
                        if tracking_id:
                            vehicle_label = tracking_data.get("vehicle_label", "")
                            tracking_note = f"\n🏷️ Tracking: {tracking_id}"
                            if vehicle_label:
                                tracking_note += f" ({vehicle_label})"

                        stage_notes = {
                            "1er Contacto": f"📩 Primer contacto (turno {funnel_data.get('turn_count', '?')}){referral_note}{tracking_note}",
                            "Intención": f"🎯 Interesado en: {funnel_data.get('interes', 'N/A')}",
                            "Cotización": f"📄 Cotización enviada: {funnel_data.get('interes', 'N/A')}",
                            "Cita Programada": f"✅ Cita programada: {funnel_data.get('cita', 'N/A')}",
                            "Sin Interes": f"🚫 Lead expresó desinterés",
                        }

                        if is_stage_change:
                            note = stage_notes.get(funnel_stage)
                        else:
                            note = f"👤 Nombre detectado: {current_name}"

                        effective_stage = funnel_stage if is_stage_change else None
                        logger.info(f"📊 FUNNEL V2 [{funnel_stage}]: {lead_data.get('telefono')} - nombre={current_name} - {lead_data.get('interes')}")
                        monday_item_id = await monday_service.create_or_update_lead(lead_data, stage=effective_stage, add_note=note)

                        # V3: Connect lead to Anuncio board item if tracking ID exists
                        if monday_item_id and tracking_id:
                            try:
                                anuncio = await monday_service.find_anuncio_by_tracking_id(tracking_id)
                                if anuncio:
                                    await monday_service.connect_lead_to_anuncio(monday_item_id, anuncio["id"])
                            except Exception as e:
                                logger.error(f"⚠️ Error conectando lead a anuncio: {e}")

                except Exception as e:
                    logger.error(f"❌ Error actualizando funnel en Monday: {e}")

            # === SLOT-BASED INCREMENTAL MONDAY SYNC ===
            # When FSM reports slot changes, sync each changed slot to Monday
            # independently. This ensures granular CRM updates even if the
            # conversation doesn't trigger a full stage change.
            slot_changes = result.get("slot_changes") or []
            if slot_changes and monday_service:
                try:
                    phone = remote_jid.split("@")[0]
                    sanitized = monday_service._sanitize_phone(phone)
                    existing_item = await monday_service._find_item_by_phone(sanitized)
                    if existing_item:
                        item_id = int(existing_item["id"])

                        # Map slot changes to Monday column updates
                        col_vals = {}
                        slot_notes = []
                        skipped = []
                        for change in slot_changes:
                            slot_name = change.get("slot", "")
                            new_val = change.get("new", "")
                            old_val = change.get("old", "")
                            if not new_val:
                                skipped.append(f"{slot_name}(empty)")
                                continue

                            if slot_name == "interest" and monday_service.vehicle_col_id:
                                from src.monday_service import resolve_vehicle_to_dropdown
                                vehicle_label = resolve_vehicle_to_dropdown(new_val)
                                if vehicle_label:
                                    col_vals[monday_service.vehicle_col_id] = {"labels": [vehicle_label]}
                                    slot_notes.append(f"Vehículo: {vehicle_label}")
                                else:
                                    skipped.append(f"interest(no_dropdown_match:{new_val})")

                            elif slot_name == "payment" and monday_service.payment_col_id:
                                from src.monday_service import resolve_payment_to_label
                                payment_label = resolve_payment_to_label(new_val)
                                if payment_label != "Por definir":
                                    col_vals[monday_service.payment_col_id] = {"label": payment_label}
                                    slot_notes.append(f"Pago: {payment_label}")
                                else:
                                    skipped.append(f"payment(por_definir)")

                            elif slot_name == "appointment" and monday_service.appointment_col_id:
                                from src.monday_service import resolve_appointment_to_iso
                                appt_iso = resolve_appointment_to_iso(new_val)
                                if appt_iso and appt_iso.get("date"):
                                    col_vals[monday_service.appointment_col_id] = {"date": appt_iso["date"]}
                                    if monday_service.appointment_time_col_id and appt_iso.get("time"):
                                        try:
                                            tp = appt_iso["time"].split(":")
                                            col_vals[monday_service.appointment_time_col_id] = {
                                                "hour": int(tp[0]),
                                                "minute": int(tp[1]) if len(tp) > 1 else 0,
                                            }
                                        except (ValueError, IndexError):
                                            pass
                                    slot_notes.append(f"Cita: {new_val}")
                                else:
                                    skipped.append(f"appointment(no_iso:{new_val})")

                            elif slot_name == "email":
                                # Email column: use email_col_id if configured, otherwise note-only
                                email_col = getattr(monday_service, "email_col_id", None)
                                if email_col:
                                    col_vals[email_col] = {"email": new_val, "text": new_val}
                                slot_notes.append(f"Email: {new_val}")

                            elif slot_name == "city":
                                # City column: use city_col_id if configured, otherwise note-only
                                city_col = getattr(monday_service, "city_col_id", None)
                                if city_col:
                                    col_vals[city_col] = new_val
                                slot_notes.append(f"Ciudad: {new_val}")

                            elif slot_name == "offer_amount":
                                # Offer column: use offer_col_id if configured, otherwise note-only
                                offer_col = getattr(monday_service, "offer_col_id", None)
                                if offer_col:
                                    col_vals[offer_col] = new_val
                                slot_notes.append(f"Propuesta: {new_val}")

                            elif slot_name == "name":
                                # Update item name in Monday
                                try:
                                    mutation = 'mutation ($id: ID!, $name: String!) { change_simple_column_value(item_id: $id, board_id: %s, column_id: "name", value: $name) { id } }' % monday_service.board_id
                                    await monday_service._graphql(mutation, {"id": str(item_id), "name": new_val})
                                    slot_notes.append(f"Nombre: {new_val}")
                                except Exception as name_err:
                                    logger.error(f"⚠️ SLOT_SYNC name update failed: {name_err}")
                                    skipped.append(f"name(graphql_error)")

                            else:
                                skipped.append(f"{slot_name}(unmapped)")

                        # Apply column updates if any
                        if col_vals:
                            try:
                                import json as _json
                                mutation = 'mutation ($id: ID!, $vals: JSON!) { change_multiple_column_values(item_id: $id, board_id: %s, column_values: $vals) { id } }' % monday_service.board_id
                                await monday_service._graphql(mutation, {
                                    "id": str(item_id),
                                    "vals": _json.dumps(col_vals),
                                })
                                logger.info(
                                    f"📊 SLOT_SYNC OK: item={item_id} phone={sanitized} "
                                    f"updated=[{', '.join(slot_notes)}]"
                                    + (f" skipped=[{', '.join(skipped)}]" if skipped else "")
                                )
                            except Exception as col_err:
                                logger.error(
                                    f"❌ SLOT_SYNC column update FAILED: item={item_id} "
                                    f"phone={sanitized} error={col_err}"
                                )
                        elif slot_notes:
                            logger.info(
                                f"📊 SLOT_SYNC note-only: item={item_id} phone={sanitized} "
                                f"notes=[{', '.join(slot_notes)}]"
                                + (f" skipped=[{', '.join(skipped)}]" if skipped else "")
                            )

                        # Add a note with slot updates
                        if slot_notes:
                            try:
                                note_text = "📊 Datos actualizados:\n" + "\n".join(f"• {s}" for s in slot_notes)
                                await monday_service.add_note_to_item(item_id, note_text)
                            except Exception as note_err:
                                logger.error(f"⚠️ SLOT_SYNC note failed: item={item_id} error={note_err}")

                        if skipped and not slot_notes:
                            logger.info(
                                f"📊 SLOT_SYNC all skipped: item={item_id} phone={sanitized} "
                                f"skipped=[{', '.join(skipped)}]"
                            )

                    else:
                        # Item not found — log explicitly so we know sync was attempted but had no target
                        logger.warning(
                            f"📊 SLOT_SYNC_SKIPPED: item_not_found | phone={sanitized} | "
                            f"changes={[c.get('slot') for c in slot_changes]} | "
                            f"These slot changes were NOT persisted to Monday."
                        )

                except Exception as e:
                    logger.error(
                        f"❌ SLOT_SYNC error: phone={remote_jid.split('@')[0]} "
                        f"changes={[c.get('slot') for c in slot_changes]} error={e}"
                    )

            # === CAMPAIGN DATA: Guardar datos de campaña como nota en Monday ===
            campaign_data = result.get("campaign_data")
            if campaign_data and isinstance(campaign_data, dict) and campaign_data.get("resumen"):
                try:
                    phone = remote_jid.split("@")[0]
                    sanitized = monday_service._sanitize_phone(phone)
                    existing_item = await monday_service._find_item_by_phone(sanitized)
                    if existing_item:
                        await monday_service.add_note_to_item(
                            int(existing_item["id"]),
                            f"📋 Datos de campaña:\n{campaign_data['resumen']}"
                        )
                        logger.info(f"📋 Campaign data guardado en Monday para {phone}")
                    else:
                        logger.warning(f"📋 Campaign data recibido pero no hay item en Monday para {phone}")
                except Exception as e:
                    logger.error(f"⚠️ Error guardando campaign data en Monday: {e}")

            # Lead calificado - notificar
            # Get referral source and tracking ID for alerts
            result_ctx = result.get("context", context) or {}
            alert_referral = result_ctx.get("referral_source") or context.get("referral_source") or ""
            alert_tracking = result_ctx.get("tracking_id") or context.get("tracking_id") or ""

            if lead_info:
                try:
                    lead_key = f"{remote_jid}|lead"
                    if lead_key not in bot_state.processed_lead_ids:
                        bot_state.processed_lead_ids.add(lead_key)
                        await notify_owner(bot_state, remote_jid, combined_message, reply_text, is_lead=True, referral_source=alert_referral, tracking_id=alert_tracking)
                except Exception as e:
                    logger.error(f"❌ Error procesando LEAD calificado: {e}")
            else:
                await notify_owner(bot_state, remote_jid, combined_message, reply_text, is_lead=False, referral_source=alert_referral, tracking_id=alert_tracking)


async def _schedule_accumulated_processing(bot_state: GlobalState, remote_jid: str):
    """
    Espera MESSAGE_ACCUMULATION_SECONDS y luego procesa los mensajes acumulados.
    Si llegan más mensajes, esta tarea se cancela y se crea una nueva.
    """
    try:
        await asyncio.sleep(settings.MESSAGE_ACCUMULATION_SECONDS)
        await _process_accumulated_messages(bot_state, remote_jid)
    except asyncio.CancelledError:
        # Se canceló porque llegó otro mensaje - normal
        pass
    except Exception as e:
        logger.error(f"❌ Error en procesamiento acumulado: {e}")


# === 7. TRANSCRIPCIÓN DE AUDIO ===
async def _handle_audio_transcription(bot_state: GlobalState, msg_id: str, remote_jid: str) -> str:
    """
    Descarga el audio DESENCRIPTADO desde Evolution API y lo transcribe con Whisper.
    Con retry y mejor diagnóstico de errores.
    """
    if not msg_id or not remote_jid:
        logger.warning("⚠️ msg_id o remote_jid vacío para audio")
        return ""

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as temp_audio:
            temp_path = temp_audio.name

        client = bot_state.http_client
        if not client:
            logger.error("❌ Cliente HTTP no inicializado para audio")
            return ""

        # === PASO 1: Descargar audio de Evolution API (con retry) ===
        media_url = f"/chat/getBase64FromMediaMessage/{quote(settings.EVO_INSTANCE, safe='')}"

        payload = {
            "message": {
                "key": {
                    "remoteJid": remote_jid,
                    "id": msg_id,
                    "fromMe": False
                }
            },
            "convertToMp4": False
        }

        base64_audio = None
        for _audio_attempt in range(3):
            logger.info(f"⬇️ Descargando audio (intento {_audio_attempt + 1}/3)...")
            try:
                response = await _evo_post(client, media_url, json=payload)
            except Exception as e:
                logger.error(f"❌ Error de conexión descargando audio: {e}")
                if _audio_attempt < 2:
                    await asyncio.sleep(2.0)
                continue

            if response.status_code not in [200, 201]:
                logger.error(f"❌ Evolution audio status {response.status_code}: {response.text[:200]}")
                if _audio_attempt < 2:
                    await asyncio.sleep(2.0)
                continue

            try:
                data = response.json()
            except Exception as e:
                logger.error(f"❌ Evolution audio respuesta no JSON: {e}")
                if _audio_attempt < 2:
                    await asyncio.sleep(2.0)
                continue

            # Extraer base64 de diferentes formatos de respuesta
            if isinstance(data, dict):
                base64_audio = data.get("base64") or data.get("media") or data.get("data")
                if not base64_audio:
                    logger.error(f"❌ Respuesta sin base64. Keys: {list(data.keys())}")
            elif isinstance(data, str):
                base64_audio = data
            else:
                logger.error(f"❌ Tipo de respuesta inesperado: {type(data)}")

            if base64_audio:
                # Limpiar data URI prefix si existe
                if "base64," in base64_audio:
                    base64_audio = base64_audio.split("base64,", 1)[1]
                break
            elif _audio_attempt < 2:
                await asyncio.sleep(2.0)

        if not base64_audio:
            logger.error("❌ No se pudo obtener base64 de audio después de 3 intentos")
            return ""

        # Decodificar y guardar
        try:
            audio_bytes = base64.b64decode(base64_audio)
        except Exception as e:
            logger.error(f"❌ Error decodificando base64 audio: {e}")
            return ""

        with open(temp_path, "wb") as f:
            f.write(audio_bytes)

        logger.info(f"✅ Audio descargado: {len(audio_bytes)} bytes")

        # Verificar que el archivo no esté vacío o corrupto
        if len(audio_bytes) < 100:
            logger.error(f"❌ Audio demasiado pequeño ({len(audio_bytes)} bytes), posiblemente corrupto")
            return ""

        # === PASO 2: Transcribir con Whisper (OpenAI) ===
        try:
            from src.conversation_logic import openai_client

            with open(temp_path, "rb") as audio_file:
                transcript = await openai_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    language="es",
                    response_format="text"
                )

            if isinstance(transcript, str):
                texto = transcript.strip()
            else:
                texto = (getattr(transcript, "text", "") or "").strip()

            if texto:
                logger.info(f"🎤 Audio transcrito: '{texto[:150]}...'")
            else:
                logger.warning("⚠️ Transcripción vacía")

            return texto

        except Exception as e:
            logger.error(f"❌ Error en Whisper API: {e}")
            return ""

    except Exception as e:
        logger.error(f"❌ Error general procesando audio: {e}")
        return ""

    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


# === 7.5 ANÁLISIS DE IMÁGENES CON VISION ===
async def _handle_image_analysis(bot_state: GlobalState, msg_id: str, remote_jid: str) -> str:
    """
    Descarga la imagen desde Evolution API y la analiza con OpenAI Vision.
    Retorna una descripción breve del contenido de la imagen.
    """
    if not msg_id or not remote_jid:
        logger.warning("⚠️ msg_id o remote_jid vacío para imagen")
        return ""

    try:
        client = bot_state.http_client
        if not client:
            logger.error("❌ Cliente HTTP no inicializado para imagen")
            return ""

        # === PASO 1: Descargar imagen de Evolution API (con retry) ===
        media_url = f"/chat/getBase64FromMediaMessage/{quote(settings.EVO_INSTANCE, safe='')}"

        payload = {
            "message": {
                "key": {
                    "remoteJid": remote_jid,
                    "id": msg_id,
                    "fromMe": False
                }
            },
            "convertToMp4": False
        }

        base64_image = None
        for _img_attempt in range(3):
            logger.info(f"⬇️ Descargando imagen (intento {_img_attempt + 1}/3)...")
            try:
                response = await _evo_post(client, media_url, json=payload)
            except Exception as e:
                logger.error(f"❌ Error de conexión descargando imagen: {e}")
                if _img_attempt < 2:
                    await asyncio.sleep(2.0)
                continue

            if response.status_code not in [200, 201]:
                logger.error(f"❌ Evolution imagen status {response.status_code}: {response.text[:200]}")
                if _img_attempt < 2:
                    await asyncio.sleep(2.0)
                continue

            try:
                data = response.json()
            except Exception as e:
                logger.error(f"❌ Evolution imagen respuesta no JSON: {e}")
                if _img_attempt < 2:
                    await asyncio.sleep(2.0)
                continue

            # Extraer base64 de diferentes formatos de respuesta
            if isinstance(data, dict):
                base64_image = data.get("base64") or data.get("media") or data.get("data")
                if not base64_image:
                    logger.error(f"❌ Respuesta sin base64. Keys: {list(data.keys())}")
            elif isinstance(data, str):
                base64_image = data
            else:
                logger.error(f"❌ Tipo de respuesta inesperado: {type(data)}")

            if base64_image:
                # Limpiar data URI prefix si existe
                if "base64," in base64_image:
                    base64_image = base64_image.split("base64,", 1)[1]
                break
            elif _img_attempt < 2:
                await asyncio.sleep(2.0)

        if not base64_image:
            logger.error("❌ No se pudo obtener base64 de imagen después de 3 intentos")
            return ""

        logger.info(f"✅ Imagen descargada: ~{len(base64_image) // 1024} KB base64")

        # === PASO 2: Analizar con Gemini Vision ===
        try:
            from src.conversation_logic import client as gemini_client, MODEL_NAME

            vision_prompt = (
                "Describe brevemente esta imagen en español. "
                "Si es un vehículo (camión, pickup, tractocamión, van), menciona marca, modelo y detalles visibles. "
                "Si hay texto promocional o datos (precio, características, especificaciones), inclúyelos. "
                "Si es una captura de pantalla o documento, resume el contenido. "
                "Máximo 3 oraciones."
            )

            vision_response = await gemini_client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": vision_prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{base64_image}"
                                }
                            }
                        ]
                    }
                ],
                max_tokens=200,
            )

            description = (vision_response.choices[0].message.content or "").strip()
            if description:
                logger.info(f"🖼️ Imagen analizada: '{description[:150]}...'")
            else:
                logger.warning("⚠️ Descripción de imagen vacía")

            return description

        except Exception as e:
            logger.error(f"❌ Error en OpenAI Vision API: {e}")
            return ""

    except Exception as e:
        logger.error(f"❌ Error general analizando imagen: {e}")
        return ""


# === 8. ENVÍO DE MENSAJES (CON RASTREO) ===
async def send_evolution_message(bot_state: GlobalState, number_or_jid: str, text: str, media_urls: Optional[List[str]] = None):
    media_urls = media_urls or []
    text = (text or "").strip()

    if not text and not media_urls:
        return

    clean_number = _clean_phone_or_jid(number_or_jid)
    if not clean_number:
        logger.error(f"❌ No se pudo limpiar número/jid: {number_or_jid}")
        return

    client = bot_state.http_client
    if not client:
        logger.error("❌ Cliente HTTP no inicializado (lifespan).")
        return

    try:
        if media_urls:
            total_fotos = len(media_urls)
            fotos_enviadas = 0
            for i, media_url in enumerate(media_urls):
                url = f"/message/sendMedia/{quote(settings.EVO_INSTANCE, safe='')}"

                caption_part = text if (i == total_fotos - 1) else ""

                payload = {
                    "number": clean_number,
                    "mediatype": "image",
                    "mimetype": "image/jpeg",
                    "caption": caption_part,
                    "media": media_url,
                }

                if i > 0:
                    await asyncio.sleep(0.5)

                # Retry individual photo sends (up to 2 retries)
                sent = False
                for _photo_attempt in range(3):
                    response = await _evo_post(client, url, json=payload)
                    if response.status_code < 400:
                        sent = True
                        fotos_enviadas += 1
                        logger.info(f"✅ Enviada foto {i+1}/{total_fotos} a {clean_number}")
                        try:
                            resp_data = response.json()
                            msg_id = resp_data.get("key", {}).get("id")
                            if msg_id:
                                bot_state.bot_sent_message_ids.add(msg_id)
                        except Exception:
                            pass
                        break
                    else:
                        if _photo_attempt < 2:
                            logger.warning(f"⚠️ Retry foto {i+1} intento {_photo_attempt+1}: {response.status_code}")
                            await asyncio.sleep(1.5)
                        else:
                            logger.error(f"❌ Error foto {i+1} después de 3 intentos: {response.text[:200]}")
                            logger.error(f"❌ URL que falló: {media_url[:200]}")

            # If no photos were sent but we had text with photo promise, send text anyway
            if fotos_enviadas == 0 and text:
                logger.warning(f"⚠️ Ninguna foto enviada de {total_fotos} intentadas, enviando solo texto")
                # Replace photo-promise text with an apology so the client isn't confused
                fallback_text = "Estoy teniendo problemas para enviar las fotos. Un asesor te las comparte en breve."
                url_text = f"/message/sendText/{quote(settings.EVO_INSTANCE, safe='')}"
                payload_text = {"number": clean_number, "text": fallback_text}
                await _evo_post(client, url_text, json=payload_text)

        else:
            url = f"/message/sendText/{quote(settings.EVO_INSTANCE, safe='')}"
            payload = {"number": clean_number, "text": text}
            response = await _evo_post(client, url, json=payload)

            if response.status_code >= 400:
                logger.error(f"⚠️ Error Evolution API ({response.status_code}): {response.text}")
            else:
                logger.info(f"✅ Enviado a {clean_number} (TEXT)")
                
                jid = f"{clean_number}@s.whatsapp.net"
                
                try:
                    resp_data = response.json()
                    msg_id = resp_data.get("key", {}).get("id")
                    if msg_id:
                        bot_state.bot_sent_message_ids.add(msg_id)
                        logger.debug(f"📤 Rastreando msg_id: {msg_id[:20]}...")
                except Exception:
                    pass

                if jid not in bot_state.bot_sent_texts:
                    bot_state.bot_sent_texts[jid] = deque(maxlen=10)
                bot_state.bot_sent_texts[jid].append(text)
                
                bot_state.last_bot_message_time[jid] = time.time()

    except httpx.RequestError as e:
        logger.error(f"❌ Error de conexión: {e}")
    except Exception as e:
        logger.error(f"❌ Error inesperado: {e}")


async def send_evolution_document(bot_state: GlobalState, number_or_jid: str, text: str, pdf_url: str, filename: str):
    """
    Envía primero un mensaje de texto y luego un PDF como documento.
    El texto se envía antes del PDF para dar contexto al usuario.
    """
    clean_number = _clean_phone_or_jid(number_or_jid)
    if not clean_number:
        logger.error(f"❌ No se pudo limpiar número/jid: {number_or_jid}")
        return

    client = bot_state.http_client
    if not client:
        logger.error("❌ Cliente HTTP no inicializado (lifespan).")
        return

    try:
        # 1. Enviar texto primero
        if text:
            url_text = f"/message/sendText/{quote(settings.EVO_INSTANCE, safe='')}"
            payload_text = {"number": clean_number, "text": text}
            response = await _evo_post(client, url_text, json=payload_text)

            if response.status_code >= 400:
                logger.error(f"⚠️ Error enviando texto antes de PDF: {response.text}")
            else:
                logger.info(f"✅ Texto enviado antes de PDF a {clean_number}")
                try:
                    resp_data = response.json()
                    msg_id = resp_data.get("key", {}).get("id")
                    if msg_id:
                        bot_state.bot_sent_message_ids.add(msg_id)
                except Exception:
                    pass

            # Pequeña espera para que WhatsApp ordene los mensajes
            await asyncio.sleep(1.2)

        # 2. Descargar PDF y convertir a base64 para enviarlo como archivo adjunto
        try:
            pdf_response = await client.get(pdf_url, follow_redirects=True, timeout=30.0)
            pdf_response.raise_for_status()
            pdf_b64 = base64.b64encode(pdf_response.content).decode("utf-8")
            logger.info(f"📥 PDF descargado: {filename} ({len(pdf_response.content)} bytes)")
        except Exception as _dl_err:
            logger.error(f"❌ No se pudo descargar PDF {pdf_url}: {_dl_err}")
            await _evo_post(client, f"/message/sendText/{quote(settings.EVO_INSTANCE, safe='')}", json={"number": clean_number, "text": "No pude enviar el PDF en este momento. Un asesor te lo comparte."})
            return

        url_media = f"/message/sendMedia/{quote(settings.EVO_INSTANCE, safe='')}"
        payload_pdf = {
            "number": clean_number,
            "mediatype": "document",
            "mimetype": "application/pdf",
            "media": pdf_b64,
            "fileName": filename,
            "caption": ""
        }

        pdf_sent = False
        for _pdf_attempt in range(3):
            response = await _evo_post(client, url_media, json=payload_pdf)
            if response.status_code < 400:
                pdf_sent = True
                logger.info(f"✅ PDF enviado a {clean_number}: {filename}")
                try:
                    resp_data = response.json()
                    msg_id = resp_data.get("key", {}).get("id")
                    if msg_id:
                        bot_state.bot_sent_message_ids.add(msg_id)
                except Exception:
                    pass
                break
            else:
                if _pdf_attempt < 2:
                    logger.warning(f"⚠️ Retry PDF intento {_pdf_attempt+1}: {response.status_code}")
                    await asyncio.sleep(2.0)
                else:
                    logger.error(f"❌ Error enviando PDF después de 3 intentos: {response.text[:200]}")

        if not pdf_sent:
            # Fallback: inform the user the PDF couldn't be sent
            fallback_text = "No pude enviar el PDF en este momento. Un asesor te lo comparte."
            url_fallback = f"/message/sendText/{quote(settings.EVO_INSTANCE, safe='')}"
            await _evo_post(client, url_fallback, json={"number": clean_number, "text": fallback_text})

    except httpx.RequestError as e:
        logger.error(f"❌ Error de conexión enviando PDF: {e}")
    except Exception as e:
        logger.error(f"❌ Error inesperado enviando PDF: {e}")


# === 9. ALERTAS AL DUEÑO ===
async def notify_owner(bot_state: GlobalState, user_number_or_jid: str, user_message: str, bot_reply: str, is_lead: bool = False, referral_source: str = "", tracking_id: str = ""):
    if not settings.OWNER_PHONE:
        return

    clean_client = _clean_phone_or_jid(user_number_or_jid)

    if is_lead:
        alert_text = (
            "*NUEVO LEAD EN MONDAY*\n\n"
            f"Cliente: wa.me/{clean_client}\n"
            "El bot cerró una cita. Revisa el tablero."
        )
        if referral_source:
            alert_text += f"\nOrigen: {referral_source}"
        if tracking_id:
            alert_text += f"\nTracking: {tracking_id}"
        await send_evolution_message(bot_state, settings.OWNER_PHONE, alert_text)
        return

    keywords = [
        "precio", "cuanto", "cuánto", "interesa", "verlo", "ubicacion", "ubicación",
        "dónde", "donde", "trato", "comprar", "informes", "info"
    ]

    msg_lower = (user_message or "").lower()
    if not any(word in msg_lower for word in keywords):
        return

    alert_text = (
        "*Interés Detectado*\n"
        f"Cliente: wa.me/{clean_client}\n"
        f"Dijo: \"{user_message}\"\n"
        f"Bot: \"{(bot_reply or '')[:60]}...\""
    )
    if referral_source:
        alert_text += f"\nOrigen: {referral_source}"
    if tracking_id:
        alert_text += f"\nTracking: {tracking_id}"
    await send_evolution_message(bot_state, settings.OWNER_PHONE, alert_text)


# === 10. PROCESADOR CENTRAL ===
async def process_single_event(bot_state: GlobalState, data: Dict[str, Any]):
    key = data.get("key", {}) or {}
    remote_jid = (key.get("remoteJid", "") or "").strip()
    from_me = key.get("fromMe", False)
    msg_id = (key.get("id", "") or "").strip()

    if not remote_jid:
        return

    logger.info(f"📩 Evento: msg_id={msg_id[:20]}... from_me={from_me}")

    # Ignorar grupos/broadcast
    if remote_jid.endswith("@g.us") or "broadcast" in remote_jid:
        return

    # Deduplicación por msg_id
    if msg_id and msg_id in bot_state.processed_message_ids:
        logger.debug(f"🔁 Mensaje duplicado ignorado: {msg_id}")
        return

    if msg_id:
        bot_state.processed_message_ids.add(msg_id)

    # === REFERRAL TRACKING (Facebook/Instagram Ads) ===
    # En Baileys, contextInfo.conversionSource llega en mensajes fromMe=true,
    # así que extraemos ANTES del filtro fromMe para no perder datos de atribución.
    if remote_jid not in bot_state.pending_referrals:
        referral_data = _extract_referral_data(data)
        if referral_data:
            bot_state.pending_referrals[remote_jid] = referral_data
            logger.info(f"📢 Referral capturado para {remote_jid}: {_build_referral_label(referral_data)}")

    # === DETECCIÓN DE HANDOFF (MENSAJE SALIENTE) ===
    # Si el mensaje sale del WhatsApp del negocio (from_me=true)
    # y NO fue enviado por el bot → PODRÍA ser un HUMANO ASESOR
    if from_me:
        msg_obj = data.get("message", {}) or {}

        # 0. Ignorar mensajes del sistema de WhatsApp (botInvokeMessage, protocolMessage, etc.)
        #    Estos NO son mensajes de un humano y NO deben activar handoff.
        _SYSTEM_MESSAGE_KEYS = {"botInvokeMessage", "protocolMessage", "reactionMessage",
                                "senderKeyDistributionMessage", "messageContextInfo"}
        msg_content_keys = set(msg_obj.keys()) - {"messageContextInfo"}
        if msg_content_keys and msg_content_keys.issubset(_SYSTEM_MESSAGE_KEYS):
            logger.info(f"✓ Mensaje del sistema WhatsApp ignorado ({msg_content_keys})")
            return

        msg_text, _, _ = _extract_user_message(msg_obj)
        msg_text = msg_text.strip()

        # 1. Verificar si este mensaje fue enviado por el bot
        if _is_bot_message(bot_state, remote_jid, msg_id, msg_text):
            logger.debug(f"✓ Confirmado mensaje del bot, ignorando")
            return

        # 2. Verificar si es un mensaje automático (WhatsApp Business greeting, n8n, etc)
        #    Estos NO deben silenciar al bot
        if _is_automated_greeting(msg_text):
            logger.info(f"✓ Mensaje automático ignorado (bot sigue activo)")
            return

        # 3. Si NO es del bot Y NO es automático → Es un HUMANO → SILENCIAR
        logger.info(f"🤐 HUMANO DETECTADO en {remote_jid} - silenciando bot por {settings.AUTO_REACTIVATE_MINUTES} min")
        bot_state.silenced_users[remote_jid] = time.time() + (settings.AUTO_REACTIVATE_MINUTES * 60)
        return

    # === EXTRACCIÓN DE MENSAJE (TEXTO, AUDIO O IMAGEN) ===
    msg_obj = data.get("message", {}) or {}
    user_message, is_audio, is_image = _extract_user_message(msg_obj)
    user_message = user_message.strip()

    # Si es IMAGEN, analizar con Vision API
    if is_image:
        logger.info(f"🖼️ Imagen detectada, analizando con Vision...")
        image_description = await _handle_image_analysis(bot_state, msg_id, remote_jid)

        if image_description:
            if user_message:
                # Tiene caption + descripción de imagen
                user_message = f"{user_message} [El cliente envió una foto que muestra: {image_description}]"
            else:
                user_message = f"[El cliente envió una foto que muestra: {image_description}]"
            logger.info(f"✅ Imagen analizada, procesando con contexto visual...")
        else:
            # Vision falló, usar caption o fallback
            if not user_message:
                user_message = "(El cliente envió una foto pero no se pudo analizar)"
            logger.warning(f"⚠️ No se pudo analizar imagen, usando fallback")

    # Si NO hay texto y es audio, transcribir
    if not user_message and is_audio:
        logger.info(f"🎤 Audio detectado, procesando...")
        user_message = await _handle_audio_transcription(bot_state, msg_id, remote_jid)

        if not user_message:
            await send_evolution_message(
                bot_state, remote_jid,
                "Tuve un problema escuchando el audio. ¿Me lo puedes escribir o mandar de nuevo?"
            )
            return

        logger.info(f"✅ Transcripción exitosa, procesando como texto...")

    if not user_message:
        return

    # === TRACKING ID DETECTION (V3: Internal ad attribution) ===
    # Detect tracking ID pattern (e.g., TG9-A1) in the first message
    if remote_jid not in bot_state.pending_tracking_ids:
        tracking_data = extract_tracking_id(user_message)
        if tracking_data:
            bot_state.pending_tracking_ids[remote_jid] = tracking_data
            logger.info(f"🏷️ Tracking ID capturado para {remote_jid}: {tracking_data['tracking_id']} → {tracking_data['vehicle_label']}")

    # === ACUMULACIÓN DE MENSAJES RÁPIDOS ===
    # En lugar de procesar inmediatamente, acumulamos y esperamos
    # para ver si el cliente envía más mensajes seguidos

    # Agregar mensaje a la lista pendiente
    if remote_jid not in bot_state.pending_messages:
        bot_state.pending_messages[remote_jid] = []
    bot_state.pending_messages[remote_jid].append(user_message)

    logger.info(f"📥 Mensaje acumulado ({len(bot_state.pending_messages[remote_jid])} pendientes): '{user_message[:50]}...'")

    # Cancelar tarea anterior si existe (reinicia el timer)
    if remote_jid in bot_state.pending_message_tasks:
        old_task = bot_state.pending_message_tasks[remote_jid]
        if not old_task.done():
            old_task.cancel()
            logger.debug(f"⏱️ Timer reiniciado para {remote_jid}")

    # Programar nuevo procesamiento después de MESSAGE_ACCUMULATION_SECONDS
    task = asyncio.create_task(_schedule_accumulated_processing(bot_state, remote_jid))
    bot_state.pending_message_tasks[remote_jid] = task


# === 11. ENDPOINTS ===
@app.get("/")
async def root():
    """Ruta raíz para Render health-check (HEAD / y GET /)."""
    return {"ok": True, "service": "tono-bot"}


@app.get("/health")
async def health(request: Request):
    """Endpoint de salud con métricas del sistema."""
    from src.conversation_logic import LLM_PRIMARY as _current_primary
    bot_state: GlobalState = request.app.state.bot
    return {
        "status": "ok",
        "instance": settings.EVO_INSTANCE,
        "llm_primary": _current_primary,
        "llm_model": LLM_MODEL_NAME,
        "llm_fallback": FALLBACK_MODEL,
        "inventory_count": len(getattr(bot_state.inventory, "items", []) or []),
        "active_campaigns": len(bot_state.campaigns.get_active_campaigns()) if bot_state.campaigns else 0,
        "silenced_chats": len(bot_state.silenced_users),
        "processed_msgs_cache": len(bot_state.processed_message_ids),
        "processed_leads_cache": len(bot_state.processed_lead_ids),
        "bot_messages_tracked": len(bot_state.bot_sent_message_ids),
        "pending_message_queues": len(bot_state.pending_messages),
        "handoff_enabled": bool(settings.TEAM_NUMBERS.strip()),
        "auto_reactivate_minutes": settings.AUTO_REACTIVATE_MINUTES,
        "message_accumulation_seconds": settings.MESSAGE_ACCUMULATION_SECONDS,
    }


@app.get("/campaigns")
async def campaigns_endpoint(request: Request):
    """Muestra campañas activas cargadas desde Google Sheets."""
    bot_state: GlobalState = request.app.state.bot
    if not bot_state.campaigns:
        return {"status": "disabled", "campaigns": [], "message": "CAMPAIGNS_CSV_URL no configurado"}

    active = bot_state.campaigns.get_active_campaigns()
    return {
        "status": "ok",
        "total_loaded": len(bot_state.campaigns.campaigns),
        "active_count": len(active),
        "campaigns": [
            {
                "name": c.name,
                "tracking_id": c.tracking_id or None,
                "keywords": c.keywords or [],
                "instructions_preview": c.instructions[:150] + "..." if len(c.instructions) > 150 else c.instructions,
            }
            for c in active
        ],
    }


async def _background_process_events(bot_state: GlobalState, events: List[Dict[str, Any]]):
    """Procesa eventos en background para ACK inmediato al webhook."""
    for event in events:
        try:
            await process_single_event(bot_state, event)
        except Exception as e:
            logger.error(f"❌ Error procesando evento en background: {e}")


@app.post("/webhook")
async def evolution_webhook(request: Request):
    """
    Webhook anti-reintentos:
    - SIEMPRE responde 200 rápido (ACK inmediato)
    - Procesa en background para que Evolution no reintente
    """
    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"❌ webhook: JSON inválido: {e}")
        return {"status": "ignored", "reason": "invalid_json"}

    # Log del payload (controlado Y SANITIZADO)
    _safe_log_payload("🧾 WEBHOOK: ", body)

    try:
        data_payload = body.get("data")
        if not data_payload:
            return {"status": "ignored", "reason": "no_data"}

        events = data_payload if isinstance(data_payload, list) else [data_payload]

        # ACK inmediato: dispara background y regresa
        bot_state: GlobalState = request.app.state.bot
        asyncio.create_task(_background_process_events(bot_state, events))
        return {"status": "accepted"}

    except Exception as e:
        logger.error(f"❌ webhook ERROR GENERAL: {e}")
        return {"status": "error_but_acked"}

