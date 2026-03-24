"""
LLM Writer — Focused text generation for specific actions.

The LLM does NOT decide what to do. It only writes text for a
predetermined action. This keeps responses consistent and predictable.

V2: Adds deterministic templates for repetitive actions (ASK_NAME, ASK_EMAIL,
    ASK_CITY, ASK_TIMELINE, WAIT_MODE, ESCALATE) — these skip the LLM entirely.
"""

import hashlib
import logging
from typing import Any, Dict, List, Optional, Tuple

from src.conversation_fsm import Action, Slots

logger = logging.getLogger(__name__)


# ============================================================
# BASE PERSONALITY (shared across all actions)
# ============================================================
_PERSONALITY = """Eres "Adrian Jimenez", asesor de 'Tractos y Max' (distribuidor de vehículos comerciales).
REGLAS ABSOLUTAS:
- Responde en español
- Máximo 2 oraciones
- Sin emojis
- Tono profesional pero natural
- NUNCA repitas un mensaje anterior (revisa TUS ÚLTIMOS MENSAJES)
- NUNCA pidas un dato que ya tienes (revisa DATOS RECOPILADOS)
- NUNCA inventes vehículos que no estén en el inventario"""


# ============================================================
# ACTION-SPECIFIC PROMPT TEMPLATES
# ============================================================

_ACTION_PROMPTS: Dict[Action, str] = {
    Action.GREET: (
        "ACCIÓN: Saluda al cliente brevemente y pregunta en qué le puedes ayudar.\n"
        "Ejemplo: 'Hola, soy Adrian de Tractos y Max. ¿En qué te puedo apoyar?'"
    ),

    Action.PRESENT_CAMPAIGN: (
        "ACCIÓN: Presenta la campaña al cliente que acaba de llegar.\n"
        "Usa las INSTRUCCIONES DE CAMPAÑA proporcionadas.\n"
        "Sé breve: presenta la dinámica y pregunta si le interesa participar."
    ),

    Action.ACKNOWLEDGE_AND_ASK_NEXT: (
        "ACCIÓN: Reconoce el dato que acaba de dar el cliente y pide el SIGUIENTE dato faltante.\n"
        "DATO NUEVO: {acknowledged_data}\n"
        "DATO QUE FALTA: {next_slot_label}\n"
        "Ejemplo: 'Perfecto, anotado. ¿Me compartes tu {next_slot_label}?'"
    ),

    Action.ASK_NAME: (
        "ACCIÓN: Pide el nombre completo del cliente.\n"
        "Sé natural: '¿Me compartes tu nombre completo, por favor?'"
    ),

    Action.ASK_EMAIL: (
        "ACCIÓN: Pide el correo electrónico del cliente.\n"
        "Sé natural: '¿Me compartes tu correo electrónico?'"
    ),

    Action.ASK_CITY: (
        "ACCIÓN: Pregunta de qué ciudad es el cliente.\n"
        "Sé natural: '¿De qué ciudad nos visitas?' o '¿De dónde eres?'"
    ),

    Action.ASK_TIMELINE: (
        "ACCIÓN: Pregunta el tiempo estimado para liquidar/cerrar el trato.\n"
        "Sé natural: '¿Cuál sería tu tiempo estimado para liquidar?'"
    ),

    Action.ASK_OFFER: (
        "ACCIÓN: Pide al cliente el monto de su propuesta/oferta.\n"
        "Sé natural: '¿Cuál sería el monto de tu propuesta?'"
    ),

    Action.SOFT_DENY: (
        "ACCIÓN: El cliente dijo 'no' o dudó. NO cierres la conversación.\n"
        "Responde de forma comercial: quita presión, ofrece valor, mantén la puerta abierta.\n"
        "Ejemplo: 'No te preocupes, no hay compromiso. Si te interesa conocer más sobre la unidad, aquí estoy.'"
    ),

    Action.CONFIRM_REGISTRATION: (
        "ACCIÓN: Confirma que todos los datos fueron registrados. Un asesor se pondrá en contacto.\n"
        "Datos registrados: {slots_summary}\n"
        "Sé breve: 'Perfecto, ya tengo tus datos registrados. Un asesor se pone en contacto contigo en breve.'"
    ),

    Action.ANSWER_QUESTION: (
        "ACCIÓN: Responde DIRECTAMENTE la pregunta que hizo el cliente. No cambies de tema ni pidas datos hasta haber respondido.\n"
        "Usa el INVENTARIO o INSTRUCCIONES DE CAMPAÑA si son relevantes. Sé conciso (máximo 2 oraciones).\n"
        "Si hay varias unidades del mismo modelo en diferentes ubicaciones, pregunta al cliente cuál le interesa.\n"
        "Si el cliente pregunta por ubicación, usa la ubicación de la unidad específica del inventario.\n"
        "Si no sabes algo: 'Eso lo confirmo y te aviso.'"
    ),

    Action.SHOW_INVENTORY: (
        "ACCIÓN: Muestra las opciones de inventario disponibles.\n"
        "Lista brevemente los modelos relevantes del INVENTARIO.\n"
        "Pregunta cuál le interesa."
    ),

    Action.SEND_PHOTOS: (
        "ACCIÓN: Confirma que envías fotos.\n"
        "Responde: 'Claro, aquí tienes.' (el sistema adjunta las fotos automáticamente)"
    ),

    Action.SEND_PDF: (
        "ACCIÓN: Confirma que envías el PDF.\n"
        "Si es ficha técnica: 'Te comparto la ficha técnica.'\n"
        "Si es corrida: 'Te comparto la simulación de financiamiento. Es ilustrativa.'"
    ),

    Action.ASK_INTEREST: (
        "ACCIÓN: Pregunta qué tipo de vehículo le interesa al cliente.\n"
        "NO listes todos los modelos. Pregunta de forma abierta:\n"
        "'¿Qué tipo de vehículo te interesa? ¿Pickup, tractocamión, van?'"
    ),

    Action.ASK_APPOINTMENT: (
        "ACCIÓN: Sugiere agendar una cita/visita.\n"
        "Sé natural: '¿Te gustaría agendar una visita? ¿Qué día y hora te funcionan?'"
    ),

    Action.CONFIRM_LEAD: (
        "ACCIÓN: Confirma la cita agendada con los datos del cliente.\n"
        "Datos: {slots_summary}\n"
        "Incluye día, hora y ubicación. Sé breve."
    ),

    Action.WAIT_MODE: (
        "ACCIÓN: Responde que queda pendiente.\n"
        "Responde: 'Sin problema, aquí quedo pendiente.'"
    ),

    Action.ESCALATE: (
        "ACCIÓN: Indica que un asesor especializado se pondrá en contacto.\n"
        "'Un asesor se pone en contacto contigo para darte más detalles.'"
    ),

    Action.ACKNOWLEDGE_SWITCH: (
        "ACCIÓN: Reconoce que el cliente quiere cambiar de modelo.\n"
        "Nuevo interés: {new_interest}\n"
        "Responde: 'Claro, con gusto te ayudo con la {new_interest}.' y da info del inventario."
    ),

    Action.SEND_FORM: (
        "ACCIÓN: Dirige al cliente al formulario de registro para dejar su propuesta.\n"
        "Link del formulario: {form_url}\n"
        "Sé breve y natural: 'Para registrar tu propuesta, completa el formulario aquí: {form_url}'\n"
        "No pidas datos por chat."
    ),
}

# Slot name → human-readable label
_SLOT_LABELS = {
    "name": "nombre completo",
    "email": "correo electrónico",
    "city": "ciudad",
    "timeline": "tiempo estimado para liquidar",
    "offer_amount": "monto de tu propuesta",
    "appointment": "día y hora para la cita",
    "interest": "modelo de interés",
    "payment": "forma de pago",
}


def build_writer_prompt(
    action: Action,
    slots: Slots,
    user_message: str,
    history: str,
    last_bot_messages: List[str],
    inventory_text: str = "",
    campaign_instructions: str = "",
    meta: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Build a focused prompt for the LLM to generate ONLY the response text.

    Returns the full system prompt string.
    """
    meta = meta or {}

    # Start with personality
    parts = [_PERSONALITY, ""]

    # Data context
    parts.append(f"DATOS RECOPILADOS: {slots.filled_summary()}")
    parts.append("")

    # Last bot messages (anti-repetition)
    if last_bot_messages:
        parts.append("TUS ÚLTIMOS MENSAJES (NO REPETIR):")
        for i, msg in enumerate(last_bot_messages[-3:], 1):
            parts.append(f"  [{i}]: {msg[:200]}")
        parts.append("")

    # Campaign instructions (if relevant)
    if campaign_instructions and action in (
        Action.PRESENT_CAMPAIGN, Action.ACKNOWLEDGE_AND_ASK_NEXT,
        Action.CONFIRM_REGISTRATION, Action.ANSWER_QUESTION, Action.SEND_FORM,
    ):
        parts.append(f"INSTRUCCIONES DE CAMPAÑA:\n{campaign_instructions}")
        parts.append("")

    # Inventory (if relevant)
    if inventory_text and action in (
        Action.ANSWER_QUESTION, Action.SHOW_INVENTORY,
        Action.ASK_INTEREST, Action.PRESENT_CAMPAIGN,
        Action.ACKNOWLEDGE_SWITCH,
    ):
        parts.append(f"INVENTARIO DISPONIBLE:\n{inventory_text}")
        parts.append("")

    # Action-specific prompt
    action_prompt = _ACTION_PROMPTS.get(action, "ACCIÓN: Responde de forma breve y profesional.")

    # Replace placeholders in action prompt
    next_slot = meta.get("next_slot", "")
    acknowledged_data = meta.get("acknowledged_data", {})
    ack_str = ", ".join(f"{k}={v}" for k, v in acknowledged_data.items() if v) if acknowledged_data else ""

    action_prompt = action_prompt.replace("{next_slot_label}", _SLOT_LABELS.get(next_slot, next_slot))
    action_prompt = action_prompt.replace("{acknowledged_data}", ack_str)
    action_prompt = action_prompt.replace("{slots_summary}", slots.filled_summary())
    action_prompt = action_prompt.replace("{new_interest}", meta.get("new_interest", ""))
    action_prompt = action_prompt.replace("{form_url}", meta.get("form_url", ""))

    parts.append(action_prompt)

    # Trust concern: inject credibility guidance so LLM addresses the objection
    if meta.get("is_trust_concern"):
        parts.append(
            "\nCONTEXTO ESPECIAL — OBJECIÓN DE CONFIANZA:\n"
            "El cliente expresó dudas sobre la legitimidad del proceso (fraude, permiso, garantías, etc.).\n"
            "Responde PRIMERO la objeción de forma directa y tranquilizadora:\n"
            "- Tractos y Max es una empresa establecida que emite factura original.\n"
            "- La unidad tiene garantía de fábrica vigente en todo México.\n"
            "- Opera de forma transparente: precio visible, proceso claro, sin intermediarios.\n"
            "NO sigas pidiendo datos hasta que el cliente muestre apertura.\n"
            "Si la duda persiste, ofrece conectarlo con un asesor humano."
        )

    # Form-based campaign: when action is PRESENT_CAMPAIGN, include the form link
    if meta.get("form_url") and action == Action.PRESENT_CAMPAIGN:
        parts.append(
            f"\nOBLIGATORIO — LINK DE REGISTRO (incluir siempre):\n"
            f"Esta campaña registra propuestas vía formulario externo. "
            f"Presenta la dinámica brevemente (1 oración) y cierra TU MENSAJE con esta línea exacta:\n"
            f"Para registrar tu propuesta: {meta['form_url']}\n"
            f"NO hagas preguntas de seguimiento. El link es todo lo que necesitan."
        )

    # Form-based campaign: when answering a side question, remind client of form at the end
    if meta.get("form_url") and action == Action.ANSWER_QUESTION and meta.get("is_side_question"):
        parts.append(
            f"\nOBLIGATORIO — INCLUYE ESTA LÍNEA AL FINAL DE TU RESPUESTA (textual):\n"
            f"Para registrar tu propuesta: {meta['form_url']}"
        )

    # Sandwich: answer side question AND ask for next missing slot in one message
    if meta.get("sandwich_next") and not meta.get("is_trust_concern"):
        next_label = _SLOT_LABELS.get(meta["sandwich_next"], meta["sandwich_next"])
        parts.append(
            "\nIMPORTANTE — RESPUESTA + CONTINUACIÓN:\n"
            f"Responde la pregunta del cliente PRIMERO (máx. 1 oración). "
            f"Luego, en la MISMA respuesta, pide su {next_label} de forma natural.\n"
            f"Ejemplo: 'Puedes visitarla de lunes a viernes de 9 a 6. ¿Me compartes tu {next_label}?'"
        )

    # When an offer was just provided, add validation guidance
    if acknowledged_data.get("offer_amount") and campaign_instructions:
        parts.append(
            "\nIMPORTANTE SOBRE LA PROPUESTA:\n"
            "- Compara el monto de la propuesta con el PRECIO DE SALIDA en las instrucciones de campaña.\n"
            "- Si la propuesta es MENOR al precio de salida, informa amablemente que la propuesta debe ser "
            "IGUAL O SUPERIOR al precio de salida para participar. Indica cuál es el precio de salida.\n"
            "- Si es IGUAL o SUPERIOR, reconoce la propuesta positivamente y continúa con el registro.\n"
            "- Sigue pidiendo el siguiente dato faltante después de tu comentario sobre la propuesta."
        )
    parts.append("")

    # Conversation history (truncated)
    if history:
        parts.append(f"HISTORIAL DE CHAT (últimos mensajes):\n{history[-2000:]}")
        parts.append("")

    # The actual user message
    parts.append(f"MENSAJE DEL CLIENTE: {user_message}")

    return "\n".join(parts)


# ============================================================
# DETERMINISTIC TEMPLATES — skip LLM for repetitive actions
# ============================================================

# Each action maps to a list of template variants for rotation.
# When the acknowledged data or slot summary is needed, use {ack} and {slot_label} placeholders.
_DETERMINISTIC_TEMPLATES: Dict[Action, List[str]] = {
    Action.ASK_NAME: [
        "¿Me compartes tu nombre completo, por favor?",
        "¿Con quién tengo el gusto?",
        "Para registrarte, ¿me das tu nombre completo?",
    ],
    Action.ASK_EMAIL: [
        "¿Me compartes tu correo electrónico?",
        "¿A qué correo te puedo enviar la información?",
        "Para enviarte los detalles, ¿cuál es tu correo?",
    ],
    Action.ASK_CITY: [
        "¿De qué ciudad nos visitas?",
        "¿De dónde eres?",
        "¿En qué ciudad te encuentras?",
    ],
    Action.ASK_TIMELINE: [
        "¿Cuál sería tu tiempo estimado para liquidar?",
        "¿En cuánto tiempo planeas cerrar la compra?",
        "¿Tienes un plazo estimado para la adquisición?",
    ],
    Action.ASK_OFFER: [
        "¿Cuál sería el monto de tu propuesta?",
        "¿Qué monto tienes en mente para tu oferta?",
        "Para registrar tu propuesta, ¿de cuánto sería el monto?",
    ],
    Action.SOFT_DENY: [
        "No te preocupes, no hay compromiso. Si te interesa conocer más sobre la unidad, con gusto te apoyo.",
        "Sin problema, no hay presión. La información queda a tu disposición por si más adelante te interesa.",
        "Entendido. Si en algún momento quieres más detalles o tienes alguna duda, aquí estoy para ayudarte.",
    ],
    Action.WAIT_MODE: [
        "Sin problema, aquí quedo pendiente.",
        "Perfecto, cuando gustes aquí estamos.",
        "Claro, sin problema. Aquí quedo al pendiente.",
    ],
    Action.ESCALATE: [
        "Un asesor se pone en contacto contigo para darte más detalles.",
        "Te comunico con un asesor especializado que te puede ayudar mejor.",
    ],
    Action.SEND_PHOTOS: [
        "Claro, aquí tienes.",
        "Con gusto, aquí te las envío.",
    ],
}

# Templates with acknowledged data: "{ack}" is replaced with what was given
_ACK_TEMPLATES: List[str] = [
    "Perfecto, anotado. ¿Me compartes tu {slot_label}?",
    "Entendido. ¿Me podrías dar tu {slot_label}?",
    "Muy bien. ¿Me das tu {slot_label}, por favor?",
]


def _is_duplicate_response(response: str, last_msgs: List[str]) -> bool:
    """Check if a response is a near-duplicate of recent bot messages using Jaccard similarity."""
    if not response or not last_msgs:
        return False
    response_tokens = set(response.lower().split())
    if len(response_tokens) < 3:
        return False
    for prev in last_msgs:
        prev_tokens = set(prev.lower().split())
        if prev_tokens:
            union = response_tokens | prev_tokens
            if union and len(response_tokens & prev_tokens) / len(union) >= 0.75:
                return True
    return False


def try_deterministic_response(
    action: Action,
    slots: Slots,
    meta: Optional[Dict[str, Any]] = None,
    last_bot_messages: Optional[List[str]] = None,
    turn_count: int = 0,
    jid: str = "",
) -> Optional[str]:
    """
    Try to generate a deterministic response (no LLM needed).
    Returns the response text, or None if the action needs LLM.

    Uses stable hash-based rotation (not random) for reproducibility.
    Avoids repeating the last bot message.
    Falls back to None (LLM) if the deterministic response is a duplicate.
    """
    meta = meta or {}
    last_msgs = [m.lower() for m in (last_bot_messages or [])]

    # ACKNOWLEDGE_AND_ASK_NEXT: deterministic with acknowledged data
    if action == Action.ACKNOWLEDGE_AND_ASK_NEXT:
        ack_data = meta.get("acknowledged_data") or {}
        # If offer_amount was just provided, let LLM handle it so it can
        # validate against starting price and respond contextually.
        # If appointment was just provided, let LLM acknowledge it warmly.
        if ack_data.get("offer_amount") or ack_data.get("appointment"):
            return None
        next_slot = meta.get("next_slot", "")
        slot_label = _SLOT_LABELS.get(next_slot, next_slot)
        candidates = [t.replace("{slot_label}", slot_label) for t in _ACK_TEMPLATES]
        response = _pick_non_repeat(candidates, last_msgs, action.value, turn_count, jid)
        if _is_duplicate_response(response, last_msgs):
            return None  # All ACK variants are duplicates → let LLM generate fresh
        return response

    # SEND_FORM: deterministic — always include the actual URL
    if action == Action.SEND_FORM:
        form_url = (meta or {}).get("form_url", "")
        if form_url:
            return f"Para registrar tu propuesta, completa el formulario aquí: {form_url}"
        return None  # No URL available — fall back to LLM

    # CONFIRM_REGISTRATION: deterministic
    if action == Action.CONFIRM_REGISTRATION:
        return "Perfecto, ya tengo tus datos registrados. Un asesor se pondrá en contacto contigo en breve. ¿Tienes alguna duda sobre la dinámica o la unidad?"

    # Simple deterministic actions
    templates = _DETERMINISTIC_TEMPLATES.get(action)
    if templates:
        response = _pick_non_repeat(templates, last_msgs, action.value, turn_count, jid)
        if _is_duplicate_response(response, last_msgs):
            return None  # Duplicate → let LLM rephrase
        return response

    return None


def _pick_non_repeat(
    candidates: List[str],
    last_msgs: List[str],
    action_name: str = "",
    turn_count: int = 0,
    jid: str = "",
) -> str:
    """
    Pick a template using stable hash-based rotation.
    Deterministic: same (action, turn, jid) → same pick.
    Falls back to next candidate if the pick was already said.
    """
    if not candidates:
        return ""

    # Stable index from hash of (action + turn + jid)
    seed = f"{action_name}:{turn_count}:{jid}"
    idx = int(hashlib.md5(seed.encode()).hexdigest(), 16) % len(candidates)

    # Try starting from hashed index, skip if it was the last message
    for offset in range(len(candidates)):
        pick = candidates[(idx + offset) % len(candidates)]
        if pick.lower() not in last_msgs:
            return pick

    # All repeated — return the hashed pick anyway
    return candidates[idx]
