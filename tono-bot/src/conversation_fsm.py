"""
Conversation FSM — State machine + slot manager + action planner.

Separates decision logic from LLM text generation.
The LLM only writes; rules decide.
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
# INTENT CLASSIFIER (keyword-based, deterministic)
# ============================================================

_GREETING_WORDS = {"hola", "buenas", "buenos", "hey", "hi", "buen dia", "buenas tardes", "buenas noches", "qué tal"}
_CONFIRM_WORDS = {"si", "sí", "dale", "ok", "okay", "este mismo", "ese", "claro", "va", "perfecto", "eso", "correcto", "exacto", "afirmativo"}
_DENY_WORDS = {"no", "nel", "nah", "no gracias", "no me interesa"}
_WAIT_WORDS = {"luego", "déjame ver", "dejame ver", "después", "despues", "ocupado", "ahorita no", "más tarde", "mas tarde", "lo pienso"}
_DISINTEREST_WORDS = {"no me interesa", "ya no", "no quiero", "no gracias ya", "dejalo", "déjalo", "olvidalo", "olvídalo"}
_PHOTO_WORDS = {"foto", "fotos", "imagen", "imagenes", "ver", "mándame", "mandame", "envíame", "enviame", "otra foto", "más fotos"}
_PDF_WORDS = {"ficha", "ficha técnica", "ficha tecnica", "specs", "corrida", "simulación", "simulacion"}
_FINANCING_WORDS = {"financiamiento", "crédito", "credito", "mensualidad", "enganche", "plazo", "mensual"}
_LOCATION_WORDS = {"ubicación", "ubicacion", "dónde", "donde", "dirección", "direccion", "mapa"}
_APPOINTMENT_WORDS = {"cita", "visita", "ir", "agendar", "cuándo", "cuando puedo"}
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
) -> Intent:
    """
    Deterministic intent classification from message keywords.
    Returns the most likely intent.
    """
    msg = message.lower().strip()
    words_set = set(msg.split())

    # Check disinterest first (strong signal)
    if any(w in msg for w in _DISINTEREST_WORDS):
        return Intent.DISINTEREST

    # Check wait/pause
    if any(w in msg for w in _WAIT_WORDS):
        return Intent.WAIT

    # Check if providing data (in response to a previous ask)
    if new_data and any(new_data.get(k) for k in ("name", "email", "city", "phone", "offer_amount", "timeline")):
        # If they also asked something, prioritize the data provision
        # but check if there's also a question
        has_question = "?" in msg
        if not has_question:
            return Intent.PROVIDE_DATA

    # Check offer (campaign context)
    if _OFFER_PATTERN.search(msg):
        return Intent.MAKE_OFFER

    # Check confirm/deny
    if msg in _CONFIRM_WORDS or msg.rstrip("?!., ") in _CONFIRM_WORDS:
        return Intent.CONFIRM
    if msg in _DENY_WORDS or msg.rstrip("?!., ") in _DENY_WORDS:
        return Intent.DENY

    # Check specific intents
    if any(w in msg for w in _PHOTO_WORDS):
        return Intent.ASK_PHOTOS
    if any(w in msg for w in _PDF_WORDS):
        return Intent.ASK_PDF
    if any(w in msg for w in _FINANCING_WORDS):
        return Intent.ASK_FINANCING
    if any(w in msg for w in _LOCATION_WORDS):
        return Intent.ASK_LOCATION
    if any(w in msg for w in _APPOINTMENT_WORDS):
        return Intent.ASK_APPOINTMENT
    if any(w in msg for w in _INVENTORY_WORDS):
        return Intent.ASK_INVENTORY
    if any(w in msg for w in ("precio", "costo", "cuánto", "cuanto", "vale")):
        return Intent.ASK_PRICE

    # If new data was provided along with a question
    if new_data and any(new_data.get(k) for k in ("name", "email", "city")):
        return Intent.PROVIDE_DATA

    # Greeting (only if no other intent)
    if any(w in words_set for w in _GREETING_WORDS) and len(msg.split()) <= 5:
        return Intent.GREETING

    # Default
    return Intent.ASK_QUESTION


# ============================================================
# ACTION PLANNER (deterministic rules)
# ============================================================

# Campaign required slots in order of collection
_CAMPAIGN_SLOT_ORDER = ["name", "email", "city", "timeline"]

# Slot → Action mapping
_SLOT_TO_ACTION = {
    "name": Action.ASK_NAME,
    "email": Action.ASK_EMAIL,
    "city": Action.ASK_CITY,
    "timeline": Action.ASK_TIMELINE,
}


def decide_action(
    state: ConversationState,
    slots: Slots,
    intent: Intent,
    new_data: Dict[str, str],
    has_campaign: bool,
    turn_count: int,
) -> Tuple[Action, ConversationState, Dict[str, Any]]:
    """
    Pure deterministic function. NO LLM calls.
    Returns (action, new_state, metadata).
    """
    meta: Dict[str, Any] = {}

    # ---- DISINTEREST: overrides everything ----
    if intent == Intent.DISINTEREST:
        return Action.WAIT_MODE, ConversationState.WAITING, {"is_disinterest": True}

    # ---- WAIT: client wants to pause ----
    if intent == Intent.WAIT:
        return Action.WAIT_MODE, ConversationState.WAITING, {}

    # ---- GREETING state ----
    if state == ConversationState.GREETING:
        if has_campaign:
            return Action.PRESENT_CAMPAIGN, ConversationState.CAMPAIGN_ENTRY, {}
        else:
            return Action.GREET, ConversationState.INTEREST_DISCOVERY, {}

    # ---- CAMPAIGN_ENTRY state ----
    if state == ConversationState.CAMPAIGN_ENTRY:
        # Handle questions during campaign (don't lose state)
        if intent in (Intent.ASK_PHOTOS, Intent.ASK_PDF):
            action = Action.SEND_PHOTOS if intent == Intent.ASK_PHOTOS else Action.SEND_PDF
            return action, ConversationState.CAMPAIGN_ENTRY, {}

        if intent == Intent.ASK_INVENTORY:
            return Action.SHOW_INVENTORY, ConversationState.CATALOG_BROWSING, {"side_question": True}

        if intent == Intent.MODEL_SWITCH:
            return Action.ACKNOWLEDGE_SWITCH, ConversationState.INTEREST_DISCOVERY, {}

        if intent in (Intent.ASK_QUESTION, Intent.ASK_PRICE, Intent.ASK_FINANCING,
                       Intent.ASK_LOCATION):
            return Action.ANSWER_QUESTION, ConversationState.CAMPAIGN_ENTRY, {}

        # Check if data was provided or offer made
        if intent in (Intent.PROVIDE_DATA, Intent.MAKE_OFFER, Intent.CONFIRM):
            # Determine next missing slot
            missing = _get_campaign_missing(slots)
            if not missing:
                return Action.CONFIRM_REGISTRATION, ConversationState.QUALIFIED, {}
            # Acknowledge what was given, ask for next
            next_slot = missing[0]
            return Action.ACKNOWLEDGE_AND_ASK_NEXT, ConversationState.CAMPAIGN_ENTRY, {
                "next_slot": next_slot,
                "acknowledged_data": new_data,
            }

        # Default for campaign: check what's missing and ask
        missing = _get_campaign_missing(slots)
        if not missing:
            return Action.CONFIRM_REGISTRATION, ConversationState.QUALIFIED, {}
        next_slot = missing[0]
        return _SLOT_TO_ACTION.get(next_slot, Action.ASK_NAME), ConversationState.CAMPAIGN_ENTRY, {
            "next_slot": next_slot,
        }

    # ---- INTEREST_DISCOVERY state ----
    if state == ConversationState.INTEREST_DISCOVERY:
        if intent == Intent.ASK_PHOTOS:
            return Action.SEND_PHOTOS, ConversationState.INTEREST_DISCOVERY, {}
        if intent == Intent.ASK_PDF:
            return Action.SEND_PDF, ConversationState.INTEREST_DISCOVERY, {}
        if intent == Intent.ASK_INVENTORY:
            return Action.SHOW_INVENTORY, ConversationState.CATALOG_BROWSING, {}

        # If interest was just detected, start collecting data
        if slots.interest and not slots.name:
            return Action.ASK_NAME, ConversationState.COLLECTING_DATA, {}
        if not slots.interest:
            # Answer their question but guide toward interest
            if intent in (Intent.ASK_QUESTION, Intent.ASK_PRICE, Intent.ASK_FINANCING):
                return Action.ANSWER_QUESTION, ConversationState.INTEREST_DISCOVERY, {}
            return Action.ASK_INTEREST, ConversationState.INTEREST_DISCOVERY, {}

        return Action.ANSWER_QUESTION, ConversationState.INTEREST_DISCOVERY, {}

    # ---- COLLECTING_DATA state ----
    if state == ConversationState.COLLECTING_DATA:
        # Allow questions without losing state
        if intent in (Intent.ASK_PHOTOS, Intent.ASK_PDF):
            action = Action.SEND_PHOTOS if intent == Intent.ASK_PHOTOS else Action.SEND_PDF
            return action, ConversationState.COLLECTING_DATA, {}

        if intent == Intent.ASK_INVENTORY:
            return Action.SHOW_INVENTORY, ConversationState.CATALOG_BROWSING, {"side_question": True}

        if intent in (Intent.ASK_QUESTION, Intent.ASK_PRICE, Intent.ASK_FINANCING,
                       Intent.ASK_LOCATION):
            return Action.ANSWER_QUESTION, ConversationState.COLLECTING_DATA, {}

        if intent == Intent.ASK_APPOINTMENT:
            if slots.name:
                return Action.ASK_APPOINTMENT, ConversationState.APPOINTMENT_SCHEDULING, {}
            else:
                return Action.ASK_NAME, ConversationState.COLLECTING_DATA, {"reason": "need_name_for_appointment"}

        # Check for lead completion
        if not slots.missing_for_lead():
            return Action.CONFIRM_LEAD, ConversationState.QUALIFIED, {}

        # Otherwise, acknowledge and ask next
        if intent == Intent.PROVIDE_DATA and new_data:
            missing = slots.missing_for_lead()
            if not missing:
                return Action.CONFIRM_LEAD, ConversationState.QUALIFIED, {}
            return Action.ACKNOWLEDGE_AND_ASK_NEXT, ConversationState.COLLECTING_DATA, {
                "next_slot": missing[0],
                "acknowledged_data": new_data,
            }

        # Default: ask for next missing
        missing = slots.missing_for_lead()
        if missing:
            next_slot = missing[0]
            return _SLOT_TO_ACTION.get(next_slot, Action.ANSWER_QUESTION), ConversationState.COLLECTING_DATA, {}

        return Action.ANSWER_QUESTION, ConversationState.COLLECTING_DATA, {}

    # ---- APPOINTMENT_SCHEDULING state ----
    if state == ConversationState.APPOINTMENT_SCHEDULING:
        if slots.appointment:
            return Action.CONFIRM_LEAD, ConversationState.QUALIFIED, {}
        return Action.ASK_APPOINTMENT, ConversationState.APPOINTMENT_SCHEDULING, {}

    # ---- CATALOG_BROWSING state ----
    if state == ConversationState.CATALOG_BROWSING:
        if intent == Intent.ASK_PHOTOS:
            return Action.SEND_PHOTOS, ConversationState.CATALOG_BROWSING, {}
        if intent == Intent.ASK_PDF:
            return Action.SEND_PDF, ConversationState.CATALOG_BROWSING, {}
        if intent == Intent.ASK_APPOINTMENT:
            if slots.name:
                return Action.ASK_APPOINTMENT, ConversationState.APPOINTMENT_SCHEDULING, {}
            return Action.ASK_NAME, ConversationState.COLLECTING_DATA, {"reason": "need_name_for_appointment"}
        return Action.ANSWER_QUESTION, ConversationState.CATALOG_BROWSING, {}

    # ---- QUALIFIED state (keep answering questions) ----
    if state == ConversationState.QUALIFIED:
        if intent == Intent.ASK_PHOTOS:
            return Action.SEND_PHOTOS, ConversationState.QUALIFIED, {}
        return Action.ANSWER_QUESTION, ConversationState.QUALIFIED, {}

    # ---- WAITING state ----
    if state == ConversationState.WAITING:
        # Client came back
        if intent == Intent.GREETING:
            return Action.GREET, ConversationState.INTEREST_DISCOVERY, {}
        # Resume from where we were
        return Action.ANSWER_QUESTION, ConversationState.INTEREST_DISCOVERY, {}

    # Fallback
    return Action.ANSWER_QUESTION, ConversationState.INTEREST_DISCOVERY, {}


def _get_campaign_missing(slots: Slots) -> List[str]:
    """Returns missing campaign slots in collection order."""
    missing = []
    if not slots.offer_amount:
        # Don't add to missing — the campaign instructions guide this
        pass
    for slot_name in _CAMPAIGN_SLOT_ORDER:
        if not getattr(slots, slot_name, None):
            missing.append(slot_name)
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
        missing = _get_campaign_missing(slots)
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
    """
    # Load slots from context
    slots = Slots.from_context(context)

    # Apply new data to slots
    for key, value in new_data.items():
        if value and hasattr(slots, key):
            setattr(slots, key, value)

    # Resolve current state
    state = resolve_state(context, slots, has_campaign, turn_count)

    # Classify intent
    last_action_str = context.get("last_action")
    last_action = None
    if last_action_str:
        try:
            last_action = Action(last_action_str)
        except ValueError:
            pass

    intent = classify_intent(user_message, slots, last_action, new_data)

    # Decide action
    action, new_state, meta = decide_action(
        state=state,
        slots=slots,
        intent=intent,
        new_data=new_data,
        has_campaign=has_campaign,
        turn_count=turn_count,
    )

    logger.info(
        f"🔀 FSM: {state.value} → {new_state.value} | "
        f"intent={intent.value} | action={action.value} | "
        f"slots_filled={slots.filled_summary()}"
    )

    # Store state and action in context for next turn
    context["fsm_state"] = new_state.value
    context["last_action"] = action.value

    # Write slots back to context
    slots.update_context(context)

    return action, new_state, slots, meta
