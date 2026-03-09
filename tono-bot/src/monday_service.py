import os
import asyncio
import httpx
import json
import logging
import re
from datetime import datetime, timedelta

import pytz

logger = logging.getLogger(__name__)

# Meses en español para nombres de grupos en Monday
MESES_ES = {
    1: "ENERO", 2: "FEBRERO", 3: "MARZO", 4: "ABRIL",
    5: "MAYO", 6: "JUNIO", 7: "JULIO", 8: "AGOSTO",
    9: "SEPTIEMBRE", 10: "OCTUBRE", 11: "NOVIEMBRE", 12: "DICIEMBRE"
}

# ============================================================
# V2: STAGE HIERARCHY (solo avanza, nunca retrocede)
# ============================================================
STAGE_HIERARCHY = {
    "1er Contacto": 1,
    "Intención": 2,
    "Cotización": 3,
    "Cita Programada": 4,
}

# Estados terminales: si el lead está en uno de estos, se crea nuevo item
TERMINAL_STAGES = {"Venta Cerrada", "Venta Caida", "Sin Interes"}

# ============================================================
# V2: VEHICLE SYNONYMS → Monday dropdown labels
# ============================================================
VEHICLE_DROPDOWN_MAP = {
    "Tunland E5": ["e5", "tunland", "tunland e5"],
    "ESTA 6x4 11.8": ["esta 11.8", "6x4 11.8", "esta"],
    "ESTA 6x4 X13": ["esta x13", "6x4 x13"],
    "Miler": ["miler", "miller"],
    "Toano Panel": ["toano", "panel", "toano panel"],
    "Tunland G7": ["g7", "tunland g7"],
    "Tunland G9": ["g9", "tunland g9"],
    "Cascadia": ["cascadia", "freightliner"],
}


# ============================================================
# V3: TRACKING ID → Internal ad attribution (Baileys workaround)
# ============================================================
# Format: <MODEL_CODE>-A<NUMBER> (e.g., TG9-A1 = Tunland G9, Ad 1)
MODEL_CODE_MAP = {
    "TG7": "Tunland G7",
    "TG9": "Tunland G9",
    "TE5": "Tunland E5",
    "ML":  "Miler",
    "TP":  "Toano Panel",
    "E11": "ESTA 6x4 11.8",
    "EX":  "ESTA 6x4 X13",
    "CA":  "Cascadia",
}

TRACKING_ID_PATTERN = re.compile(r'\b([A-Z][A-Z0-9]{1,3})-A(\d{1,3})\b', re.IGNORECASE)


def extract_tracking_id(text: str) -> dict | None:
    """
    Detects a tracking ID pattern in the message text.
    Returns dict with tracking_id, model_code, ad_number, vehicle_label or None.
    """
    if not text:
        return None

    m = TRACKING_ID_PATTERN.search(text)
    if not m:
        return None

    model_code = m.group(1).upper()
    ad_number = int(m.group(2))
    vehicle_label = MODEL_CODE_MAP.get(model_code)

    if not vehicle_label:
        return None

    tracking_id = f"{model_code}-A{ad_number}"
    return {
        "tracking_id": tracking_id,
        "model_code": model_code,
        "ad_number": ad_number,
        "vehicle_label": vehicle_label,
    }


def strip_tracking_id(text: str) -> str:
    """
    Removes the tracking ID from the message text.
    If the result is empty/whitespace, returns 'Hola'.
    """
    cleaned = TRACKING_ID_PATTERN.sub("", text).strip()
    # Collapse multiple spaces
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned if cleaned else "Hola"


# ============================================================
# V2.1: REFERRAL → Granular Monday column helpers
# ============================================================
def _resolve_channel_label(referral_data: dict) -> str:
    """
    Deriva el label de Canal para Monday desde referral data.
    Retorna: 'Facebook', 'Instagram', 'Directo'
    """
    if not referral_data:
        return "Directo"

    entry_app = (referral_data.get("entry_app") or "").lower()
    source_url = (referral_data.get("source_url") or "").lower()
    conversion_source = (referral_data.get("conversion_source") or "").lower()

    if "instagram" in entry_app or "instagram" in source_url:
        return "Instagram"
    if "facebook" in entry_app or "fb" in entry_app or "facebook" in source_url or "fb.com" in source_url:
        return "Facebook"
    if "fb" in conversion_source:
        return "Facebook"

    return "Facebook"


def _resolve_source_type_label(referral_data: dict) -> str:
    """
    Deriva el label de Tipo Origen para Monday desde referral data.
    Retorna: 'Ad', 'Post', 'Directo'
    """
    if not referral_data:
        return "Directo"

    source_type = (referral_data.get("source_type") or "").lower()
    if source_type == "ad":
        return "Ad"
    if source_type == "post":
        return "Post"

    entry_point = (referral_data.get("entry_point") or "").lower()
    conversion_source = (referral_data.get("conversion_source") or "").lower()
    if "ad" in entry_point or "ad" in conversion_source:
        return "Ad"
    if "post" in entry_point:
        return "Post"

    return "Directo"


def _get_current_month_group_name() -> str:
    """Retorna el nombre del grupo del mes actual: 'FEBRERO 2026'"""
    try:
        tz = pytz.timezone("America/Mexico_City")
        now = datetime.now(tz)
    except Exception:
        now = datetime.now()

    mes = MESES_ES.get(now.month, "")
    return f"{mes} {now.year}"


def resolve_vehicle_to_dropdown(interest: str) -> str:
    """
    Dado un interés detectado por el bot (ej. 'Tunland G9 2025'),
    devuelve el label EXACTO del dropdown de Monday (ej. 'Tunland G9').

    1. Primero intenta match contra VEHICLE_DROPDOWN_MAP (aliases conocidos).
    2. Si no hay match, genera un label dinámico limpiando marcas y años.
       Esto permite que vehículos nuevos en inventario se registren en Monday
       sin necesidad de actualizar el código.
    """
    if not interest:
        return ""

    # Strip known brand names and noise
    _brand_noise = ["foton", "freightliner"]
    interest_lower = interest.lower()
    for brand in _brand_noise:
        interest_lower = interest_lower.replace(brand, "")
    interest_lower = interest_lower.replace("diesel", "").replace("4x4", "").strip()

    # 1) Try static map (exact alias matching)
    best_label = ""
    best_score = 0

    for label, synonyms in VEHICLE_DROPDOWN_MAP.items():
        score = 0
        for syn in synonyms:
            if syn in interest_lower:
                score += len(syn)  # Longer match = higher score
        if score > best_score:
            best_score = score
            best_label = label

    if best_label:
        return best_label

    # 2) Dynamic fallback: generate label from cleaned interest
    # Remove year patterns (2020-2029)
    fallback = re.sub(r'\b20\d{2}\b', '', interest_lower).strip()
    # Collapse whitespace
    fallback = re.sub(r'\s+', ' ', fallback).strip()

    if fallback and len(fallback) >= 2:
        return fallback.title()

    return ""


def resolve_payment_to_label(payment: str) -> str:
    """
    Mapea el pago extraído por el bot al label EXACTO de Monday.
    'Contado' → 'De Contado', 'Crédito' → 'Financiamiento', default → 'Por definir'
    """
    if not payment:
        return "Por definir"

    p = payment.lower().strip()
    if p in ("contado", "de contado", "cash"):
        return "De Contado"
    if p in ("crédito", "credito", "financiamiento", "financiación"):
        return "Financiamiento"
    return "Por definir"


def resolve_appointment_to_iso(appointment_text: str) -> dict:
    """
    Convierte texto de cita (ej. 'Viernes 10:00 AM') a formato ISO para Monday date column.
    Retorna dict con 'date' y opcionalmente 'time', o {} si no se puede parsear.
    Timezone: America/Mexico_City
    """
    if not appointment_text:
        return {}

    try:
        tz = pytz.timezone("America/Mexico_City")
        now = datetime.now(tz)
    except Exception:
        now = datetime.now()

    text = appointment_text.strip().lower()
    target_date = None
    target_time = None

    # --- Parse day ---
    dias_map = {
        "lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2,
        "jueves": 3, "viernes": 4, "sábado": 5, "sabado": 5, "domingo": 6,
    }

    if "mañana" in text:
        target_date = now.date() + timedelta(days=1)
    elif "pasado mañana" in text:
        target_date = now.date() + timedelta(days=2)
    elif "próxima semana" in text or "proxima semana" in text:
        # Next Monday
        days_until_monday = (7 - now.weekday()) % 7
        if days_until_monday == 0:
            days_until_monday = 7
        target_date = now.date() + timedelta(days=days_until_monday)
    else:
        for dia_name, dia_num in dias_map.items():
            if dia_name in text:
                days_ahead = (dia_num - now.weekday()) % 7
                if days_ahead == 0:
                    days_ahead = 7  # Next week if same day
                target_date = now.date() + timedelta(days=days_ahead)
                break

    # Try explicit date: "10 de marzo", "marzo 10", etc.
    if not target_date:
        meses_map = {
            "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
            "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
            "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
        }
        m = re.search(r"(\d{1,2})\s+de\s+(\w+)", text)
        if m:
            day_num = int(m.group(1))
            month_name = m.group(2).lower()
            month_num = meses_map.get(month_name)
            if month_num and 1 <= day_num <= 31:
                year = now.year
                if month_num < now.month or (month_num == now.month and day_num < now.day):
                    year += 1
                try:
                    target_date = datetime(year, month_num, day_num).date()
                except ValueError:
                    pass

    # If still no date, default to today (appointment mentioned without day)
    if not target_date:
        target_date = now.date()

    # --- Parse time ---
    # "medio día" / "mediodía"
    if "medio dia" in text or "mediodía" in text or "medio día" in text:
        target_time = "12:00:00"

    if not target_time:
        m = re.search(r"(\d{1,2})\s*y\s*media", text)
        if m:
            h = int(m.group(1))
            if 1 <= h <= 12:
                target_time = f"{h:02d}:30:00"

    if not target_time:
        m = re.search(r"(\d{1,2})\s*:\s*(\d{2})\s*(am|pm)?", text, re.IGNORECASE)
        if m:
            h = int(m.group(1))
            mm = int(m.group(2))
            meridiem = (m.group(3) or "").lower()
            if meridiem == "pm" and h < 12:
                h += 12
            elif meridiem == "am" and h == 12:
                h = 0
            if 0 <= h <= 23 and 0 <= mm <= 59:
                target_time = f"{h:02d}:{mm:02d}:00"

    if not target_time:
        m = re.search(r"(\d{1,2})\s*(am|pm)", text, re.IGNORECASE)
        if m:
            h = int(m.group(1))
            meridiem = m.group(2).lower()
            if 1 <= h <= 12:
                hh = h % 12
                if meridiem == "pm":
                    hh += 12
                target_time = f"{hh:02d}:00:00"

    if not target_time:
        if "tarde" in text:
            target_time = "15:00:00"
        elif "mañana" in text and "por la" in text:
            target_time = "10:00:00"

    result = {}
    if target_date:
        result["date"] = target_date.isoformat()
    if target_time:
        result["time"] = target_time
    return result


class MondayService:
    def __init__(self):
        self.api_key = os.getenv("MONDAY_API_KEY")
        self.board_id = os.getenv("MONDAY_BOARD_ID")
        self.api_url = "https://api.monday.com/v2"

        # --- V1 Column IDs ---
        self.phone_dedupe_col_id = os.getenv("MONDAY_DEDUPE_COLUMN_ID")
        self.last_msg_id_col_id = os.getenv("MONDAY_LAST_MSG_ID_COLUMN_ID")
        self.phone_real_col_id = os.getenv("MONDAY_PHONE_COLUMN_ID")
        self.stage_col_id = os.getenv("MONDAY_STAGE_COLUMN_ID")

        # --- V2 NEW Column IDs ---
        self.vehicle_col_id = os.getenv("MONDAY_VEHICLE_COLUMN_ID")
        self.payment_col_id = os.getenv("MONDAY_PAYMENT_COLUMN_ID")
        self.appointment_col_id = os.getenv("MONDAY_APPOINTMENT_COLUMN_ID")
        self.appointment_time_col_id = os.getenv("MONDAY_APPOINTMENT_TIME_COLUMN_ID")
        self.cmv_col_id = os.getenv("MONDAY_CMV_COLUMN_ID")

        # --- V2 Referral Tracking ---
        self.source_col_id = os.getenv("MONDAY_SOURCE_COLUMN_ID")

        # --- V2.1 Granular Referral Columns ---
        self.channel_col_id = os.getenv("MONDAY_CHANNEL_COLUMN_ID")
        self.source_type_col_id = os.getenv("MONDAY_SOURCE_TYPE_COLUMN_ID")
        self.ad_id_col_id = os.getenv("MONDAY_AD_ID_COLUMN_ID")
        self.ctwa_clid_col_id = os.getenv("MONDAY_CTWA_CLID_COLUMN_ID")
        self.campaign_name_col_id = os.getenv("MONDAY_CAMPAIGN_NAME_COLUMN_ID")
        self.adset_name_col_id = os.getenv("MONDAY_ADSET_NAME_COLUMN_ID")
        self.ad_name_col_id = os.getenv("MONDAY_AD_NAME_COLUMN_ID")

        # --- V3: Tracking ID / Anuncios Board ---
        self.tracking_id_col_id = os.getenv("MONDAY_TRACKING_ID_COLUMN_ID")
        self.ads_board_id = os.getenv("MONDAY_ADS_BOARD_ID")
        self.ads_tracking_col_id = os.getenv("MONDAY_ADS_TRACKING_COLUMN_ID")
        self.leads_connect_ads_col_id = os.getenv("MONDAY_LEADS_CONNECT_ADS_COLUMN_ID")

        # Log config
        if self.stage_col_id:
            logger.info(f"✅ Monday Stage Column: {self.stage_col_id}")
        else:
            logger.warning("⚠️ MONDAY_STAGE_COLUMN_ID no configurada")

        v2_cols = {
            "vehicle": self.vehicle_col_id,
            "payment": self.payment_col_id,
            "appointment": self.appointment_col_id,
            "appointment_time": self.appointment_time_col_id,
            "source": self.source_col_id,
            "channel": self.channel_col_id,
            "source_type": self.source_type_col_id,
            "ad_id": self.ad_id_col_id,
            "ctwa_clid": self.ctwa_clid_col_id,
            "campaign_name": self.campaign_name_col_id,
            "adset_name": self.adset_name_col_id,
            "ad_name": self.ad_name_col_id,
            "tracking_id": self.tracking_id_col_id,
            "ads_board": self.ads_board_id,
        }
        configured = {k: v for k, v in v2_cols.items() if v}
        if configured:
            logger.info(f"✅ V2 columns: {configured}")
        else:
            logger.warning("⚠️ No V2 column IDs configured (vehicle/payment/appointment)")

    def _sanitize_phone(self, phone: str) -> str:
        if not phone:
            return ""
        return re.sub(r'\D', '', str(phone))

    async def _graphql(self, query: str, variables: dict):
        if not self.api_key:
            raise RuntimeError("MONDAY_API_KEY no configurada")

        headers = {"Authorization": self.api_key, "Content-Type": "application/json"}
        payload = {"query": query, "variables": variables}

        _MAX_RETRIES = 3
        for _attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=25.0) as client:
                    resp = await client.post(self.api_url, json=payload, headers=headers)

                if resp.status_code >= 500 and _attempt < _MAX_RETRIES - 1:
                    backoff = 2 ** (_attempt + 1)
                    logger.warning(f"⚠️ Monday 5xx retry {_attempt + 1}/{_MAX_RETRIES} tras {backoff}s: {resp.status_code}")
                    await asyncio.sleep(backoff)
                    continue

                data = resp.json()
                if "errors" in data:
                    logger.error(f"Monday API Error: {data['errors']}")
                return data

            except (httpx.TimeoutException, httpx.RequestError) as e:
                if _attempt < _MAX_RETRIES - 1:
                    backoff = 2 ** (_attempt + 1)
                    logger.warning(f"⚠️ Monday retry {_attempt + 1}/{_MAX_RETRIES} tras {backoff}s: {e}")
                    await asyncio.sleep(backoff)
                else:
                    raise

    async def _find_item_by_phone(self, phone_limpio: str):
        """
        V2: Busca item por teléfono y retorna dict con id, name, y stage actual.
        Retorna el item MÁS RECIENTE (último creado) si hay múltiples.
        """
        if not phone_limpio or not self.phone_dedupe_col_id:
            return None

        query = """
        query ($board_id: ID!, $col_id: String!, $val: String!) {
          items_page_by_column_values(
            limit: 10,
            board_id: $board_id,
            columns: [{column_id: $col_id, column_values: [$val]}]
          ) {
            items {
              id
              name
              column_values {
                id
                text
                value
              }
            }
          }
        }
        """
        variables = {
            "board_id": int(self.board_id),
            "col_id": self.phone_dedupe_col_id,
            "val": phone_limpio
        }

        data = await self._graphql(query, variables)
        items = data.get("data", {}).get("items_page_by_column_values", {}).get("items", [])

        if not items:
            return None

        # Find the most recent item (highest ID = most recently created)
        best_item = max(items, key=lambda x: int(x.get("id", 0)))

        # Extract current stage from column_values
        current_stage = ""
        if self.stage_col_id:
            for col in best_item.get("column_values", []):
                if col.get("id") == self.stage_col_id:
                    current_stage = (col.get("text") or "").strip()
                    break

        return {
            "id": best_item["id"],
            "name": best_item.get("name", ""),
            "current_stage": current_stage,
        }

    async def _create_group(self, group_name: str) -> str:
        """Crea un nuevo grupo en el tablero y retorna su ID."""
        query = """
        mutation ($board_id: ID!, $group_name: String!) {
            create_group (board_id: $board_id, group_name: $group_name) {
                id
            }
        }
        """
        variables = {
            "board_id": int(self.board_id),
            "group_name": group_name,
        }
        data = await self._graphql(query, variables)
        group_id = data.get("data", {}).get("create_group", {}).get("id")
        if group_id:
            logger.info(f"✅ Grupo creado: '{group_name}' (ID: {group_id})")
        else:
            logger.error(f"❌ No se pudo crear grupo '{group_name}': {data}")
        return group_id

    async def _get_group_id_by_name(self, group_name: str):
        if not group_name:
            return None

        query = """
        query ($board_id: ID!) {
          boards(ids: [$board_id]) {
            groups {
              id
              title
            }
          }
        }
        """
        variables = {"board_id": int(self.board_id)}

        data = await self._graphql(query, variables)
        boards = data.get("data", {}).get("boards", [])

        if not boards:
            return None

        groups = boards[0].get("groups", [])

        for group in groups:
            if group.get("title", "").upper() == group_name.upper():
                logger.info(f"✅ Grupo encontrado: '{group['title']}' (ID: {group['id']})")
                return group["id"]

        # Grupo no encontrado → crearlo automáticamente
        logger.info(f"📂 Grupo '{group_name}' no encontrado, creándolo automáticamente...")
        try:
            new_group_id = await self._create_group(group_name)
            return new_group_id
        except Exception as e:
            logger.error(f"❌ Error creando grupo '{group_name}': {e}")
            return None

    def _should_advance_stage(self, current_stage: str, candidate_stage: str) -> bool:
        """V2: Solo avanza el embudo, nunca retrocede."""
        current_rank = STAGE_HIERARCHY.get(current_stage, 0)
        candidate_rank = STAGE_HIERARCHY.get(candidate_stage, 0)
        return candidate_rank > current_rank

    def _build_column_values(self, lead_data: dict, stage: str = None, is_new: bool = False,
                             current_stage: str = "") -> dict:
        """
        V2: Construye el dict de column_values para Monday GraphQL.
        Respeta la jerarquía del embudo (solo avanza).
        """
        col_vals = {}

        # Dedupe phone (always)
        phone_limpio = self._sanitize_phone(str(lead_data.get("telefono", "")))
        if self.phone_dedupe_col_id and phone_limpio:
            col_vals[self.phone_dedupe_col_id] = phone_limpio

        # Message ID tracking
        msg_id = str(lead_data.get("external_id", "")).strip()
        if self.last_msg_id_col_id and msg_id:
            col_vals[self.last_msg_id_col_id] = msg_id

        # Real phone column
        if self.phone_real_col_id and phone_limpio:
            col_vals[self.phone_real_col_id] = {"phone": phone_limpio, "countryShortName": "MX"}

        # --- V2: Stage (Embudo) with direction enforcement ---
        if stage and self.stage_col_id:
            if is_new:
                col_vals[self.stage_col_id] = {"label": stage}
                logger.info(f"📊 Nuevo item → Embudo: {stage}")
            elif stage == "Sin Interes":
                # Sin Interes overrides any stage
                col_vals[self.stage_col_id] = {"label": stage}
                logger.info(f"📊 Sin Interes → Embudo: {stage}")
            elif self._should_advance_stage(current_stage, stage):
                col_vals[self.stage_col_id] = {"label": stage}
                logger.info(f"📊 Embudo avanza: {current_stage} → {stage}")
            else:
                logger.info(f"📊 Embudo NO retrocede: {current_stage} → {stage} (ignorado)")

        # --- V2: Vehicle (Dropdown) ---
        if self.vehicle_col_id:
            interest = lead_data.get("interes") or ""
            vehicle_label = resolve_vehicle_to_dropdown(interest)
            if vehicle_label:
                col_vals[self.vehicle_col_id] = {"labels": [vehicle_label]}

        # --- V2: Payment (Status) ---
        if self.payment_col_id:
            payment_raw = lead_data.get("pago") or ""
            payment_label = resolve_payment_to_label(payment_raw)
            if is_new or payment_label != "Por definir":
                col_vals[self.payment_col_id] = {"label": payment_label}

        # --- V2: Appointment (Date + Time in separate columns) ---
        if self.appointment_col_id:
            appointment_text = lead_data.get("cita") or ""
            appointment_iso = lead_data.get("cita_iso") or resolve_appointment_to_iso(appointment_text)
            if appointment_iso and appointment_iso.get("date"):
                # Date column: only the date, no time
                col_vals[self.appointment_col_id] = {"date": appointment_iso["date"]}

                # Time column (Hour type): separate hour and minute
                if self.appointment_time_col_id and appointment_iso.get("time"):
                    try:
                        time_parts = appointment_iso["time"].split(":")
                        hour = int(time_parts[0])
                        minute = int(time_parts[1]) if len(time_parts) > 1 else 0
                        col_vals[self.appointment_time_col_id] = {"hour": hour, "minute": minute}
                    except (ValueError, IndexError) as e:
                        logger.warning(f"⚠️ No se pudo parsear hora de cita: {appointment_iso.get('time')} - {e}")

        # --- V2: Source / Referral (Status column - label compuesto) ---
        if self.source_col_id:
            referral_source = lead_data.get("referral_source") or "Directo"
            if is_new or referral_source != "Directo":
                col_vals[self.source_col_id] = {"label": referral_source}

        # --- V2.1: Granular Referral Columns ---
        referral_detail = lead_data.get("referral_data") or {}

        # Canal (Status): Facebook / Instagram / Directo
        if self.channel_col_id:
            channel_label = _resolve_channel_label(referral_detail)
            if is_new or channel_label != "Directo":
                col_vals[self.channel_col_id] = {"label": channel_label}

        # Tipo Origen (Status): Ad / Post / Directo
        if self.source_type_col_id:
            source_type_label = _resolve_source_type_label(referral_detail)
            if is_new or source_type_label != "Directo":
                col_vals[self.source_type_col_id] = {"label": source_type_label}

        # Ad ID (Text): Meta Ad ID from source_id
        if self.ad_id_col_id:
            ad_id = (referral_detail.get("source_id") or "").strip()
            if ad_id:
                col_vals[self.ad_id_col_id] = ad_id

        # CTWA CLID (Text): Click-to-WhatsApp click ID
        if self.ctwa_clid_col_id:
            ctwa_clid = (referral_detail.get("ctwa_clid") or "").strip()
            if ctwa_clid:
                col_vals[self.ctwa_clid_col_id] = ctwa_clid

        # Campaign Name (Text): future Meta Marketing API enrichment
        if self.campaign_name_col_id:
            campaign_name = (referral_detail.get("campaign_name") or "").strip()
            if campaign_name:
                col_vals[self.campaign_name_col_id] = campaign_name

        # Ad Set Name (Text): future Meta Marketing API enrichment
        if self.adset_name_col_id:
            adset_name = (referral_detail.get("adset_name") or "").strip()
            if adset_name:
                col_vals[self.adset_name_col_id] = adset_name

        # Ad Name (Text): future Meta Marketing API enrichment
        if self.ad_name_col_id:
            ad_name = (referral_detail.get("ad_name") or "").strip()
            if ad_name:
                col_vals[self.ad_name_col_id] = ad_name

        # --- V3: Tracking ID (Text) ---
        if self.tracking_id_col_id:
            tracking_id = (lead_data.get("tracking_id") or "").strip()
            if tracking_id:
                col_vals[self.tracking_id_col_id] = tracking_id

        return col_vals

    async def create_or_update_lead(self, lead_data: dict, stage: str = None, add_note: str = None):
        """
        V2: Crea o actualiza un lead en Monday.com.

        Reglas V2:
        - Labels: 1er Contacto, Intención, Cotización, Cita Programada, Sin Interes
        - Terminal states (Venta Cerrada, Venta Caida, Sin Interes): crear nuevo item
        - Embudo solo avanza, nunca retrocede
        - Vehículo, Pago, Agenda Citas → columnas dedicadas
        """
        raw_phone = str(lead_data.get("telefono", ""))
        phone_limpio = self._sanitize_phone(raw_phone)
        nombre = str(lead_data.get("nombre", "")).strip() or "Lead sin nombre"
        msg_id = str(lead_data.get("external_id", "")).strip()

        if not phone_limpio:
            logger.warning("⚠️ Lead sin teléfono, no se puede procesar.")
            return None

        # 1. BUSCAR DUPLICADO
        existing = await self._find_item_by_phone(phone_limpio)

        # 2. DECIDIR: CREAR o ACTUALIZAR
        is_new = False
        item_id = None
        current_stage = ""

        if existing:
            current_stage = existing.get("current_stage", "")
            item_id = existing["id"]

            # V2: Terminal state → crear nuevo item (nuevo ciclo)
            if current_stage in TERMINAL_STAGES:
                logger.info(
                    f"🔄 Lead en estado terminal '{current_stage}' → creando nuevo ciclo para {phone_limpio}"
                )
                is_new = True
                item_id = None
                current_stage = ""
            else:
                is_new = False
        else:
            is_new = True

        # 3. CONSTRUIR COLUMN VALUES
        effective_stage = stage or ("1er Contacto" if is_new else "")
        col_vals = self._build_column_values(
            lead_data,
            stage=effective_stage,
            is_new=is_new,
            current_stage=current_stage,
        )

        # 4. CREAR O ACTUALIZAR
        if is_new:
            # --- CREAR NUEVO ---
            month_group_name = _get_current_month_group_name()
            group_id = await self._get_group_id_by_name(month_group_name)

            item_name_display = f"{nombre} | {phone_limpio}"

            if group_id:
                logger.info(f"🆕 Creando lead [{effective_stage}] en grupo '{month_group_name}': {phone_limpio}")
                query_create = """
                mutation ($board_id: ID!, $group_id: String!, $name: String!, $vals: JSON!) {
                    create_item (board_id: $board_id, group_id: $group_id, item_name: $name, column_values: $vals, create_labels_if_missing: true) { id }
                }
                """
            else:
                logger.info(f"🆕 Creando lead [{effective_stage}] (sin grupo): {phone_limpio}")
                query_create = """
                mutation ($board_id: ID!, $name: String!, $vals: JSON!) {
                    create_item (board_id: $board_id, item_name: $name, column_values: $vals, create_labels_if_missing: true) { id }
                }
                """

            vars_create = {
                "board_id": int(self.board_id),
                "name": item_name_display,
                "vals": json.dumps(col_vals)
            }
            if group_id:
                vars_create["group_id"] = group_id

            res = await self._graphql(query_create, vars_create)
            item_id = res.get("data", {}).get("create_item", {}).get("id")

        else:
            # --- ACTUALIZAR EXISTENTE ---
            logger.info(f"♻️ Actualizando lead [{effective_stage or 'datos'}] (ID: {item_id})")

            if col_vals:
                query_update = """
                mutation ($item_id: ID!, $board_id: ID!, $vals: JSON!) {
                    change_multiple_column_values (item_id: $item_id, board_id: $board_id, column_values: $vals, create_labels_if_missing: true) { id }
                }
                """
                vars_update = {
                    "item_id": int(item_id),
                    "board_id": int(self.board_id),
                    "vals": json.dumps(col_vals)
                }
                await self._graphql(query_update, vars_update)

            # Update item name if we have a real name
            if nombre and nombre != "Lead sin nombre":
                new_item_name = f"{nombre} | {phone_limpio}"
                query_rename = """
                mutation ($board_id: ID!, $item_id: ID!, $col_id: String!, $value: String!) {
                    change_simple_column_value(board_id: $board_id, item_id: $item_id, column_id: $col_id, value: $value) { id }
                }
                """
                vars_rename = {
                    "board_id": int(self.board_id),
                    "item_id": int(item_id),
                    "col_id": "name",
                    "value": new_item_name,
                }
                try:
                    await self._graphql(query_rename, vars_rename)
                    logger.info(f"✅ Nombre actualizado en Monday: '{new_item_name}'")
                except Exception as e:
                    logger.error(f"⚠️ Error actualizando nombre en Monday: {e}")

        # 5. AGREGAR NOTA
        if item_id and (is_new or add_note):
            if is_new:
                detalles = (
                    f"📊 ETAPA: {effective_stage}\n"
                    f"👤 Nombre: {nombre}\n"
                    f"📞 Tel: {phone_limpio}\n"
                    f"📝 Interés: {lead_data.get('interes', 'N/A')}\n"
                )
                if lead_data.get('cita'):
                    cita_iso = lead_data.get('cita_iso') or resolve_appointment_to_iso(lead_data.get('cita', ''))
                    if cita_iso.get('date'):
                        detalles += f"📅 Cita: {cita_iso['date']}"
                        if cita_iso.get('time'):
                            detalles += f" {cita_iso['time']}"
                        detalles += "\n"
                    else:
                        detalles += f"📅 Cita: {lead_data.get('cita')}\n"
                pago_label = resolve_payment_to_label(lead_data.get('pago', ''))
                detalles += f"💰 Pago: {pago_label}\n"
                # Referral source
                referral_source = lead_data.get('referral_source', '')
                if referral_source:
                    detalles += f"📢 Origen: {referral_source}\n"
                    referral_detail = lead_data.get('referral_data') or {}
                    if referral_detail.get('headline'):
                        detalles += f"📋 Anuncio: {referral_detail['headline']}\n"
                    if referral_detail.get('source_id'):
                        detalles += f"🔗 Ad ID: {referral_detail['source_id']}\n"
                    if referral_detail.get('ctwa_clid'):
                        detalles += f"🆔 CTWA Click ID: {referral_detail['ctwa_clid']}\n"
                # Tracking ID attribution
                tracking_id = lead_data.get('tracking_id', '')
                if tracking_id:
                    tracking_data = lead_data.get('tracking_data') or {}
                    vehicle = tracking_data.get('vehicle_label', '')
                    detalles += f"🏷️ Tracking ID: {tracking_id}"
                    if vehicle:
                        detalles += f" ({vehicle})"
                    detalles += "\n"
            else:
                detalles = add_note or f"📊 Actualizado a etapa: {effective_stage}"

            query_note = """
            mutation ($item_id: ID!, $body: String!) {
                create_update (item_id: $item_id, body: $body) { id }
            }
            """
            await self._graphql(query_note, {"item_id": int(item_id), "body": detalles})

        return item_id

    # ============================================================
    # V3: ANUNCIOS BOARD - Tracking ID lookup + Connect Boards
    # ============================================================
    async def find_anuncio_by_tracking_id(self, tracking_id: str) -> dict | None:
        """
        Searches the Anuncios board for an item matching the tracking_id.
        Returns dict with 'id' and 'name', or None.
        """
        if not self.ads_board_id or not self.ads_tracking_col_id or not tracking_id:
            return None

        query = """
        query ($board_id: ID!, $col_id: String!, $val: String!) {
          items_page_by_column_values(
            limit: 1,
            board_id: $board_id,
            columns: [{column_id: $col_id, column_values: [$val]}]
          ) {
            items {
              id
              name
            }
          }
        }
        """
        variables = {
            "board_id": int(self.ads_board_id),
            "col_id": self.ads_tracking_col_id,
            "val": tracking_id.upper(),
        }

        try:
            data = await self._graphql(query, variables)
            items = data.get("data", {}).get("items_page_by_column_values", {}).get("items", [])
            if items:
                logger.info(f"🏷️ Anuncio encontrado: {items[0]['name']} (ID: {items[0]['id']}) para tracking_id={tracking_id}")
                return {"id": items[0]["id"], "name": items[0].get("name", "")}
        except Exception as e:
            logger.error(f"⚠️ Error buscando anuncio por tracking_id={tracking_id}: {e}")

        return None

    async def connect_lead_to_anuncio(self, lead_item_id: str, anuncio_item_id: str):
        """
        Connects a lead item to an anuncio item via Connect Boards column.
        """
        if not self.leads_connect_ads_col_id or not lead_item_id or not anuncio_item_id:
            return

        query = """
        mutation ($board_id: ID!, $item_id: ID!, $col_id: String!, $value: JSON!) {
            change_multiple_column_values(board_id: $board_id, item_id: $item_id, column_values: $value) { id }
        }
        """
        col_vals = {
            self.leads_connect_ads_col_id: {"linkedPulseIds": [{"linkedPulseId": int(anuncio_item_id)}]}
        }
        variables = {
            "board_id": int(self.board_id),
            "item_id": int(lead_item_id),
            "col_id": self.leads_connect_ads_col_id,
            "value": json.dumps(col_vals),
        }

        try:
            await self._graphql(query, variables)
            logger.info(f"🔗 Lead {lead_item_id} vinculado a Anuncio {anuncio_item_id}")
        except Exception as e:
            logger.error(f"⚠️ Error vinculando lead a anuncio: {e}")


# Instancia lista para usar
monday_service = MondayService()
