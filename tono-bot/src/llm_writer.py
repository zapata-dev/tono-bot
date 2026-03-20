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

from conversation_fsm import Action, Slots

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

    Action.CONFIRM_REGISTRATION: (
        "ACCIÓN: Confirma que todos los datos fueron registrados. Un asesor se pondrá en contacto.\n"
        "Datos registrados: {slots_summary}\n"
        "Sé breve: 'Perfecto, ya tengo tus datos registrados. Un asesor se pone en contacto contigo en breve.'"
    ),

    Action.ANSWER_QUESTION: (
        "ACCIÓN: Responde la pregunta del cliente usando el contexto disponible.\n"
        "Usa el INVENTARIO si es relevante. Sé conciso (máximo 2 oraciones).\n"
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
}

# Slot name → human-readable label
_SLOT_LABELS = {
    "name": "nombre completo",
    "email": "correo electrónico",
    "city": "ciudad",
    "timeline": "tiempo estimado para liquidar",
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
        Action.CONFIRM_REGISTRATION, Action.ANSWER_QUESTION,
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

    parts.append(action_prompt)
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
    "Listo, registrado. ¿Cuál es tu {slot_label}?",
    "Muy bien. ¿Me das tu {slot_label}, por favor?",
]


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
    """
    meta = meta or {}
    last_msgs = [m.lower() for m in (last_bot_messages or [])]

    # ACKNOWLEDGE_AND_ASK_NEXT: deterministic with acknowledged data
    if action == Action.ACKNOWLEDGE_AND_ASK_NEXT:
        next_slot = meta.get("next_slot", "")
        slot_label = _SLOT_LABELS.get(next_slot, next_slot)
        candidates = [t.replace("{slot_label}", slot_label) for t in _ACK_TEMPLATES]
        return _pick_non_repeat(candidates, last_msgs, action.value, turn_count, jid)

    # CONFIRM_REGISTRATION: deterministic
    if action == Action.CONFIRM_REGISTRATION:
        return "Perfecto, ya tengo tus datos registrados. Un asesor se pone en contacto contigo en breve."

    # Simple deterministic actions
    templates = _DETERMINISTIC_TEMPLATES.get(action)
    if templates:
        return _pick_non_repeat(templates, last_msgs, action.value, turn_count, jid)

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
