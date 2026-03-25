"""
Conversation FSM — State machine + slot manager + action planner.

Separates decision logic from LLM text generation.
The LLM only writes; rules decide.

V2: Adds encapsulated entity extraction, slot diffing, primary/secondary flow,
    and context-aware intent classification.
"""

import logging
import re
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ============================================================
# STATES
# ============================================================
class ConversationState(str, Enum):
    GREETING = "greeting"                      # First contact
    CAMPAIGN_ENTRY = "campaign_entry"          # Arrived via tracking ID, campaign active
    INTEREST_DISCOVERY = "interest_discovery"  # Figuring out what they want
    COLLECTING_DATA = "collecting_data"        # Gathering contact info
    APPOINTMENT_SCHEDULING = "appointment"     # Scheduling visit
    QUALIFIED = "qualified"                    # Full lead generated (name + interest + appointment)
    CATALOG_BROWSING = "catalog_browsing"      # Browsing inventory / asking questions
    WAITING = "waiting"                        # Client said "let me think"
    HUMAN_HANDOFF = "human_handoff"            # Handed to human agent


# ============================================================
# INTENTS
# ============================================================
class Intent(str, Enum):
    GREETING = "greeting"
    PROVIDE_DATA = "provide_data"       # Gave name, email, city, etc.
    ASK_PRICE = "ask_price"
    ASK_PHOTOS = "ask_photos"
    ASK_PDF = "ask_pdf"
    ASK_FINANCING = "ask_financing"
    ASK_LOCATION = "ask_location"
    ASK_APPOINTMENT = "ask_appointment"
    ASK_INVENTORY = "ask_inventory"     # "tienes más camiones?"
    ASK_QUESTION = "ask_question"       # General question about vehicle/business
    MAKE_OFFER = "make_offer"           # "te doy 700 mil"
    CONFIRM = "confirm"                 # "sí", "este mismo", "dale"
    DENY = "deny"                       # "no", "no quiero"
    MODEL_SWITCH = "model_switch"       # Wants a different model
    WAIT = "wait"                       # "déjame ver", "luego"
    DISINTEREST = "disinterest"         # "no me interesa", "ya no"
    TRUST_CONCERN = "trust_concern"     # "me suena a fraude", "tienen permiso?"
    UNKNOWN = "unknown"


# ============================================================
# ACTIONS
# ============================================================
class Action(str, Enum):
    GREET = "greet"
    PRESENT_CAMPAIGN = "present_campaign"
    ACKNOWLEDGE_AND_ASK_NEXT = "acknowledge_and_ask_next"  # Acknowledge new data, ask for next missing slot
    ASK_NAME = "ask_name"
    ASK_EMAIL = "ask_email"
    ASK_CITY = "ask_city"
    ASK_TIMELINE = "ask_timeline"         # Campaign: liquidation time
    ASK_OFFER = "ask_offer"               # Campaign: ask for offer/propuesta amount
    SOFT_DENY = "soft_deny"               # Commercial response to "no" — destrabar
    CONFIRM_REGISTRATION = "confirm_registration"  # All campaign data collected
    ANSWER_QUESTION = "answer_question"   # Answer with inventory/business context
    SHOW_INVENTORY = "show_inventory"
    SEND_PHOTOS = "send_photos"
    SEND_PDF = "send_pdf"
    ASK_INTEREST = "ask_interest"         # "¿Qué vehículo te interesa?"
    ASK_APPOINTMENT = "ask_appointment"
    CONFIRM_LEAD = "confirm_lead"         # Full lead: name + interest + appointment
    WAIT_MODE = "wait_mode"
    ESCALATE = "escalate"
    ACKNOWLEDGE_SWITCH = "acknowledge_switch"  # Model switch detected
    SEND_FORM = "send_form"                    # Campaign has a Google Form for data collection


# ============================================================
# SLOTS
# ============================================================
@dataclass
class Slots:
    """Tracks all collected data for a conversation."""
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    city: Optional[str] = None
    interest: Optional[str] = None       # Vehicle model
    appointment: Optional[str] = None
    payment: Optional[str] = None        # Contado / Financiamiento
    offer_amount: Optional[str] = None   # For campaign offers (e.g., "$700,000")
    timeline: Optional[str] = None       # For campaign: estimated liquidation time

    # --- Campaign slots required by the sheet instructions ---
    _campaign_required: List[str] = field(default_factory=list)

    def missing_for_campaign(self) -> List[str]:
        """Returns missing slots required for campaign completion."""
        missing = []
        for slot_name in self._campaign_required:
            if not getattr(self, slot_name, None):
                missing.append(slot_name)
        return missing

    def missing_for_lead(self) -> List[str]:
        """Returns missing slots for a full lead (name + interest + appointment)."""
        missing = []
        if not self.name:
            missing.append("name")
        if not self.interest:
            missing.append("interest")
        if not self.appointment:
            missing.append("appointment")
        return missing

    def filled_summary(self) -> str:
        """Returns human-readable summary for LLM context."""
        parts = []
        if self.name:
            parts.append(f"NOMBRE: {self.name}")
        if self.phone:
            parts.append(f"TELÉFONO: {self.phone} (ya lo tienes, NO lo pidas)")
        if self.email:
            parts.append(f"EMAIL: {self.email}")
        if self.city:
            parts.append(f"CIUDAD: {self.city}")
        if self.interest:
            parts.append(f"INTERÉS: {self.interest}")
        if self.offer_amount:
            parts.append(f"PROPUESTA: {self.offer_amount}")
        if self.timeline:
            parts.append(f"PLAZO LIQUIDACIÓN: {self.timeline}")
        if self.appointment:
            parts.append(f"CITA: {self.appointment}")
        if self.payment:
            parts.append(f"PAGO: {self.payment}")
        return " | ".join(parts) if parts else "(ninguno)"

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for context storage."""
        d = asdict(self)
        d.pop("_campaign_required", None)
        return {k: v for k, v in d.items() if v is not None}

    @classmethod
    def from_context(cls, context: Dict[str, Any]) -> "Slots":
        """Load slots from conversation context."""
        return cls(
            name=(context.get("user_name") or "").strip() or None,
            phone=(context.get("user_phone") or "").strip() or None,
            email=(context.get("user_email") or "").strip() or None,
            city=(context.get("user_city") or "").strip() or None,
            interest=(context.get("last_interest") or "").strip() or None,
            appointment=(context.get("last_appointment") or "").strip() or None,
            payment=(context.get("last_payment") or "").strip() or None,
            offer_amount=(context.get("offer_amount") or "").strip() or None,
            timeline=(context.get("timeline") or "").strip() or None,
        )

    def update_context(self, context: Dict[str, Any]) -> None:
        """Write slots back to conversation context."""
        if self.name:
            context["user_name"] = self.name
        if self.phone:
            context["user_phone"] = self.phone
        if self.email:
            context["user_email"] = self.email
        if self.city:
            context["user_city"] = self.city
        if self.interest:
            context["last_interest"] = self.interest
        if self.appointment:
            context["last_appointment"] = self.appointment
        if self.payment:
            context["last_payment"] = self.payment
        if self.offer_amount:
            context["offer_amount"] = self.offer_amount
        if self.timeline:
            context["timeline"] = self.timeline


# ============================================================
# SLOT DIFFING — detect real changes per turn
# ============================================================
@dataclass
class SlotChange:
    """Represents a single slot that changed value."""
    slot: str
    old_value: Optional[str]
    new_value: str


def diff_slots(old: Slots, new: Slots) -> List[SlotChange]:
    """Compare two Slots snapshots and return list of changes."""
    changes: List[SlotChange] = []
    for field_name in ("name", "phone", "email", "city", "interest",
                       "appointment", "payment", "offer_amount", "timeline"):
        old_val = getattr(old, field_name, None)
        new_val = getattr(new, field_name, None)
        if new_val and new_val != old_val:
            changes.append(SlotChange(slot=field_name, old_value=old_val, new_value=new_val))
    return changes


# ============================================================
# ENTITY EXTRACTION — encapsulated, replaces legacy extraction
# ============================================================

# Words that are vehicle/ad terms, NOT cities
_CITY_NOISE = {
    "foton", "tunland", "toano", "miler", "cascadia", "esta", "panel",
    "pickup", "camioneta", "camion", "tracto", "van", "g7", "g9", "e5",
    "anuncio", "anuncion", "foto", "fotos", "modelo", "unidad",
    "freightliner", "tractocamion", "volteo", "truck", "trailer",
    "camiones", "tractos", "camionetas", "tractocamiones",
    "te", "doy", "bien", "tienes", "mas", "quiero", "este", "ese",
    "mil", "pesos", "si", "no", "precio", "oferta", "propuesta",
    "hola", "buenas", "buenos", "ok", "gracias", "perfecto",
    "calidad", "baratos", "barato", "nuevo", "nuevos", "usado", "usados",
    "mejor", "grande", "chico", "bueno", "buenos", "bonito",
    "padrino", "jefe", "amigo", "compa",
    "gobernación", "gobernacion", "momento", "puja", "ofrecer", "entender",
    "fraude", "estafa", "permiso", "como", "ultimo", "último",
}

_CITY_PATTERNS = [
    r'\b(?:de|en|desde|vivo en|soy de|ciudad)\s+([A-ZÁÉÍÓÚÑa-záéíóúñ]+(?:[,\s]+[A-ZÁÉÍÓÚÑa-záéíóúñ]+){0,3})',
]

# Mexican states and country names — stripped from city extractions
_STATE_COUNTRY = {
    "mexico", "méxico", "mx",
    "jalisco", "guanajuato", "puebla", "veracruz", "oaxaca", "chiapas",
    "tabasco", "guerrero", "michoacán", "michoacan", "sonora", "sinaloa",
    "durango", "chihuahua", "coahuila", "tamaulipas", "zacatecas",
    "aguascalientes", "hidalgo", "querétaro", "queretaro", "morelos",
    "tlaxcala", "nayarit", "colima", "campeche", "yucatán", "yucatan",
    "quintanaroo", "quintana", "bcs", "bcn",
    "nuevo", "león", "leon",  # "Nuevo León" as parts
    "san", "luis", "potosí", "potosi",  # "San Luis Potosí" as parts
    "estado", "república", "republica",
}


def _normalize_city(city: str) -> str:
    """Strip state/country suffixes and title-case the city name.
    'agrandas jalisco mexico' → 'Agrandas'
    'León Guanajuato' → 'León'
    'CDMX' → 'CDMX'
    """
    words = city.strip().split()
    if not words:
        return city

    # Remove trailing state/country words
    clean = []
    for w in words:
        w_stripped = w.rstrip(",;.")
        if w_stripped.lower() in _STATE_COUNTRY and clean:
            # Only strip if we already have at least one city word
            break
        clean.append(w_stripped)

    result = " ".join(clean)
    # Title case unless it's an acronym (CDMX, etc.)
    if result.isupper() and len(result) <= 5:
        return result
    return result.title()

_NAME_BAD_WORDS = {
    "aqui", "aquí", "nadie", "yo", "el", "ella", "amigo", "desconocido",
    "cliente", "usuario", "quien", "quién",
    "si", "sí", "no", "bueno", "ok", "okey", "hola", "bien", "gracias",
    "vale", "perfecto", "listo", "claro", "sale", "dale",
    "que", "qué", "como", "cómo", "cuando", "cuándo", "donde", "dónde",
    "precio", "fotos", "foto", "info", "información", "informacion",
    "ubicación", "ubicacion", "costo", "interesado", "interesada",
    "cotización", "cotizacion", "modelo", "camioneta", "camion", "camión",
    "credito", "crédito", "contado", "financiamiento",
    "quiero", "necesito", "busco", "tengo", "puedo", "estoy",
    "es", "eso", "ese", "esa", "este", "esta", "eh", "ah",
    "ya", "pasé", "pase", "arriba", "anterior", "antes",
}

_NAME_TRAILING_STOP = {
    "disculpa", "disculpe", "disculpen", "perdón", "perdon", "perdona",
    "oye", "oiga", "mira", "mire",
    "quisiera", "quería", "queria", "necesito", "quiero",
    "me", "te", "se", "le", "nos",
    "en", "de", "del", "por", "para", "con",
    "una", "un", "la", "el", "lo", "las", "los",
    "favor", "pregunta", "consulta", "duda",
    "buenos", "buenas", "buen",
    "hablando", "andamos", "estamos", "andas", "estas",
}


def extract_entities_for_fsm(
    user_message: str,
    history: str,
    context: Dict[str, Any],
) -> Dict[str, str]:
    """
    Encapsulated entity extraction for FSM.
    Returns dict of freshly extracted data: {slot_name: value}.

    This replaces the scattered extraction in handle_message() for FSM paths.
    All noise filtering happens HERE — the FSM receives clean data.
    """
    extracted: Dict[str, str] = {}

    if not user_message:
        return extracted

    msg_lines = [l.strip() for l in user_message.strip().split("\n") if l.strip()]
    is_multiline = len(msg_lines) > 1

    # --- NAME ---
    name = _extract_name(user_message, history)
    if not name and is_multiline:
        for line in msg_lines:
            name = _extract_name(line, history)
            if name:
                break
    if name:
        extracted["name"] = name

    # --- EMAIL ---
    email_match = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', user_message)
    if email_match:
        extracted["email"] = email_match.group(0)

    # --- PHONE ---
    phone_match = re.search(r'\b\d{10,15}\b', user_message)
    if phone_match:
        extracted["phone"] = phone_match.group(0)

    # --- CITY (with noise filter) ---
    city = _extract_city(user_message, history, msg_lines, is_multiline, name)
    if city:
        old_city = (context.get("user_city") or "").strip()
        if city != old_city:
            extracted["city"] = city

    # --- PAYMENT ---
    payment = _extract_payment(user_message)
    if payment:
        extracted["payment"] = payment

    # --- APPOINTMENT ---
    appointment = _extract_appointment(user_message)
    if appointment:
        extracted["appointment"] = appointment

    # --- OFFER AMOUNT ---
    offer = _extract_offer(user_message, history)
    if offer:
        extracted["offer_amount"] = offer

    # --- TIMELINE (for campaigns: "en 3 meses", "inmediato", etc.) ---
    timeline = _extract_timeline(user_message, history)
    if timeline:
        extracted["timeline"] = timeline

    return extracted


def _extract_name(text: str, history: str) -> Optional[str]:
    """Extract probable customer name (conservative, with context awareness)."""
    t = (text or "").strip()
    if not t or re.search(r'[0-9?¿!¡]', t):
        return None

    # Explicit patterns
    patterns = [
        r"\bme llamo\s+([A-Za-zÁÉÍÓÚÑÜáéíóúñü]+(?:\s+[A-Za-zÁÉÍÓÚÑÜáéíóúñü]+){0,3})\b",
        r"\bsoy\s+([A-Za-zÁÉÍÓÚÑÜáéíóúñü]+(?:\s+[A-Za-zÁÉÍÓÚÑÜáéíóúñü]+){0,3})\b",
        r"\bmi nombre es\s+([A-Za-zÁÉÍÓÚÑÜáéíóúñü]+(?:\s+[A-Za-zÁÉÍÓÚÑÜáéíóúñü]+){0,3})\b",
        r"\bcon\s+([A-Za-zÁÉÍÓÚÑÜáéíóúñü]+(?:\s+[A-Za-zÁÉÍÓÚÑÜáéíóúñü]+){0,2})\b",
    ]
    for p in patterns:
        m = re.search(p, t, flags=re.IGNORECASE)
        if m:
            name_raw = m.group(1).strip()
            words = name_raw.split()
            while words and words[-1].lower() in _NAME_TRAILING_STOP:
                words.pop()
            if not words:
                return None
            name_clean = " ".join(words)
            if name_clean.lower() in _NAME_BAD_WORDS:
                return None
            # Reject if first word is a common non-name word (articles, pronouns)
            if words[0].lower() in _NAME_BAD_WORDS:
                return None
            return " ".join(w.capitalize() for w in name_clean.split())

    # Context-aware: bot just asked for name → accept plain 1-4 word reply
    if history:
        last_bot = ""
        for line in reversed(history.split("\n")):
            if line.strip().startswith("A:"):
                last_bot = line.lower()
                break
        name_asking = [
            "tu nombre", "cómo te llamas", "como te llamas",
            "me compartes tu nombre", "me das tu nombre",
            "a nombre de quién", "a nombre de quien",
            "quién me busca", "quien me busca",
            "nombre del interesado", "nombre completo",
            "con quién tengo el gusto", "con quien tengo el gusto",
        ]
        if any(k in last_bot for k in name_asking):
            words = t.split()
            if 1 <= len(words) <= 4:
                if all(re.match(r'^[A-Za-zÁÉÍÓÚÑÜáéíóúñü.]+$', w) for w in words):
                    if words[0].lower() not in _NAME_BAD_WORDS:
                        return " ".join(w.capitalize() for w in words)

    return None


def _extract_city(
    user_message: str,
    history: str,
    msg_lines: List[str],
    is_multiline: bool,
    extracted_name: Optional[str],
) -> Optional[str]:
    """Extract city with aggressive noise filtering."""
    last_bot = ""
    if history:
        for hl in reversed(history.strip().split("\n")):
            if hl.startswith("A: "):
                last_bot = hl.lower()
                break

    # Direct city reply: bot asked for city
    city_asking = ["ciudad", "de dónde", "de donde", "localidad", "estado", "ubicación"]
    if any(k in last_bot for k in city_asking):
        candidates = msg_lines if is_multiline else [user_message.strip()]
        for city_line in candidates:
            words = city_line.split()
            if 1 <= len(words) <= 5 and not re.search(r'\d', city_line) and "?" not in city_line:
                if "@" not in city_line and city_line != extracted_name:
                    word_set = {w.rstrip("?!.,;:").lower() for w in city_line.split()}
                    if word_set & _CITY_NOISE:
                        continue
                    return city_line

    # Explicit pattern matching — skip if message is a question (not providing location)
    if "?" not in user_message and "¿" not in user_message:
        for cp in _CITY_PATTERNS:
            cm = re.search(cp, user_message, re.IGNORECASE)
            if cm:
                candidate = cm.group(1).strip()
                if len(candidate) > 2 and candidate.lower() not in {"si", "no", "ok"}:
                    word_set = {w.rstrip("?!.,;:").lower() for w in candidate.split()}
                    if word_set & _CITY_NOISE:
                        logger.info(f"🏙️ Ciudad descartada (ruido): {candidate}")
                        continue
                    # Normalize: strip state/country suffixes (jalisco, mexico, etc.)
                    candidate = _normalize_city(candidate)
                    return candidate

    # Multi-line fallback
    if is_multiline and history:
        data_asking = ["nombre", "teléfono", "correo", "ciudad", "datos", "registro", "completar"]
        if any(k in last_bot for k in data_asking):
            for city_line in msg_lines:
                words = city_line.split()
                if 1 <= len(words) <= 4 and not re.search(r'[\d@]', city_line) and "?" not in city_line:
                    if extracted_name and city_line.lower() == extracted_name.lower():
                        continue
                    if any(k in city_line.lower() for k in ["mes", "semana", "día", "año"]):
                        continue
                    word_set = {w.rstrip("?!.,;:").lower() for w in city_line.split()}
                    if word_set & _CITY_NOISE:
                        continue
                    return city_line

    return None


def _extract_payment(text: str) -> Optional[str]:
    """Extract payment intent from message."""
    msg = (text or "").lower()
    neg_credit = [
        r"\bno\b.{0,15}\b(crédito|credito|financiamiento|financiación|mensualidades)\b",
        r"\bsin\b.{0,15}\b(crédito|credito|financiamiento|financiación)\b",
    ]
    neg_cash = [
        r"\bno\b.{0,15}\b(contado|cash)\b",
        r"\bsin\b.{0,15}\b(contado|cash)\b",
    ]
    if any(re.search(p, msg) for p in neg_credit):
        return "Contado"
    if any(re.search(p, msg) for p in neg_cash):
        return "Crédito"
    if any(k in msg for k in ["contado", "cash", "de contado"]):
        return "Contado"
    if any(k in msg for k in ["crédito", "credito", "financiamiento", "financiación", "mensualidades"]):
        return "Crédito"
    return None


def _extract_appointment(text: str) -> Optional[str]:
    """Basic Spanish appointment extractor for day/time."""
    t = (text or "").strip().lower()
    if not t:
        return None

    day: Optional[str] = None
    if "mañana" in t and "por la mañana" not in t and "en la mañana" not in t:
        day = "Mañana"
    else:
        for d in ["lunes", "martes", "miércoles", "miercoles", "jueves", "viernes", "sábado", "sabado", "domingo"]:
            if d in t:
                day = d.capitalize().replace("Miercoles", "Miércoles").replace("Sabado", "Sábado")
                break

    time_str: Optional[str] = None
    if "medio dia" in t or "mediodía" in t or "medio día" in t:
        time_str = "12:00"
    if not time_str:
        m = re.search(r"\b(\d{1,2})\s*y\s*media\b", t)
        if m:
            time_str = f"{int(m.group(1))}:30"
    if not time_str:
        m = re.search(r"\b(\d{1,2})\s*:\s*(\d{2})\b", t)
        if m:
            h, mm = int(m.group(1)), int(m.group(2))
            if 0 <= h <= 23 and 0 <= mm <= 59:
                time_str = f"{h}:{mm:02d}"
    if not time_str:
        m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", t)
        if m:
            h = int(m.group(1))
            if 1 <= h <= 12:
                hh = h % 12 + (12 if m.group(2) == "pm" else 0)
                time_str = f"{hh}:00"
    if not time_str:
        if "en la tarde" in t or "por la tarde" in t:
            time_str = "(tarde)"
        elif "en la mañana" in t or "por la mañana" in t:
            time_str = "(mañana)"
        elif "en la noche" in t or "por la noche" in t:
            time_str = "(noche)"

    def _fmt(h24: int, mm: str) -> str:
        if h24 == 0: return f"12:{mm} AM"
        if 1 <= h24 <= 11: return f"{h24}:{mm} AM"
        if h24 == 12: return f"12:{mm} PM"
        return f"{h24 - 12}:{mm} PM"

    if day and time_str:
        if re.fullmatch(r"\d{1,2}:\d{2}", time_str):
            return f"{day} {_fmt(int(time_str.split(':')[0]), time_str.split(':')[1])}"
        return f"{day} {time_str}"
    if day:
        return day
    if time_str:
        if re.fullmatch(r"\d{1,2}:\d{2}", time_str):
            return _fmt(int(time_str.split(":")[0]), time_str.split(":")[1])
        return time_str
    return None


def _format_offer_amount(raw: str) -> Optional[str]:
    """Normalize a numeric offer string into MXN display format."""
    digits = re.sub(r"[^\d]", "", raw or "")
    if not digits.isdigit():
        return None

    val = int(digits)
    if val <= 0:
        return None

    # In these campaign chats, short amounts like "700" mean $700,000.
    if val < 10000:
        val *= 1000

    # Guard against phone numbers or absurdly large accidental values.
    if val < 100000 or val > 100000000:
        return None

    return f"${val:,}"


def _extract_offer(text: str, history: str = "") -> Optional[str]:
    """Extract offer amount from message.

    Supports:
    - Explicit phrases: "te doy 670 mil", "propuesta de 688000"
    - Contextual bare numbers after the bot asks for the offer: "688000", "700"
    """
    msg = (text or "").strip()
    if not msg:
        return None

    m = re.search(
        r'(?:(?:(?:te\s+)?(?:doy|ofrezco|propongo|pongo)|(?:quiero|puedo|voy\s+a)\s+dar|propuesta|oferta|monto)\s*(?:de\s+)?\$?\s*(\d[\d,\.]*)\s*(?:mil|k|pesos?)?)'
        r'|\$?\s*(\d[\d,\.]*)\s*(?:mil|k|pesos?)\b',
        msg, re.IGNORECASE
    )
    if m:
        formatted = _format_offer_amount(m.group(1) or m.group(2) or "")
        if formatted:
            return formatted

    # If the bot just asked for the proposal amount, accept a bare numeric reply.
    last_bot = ""
    if history:
        for line in reversed(history.split("\n")):
            if line.strip().startswith("A:"):
                last_bot = line.lower()
                break

    offer_asking = ["propuesta", "oferta", "monto", "cuánto sería", "cuanto sería"]
    bot_asked_offer = any(k in last_bot for k in offer_asking)
    if bot_asked_offer:
        contextual_amount = re.search(
            r'(?:^|(?:que\s+)?(?:(?:son|es|sería[n]?)\s+)?)\$?\s*(\d[\d,\.\s]{0,9})\s*(?:pesos?)?',
            msg,
            re.IGNORECASE,
        )
        if contextual_amount:
            return _format_offer_amount(contextual_amount.group(1))

    return None


def _extract_timeline(text: str, history: str) -> Optional[str]:
    """Extract timeline/liquidation period from message."""
    t = (text or "").lower().strip()
    if not t:
        return None

    # Check if bot asked for timeline
    last_bot = ""
    if history:
        for line in reversed(history.split("\n")):
            if line.strip().startswith("A:"):
                last_bot = line.lower()
                break

    timeline_asking = ["tiempo", "liquidar", "plazo", "estimado"]
    bot_asked = any(k in last_bot for k in timeline_asking)

    # Explicit patterns
    patterns = [
        r'(\d+)\s*(?:meses?|mes)',
        r'(\d+)\s*(?:semanas?)',
        r'(\d+)\s*(?:días?|dias?)',
        r'(\d+)\s*(?:años?|anios?)',
    ]
    for p in patterns:
        m = re.search(p, t)
        if m:
            return m.group(0).strip()

    # Keywords
    if any(k in t for k in ["inmediato", "inmediatamente", "ya", "lo antes posible", "cuanto antes"]):
        return "Inmediato"

    # If bot asked and reply is short, accept it as timeline
    # BUT reject bare deny/confirm words — those are intents, not timelines
    _timeline_reject = {"no", "si", "sí", "ok", "ya", "eh", "va", "dale", "nel", "nah", "bueno"}
    # Words that indicate hesitation/appointment intent, NOT a time period
    _timeline_context_reject = {
        "primero", "antes", "ver", "verlo", "visitar", "ir",
        "puja", "pero", "entender", "explicar", "saber",
        "momento", "ultimo", "último", "fraude",
    }
    if bot_asked:
        words = t.split()
        if 1 <= len(words) <= 6 and not re.search(r'[@?¿]', t):
            if t.strip() not in _timeline_reject:
                if not any(w in words for w in _timeline_context_reject):
                    return t.strip()

    return None


# ============================================================
# INTENT CLASSIFIER (keyword-based, deterministic)
# ============================================================

_GREETING_WORDS = {"hola", "buenas", "buenos", "hey", "hi", "buen dia", "buenas tardes", "buenas noches", "qué tal"}
_CONFIRM_WORDS = {"si", "sí", "dale", "ok", "okay", "este mismo", "ese", "claro", "va", "perfecto", "eso", "correcto", "exacto", "afirmativo", "listo"}
_DENY_WORDS = {"no", "nel", "nah", "no gracias", "no me interesa"}
_WAIT_WORDS = {"luego", "déjame ver", "dejame ver", "después", "despues", "ocupado", "ahorita no", "más tarde", "mas tarde", "lo pienso"}
_DISINTEREST_WORDS = {"no me interesa", "ya no", "no quiero", "no gracias ya", "dejalo", "déjalo", "olvidalo", "olvídalo"}
_TRUST_WORDS = {
    "fraude", "estafa", "permiso", "gobernación", "gobernacion",
    "ilegal", "confiable", "derecho", "que me asegura", "qué me asegura",
    "garantiza", "oficial", "legítimo", "legitimo", "autorizado", "certificado",
    "seguro que",
}
_PHOTO_WORDS = {"foto", "fotos", "imagen", "imagenes", "mándame", "mandame", "envíame", "enviame", "otra foto", "más fotos"}
_PDF_WORDS = {"ficha", "ficha técnica", "ficha tecnica", "specs", "corrida", "simulación", "simulacion"}
_FINANCING_WORDS = {"financiamiento", "crédito", "credito", "mensualidad", "enganche", "plazo", "mensual"}
_LOCATION_WORDS = {"ubicación", "ubicacion", "dónde", "donde", "dirección", "direccion", "mapa"}
_APPOINTMENT_WORDS = {"cita", "visita", "agendar", "cuándo", "cuando puedo"}
_INVENTORY_WORDS = {"más camiones", "mas camiones", "más tractos", "mas tractos", "más opciones", "mas opciones",
                    "otros modelos", "qué más", "que mas", "qué tienen", "que tienen", "tienen más", "tienen mas"}

# Offer patterns: amounts like "670 mil", "te doy 700", "$650,000"
_OFFER_PATTERN = re.compile(
    r'(?:te\s+(?:doy|ofrezco|propongo)|propuesta|oferta|monto|precio)\s*'
    r'(?:de\s+)?\$?\s*(\d[\d,\.]*)\s*(?:mil|pesos|k)?'
    r'|'
    r'\$?\s*(\d[\d,\.]*)\s*(?:mil|k)\b',
    re.IGNORECASE
)


def classify_intent(
    message: str,
    slots: "Slots",
    last_action: Optional[Action] = None,
    new_data: Optional[Dict[str, str]] = None,
    current_state: Optional[ConversationState] = None,
    has_campaign: bool = False,
) -> Intent:
    """
    Deterministic intent classification from message keywords.
    Now context-aware: uses current_state and has_campaign to disambiguate.
    """
    msg = message.lower().strip()
    words_set = set(msg.split())

    # Check disinterest first (strong signal)
    if any(w in msg for w in _DISINTEREST_WORDS):
        return Intent.DISINTEREST

    # Check trust/fraud concern (high priority — before data extraction)
    if any(w in msg for w in _TRUST_WORDS):
        return Intent.TRUST_CONCERN

    # Check wait/pause
    if any(w in msg for w in _WAIT_WORDS):
        return Intent.WAIT

    # Check if providing data (in response to a previous ask)
    if new_data and any(new_data.get(k) for k in ("name", "email", "city", "phone", "offer_amount", "timeline", "appointment")):
        has_question = "?" in msg
        if not has_question:
            return Intent.PROVIDE_DATA
        # Data + question: in campaign/collecting states, prioritize data
        if current_state in (ConversationState.CAMPAIGN_ENTRY, ConversationState.COLLECTING_DATA):
            return Intent.PROVIDE_DATA

    # Check offer (campaign context — only meaningful in campaign states)
    if _OFFER_PATTERN.search(msg):
        return Intent.MAKE_OFFER

    # Check confirm/deny — context-aware
    stripped = msg.rstrip("?!., ")
    if msg in _CONFIRM_WORDS or stripped in _CONFIRM_WORDS:
        # "sí" in CAMPAIGN_ENTRY = confirm participation
        # "sí" in CATALOG_BROWSING = might mean something else
        return Intent.CONFIRM
    if msg in _DENY_WORDS or stripped in _DENY_WORDS:
        # Simple "no" in campaign = deny, but "no gracias" = could be disinterest
        if stripped == "no" and current_state == ConversationState.CAMPAIGN_ENTRY:
            return Intent.DENY
        if stripped in ("no gracias", "no me interesa"):
            return Intent.DISINTEREST
        return Intent.DENY

    # Check specific intents — location before photos to avoid "verla" matching "ver"
    if any(w in msg for w in _LOCATION_WORDS):
        return Intent.ASK_LOCATION
    if any(w in msg for w in _PHOTO_WORDS):
        return Intent.ASK_PHOTOS
    if any(w in msg for w in _PDF_WORDS):
        return Intent.ASK_PDF
    if any(w in msg for w in _FINANCING_WORDS):
        return Intent.ASK_FINANCING
    if any(w in msg for w in _APPOINTMENT_WORDS):
        return Intent.ASK_APPOINTMENT
    if any(w in msg for w in _INVENTORY_WORDS):
        return Intent.ASK_INVENTORY
    if any(w in msg for w in ("precio", "costo", "cuánto", "cuanto", "vale")):
        return Intent.ASK_PRICE

    # If new data was provided along with a question
    if new_data and any(new_data.get(k) for k in ("name", "email", "city")):
        return Intent.PROVIDE_DATA

    # Greeting (only if no other intent and early in conversation)
    if any(w in words_set for w in _GREETING_WORDS) and len(msg.split()) <= 5:
        return Intent.GREETING

    # Default
    return Intent.ASK_QUESTION


# ============================================================
# ACTION PLANNER (deterministic rules)
# ============================================================

# Campaign required slots in order of collection
_CAMPAIGN_SLOT_ORDER = ["name", "email", "city", "timeline"]
# Special campaign types (SU/LQ/PR/EV) also require offer_amount
_CAMPAIGN_OFFER_TYPES = {"SU", "LQ", "PR", "EV"}

# Slot → Action mapping
_SLOT_TO_ACTION = {
    "name": Action.ASK_NAME,
    "email": Action.ASK_EMAIL,
    "city": Action.ASK_CITY,
    "timeline": Action.ASK_TIMELINE,
    "offer_amount": Action.ASK_OFFER,
}


def decide_action(
    state: ConversationState,
    slots: Slots,
    intent: Intent,
    new_data: Dict[str, str],
    has_campaign: bool,
    turn_count: int,
    campaign_type: str = "A",
    form_url: str = "",
) -> Tuple[Action, ConversationState, Dict[str, Any]]:
    """
    Pure deterministic function. NO LLM calls.
    Returns (action, new_state, metadata).

    metadata includes:
      - primary_flow: the main conversation track (campaign_registration, lead_qualification, browsing)
      - is_side_question: True if the user asked something lateral without changing flow
    """
    meta: Dict[str, Any] = {}

    # Determine primary flow
    if has_campaign:
        meta["primary_flow"] = "campaign_registration"
    elif slots.interest and not slots.appointment:
        meta["primary_flow"] = "lead_qualification"
    elif slots.interest and slots.appointment:
        meta["primary_flow"] = "qualified"
    else:
        meta["primary_flow"] = "browsing"

    # Helper to return meta with primary_flow preserved
    def _ret(action: Action, new_state: ConversationState, extra: Optional[Dict[str, Any]] = None) -> Tuple[Action, ConversationState, Dict[str, Any]]:
        result = dict(meta)
        if extra:
            result.update(extra)
        return action, new_state, result

    # ---- DISINTEREST: overrides everything ----
    if intent == Intent.DISINTEREST:
        return _ret(Action.WAIT_MODE, ConversationState.WAITING, {"is_disinterest": True})

    # ---- WAIT: client wants to pause ----
    if intent == Intent.WAIT:
        return _ret(Action.WAIT_MODE, ConversationState.WAITING)

    # ---- GREETING state ----
    if state == ConversationState.GREETING:
        if has_campaign:
            extra: Dict[str, Any] = {}
            if form_url:
                extra["form_url"] = form_url
            return _ret(Action.PRESENT_CAMPAIGN, ConversationState.CAMPAIGN_ENTRY, extra or None)
        else:
            return _ret(Action.GREET, ConversationState.INTEREST_DISCOVERY)

    # ---- CAMPAIGN_ENTRY state ----
    if state == ConversationState.CAMPAIGN_ENTRY:
        if intent in (Intent.ASK_PHOTOS, Intent.ASK_PDF):
            action = Action.SEND_PHOTOS if intent == Intent.ASK_PHOTOS else Action.SEND_PDF
            return _ret(action, ConversationState.CAMPAIGN_ENTRY)

        if intent == Intent.ASK_INVENTORY:
            return _ret(Action.SHOW_INVENTORY, ConversationState.CATALOG_BROWSING, {"is_side_question": True})

        if intent == Intent.MODEL_SWITCH:
            return _ret(Action.ACKNOWLEDGE_SWITCH, ConversationState.INTEREST_DISCOVERY)

        if intent in (Intent.ASK_QUESTION, Intent.ASK_PRICE, Intent.ASK_FINANCING,
                       Intent.ASK_LOCATION, Intent.ASK_APPOINTMENT, Intent.TRUST_CONCERN):
            meta_extra: Dict[str, Any] = {"is_trust_concern": True} if intent == Intent.TRUST_CONCERN else {}
            if form_url:
                # With a form, mention the link after answering instead of asking for the next slot
                meta_extra["form_url"] = form_url
            elif intent != Intent.TRUST_CONCERN:
                # Sandwich: after answering, naturally ask for next missing slot
                # (except trust concerns — give the client space to settle doubts first)
                _missing = _get_campaign_missing(slots, campaign_type)
                if _missing:
                    meta_extra["sandwich_next"] = _missing[0]
            return _ret(Action.ANSWER_QUESTION, ConversationState.CAMPAIGN_ENTRY,
                        {"is_side_question": True, **meta_extra})

        # DENY in campaign: respond commercially (destrabar), stay in campaign
        if intent == Intent.DENY:
            return _ret(Action.SOFT_DENY, ConversationState.CAMPAIGN_ENTRY)

        # --- Form-based registration: skip all slot collection ---
        if form_url:
            return _ret(Action.SEND_FORM, ConversationState.CAMPAIGN_ENTRY, {"form_url": form_url})

        # Check if data was provided or offer made
        if intent in (Intent.PROVIDE_DATA, Intent.MAKE_OFFER, Intent.CONFIRM):
            missing = _get_campaign_missing(slots, campaign_type)
            if not missing:
                return _ret(Action.CONFIRM_REGISTRATION, ConversationState.QUALIFIED)
            next_slot = missing[0]
            return _ret(Action.ACKNOWLEDGE_AND_ASK_NEXT, ConversationState.CAMPAIGN_ENTRY, {
                "next_slot": next_slot,
                "acknowledged_data": new_data,
            })

        # Default for campaign: check what's missing and ask
        missing = _get_campaign_missing(slots, campaign_type)
        if not missing:
            return _ret(Action.CONFIRM_REGISTRATION, ConversationState.QUALIFIED)
        next_slot = missing[0]
        return _ret(_SLOT_TO_ACTION.get(next_slot, Action.ASK_NAME), ConversationState.CAMPAIGN_ENTRY, {
            "next_slot": next_slot,
        })

    # ---- INTEREST_DISCOVERY state ----
    if state == ConversationState.INTEREST_DISCOVERY:
        if intent == Intent.ASK_PHOTOS:
            return _ret(Action.SEND_PHOTOS, ConversationState.INTEREST_DISCOVERY)
        if intent == Intent.ASK_PDF:
            return _ret(Action.SEND_PDF, ConversationState.INTEREST_DISCOVERY)
        if intent == Intent.ASK_INVENTORY:
            return _ret(Action.SHOW_INVENTORY, ConversationState.CATALOG_BROWSING)

        if slots.interest and not slots.name:
            return _ret(Action.ASK_NAME, ConversationState.COLLECTING_DATA)
        if not slots.interest:
            if intent in (Intent.ASK_QUESTION, Intent.ASK_PRICE, Intent.ASK_FINANCING):
                return _ret(Action.ANSWER_QUESTION, ConversationState.INTEREST_DISCOVERY)
            return _ret(Action.ASK_INTEREST, ConversationState.INTEREST_DISCOVERY)

        return _ret(Action.ANSWER_QUESTION, ConversationState.INTEREST_DISCOVERY)

    # ---- COLLECTING_DATA state ----
    if state == ConversationState.COLLECTING_DATA:
        if intent in (Intent.ASK_PHOTOS, Intent.ASK_PDF):
            action = Action.SEND_PHOTOS if intent == Intent.ASK_PHOTOS else Action.SEND_PDF
            return _ret(action, ConversationState.COLLECTING_DATA)

        if intent == Intent.ASK_INVENTORY:
            return _ret(Action.SHOW_INVENTORY, ConversationState.CATALOG_BROWSING, {"is_side_question": True})

        if intent in (Intent.ASK_QUESTION, Intent.ASK_PRICE, Intent.ASK_FINANCING,
                       Intent.ASK_LOCATION):
            return _ret(Action.ANSWER_QUESTION, ConversationState.COLLECTING_DATA, {"is_side_question": True})

        if intent == Intent.ASK_APPOINTMENT:
            if slots.name:
                return _ret(Action.ASK_APPOINTMENT, ConversationState.APPOINTMENT_SCHEDULING)
            else:
                # Answer the appointment question FIRST, then ask for name (sandwich pattern)
                return _ret(Action.ANSWER_QUESTION, ConversationState.COLLECTING_DATA, {
                    "is_side_question": True,
                    "sandwich_next": "name",
                })

        if not slots.missing_for_lead():
            return _ret(Action.CONFIRM_LEAD, ConversationState.QUALIFIED)

        if intent == Intent.PROVIDE_DATA and new_data:
            missing = slots.missing_for_lead()
            if not missing:
                return _ret(Action.CONFIRM_LEAD, ConversationState.QUALIFIED)
            return _ret(Action.ACKNOWLEDGE_AND_ASK_NEXT, ConversationState.COLLECTING_DATA, {
                "next_slot": missing[0],
                "acknowledged_data": new_data,
            })

        missing = slots.missing_for_lead()
        if missing:
            next_slot = missing[0]
            return _ret(_SLOT_TO_ACTION.get(next_slot, Action.ANSWER_QUESTION), ConversationState.COLLECTING_DATA)

        return _ret(Action.ANSWER_QUESTION, ConversationState.COLLECTING_DATA)

    # ---- APPOINTMENT_SCHEDULING state ----
    if state == ConversationState.APPOINTMENT_SCHEDULING:
        if slots.appointment:
            return _ret(Action.CONFIRM_LEAD, ConversationState.QUALIFIED)
        return _ret(Action.ASK_APPOINTMENT, ConversationState.APPOINTMENT_SCHEDULING)

    # ---- CATALOG_BROWSING state ----
    if state == ConversationState.CATALOG_BROWSING:
        if intent == Intent.ASK_PHOTOS:
            return _ret(Action.SEND_PHOTOS, ConversationState.CATALOG_BROWSING)
        if intent == Intent.ASK_PDF:
            return _ret(Action.SEND_PDF, ConversationState.CATALOG_BROWSING)
        if intent == Intent.ASK_APPOINTMENT:
            if slots.name:
                return _ret(Action.ASK_APPOINTMENT, ConversationState.APPOINTMENT_SCHEDULING)
            return _ret(Action.ASK_NAME, ConversationState.COLLECTING_DATA, {"reason": "need_name_for_appointment"})
        return _ret(Action.ANSWER_QUESTION, ConversationState.CATALOG_BROWSING)

    # ---- QUALIFIED state (keep answering questions) ----
    if state == ConversationState.QUALIFIED:
        if intent == Intent.ASK_PHOTOS:
            return _ret(Action.SEND_PHOTOS, ConversationState.QUALIFIED)
        # Campaign with form: CONFIRM → send form; other intents → answer and remind link
        if has_campaign and form_url:
            if intent == Intent.CONFIRM:
                return _ret(Action.SEND_FORM, ConversationState.CAMPAIGN_ENTRY, {"form_url": form_url})
            return _ret(Action.ANSWER_QUESTION, ConversationState.QUALIFIED, {"is_side_question": True})
        return _ret(Action.ANSWER_QUESTION, ConversationState.QUALIFIED)

    # ---- WAITING state ----
    if state == ConversationState.WAITING:
        if intent == Intent.GREETING:
            return _ret(Action.GREET, ConversationState.INTEREST_DISCOVERY)
        return _ret(Action.ANSWER_QUESTION, ConversationState.INTEREST_DISCOVERY)

    # Fallback
    return _ret(Action.ANSWER_QUESTION, ConversationState.INTEREST_DISCOVERY)


def _get_campaign_missing(slots: Slots, campaign_type: str = "A") -> List[str]:
    """Returns missing campaign slots in collection order.
    For SU/LQ/PR/EV campaigns, offer_amount is required.
    """
    missing = []
    for slot_name in _CAMPAIGN_SLOT_ORDER:
        if not getattr(slots, slot_name, None):
            missing.append(slot_name)
    # Special campaigns require offer_amount
    if campaign_type.upper() in _CAMPAIGN_OFFER_TYPES and not slots.offer_amount:
        missing.append("offer_amount")
    return missing


# ============================================================
# STATE RESOLUTION (from existing context)
# ============================================================

def resolve_state(
    context: Dict[str, Any],
    slots: Slots,
    has_campaign: bool,
    turn_count: int,
) -> ConversationState:
    """
    Resolve current conversation state from context + slots.
    Used for existing conversations that started before FSM was added.
    """
    # Check if state is already stored
    stored = context.get("fsm_state")
    if stored:
        try:
            return ConversationState(stored)
        except ValueError:
            pass  # Invalid stored state, resolve from data

    # Resolve from data
    if turn_count <= 1:
        return ConversationState.GREETING

    if has_campaign and not slots.name:
        return ConversationState.CAMPAIGN_ENTRY

    if has_campaign and slots.name:
        campaign_type = context.get("tracking_data", {}).get("campaign_type", "A")
        missing = _get_campaign_missing(slots, campaign_type)
        if missing:
            return ConversationState.CAMPAIGN_ENTRY
        return ConversationState.QUALIFIED

    if not slots.interest:
        return ConversationState.INTEREST_DISCOVERY

    if slots.interest and slots.appointment:
        return ConversationState.QUALIFIED

    if slots.interest and slots.name:
        return ConversationState.COLLECTING_DATA

    if slots.interest:
        return ConversationState.COLLECTING_DATA

    return ConversationState.INTEREST_DISCOVERY


# ============================================================
# MAIN ENTRY POINT
# ============================================================

def process_fsm(
    user_message: str,
    context: Dict[str, Any],
    new_data: Dict[str, str],
    has_campaign: bool,
    turn_count: int,
    campaign_type: str = "A",
    form_url: str = "",
) -> Tuple[Action, ConversationState, Slots, Dict[str, Any]]:
    """
    Main FSM entry point. Called from handle_message().

    Args:
        user_message: The user's message
        context: Conversation context dict
        new_data: Freshly extracted data from this turn (name, email, city, etc.)
        has_campaign: Whether a campaign is active for this conversation
        turn_count: Current turn number

    Returns:
        (action, new_state, slots, metadata)
        metadata now includes:
          - slot_changes: List[SlotChange] of what changed this turn
          - primary_flow: str
          - is_side_question: bool (optional)
    """
    # Snapshot old slots BEFORE applying new data
    old_slots = Slots.from_context(context)

    # Load + apply new data
    slots = Slots.from_context(context)
    for key, value in new_data.items():
        if value and hasattr(slots, key):
            setattr(slots, key, value)

    # Resolve current state
    state = resolve_state(context, slots, has_campaign, turn_count)

    # Classify intent (now context-aware)
    last_action_str = context.get("last_action")
    last_action = None
    if last_action_str:
        try:
            last_action = Action(last_action_str)
        except ValueError:
            pass

    intent = classify_intent(
        user_message, slots, last_action, new_data,
        current_state=state, has_campaign=has_campaign,
    )

    # Decide action
    action, new_state, meta = decide_action(
        state=state,
        slots=slots,
        intent=intent,
        new_data=new_data,
        has_campaign=has_campaign,
        turn_count=turn_count,
        campaign_type=campaign_type,
        form_url=form_url,
    )

    # Store intent in meta so conversation_logic can use it
    meta["intent"] = intent.value

    # Compute slot changes for this turn
    changes = diff_slots(old_slots, slots)
    meta["slot_changes"] = changes

    if changes:
        change_strs = [f"{c.slot}: {c.old_value!r} → {c.new_value!r}" for c in changes]
        logger.info(f"📊 Slot changes: {', '.join(change_strs)}")

    logger.info(
        f"🔀 FSM: {state.value} → {new_state.value} | "
        f"intent={intent.value} | action={action.value} | "
        f"flow={meta.get('primary_flow', '?')} | "
        f"slots_filled={slots.filled_summary()}"
    )

    # Store state and action in context for next turn
    context["fsm_state"] = new_state.value
    context["last_action"] = action.value

    # Write slots back to context
    slots.update_context(context)

    return action, new_state, slots, meta


# ============================================================
# LEGACY VALUE VALIDATION — guard against dirty fallback data
# ============================================================

def validate_legacy_value(slot: str, value: Optional[str]) -> Optional[str]:
    """
    Validate a legacy-extracted value before it enters the FSM.
    Returns the value if valid, None if it smells like noise.

    This prevents dirty data from the legacy extraction pipeline
    from contaminating FSM slots.
    """
    if not value or not value.strip():
        return None

    v = value.strip()

    if slot == "city":
        # Reject cities that contain vehicle/ad noise
        words = {w.rstrip("?!.,;:").lower() for w in v.split()}
        if words & _CITY_NOISE:
            logger.info(f"🛡️ Legacy city rejected (noise): {v}")
            return None
        # Reject if too short or looks like a common word
        if len(v) <= 2 or v.lower() in {"si", "no", "ok", "ya", "va"}:
            return None
        # Reject if contains digits
        if re.search(r'\d', v):
            logger.info(f"🛡️ Legacy city rejected (digits): {v}")
            return None

    elif slot == "phone":
        # Must be 10-15 digits
        digits = re.sub(r'\D', '', v)
        if not (10 <= len(digits) <= 15):
            return None
        return digits

    elif slot == "appointment":
        # Must contain a day word or time pattern
        v_lower = v.lower()
        day_words = {"lunes", "martes", "miércoles", "miercoles", "jueves", "viernes",
                     "sábado", "sabado", "domingo", "mañana", "hoy"}
        time_pattern = re.search(r'\d{1,2}[:hH]\d{0,2}|\d{1,2}\s*(am|pm)|medio\s*d[ií]a|tarde|mañana|noche', v_lower)
        has_day = any(d in v_lower for d in day_words)
        if not has_day and not time_pattern:
            logger.info(f"🛡️ Legacy appointment rejected (no day/time): {v}")
            return None

    elif slot == "payment":
        # Must be one of the known labels
        v_lower = v.lower()
        valid = {"contado", "crédito", "credito", "financiamiento", "cash"}
        if not any(k in v_lower for k in valid):
            return None

    elif slot == "name":
        # Reject names that are just vehicle/business words
        words = v.lower().split()
        if any(w in _CITY_NOISE for w in words):
            return None
        if any(w in _NAME_BAD_WORDS for w in words) and len(words) <= 1:
            return None

    return v
