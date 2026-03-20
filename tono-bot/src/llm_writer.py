"""
LLM Writer — Focused text generation for specific actions.

The LLM does NOT decide what to do. It only writes text for a
predetermined action. This keeps responses consistent and predictable.
"""

import logging
from typing import Any, Dict, List, Optional

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
