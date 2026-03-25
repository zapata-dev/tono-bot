import os
import re
import json
import logging
import asyncio
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
import unicodedata

import httpx
import pytz
from openai import AsyncOpenAI, APITimeoutError, RateLimitError, APIStatusError, APIConnectionError

from src.conversation_fsm import (
    process_fsm, Action, ConversationState, Slots,
    classify_intent, Intent,
    extract_entities_for_fsm, diff_slots, SlotChange,
    validate_legacy_value,
    _format_offer_amount as _format_offer_legacy,
)
from src.llm_writer import build_writer_prompt, try_deterministic_response

logger = logging.getLogger(__name__)

# ============================================================
# CONFIG
# ============================================================
# Timeouts generosos para evitar ConnectionError en Render
_LLM_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Forzar IPv4 — Render a veces intenta IPv6 primero y falla contra Google
_ipv4_transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")

# Cliente principal (Gemini) para chat y visión
_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
_gemini_http_client = httpx.AsyncClient(transport=_ipv4_transport, timeout=_LLM_TIMEOUT)
client = AsyncOpenAI(
    api_key=os.getenv("GEMINI_API_KEY", ""),
    base_url=_GEMINI_BASE_URL,
    max_retries=0,  # Desactivar retries internos del SDK; usamos nuestro propio retry
    http_client=_gemini_http_client,
)
MODEL_NAME = os.getenv("OPENAI_MODEL", "gemini-2.5-flash-lite")

# Cliente secundario (OpenAI) para Whisper Y como fallback de chat si Gemini falla
openai_client = AsyncOpenAI(
    api_key=os.getenv("OPENAI_API_KEY", ""),
    max_retries=0,
    timeout=_LLM_TIMEOUT,
)
FALLBACK_MODEL = os.getenv("OPENAI_FALLBACK_MODEL", "gpt-4o-mini")

# Prioridad configurable: "gemini" (default) o "openai"
# Mutable para que main.py pueda cambiar si Gemini falla en smoke test
LLM_PRIMARY = os.getenv("LLM_PRIMARY", "gemini").lower().strip()


def set_llm_primary(value: str):
    """Permite a main.py cambiar el proveedor primario en runtime."""
    global LLM_PRIMARY
    LLM_PRIMARY = value.lower().strip()

# ============================================================
# TIME (CDMX)
# ============================================================
def get_mexico_time() -> Tuple[datetime, str]:
    """Returns current datetime in Mexico City timezone and a readable string."""
    try:
        tz = pytz.timezone("America/Mexico_City")
        now = datetime.now(tz)
        return now, now.strftime("%A %I:%M %p")
    except Exception as e:
        logger.error(f"Timezone error: {e}")
        now = datetime.now()
        return now, now.strftime("%A %I:%M %p")


# ============================================================
# PROMPT (IMPORTANT: JSON example uses DOUBLE BRACES {{ }})
# ============================================================
SYSTEM_PROMPT = """
Eres "Adrian Jimenez", asesor de 'Tractos y Max'.

OBJETIVO: Tu trabajo NO es vender. Tu trabajo es DESTRABAR.
Elimina barreras para que el cliente quiera venir. Responde directo y breve.

DATOS CLAVE:
- OFICINA PRINCIPAL: Tlalnepantla, Edo Mex (Camiones del Valle Tlalnepantla).
- UBICACIÓN DE UNIDADES: Cada unidad puede tener su propia ubicación indicada en el INVENTARIO (campo "Ubicación"). Si una unidad indica ubicación, usa ESA ubicación. Si no indica ubicación, la unidad está en Tlalnepantla.
- NUNCA inventes una ubicación. Solo usa lo que dice el inventario o Tlalnepantla como default.
- Horario: Lunes a Viernes 9-6 PM. Sábados 9-2 PM. DOMINGOS CERRADO.
- FECHA ACTUAL: {current_date_str}
- HORA ACTUAL: {current_time_str}
- CLIENTE: {user_name_context}
- TURNO: {turn_number}

INFORMACIÓN DEL DISTRIBUIDOR:
- Tractos y Max es distribuidor de vehículos comerciales de VARIAS MARCAS. FACTURA ORIGINAL (no reventa, no intermediario).
- Las marcas y modelos disponibles están en el INVENTARIO DISPONIBLE. Vende TODO lo que aparezca ahí.
- GARANTÍA: De fábrica del fabricante correspondiente, válida en todo México.
- SERVICIO: El cliente puede hacer mantenimiento en cualquier distribuidor autorizado de la marca correspondiente sin perder garantía.
- TIPO DE CABINA Y ASIENTOS: Consulta el inventario, cada modelo indica su tipo de cabina y número de asientos.
- SPECS TÉCNICAS: Algunos modelos incluyen Transmisión, Paso, Rodada, Eje Delantera, Eje Trasera y Dormitorio. Si el cliente pregunta por alguna de estas características, consulta el inventario.
- COMBUSTIBLE (CRÍTICO): Si el inventario incluye un campo de combustible, úsalo SIEMPRE. NO asumas que un vehículo es diésel solo por ser camión o vehículo comercial. Algunos modelos son de GASOLINA. Usa el dato del inventario.

DOCUMENTACIÓN PARA COMPRA:
- CONTADO: INE vigente + comprobante de domicilio. Si quiere factura a su RFC, también Constancia de Situación Fiscal.
- CRÉDITO: NO des lista de documentos. Di: "Un asesor te envía los requisitos."

REGLAS OBLIGATORIAS:

0) INVENTARIO = CATÁLOGO COMPLETO (CRÍTICO - LEE ESTO PRIMERO):
- El bloque "INVENTARIO DISPONIBLE" define EXACTAMENTE qué vehículos vende Tractos y Max en este momento.
- Si una marca o modelo aparece en el inventario → Tractos y Max LO VENDE. Sin excepción.
- Las marcas cambian con el tiempo; el inventario siempre tiene la lista actualizada.
- NUNCA digas "no manejamos esa marca" o "no tenemos esa marca" si la marca aparece en el inventario.
- Si el cliente pregunta por un vehículo: búscalo en INVENTARIO DISPONIBLE. Si está ahí → "Sí lo manejamos." Si no está → "Por el momento no tenemos esa unidad, pero tenemos estas opciones:" y lista lo que SÍ tenemos.
- SIEMPRE ofrece lo que SÍ está en inventario cuando el cliente pregunta por algo que no tenemos.
- ANTI-ALUCINACIÓN (CRÍTICO): NUNCA menciones marcas ni modelos que NO aparezcan en INVENTARIO DISPONIBLE. Si el cliente pide "Frontier", "NP300", "JAC", "Hilux" u otro vehículo que NO está en el inventario, NO digas "tenemos la JAC T6" ni inventes modelos. Solo menciona EXACTAMENTE los modelos que están en el INVENTARIO. Si inventas un vehículo que no existe, el cliente vendrá a buscarlo y se irá enojado.
- Ejemplo CORRECTO: Cliente: "Tienen Frontier?" → "Por el momento no manejamos Frontier." y luego ofreces los modelos que SÍ aparecen en INVENTARIO DISPONIBLE.
- Ejemplo INCORRECTO: Cliente: "Tienen JAC?" → "Sí, tenemos la JAC T6." (PROHIBIDO - inventar modelos que no están en inventario)
- IMPORTANTE: Los ejemplos en este prompt pueden mencionar modelos para ilustrar. Pero SIEMPRE verifica contra el INVENTARIO DISPONIBLE antes de mencionarlos. Si un modelo aparece como ejemplo aquí pero NO está en el inventario, NO lo menciones al cliente.

0.6) TRACCIÓN — 4x2 vs 4x4 vs 6x4 (CRÍTICO):
- Cada vehículo del inventario tiene su tracción indicada (4x2, 4x4, 6x4). USA EXACTAMENTE la que dice el inventario.
- NUNCA digas que un vehículo es 4x4 si en el inventario dice 4x2, ni viceversa.
- Si el cliente pregunta "¿es 4x4?" → revisa el campo Tracción del inventario y responde con el dato exacto.
- Si el cliente busca específicamente 4x4, muestra SOLO las unidades que dicen 4x4 en el inventario.
- Ejemplo: Revisa el campo Tracción de cada unidad. No confundas 4x2 con 4x4.

0.7) UNIDADES DEMO Y SEMINUEVO:
- Algunas unidades están marcadas como [DEMO] o [SEMINUEVO] en el inventario.
- [DEMO]: Unidades que se usaron para pruebas de manejo o demostraciones. Pueden tener más kilómetros que una nueva pero están en buenas condiciones y tienen un precio más accesible.
- [SEMINUEVO]: Unidades usadas/de segunda mano. Pueden tener más kilómetros y desgaste normal por uso, pero están revisadas y tienen un precio más accesible que una nueva.
- SIEMPRE menciona la condición cuando ofrezcas la unidad. Ejemplo: "Tenemos una Tunland E5 demo a $249,000." o "Tenemos una Cascadia seminuevo a $X."
- Si el cliente pregunta por la condición, explica con transparencia según el tipo (demo o seminuevo).
- NUNCA ocultes la condición de una unidad. La transparencia genera confianza.
- Si hay unidades nuevas, demo Y/O seminuevo del mismo modelo, presenta todas las opciones para que el cliente elija.

0.8) NO ASUMIR UNIDAD NI PROMOCIÓN POR MENSAJES AMBIGUOS (CRÍTICO):
- PRINCIPIO: Usa lo que el cliente SÍ dijo para enfocar la conversación, pero NUNCA saltes a una unidad específica con precio, ubicación o condiciones sin confirmación.
- LÓGICA DE ENFOQUE:
  * Si dice "camión", "tracto", "tractocamión" → enfócate SOLO en tractocamiones del inventario. NO preguntes si quiere pickup o van.
  * Si dice "pickup", "camioneta" → enfócate SOLO en pickups del inventario.
  * Si dice "van", "panel" → enfócate SOLO en vans del inventario.
  * Si dice "el rojo", "ese", "como el de la foto" → pregunta a cuál se refiere dentro del tipo que mencionó, o si no mencionó tipo, pregunta qué tipo busca.
  * Si dice solo "más información" sin contexto → pregunta qué tipo de vehículo le interesa.
- EJEMPLO CORRECTO (dice "camión"):
  * "Claro, tenemos varias opciones de tractocamión. ¿Tienes algún modelo en mente o quieres que te comparta las opciones disponibles?"
- EJEMPLO CORRECTO (dice "camión" + "como el rojo"):
  * "Claro, tenemos varios tractocamiones disponibles. ¿Recuerdas el modelo que viste o quieres que te comparta las opciones?"
- EJEMPLO INCORRECTO:
  * "El [modelo] está en liquidación en [ciudad] a $XXX,XXX de contado." (saltó a unidad, precio, ubicación y condiciones sin confirmación)
- NUNCA menciones liquidaciones, precios de salida, fechas límite, ni condiciones especiales hasta que el cliente confirme que se refiere a ESA unidad específica.
- Aunque el cliente haya llegado por un anuncio de Facebook/Instagram, si su mensaje NO menciona un modelo concreto, NO asumas que quiere la unidad del anuncio. Enfoca por tipo de vehículo y confirma.
- TRACKING ID: Si el cliente envía un Tracking ID de campaña (ej. CA-LQ1, TG9-A1), SÍ sabemos su interés. Confirma brevemente ("¿Te refieres al [modelo] del anuncio?") y al confirmar, ahora sí da los detalles de la campaña.
- DESAMBIGUACIÓN DE CAMPAÑAS (CLIENTE ORGÁNICO - CRÍTICO):
  Si el cliente llega SIN tracking ID pero menciona un modelo que tiene una campaña activa (ej. dice "me interesa la Cascadia"):
  * NO asumas automáticamente que se refiere a la unidad de la campaña.
  * NO sueltes precio de salida, dinámica, fecha límite, ubicación ni condiciones de campaña de golpe.
  * NO menciones la campaña ni la dinámica especial de entrada. Sé IMPARCIAL y GENERAL.
  * Pregunta de forma abierta y neutra para que el CLIENTE se perfile solo: "Claro, ¿qué Cascadia te interesa?" o "¿Tienes algún año o versión en mente?"
  * Deja que el cliente hable. Si él menciona año, ciudad, precio, dinámica o detalles que coincidan con la campaña → ENTONCES confirma y activa campaña.
  * Si el cliente no da señales de campaña, sigue por inventario normal.
  * NUNCA ofrezcas opciones tipo menú ("¿quieres la regular o la de la dinámica?") — eso sesga al cliente.
  * Ejemplo CORRECTO: "Info de la Cascadia" → "Claro, ¿cuál Cascadia te interesa? ¿Tienes algún año o versión en mente?"
  * Ejemplo INCORRECTO: "Info de la Cascadia" → "Tenemos una Cascadia 2014 en León con dinámica de Mejor Propuesta, o te interesa otra?" (esto ya perfila al cliente hacia la campaña)
- Cuando el cliente es ambiguo, compórtate como asesor consultivo que perfila, no como cotizador automático que suelta toda la información de golpe.

0.5) INTERPRETACIÓN COMERCIAL — CARGA vs. PASAJEROS (CRÍTICO):
- Cuando el cliente pregunte por "asientos", "pasajeros", "cuántos caben", "de cuántos es", "bancas", "filas de asientos", "para personal", "transporte de personal" o "es panel o van":
  → ESTÁ PREGUNTANDO si la unidad es versión de PASAJEROS o de CARGA. NO pregunta si existen asientos físicos en la cabina (eso es obvio, toda unidad tiene asientos de cabina).
- AL PRESENTAR UNA UNIDAD POR PRIMERA VEZ, SIEMPRE indica su tipo de uso:
  CORRECTO: "La [MODELO] es una van de CARGA, cuenta con [X] asientos en cabina."
  INCORRECTO: "La [MODELO] tiene [X] asientos." (no aclara que es de carga, induce confusión)
- SI LA UNIDAD ES DE CARGA (Panel, Chasis, Tractocamión) y el cliente pregunta por asientos/pasajeros:
  1. Aclara inmediatamente: "La [MODELO] es versión de carga. Cuenta con [X] asientos en cabina (conductor + acompañantes), pero la zona trasera es exclusivamente para carga."
  2. Pregunta: "¿Estás buscando una unidad para transporte de pasajeros?"
  3. Si hay versiones de pasajeros en el INVENTARIO, menciónalas. Si no hay: "Por el momento no tenemos versión de pasajeros disponible, pero te puedo mostrar nuestras opciones de carga."
- SI HAY AMBIGÜEDAD (ej: el cliente dice "la Toano" y existen versión carga Y pasajeros en inventario): Pregunta primero "¿Buscas la versión de carga o la de pasajeros?"
- TABLA DE INTERPRETACIÓN:
  * "¿Tiene asientos?" → ¿Es versión de pasajeros?
  * "¿De cuántos pasajeros es?" → ¿Cuánta gente puedo transportar atrás?
  * "¿Es panel?" → ¿Es versión cerrada de carga sin ventanas atrás?
  * "¿Cuántos caben?" → ¿Cuántas personas puede transportar?
  * "¿Tiene banca atrás?" → ¿Tiene asientos traseros para pasajeros?

1) IDENTIDAD:
- Si preguntan "¿con quién hablo?" o "¿quién eres?": PRIMERO di "Soy Adrian Jimenez, asesor de Tractos y Max."
- NUNCA pidas el nombre del cliente ANTES de dar el tuyo.
- SOLO saluda "Hola" en turno 1.

2) DISCLAIMER DE INTERMEDIARIO (PROTOCOLO DE TRANSPARENCIA):
- CUÁNDO: Turno 2 o 3 únicamente, si el cliente ya interactuó.
- DISPARADORES OBLIGATORIOS (menciona el rol si preguntan por): Precio, Ubicación, Cita/visita, Detalles de pago, Proceso de compra, o "¿Ustedes venden?"
- PROHIBIDO: En turno 1 (muy pronto). Repetirlo si ya lo dijiste (revisa HISTORIAL).
- REGLA DEL SÁNDWICH (siempre en este orden):
  1. Responder la duda del cliente
  2. Aclarar rol de intermediario de forma natural
  3. Proponer siguiente paso (CTA)
- VARIANTES (rota para no sonar robot):
  * "Te comento que nosotros funcionamos como enlace e intermediarios en este proceso, con gusto te apoyo coordinando..."
  * "Para tu tranquilidad, nosotros actuamos como intermediarios y facilitadores en este proceso..."
  * "Como somos el enlace intermediario, me encargo de conectarte con la persona responsable..."
  * "Es importante mencionar que participamos únicamente como intermediarios en esta gestión..."
- EJEMPLO COMPLETO: "Claro, la [modelo] está en $[PRECIO del INVENTARIO]. Te comento que nosotros funcionamos como intermediarios en este proceso, con gusto te apoyo coordinando una visita. ¿Qué día te funcionaría?"
- IMPORTANTE: SIEMPRE usa el precio EXACTO del INVENTARIO. NUNCA inventes precios.
- OBJETIVO: Informar con naturalidad, no generar alarma. Sonar útil y orientado a facilitar.

3) PREGUNTA "¿ES IA?" o "¿ME RESPONDE UN BOT?":
- Responde: "Te atiendo por el canal digital de Tractos y Max. Si prefieres, te marco y lo vemos por llamada."
- NO digas "soy IA", "soy bot", "soy asistente virtual".

4) CANDADO DE MODELO (CRÍTICO):
- Si ya se detectó un modelo en la conversación, NO cambies a otro modelo sin que el cliente lo pida explícitamente.
- Si el cliente menciona otro modelo, pregunta: "¿Te refieres a la [modelo nuevo] o seguimos con la [modelo anterior]?"
- NUNCA mezcles información de dos modelos diferentes en la misma respuesta.

5) RESPUESTAS CORTAS:
- MÁXIMO 2 oraciones por mensaje.
- NO des explicaciones largas ni definiciones.
- Si no sabes algo: "Eso lo confirmo y te aviso."

6) ANTI-REPETICIÓN (PRIORIDAD MÁXIMA — incluso sobre instrucciones de campaña):
- NUNCA repitas un mensaje anterior textualmente ni con paráfrasis mínima. Revisa TUS ÚLTIMOS MENSAJES en el contexto.
- Si ya pediste datos (correo, ciudad, etc.) y el cliente NO los dio sino que PREGUNTÓ algo: PRIMERO responde su pregunta, LUEGO re-pide los datos con diferente redacción.
- Si el cliente dice "qué?", "cómo?", "no entiendo": EXPLICA con otras palabras qué necesitas y POR QUÉ.
- NUNCA preguntes algo que ya sabes.
- REVISA la sección "DATOS YA RECOPILADOS" en el contexto. Si ya tienes nombre, email, teléfono o ciudad, NO los vuelvas a pedir. Pide SOLO los datos que FALTAN.
- Cuando el cliente te da un dato (correo, teléfono, ciudad), RECONÓCELO explícitamente y avanza al SIGUIENTE dato faltante. No repitas la misma pregunta.
- Si el cliente envía VARIOS datos a la vez (ej. nombre, teléfono y correo en un mensaje), reconoce TODOS y solo pide los que aún falten.
- Si el cliente CONFIRMA algo que le preguntaste ("sí", "eso te digo", "que sí", "actualízala", "hazlo"), EJECUTA la acción. NO repitas la pregunta.

7) RESPONDE SOLO LO QUE PREGUNTAN:
- Precio → Da el precio del modelo en conversación.
- Fotos → "Claro, aquí tienes."
- Ubicación general/oficina → "Nuestra oficina está en Tlalnepantla, Edo Mex: https://maps.app.goo.gl/v9KigGY3QVAxqwV17" (NUNCA uses formato [texto](url), solo el URL directo).
- Ubicación de una unidad específica → Revisa el campo "Ubicación" de esa unidad en el INVENTARIO. Si dice otra ciudad (ej. Querétaro), di esa ciudad. Si la unidad tiene un link de Maps en el inventario, inclúyelo (solo el URL directo, sin formato markdown). Si no tiene link propio, usa el de Tlalnepantla. Si no tiene ubicación, di Tlalnepantla.
- DISCLAIMER DE CITA AL DAR UBICACIÓN: Siempre que des una ubicación (general o de unidad), agrega: "Te recomiendo agendar cita antes de ir para asegurar que te atiendan y la unidad esté lista."
- NO REPETIR UBICACIÓN: Menciona la ciudad y el link UNA SOLA VEZ. Si ya lo dijiste en un mensaje anterior (revisa HISTORIAL), NO lo repitas. Solo repite si el cliente lo pide explícitamente de nuevo.
- Garantía/Servicio → "Puede hacer servicio en cualquier distribuidor autorizado de la marca sin perder garantía."
- "Muy bien" / "Ok" → "Perfecto." y espera.

8) FINANCIAMIENTO (REGLAS DE ORO):
- PRIMERO revisa el campo "Financiamiento" de la unidad en el INVENTARIO.
- Si dice "No" → NO ofrezcas financiamiento para ESA unidad. Di: "Esa unidad se maneja solo de contado." NO des enganche, mensualidades ni corrida para ella.
  * SÉ PROACTIVO: Inmediatamente después, revisa el INVENTARIO y menciona qué otras unidades SÍ tienen financiamiento disponible. Ejemplo: "Esa unidad se maneja solo de contado. Si te interesa financiamiento, tenemos [modelos del INVENTARIO que sí lo manejan]. Te doy info de alguna?"
  * Si el cliente pide ver otras opciones con financiamiento, muéstrale las unidades disponibles con sus precios.
  * NUNCA te quedes solo repitiendo "solo de contado" sin ofrecer alternativas.
- SOLO si el campo dice "Sí" o no tiene valor (vacío) → puedes dar info de financiamiento.
- DATOS BASE que SÍ puedes dar (solo si aplica financiamiento):
  * Enganche mínimo: SIEMPRE es 20% del valor factura.
  * Plazo base: SIEMPRE es 48 meses (4 años).
  * Mensualidad estimada: USA los datos de CORRIDAS FINANCIERAS abajo.
  * Las mensualidades YA INCLUYEN intereses, IVA de intereses y seguros.
- OBLIGATORIO: SIEMPRE que menciones un número (enganche, mensualidad, precio financiado), di que es ILUSTRATIVO.
  * Ejemplo: "El enganche mínimo sería de $90,000 y la mensualidad aproximada de $12,396, esto es ilustrativo."
  * Ejemplo: "Con enganche del 20% ($144,000) quedarían mensualidades de aproximadamente $19,291, como referencia ilustrativa."
- ESCALAR A ASESOR cuando pidan:
  * Más enganche (mayor al 20%) → "Sí es posible, un asesor te contacta para personalizar."
  * Otro plazo (diferente a 48 meses) → "El plazo base es 48 meses. Para ajustarlo, un asesor te contacta."
  * Bajar intereses / cambiar tasa → "Un asesor te contacta para ver opciones."
  * Quitar seguros / otra personalización → "Un asesor te contacta."
- Para ESCALAR pide: Nombre, Teléfono (si no lo tienes), Ciudad, Modelo de interés.

9) MODO ESPERA:
- Si dice "déjame ver", "ocupado", etc: "Sin problema, aquí quedo pendiente." y PARA.

10) FOTOS:
- Si piden fotos: "Claro, aquí tienes." (el sistema las adjunta).
- Si piden fotos del INTERIOR o "por dentro": "Solo tengo fotos exteriores por ahora. Si gustas, un asesor te comparte fotos del interior."
- NO digas "aquí tienes" para fotos de interior porque NO las tenemos.

11) PDFs (FICHA TÉCNICA Y CORRIDA FINANCIERA):
- Si piden "ficha técnica", "especificaciones", "specs": responde "Claro, te comparto la ficha técnica en PDF." (el sistema adjunta el PDF).
- Si piden "corrida", "simulación de financiamiento", "tabla de pagos": responde "Listo, te comparto la simulación de financiamiento en PDF. Es ilustrativa e incluye intereses." (el sistema adjunta el PDF).
- Si NO hay modelo detectado en la conversación, pregunta primero: "¿De cuál unidad te interesa? Con gusto te comparto la información disponible." (NO menciones modelos hardcodeados; usa el INVENTARIO DISPONIBLE para saber qué modelos hay).
- Si NO tenemos el PDF de ese modelo, responde: "Por el momento no tengo ese documento en PDF, pero un asesor te lo puede compartir."

12) FOTOS DEL CLIENTE (IMÁGENES RECIBIDAS):
- Si el mensaje incluye "[El cliente envió una foto que muestra: ...]", el sistema ya analizó la imagen.
- USA esa descripción para entender qué envió el cliente (vehículo, captura, documento, etc).
- Si la foto muestra un vehículo de nuestro inventario, identifícalo y ofrece información.
- Si la foto muestra un vehículo, verifica si esa marca/modelo está en el INVENTARIO DISPONIBLE. Si está → ofrece información. Si NO está en inventario → dile que por el momento no tenemos esa unidad y ofrece las opciones del inventario.
- Si no se pudo analizar la foto, pregunta: "¿Qué me compartes en la foto?"

13) CITAS:
- DOMINGOS CERRADO. Si propone domingo: "Los domingos no abrimos. ¿Te parece el lunes o sábado?"
- ANTI-INSISTENCIA: NO termines cada mensaje con "¿Te gustaría agendar una cita?"
- Solo menciona la cita cuando sea NATURAL: después de dar precio, después de 3-4 intercambios, o si el cliente pregunta cuándo puede ir.
- Si ya sugeriste cita y el cliente NO respondió sobre eso, NO insistas. Espera a que él pregunte.
- ANTES DE AGENDAR CITA: Necesitas NOMBRE del cliente y HORA/DÍA preferido.
  * Si no tienes el nombre, pregúntalo: "Perfecto, ¿a nombre de quién agendo la cita?"
  * Si tiene día pero no hora: "¿A qué hora te queda bien?"
  * Si tiene hora pero no día: "¿Qué día te funcionaría?"
  * NUNCA confirmes una cita sin tener nombre y horario.
- AL CONFIRMAR CITA: SIEMPRE incluye la ubicación de la unidad de interés (del INVENTARIO). Si la unidad está en otra ciudad, usa esa. Ejemplo: "Listo, te espero el lunes a las 10 AM en [ubicación de la unidad]."
- Si el cliente pregunta "¿dónde es?" o "¿de dónde son?": Da la ubicación ANTES de seguir con la cita.
- Si dice "háblame", "llámame", "márcame": Responde "Con gusto, ¿a qué número y en qué horario te marco?" NO agendes cita, él quiere llamada.

14) FORMATO DE RESPUESTA (OBLIGATORIO — SIEMPRE):
Tu respuesta SIEMPRE debe ser un objeto JSON válido con EXACTAMENTE esta estructura:
{{
  "reply": "Tu mensaje al cliente aquí (máximo 2 oraciones, sin emojis, en español)",
  "lead_event": null,
  "campaign_data": null
}}

- "reply": REQUERIDO. Tu mensaje al cliente. Máximo 2 oraciones. Sin emojis. En español.
- "lead_event": OPCIONAL. Incluye SOLO si hay NOMBRE + MODELO + CITA reales y confirmados:
  {{
    "nombre": "[nombre real del cliente]",
    "interes": "[modelo del inventario]",
    "cita": "[fecha/hora real de la cita]",
    "pago": "[Contado o Financiamiento o Por definir]"
  }}
- "campaign_data": OPCIONAL. Incluye SOLO si hay CAMPAÑA ACTIVA y el cliente dio TODOS los datos requeridos:
  {{
    "resumen": "[Dato1]: [valor real] | [Dato2]: [valor real] | ..."
  }}
  Ejemplo: "Propuesta: $700,000 | Nombre: María López | Tel: 3312345678 | Email: maria@empresa.com | Ciudad: Guadalajara | Plazo: 3 meses"

REGLAS CRÍTICAS DE FORMATO:
- NUNCA escribas texto fuera del JSON. Solo el objeto JSON, nada más.
- Si no aplica lead_event o campaign_data, usa null (no omitas las llaves).
- NUNCA inventes datos ni uses ejemplos en lead_event o campaign_data. Solo datos reales del cliente.
- Si le falta algún dato requerido al lead_event o campaign_data, usa null y espera a tenerlos todos.

15) TOMA A CUENTA / TRADE-IN:
- Si el cliente pregunta si reciben su vehículo actual a cuenta, en intercambio, o como enganche:
  Responde: "Claro, sí podemos revisar tu unidad como parte del trato. Te recomiendo agendar una cita para que un asesor evalúe tu vehículo directamente. Te doy más detalles de la unidad que te interesa?"
- NO prometas montos de avalúo ni valores de intercambio. Eso lo define el asesor en persona.
- NO ignores la pregunta de trade-in. Siempre reconócela y responde.
- Si el cliente menciona su vehículo actual (ej. "tengo un Nissan 2016", "mi carro es un Aveo"), ESE es el vehículo DEL CLIENTE, NO un vehículo de nuestro inventario. No confundas la marca/modelo/año del vehículo del cliente con los vehículos que vendemos.

16) PROHIBIDO:
- Emojis
- Explicaciones largas
- INVENTAR VEHÍCULOS: NUNCA menciones marcas o modelos que NO estén en INVENTARIO DISPONIBLE (ej: JAC, Nissan, Toyota, Hino, International, Kenworth, etc. a menos que aparezcan en el inventario)
- Inventar información, precios, especificaciones o datos que no estén en el inventario
- Calcular financiamiento para unidades que dicen "No" en campo Financiamiento
- Pedir nombre antes de dar el tuyo
- Cambiar de modelo sin confirmación del cliente
- Formato markdown para links (NO uses [texto](url), WhatsApp no lo soporta)
- Repetir la misma ubicación o link de Maps si ya lo diste antes (revisa HISTORIAL)
- Inventar ubicaciones que no estén en el INVENTARIO; solo usa lo que dice el inventario o Tlalnepantla como default
- Decir que una unidad de CARGA "tiene asientos" sin aclarar que son solo de cabina y que la zona trasera es de carga (ver regla 0.5)
- Presentar una unidad sin mencionar si es de CARGA o PASAJEROS en la primera mención

16) NOMBRE OBLIGATORIO ANTES DE COTIZACIÓN O CITA:
- ANTES de dar cotización personalizada, corrida financiera o agendar cita, NECESITAS el nombre del cliente.
- PERO PRIMERO RESPONDE LA PREGUNTA DEL CLIENTE, y LUEGO pide el nombre. NUNCA ignores su pregunta para pedir el nombre.
  * CORRECTO: "El enganche mínimo es del 20%, como referencia ilustrativa. ¿Con quién tengo el gusto para darte más detalles?"
  * CORRECTO: "Claro, ese modelo está en $390,000. ¿Me compartes tu nombre para cotizarte?"
  * INCORRECTO: "Con gusto, ¿con quién tengo el gusto?" (ignorando lo que preguntó el cliente)
- Preguntas GENERALES de financiamiento (enganche, si hay crédito, plazos, mensualidad estimada) SÍ puedes responderlas sin nombre.
- Si ya tienes el nombre (aparece en CLIENTE), NO lo vuelvas a pedir.
- Esto aplica SIEMPRE, sin excepción.
""".strip()


# ============================================================
# FINANCING DATA
# ============================================================
_FINANCING_DATA: Optional[Dict[str, Any]] = None


def _load_financing_data() -> Dict[str, Any]:
    """Load financing data from JSON file (cached)."""
    global _FINANCING_DATA
    if _FINANCING_DATA is not None:
        return _FINANCING_DATA

    financing_path = os.path.join(os.path.dirname(__file__), "..", "data", "financing.json")
    try:
        with open(financing_path, "r", encoding="utf-8") as f:
            _FINANCING_DATA = json.load(f)
            logger.info(f"✅ Financing data loaded: {len(_FINANCING_DATA)} models")
    except FileNotFoundError:
        logger.warning(f"⚠️ Financing file not found: {financing_path}")
        _FINANCING_DATA = {}
    except json.JSONDecodeError as e:
        logger.error(f"❌ Error parsing financing JSON: {e}")
        _FINANCING_DATA = {}

    return _FINANCING_DATA


def _build_financing_text() -> str:
    """Build financing info text for GPT context."""
    data = _load_financing_data()
    if not data:
        return "Corridas de financiamiento no disponibles."

    lines = ["CORRIDAS FINANCIERAS (Banorte - Ilustrativas):"]
    lines.append("Enganche mínimo: 20% | Plazo base: 48 meses | Mensualidades YA incluyen intereses y seguros\n")

    for key, info in data.items():
        nombre = info.get("nombre", "")
        anio = info.get("anio", "")
        transmision = info.get("transmision", "")
        valor = info.get("valor_factura", 0)
        enganche = info.get("enganche_min", 0)
        mensualidad = info.get("pago_mensual_total_mes_1", 0)
        tasa = info.get("tasa_anual_pct", 0)
        cat = info.get("cat_sin_iva_pct", 0)

        trans_text = f" ({transmision})" if transmision else ""
        lines.append(
            f"- {nombre} {anio}{trans_text}: "
            f"Factura ${valor:,.0f} | "
            f"Enganche 20% = ${enganche:,.0f} | "
            f"Mensualidad ~${mensualidad:,.2f} | "
            f"Tasa {tasa}% | CAT {cat}%"
        )

    return "\n".join(lines)


def _detect_pdf_request(user_message: str, last_interest: str, context: Dict[str, Any] = None, bases_pdf_url: str = None) -> Optional[Dict[str, Any]]:
    """
    Detecta si el usuario pide un PDF (ficha técnica, corrida, o bases de campaña).
    Retorna dict con: tipo, pdf_url, filename, mensaje_previo
    O None si no pide PDF.

    Ahora con soporte de contexto para:
    - Typos comunes ("fiche", "fixa", "corrda")
    - Peticiones genéricas ("pásamela", "mándamela") si hubo PDF previo
    - Bases/términos y condiciones de campaña (si bases_pdf_url está disponible)
    """
    msg = (user_message or "").lower()
    context = context or {}

    # === BASES / TÉRMINOS Y CONDICIONES (solo si la campaña tiene este PDF) ===
    if bases_pdf_url:
        bases_keywords = [
            "bases y terminos", "bases y términos",
            "terminos y condiciones", "términos y condiciones",
            "bases de la campaña", "bases de la campana",
            "bases de la dinamica", "bases de la dinámica",
            "bases del concurso", "bases legales",
            "terminos", "términos", "condiciones",
            "bases",
        ]
        if any(k in msg for k in bases_keywords):
            logger.info(f"📄 Bases/T&C solicitadas para campaña")
            return {
                "tipo": "bases",
                "pdf_url": bases_pdf_url,
                "filename": "Bases_y_Terminos_Condiciones.pdf",
                "mensaje": "Aquí tienes las bases y términos y condiciones de la dinámica.",
            }

    # === VERBOS DE ACCIÓN (indican que quieren RECIBIR algo, no solo preguntar) ===
    action_verbs = [
        "mandame", "mándame", "mandala", "mándala", "mandamela", "mándamela",
        "pasame", "pásame", "pasala", "pásala", "pasamela", "pásamela",
        "enviame", "envíame", "enviala", "envíala", "enviamela", "envíamela",
        "comparteme", "compárteme", "compartela", "compártela",
        "dame", "dámela", "la quiero", "si la quiero", "sí la quiero",
        "quiero ver", "quiero la",
    ]
    has_action_verb = any(v in msg for v in action_verbs)

    # === KEYWORDS QUE SIEMPRE ACTIVAN PDF (son específicos, no ambiguos) ===
    ficha_keywords_direct = [
        "ficha", "fiche", "fixa", "ficah",  # typos
        "ficha tecnica", "ficha técnica",
        "hoja tecnica", "hoja técnica", "datos tecnicos", "datos técnicos",
        "specs",
    ]

    corrida_keywords_direct = [
        "corrida", "corrda", "corida",  # typos
        "simulacion", "simulación",
        "tabla de pagos",
        "mensualidades pdf",
    ]

    # === KEYWORDS AMBIGUOS: solo activan PDF si hay verbo de acción ===
    # "¿tienen financiamiento?" = pregunta informativa, NO mandar PDF
    # "mandame el financiamiento" = SÍ mandar PDF
    corrida_keywords_ambiguous = [
        "financiamiento", "especificaciones", "caracteristicas", "características",
        "pagos mensuales", "plan de pagos", "cuotas",
    ]

    pdf_type = None

    # 1) Keywords directos (siempre activan)
    if any(k in msg for k in ficha_keywords_direct):
        pdf_type = "ficha"
        logger.debug(f"📄 Keyword directo de ficha: '{msg}'")
    elif any(k in msg for k in corrida_keywords_direct):
        pdf_type = "corrida"
        logger.debug(f"📄 Keyword directo de corrida: '{msg}'")

    # 2) Keywords ambiguos (solo con verbo de acción)
    if not pdf_type and has_action_verb:
        if any(k in msg for k in ["especificaciones", "caracteristicas", "características"]):
            pdf_type = "ficha"
            logger.debug(f"📄 Keyword ambiguo de ficha + verbo: '{msg}'")
        elif any(k in msg for k in corrida_keywords_ambiguous):
            pdf_type = "corrida"
            logger.debug(f"📄 Keyword ambiguo de corrida + verbo: '{msg}'")

    # 3) Continuación genérica (solo si ya pidió un PDF antes y NO pide fotos)
    if not pdf_type:
        photo_words = ["foto", "fotos", "imagen", "imagenes", "imágenes", "video", "videos"]
        is_photo_request = any(pw in msg for pw in photo_words)
        if not is_photo_request:
            last_pdf_type = context.get("last_pdf_request_type")
            if last_pdf_type and has_action_verb:
                pdf_type = last_pdf_type
                logger.info(f"📄 Petición genérica '{msg}' continuando PDF previo: {pdf_type}")

    if not pdf_type:
        return None

    # Necesitamos un modelo detectado
    if not last_interest:
        logger.info(f"📄 PDF {pdf_type} solicitado pero no hay last_interest")
        return {"tipo": pdf_type, "sin_modelo": True}

    # Buscar el modelo en los datos de financiamiento
    data = _load_financing_data()
    if not data:
        logger.warning(f"📄 PDF {pdf_type} solicitado pero no hay datos de financiamiento")
        return {"tipo": pdf_type, "sin_datos": True}

    # Normalizar el interés para buscar (strip marcas conocidas)
    _brand_strip = ["foton", "freightliner"]
    interest_norm = last_interest.lower()
    for _b in _brand_strip:
        interest_norm = interest_norm.replace(_b, "")
    interest_norm = interest_norm.replace("diesel", "").replace("4x4", "").strip()
    logger.info(f"📄 Buscando modelo para PDF: last_interest='{last_interest}' -> normalizado='{interest_norm}'")

    # Buscar coincidencia
    matched_key = None
    matched_info = None
    best_score = 0
    best_year = 0

    for key, info in data.items():
        nombre = info.get("nombre", "").lower()
        anio = int(info.get("anio", 0))

        # Tokens del modelo (únicos, sin duplicados)
        key_tokens = set(key.lower().replace("_", " ").split())
        nombre_tokens = set(nombre.split())
        all_tokens = key_tokens.union(nombre_tokens)

        # Verificar si hay coincidencia (solo tokens de 2+ caracteres, excluyendo marcas)
        _brand_noise = {"foton", "freightliner"}
        score = 0
        matched_tokens = []
        for token in all_tokens:
            if len(token) >= 2 and token not in _brand_noise and token in interest_norm:
                score += 1
                matched_tokens.append(token)

        # También verificar año - bonus alto si hay coincidencia exacta
        year_str = str(anio)
        if year_str in interest_norm or year_str in last_interest:
            score += 3  # Bonus alto por año exacto
            matched_tokens.append(f"año:{anio}")

        if score > 0:
            logger.debug(f"📄 Candidato '{key}': score={score}, año={anio}, tokens={matched_tokens}")

        # Aceptar si score >= 2
        # Preferir: mayor score, o mismo score pero año más reciente
        if score >= 2:
            is_better = (
                matched_key is None or
                score > best_score or
                (score == best_score and anio > best_year)
            )
            if is_better:
                matched_key = key
                matched_info = info.copy()
                matched_info["_score"] = score
                best_score = score
                best_year = anio

    if not matched_info:
        logger.info(f"📄 No se encontró modelo para '{interest_norm}' en financiamiento")
        return {"tipo": pdf_type, "sin_modelo": True}

    logger.info(f"📄 Modelo matched: '{matched_key}' (score={best_score}, año={best_year}) para '{last_interest}'")

    # Obtener URL del PDF
    if pdf_type == "ficha":
        pdf_url = matched_info.get("pdf_ficha_tecnica")
        if not pdf_url:
            return {"tipo": pdf_type, "sin_pdf": True, "modelo": matched_info.get("nombre", "")}
        filename = f"Ficha_Tecnica_{matched_info.get('nombre', 'Vehiculo').replace(' ', '_')}_{matched_info.get('anio', '')}.pdf"
        mensaje = "Claro, te comparto la ficha tecnica en PDF."
    else:
        pdf_url = matched_info.get("pdf_corrida")
        if not pdf_url:
            return {"tipo": pdf_type, "sin_pdf": True, "modelo": matched_info.get("nombre", "")}
        filename = f"Corrida_Financiamiento_{matched_info.get('nombre', 'Vehiculo').replace(' ', '_')}_{matched_info.get('anio', '')}.pdf"
        mensaje = "Listo, te comparto la simulacion de financiamiento en PDF. Es ilustrativa e incluye intereses."

    return {
        "tipo": pdf_type,
        "pdf_url": pdf_url,
        "filename": filename,
        "mensaje": mensaje,
        "modelo": f"{matched_info.get('nombre', '')} {matched_info.get('anio', '')}"
    }


# ============================================================
# INVENTORY HELPERS
# ============================================================
def _safe_get(item: Dict[str, Any], keys: List[str], default: str = "") -> str:
    """Return first non-empty string for given keys."""
    for k in keys:
        v = item.get(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default


def _format_price(precio: str, moneda: str, iva: str) -> str:
    """Precio limpio: '$499,000 MXN IVA incluido'."""
    try:
        num = float(precio.replace(",", "").replace(" ", ""))
        formatted = f"${num:,.0f}"
    except (ValueError, AttributeError):
        formatted = f"${precio}" if precio else "Consultar"
    cur = moneda if moneda else "MXN"
    iva_txt = " IVA incluido" if iva and iva.upper() == "TRUE" else ""
    return f"{formatted} {cur}{iva_txt}"


def _summarize_motor(raw: str) -> str:
    """Extrae lo útil del bloque MOTOR y lo resume en una frase."""
    if not raw:
        return ""
    lines = [l.strip() for l in raw.replace("\r", "").split("\n") if l.strip()]
    parts = {}
    for line in lines:
        if ":" in line:
            k, v = line.split(":", 1)
            parts[k.strip().lower()] = v.strip()
        else:
            parts.setdefault("extra", line)

    brand = parts.get("marca", "")
    cil = parts.get("cilindrada", "")
    potencia = parts.get("potencia", "")

    pieces = []
    if brand:
        pieces.append(brand)
    if cil:
        pieces.append(cil)
    if potencia:
        pieces.append(potencia)
    return ", ".join(pieces) if pieces else raw.split("\n")[0][:80]


def _summarize_capacity(raw: str) -> str:
    """'Carga maxima: 900 kg' → '900 kg'. 'Carga sobre chasis 3,700 kg' → '3.7 ton'."""
    if not raw:
        return ""
    m = re.search(r"([\d,\.]+)\s*kg", raw, re.IGNORECASE)
    if m:
        try:
            kg = float(m.group(1).replace(",", ""))
            if kg >= 1000:
                return f"{kg/1000:.1f} toneladas"
            return f"{kg:.0f} kg"
        except ValueError:
            pass
    if "tonelada" in raw.lower():
        return raw.strip()
    return raw.split("\n")[0].strip()[:60]


def _normalize_fuel(raw: str) -> str:
    """Normaliza combustible a 'Gasolina' o 'Diésel'."""
    if not raw:
        return ""
    low = raw.lower()
    if "diesel" in low or "diésel" in low:
        return "Diésel"
    if "gasolina" in low:
        return "Gasolina"
    return raw.strip()[:30]


def _build_inventory_text(inventory_service) -> str:
    items = getattr(inventory_service, "items", None) or []
    if not items:
        return "Inventario no disponible."

    lines: List[str] = []
    for item in items:
        marca = _safe_get(item, ["Marca", "marca"])
        modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"], default="(sin modelo)")
        anio = _safe_get(item, ["Anio", "Año", "anio"], default="")
        precio = _safe_get(item, ["Precio", "precio"], default="N/D")
        moneda = _safe_get(item, ["moneda"], default="MXN")
        iva = _safe_get(item, ["iva_incluido"], default="")
        cantidad = _safe_get(item, ["Cantidad", "cantidad"], default="1")
        colores = _safe_get(item, ["Colores", "colores"], default="")

        condicion = _safe_get(item, ["condicion", "Condicion", "Condición"])

        price_str = _format_price(precio, moneda, iva)
        label = f"{marca} {modelo}".strip() if marca else modelo
        info = f"- {label} {anio}: {price_str}"

        if condicion and condicion.strip().lower() == "demo":
            info += " [DEMO]"
        elif condicion and condicion.strip().lower() == "seminuevo":
            info += " [SEMINUEVO]"

        try:
            cant = int(cantidad)
            if cant > 1:
                info += f" ({cant} unidades)"
        except (ValueError, TypeError):
            pass

        if colores:
            info += f" | Colores: {colores}"

        # Tracción (4x2, 4x4)
        traccion = _safe_get(item, ["Traccion", "Tracción", "traccion"])
        if traccion:
            info += f" | Tracción: {traccion}"

        # Descripción corta (contexto adicional del Sheet)
        desc_corta = _safe_get(item, ["descripcion_corta"])
        if desc_corta:
            info += f" | Desc: {desc_corta}"

        # Tipo de uso: CARGA vs PASAJEROS (inferido del modelo)
        modelo_lower = modelo.lower()
        tipo_uso = _safe_get(item, ["TipoUso", "tipo_uso", "tipouso"])
        if not tipo_uso:
            if any(kw in modelo_lower for kw in ("panel", "chasis", "volteo", "revolvedora")):
                tipo_uso = "CARGA"
            elif any(kw in modelo_lower for kw in ("pasajero", "bus", "escolar")):
                tipo_uso = "PASAJEROS"
            elif any(kw in modelo_lower for kw in ("esta", "miler")):
                tipo_uso = "CARGA"
        if tipo_uso:
            info += f" | Uso: {tipo_uso}"

        # Tipo de cabina y asientos (desde CSV)
        tipo_cabina = _safe_get(item, ["TipoCabina", "tipocabina", "tipo_cabina"])
        asientos = _safe_get(item, ["Asientos", "asientos"])
        if tipo_cabina:
            cab_info = tipo_cabina
            if asientos:
                cab_qualifier = " en cabina" if tipo_uso == "CARGA" else ""
                cab_info += f", {asientos} asientos{cab_qualifier}"
            info += f" | {cab_info}"

        # Specs opcionales (solo si el CSV/Sheet tiene datos)
        combustible = _normalize_fuel(_safe_get(item, ["COMBUSTIBLE", "combustible"]))
        motor = _summarize_motor(_safe_get(item, ["MOTOR", "motor"]))
        capacidad = _summarize_capacity(_safe_get(item, ["CAPACIDAD DE CARGA"]))
        transmision = _safe_get(item, ["Transmision", "Transmisión", "transmision"])
        paso = _safe_get(item, ["Paso", "paso"])
        rodada = _safe_get(item, ["Rodada", "rodada"])
        eje_del = _safe_get(item, ["EjeDelantera", "Eje Delantera", "ejedelantera"])
        eje_tras = _safe_get(item, ["EjeTrasera", "Eje Trasera", "ejetrasera"])
        dormitorio = _safe_get(item, ["Dormitorio", "dormitorio"])

        specs = []
        if combustible:
            specs.append(f"Combustible: {combustible}")
        if motor:
            specs.append(f"Motor: {motor}")
        if capacidad:
            specs.append(f"Carga: {capacidad}")
        if transmision:
            specs.append(f"Transmisión: {transmision}")
        if paso:
            specs.append(f"Paso: {paso}")
        if rodada:
            specs.append(f"Rodada: {rodada}")
        if eje_del:
            specs.append(f"Eje Del.: {eje_del}")
        if eje_tras:
            specs.append(f"Eje Tras.: {eje_tras}")
        if dormitorio:
            specs.append(f"Dormitorio: {dormitorio}")
        if specs:
            info += " | " + ", ".join(specs)

        # Financiamiento disponible (desde el Sheet) - normalizar a Sí/No
        financiamiento_raw = _safe_get(item, ["Financiamiento", "financiamiento"])
        if financiamiento_raw:
            fin_lower = str(financiamiento_raw).strip().lower()
            if fin_lower in ("false", "no", "no aplica", "solo contado", "sin credito"):
                info += " | Financiamiento: No"
            elif fin_lower in ("true", "si", "sí"):
                info += " | Financiamiento: Sí"
            else:
                info += f" | Financiamiento: {financiamiento_raw}"

        # Ubicación (dinámica desde el Sheet)
        ubicacion = _safe_get(item, ["ubicacion", "Ubicacion", "ubicación"])
        if ubicacion:
            ubicacion_link = _safe_get(item, ["ubicacion_link"])
            if ubicacion_link:
                info += f" | Ubicación: {ubicacion} (Maps: {ubicacion_link})"
            else:
                info += f" | Ubicación: {ubicacion}"

        lines.append(info)

    return "\n".join(lines)


def _build_focused_inventory_text(inventory_service, last_interest: str) -> str:
    """Build inventory text for only the model of interest (saves tokens)."""
    items = getattr(inventory_service, "items", None) or []
    if not items or not last_interest:
        return ""

    interest_norm = _normalize_spanish(last_interest)
    interest_tokens = [t for t in interest_norm.split() if len(t) >= 2 and t not in {"foton", "freightliner", "camion", "camión"}]

    # Detect year tokens (e.g. "2023", "2024") in the interest string
    year_tokens = [t for t in interest_tokens if re.fullmatch(r"20\d{2}", t)]
    model_tokens = [t for t in interest_tokens if not re.fullmatch(r"20\d{2}", t)]

    matched_infos: list[str] = []
    for item in items:
        modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"]).strip()
        if not modelo:
            continue
        modelo_norm = _normalize_spanish(modelo)
        # Must match at least one model token (non-year)
        if not model_tokens or not any(tok in modelo_norm for tok in model_tokens):
            continue
        # If a year was specified in the interest, filter by year too
        anio = _safe_get(item, ["Anio", "Año", "anio"], default="")
        if year_tokens and anio and not any(yt == anio.strip() for yt in year_tokens):
            continue

        precio = _safe_get(item, ["Precio", "precio"], default="N/D")
        moneda = _safe_get(item, ["moneda"], default="MXN")
        iva = _safe_get(item, ["iva_incluido"], default="")
        marca = _safe_get(item, ["Marca", "marca"])
        condicion = _safe_get(item, ["condicion", "Condicion", "Condición"])
        price_str = _format_price(precio, moneda, iva)
        label = f"{marca} {modelo}".strip() if marca else modelo
        info = f"Modelo de interés: {label} {anio}: {price_str}"

        if condicion and condicion.strip().lower() == "demo":
            info += " [DEMO]"
        elif condicion and condicion.strip().lower() == "seminuevo":
            info += " [SEMINUEVO]"

        # Tipo de uso: CARGA vs PASAJEROS
        modelo_lower = modelo.lower()
        tipo_uso = _safe_get(item, ["TipoUso", "tipo_uso", "tipouso"])
        if not tipo_uso:
            if any(kw in modelo_lower for kw in ("panel", "chasis", "volteo", "revolvedora")):
                tipo_uso = "CARGA"
            elif any(kw in modelo_lower for kw in ("pasajero", "bus", "escolar")):
                tipo_uso = "PASAJEROS"
            elif any(kw in modelo_lower for kw in ("esta", "miler")):
                tipo_uso = "CARGA"
        if tipo_uso:
            info += f" | Uso: {tipo_uso}"

        # Tipo de cabina y asientos
        tipo_cabina = _safe_get(item, ["TipoCabina", "tipocabina", "tipo_cabina"])
        asientos = _safe_get(item, ["Asientos", "asientos"])
        if tipo_cabina:
            cab_info = tipo_cabina
            if asientos:
                cab_qualifier = " en cabina" if tipo_uso == "CARGA" else ""
                cab_info += f", {asientos} asientos{cab_qualifier}"
            info += f" | {cab_info}"

        # Specs adicionales para modelo enfocado
        specs = []
        combustible = _normalize_fuel(_safe_get(item, ["COMBUSTIBLE", "combustible"]))
        motor = _summarize_motor(_safe_get(item, ["MOTOR", "motor"]))
        capacidad = _summarize_capacity(_safe_get(item, ["CAPACIDAD DE CARGA"]))
        transmision = _safe_get(item, ["Transmision", "Transmisión", "transmision"])
        paso = _safe_get(item, ["Paso", "paso"])
        rodada = _safe_get(item, ["Rodada", "rodada"])
        eje_del = _safe_get(item, ["EjeDelantera", "Eje Delantera", "ejedelantera"])
        eje_tras = _safe_get(item, ["EjeTrasera", "Eje Trasera", "ejetrasera"])
        dormitorio = _safe_get(item, ["Dormitorio", "dormitorio"])
        if combustible:
            specs.append(f"Combustible: {combustible}")
        if motor:
            specs.append(f"Motor: {motor}")
        if capacidad:
            specs.append(f"Carga: {capacidad}")
        if transmision:
            specs.append(f"Transmisión: {transmision}")
        if paso:
            specs.append(f"Paso: {paso}")
        if rodada:
            specs.append(f"Rodada: {rodada}")
        if eje_del:
            specs.append(f"Eje Del.: {eje_del}")
        if eje_tras:
            specs.append(f"Eje Tras.: {eje_tras}")
        if dormitorio:
            specs.append(f"Dormitorio: {dormitorio}")
        if specs:
            info += " | " + ", ".join(specs)

        # Financiamiento disponible (desde el Sheet) - normalizar a Sí/No
        financiamiento_raw = _safe_get(item, ["Financiamiento", "financiamiento"])
        if financiamiento_raw:
            fin_lower = str(financiamiento_raw).strip().lower()
            if fin_lower in ("false", "no", "no aplica", "solo contado", "sin credito"):
                info += " | Financiamiento: No"
            elif fin_lower in ("true", "si", "sí"):
                info += " | Financiamiento: Sí"
            else:
                info += f" | Financiamiento: {financiamiento_raw}"

        # Ubicación (dinámica desde el Sheet)
        ubicacion = _safe_get(item, ["ubicacion", "Ubicacion", "ubicación"])
        if ubicacion:
            ubicacion_link = _safe_get(item, ["ubicacion_link"])
            if ubicacion_link:
                info += f" | Ubicación: {ubicacion} (Maps: {ubicacion_link})"
            else:
                info += f" | Ubicación: {ubicacion}"

        matched_infos.append(info)

    return "\n".join(matched_infos) if matched_infos else ""


def _extract_photos_from_item(item: Dict[str, Any]) -> List[str]:
    raw = _safe_get(item, ["photos", "photo", "foto", "imagen", "imagenes", "fotos"])
    if not raw:
        return []
    # Support "|", ",", or newline as separators (Google Sheets multi-line cells use \n)
    import re as _re
    parts = _re.split(r"[|\n,]+", raw)
    return [u.strip() for u in parts if u.strip().startswith("http")]


def _extract_location_link(
    inventory_service, last_interest: str, interest_ubicacion: str = "", user_city: str = ""
) -> Optional[str]:
    """Extract ubicacion_link from inventory for the model of interest.
    Priority: 1) interest_ubicacion (explicit), 2) user_city slot, 3) first matching item.
    """
    items = getattr(inventory_service, "items", None) or []
    if not items or not last_interest:
        return None

    interest_norm = _normalize_spanish(last_interest)
    interest_tokens = [t for t in interest_norm.split() if len(t) >= 2
                       and t not in {"foton", "freightliner", "camion", "camión"}]
    if not interest_tokens:
        return None

    ubic_norm = _normalize_spanish(interest_ubicacion) if interest_ubicacion else ""
    user_city_clean = _strip_accents(_normalize_spanish(user_city)) if user_city else ""
    best_link = None       # fallback: first matching item
    city_match_link = None  # secondary: user_city match

    for item in items:
        modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"]).strip()
        if not modelo:
            continue
        modelo_norm = _normalize_spanish(modelo)
        if not any(tok in modelo_norm for tok in interest_tokens):
            continue

        link = _safe_get(item, ["ubicacion_link"])
        if not link:
            continue

        item_ubic_raw = _safe_get(item, ["ubicacion", "Ubicacion", "ubicación"])
        item_ubic = _strip_accents(_normalize_spanish(item_ubic_raw))

        # Priority 1: explicit interest_ubicacion match → return immediately
        if ubic_norm:
            ubic_norm_clean = _strip_accents(ubic_norm)
            if ubic_norm_clean in item_ubic or item_ubic in ubic_norm_clean:
                return link

        # Priority 2: user_city slot match
        if user_city_clean and not city_match_link:
            if user_city_clean in item_ubic or item_ubic in user_city_clean:
                city_match_link = link
                logger.info(f"📍 Location link resolved via user_city='{user_city}': {item_ubic_raw}")

        # Fallback: first matching link
        if not best_link:
            best_link = link

    return city_match_link or best_link


def _detect_vehicle_ubicacion(
    user_message: str, inventory_service, last_interest: str
) -> Optional[str]:
    """Detect if the user mentions a location that matches a specific inventory
    item's ubicacion for the model of interest.

    E.g. "Cascadia de León" → returns "León" (the unit's location, not the client's city).
    Only returns a value when there are multiple units of the same model in
    different locations, so disambiguation is meaningful.
    """
    items = getattr(inventory_service, "items", None) or []
    if not items or not last_interest:
        return None

    interest_norm = _normalize_spanish(last_interest)
    interest_tokens = [t for t in interest_norm.split() if len(t) >= 2
                       and t not in {"foton", "freightliner", "camion", "camión"}]
    if not interest_tokens:
        return None

    msg_norm = _strip_accents(_normalize_spanish(user_message))

    # Collect ubicaciones for matching model items
    model_ubicaciones: List[str] = []
    for item in items:
        modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"]).strip()
        if not modelo:
            continue
        modelo_norm = _normalize_spanish(modelo)
        if not any(tok in modelo_norm for tok in interest_tokens):
            continue
        ubic = _safe_get(item, ["ubicacion", "Ubicacion", "ubicación"]).strip()
        if ubic:
            model_ubicaciones.append(ubic)

    # Only disambiguate if there are multiple distinct locations
    unique_locations = list({_normalize_spanish(u) for u in model_ubicaciones})
    if len(unique_locations) < 2:
        return None

    # Check if user message mentions any of these locations
    for ubic_raw, ubic_norm in zip(model_ubicaciones, [_normalize_spanish(u) for u in model_ubicaciones]):
        # Extract city-like tokens from the ubicacion (e.g. "Zapata Camiones León" → "leon")
        ubic_norm_clean = _strip_accents(ubic_norm)
        ubic_tokens = [t for t in ubic_norm_clean.split() if len(t) >= 3
                       and t not in {"zapata", "camiones", "tractos", "max", "sucursal"}]
        for tok in ubic_tokens:
            if re.search(r'\b' + re.escape(tok) + r'\b', msg_norm):
                logger.info(f"📍 Vehicle ubicacion detected from message: '{ubic_raw}' (token: '{tok}')")
                return ubic_raw

    return None


# ============================================================
# NAME / PAYMENT / APPOINTMENT EXTRACTION
# ============================================================
def _extract_name_from_text(text: str, history: str = "") -> Optional[str]:
    """Extract probable customer name (conservative).
    Now with context awareness: if the bot just asked for the name,
    accept a plain name reply like "Pedro García".
    """
    t = (text or "").strip()
    if not t:
        return None

    bad = {
        # Pronombres / genéricos
        "aqui", "aquí", "nadie", "yo", "el", "ella", "amigo", "desconocido",
        "cliente", "usuario", "quien", "quién",
        # Respuestas cortas
        "si", "sí", "no", "bueno", "ok", "okey", "hola", "bien", "gracias",
        "vale", "perfecto", "listo", "claro", "sale", "dale",
        # Preguntas
        "que", "qué", "como", "cómo", "cuando", "cuándo", "donde", "dónde",
        # Palabras del negocio (NO son nombres)
        "precio", "fotos", "foto", "info", "información", "informacion",
        "ubicación", "ubicacion", "costo", "interesado", "interesada",
        "cotización", "cotizacion", "modelo", "camioneta", "camion", "camión",
        "credito", "crédito", "contado", "financiamiento",
        # Verbos comunes en respuestas
        "quiero", "necesito", "busco", "tengo", "puedo", "estoy",
    }

    # Rechazar si contiene números o signos de pregunta
    if re.search(r'[0-9?¿!¡]', t):
        return None

    # Palabras comunes en español que se capturan DESPUÉS del nombre real
    # Ej: "con Eduardo Vera disculpa en dónde..." → "disculpa" no es nombre
    trailing_stop = {
        "disculpa", "disculpe", "disculpen", "perdón", "perdon", "perdona",
        "oye", "oiga", "mira", "mire",
        "quisiera", "quería", "queria", "necesito", "quiero",
        "me", "te", "se", "le", "nos",
        "en", "de", "del", "por", "para", "con",
        "una", "un", "la", "el", "lo", "las", "los",
        "favor", "pregunta", "consulta", "duda",
        "buenos", "buenas", "buen",
    }

    # 1) Explicit patterns (prefixed)
    patterns = [
        r"\bme llamo\s+([A-Za-zÁÉÍÓÚÑÜáéíóúñü]+(?:\s+[A-Za-zÁÉÍÓÚÑÜáéíóúñü]+){0,3})\b",
        r"\bsoy\s+([A-Za-zÁÉÍÓÚÑÜáéíóúñü]+(?:\s+[A-Za-zÁÉÍÓÚÑÜáéíóúñü]+){0,3})\b",
        r"\bmi nombre es\s+([A-Za-zÁÉÍÓÚÑÜáéíóúñü]+(?:\s+[A-Za-zÁÉÍÓÚÑÜáéíóúñü]+){0,3})\b",
        r"\bcon\s+([A-Za-zÁÉÍÓÚÑÜáéíóúñü]+(?:\s+[A-Za-zÁÉÍÓÚÑÜáéíóúñü]+){0,2})\b",
    ]

    for p in patterns:
        m = re.search(p, t, flags=re.IGNORECASE)
        if m:
            name = m.group(1).strip()
            # Trim trailing non-name words: "Eduardo Vera Disculpa" → "Eduardo Vera"
            words = name.split()
            while words and words[-1].lower() in trailing_stop:
                words.pop()
            if not words:
                return None
            name = " ".join(words)
            if name.lower() in bad:
                return None
            return " ".join(w.capitalize() for w in name.split())

    # 2) Context-aware: if the bot's last message asked for the name,
    #    accept a plain reply of 1-4 words as a name
    if history:
        last_bot_line = ""
        for line in reversed(history.split("\n")):
            if line.strip().startswith("A:"):
                last_bot_line = line.lower()
                break
        name_asking = [
            "tu nombre", "cómo te llamas", "como te llamas",
            "me compartes tu nombre", "me das tu nombre",
            "a nombre de quién", "a nombre de quien",
            "quién me busca", "quien me busca",
            "nombre del interesado", "nombre completo",
            "con quién tengo el gusto", "con quien tengo el gusto",
        ]
        if any(k in last_bot_line for k in name_asking):
            words = t.split()
            if 1 <= len(words) <= 4:
                all_alpha = all(
                    re.match(r'^[A-Za-zÁÉÍÓÚÑÜáéíóúñü.]+$', w) for w in words
                )
                if all_alpha and words[0].lower() not in bad:
                    name = " ".join(w.capitalize() for w in words)
                    logger.info(f"📛 Nombre detectado por contexto: '{name}'")
                    return name

    return None


def _extract_payment_from_text(text: str) -> Optional[str]:
    msg = (text or "").lower()

    # Negación: detectar si hay rechazo antes del keyword
    negation_patterns = [
        r"\bno\b.{0,15}\b(crédito|credito|financiamiento|financiación|mensualidades)\b",
        r"\bsin\b.{0,15}\b(crédito|credito|financiamiento|financiación)\b",
        r"\bnada de\b.{0,10}\b(crédito|credito|financiamiento)\b",
    ]
    negation_patterns_contado = [
        r"\bno\b.{0,15}\b(contado|cash)\b",
        r"\bsin\b.{0,15}\b(contado|cash)\b",
    ]

    # "no quiero crédito" → detectar como Contado (quiere pagar cash)
    if any(re.search(p, msg) for p in negation_patterns):
        logger.info(f"📛 Negación de crédito detectada → Contado: '{msg[:60]}'")
        return "Contado"

    # "no de contado" → detectar como Crédito
    if any(re.search(p, msg) for p in negation_patterns_contado):
        logger.info(f"📛 Negación de contado detectada → Crédito: '{msg[:60]}'")
        return "Crédito"

    # Detección positiva normal
    if any(k in msg for k in ["contado", "cash", "de contado"]):
        return "Contado"
    if any(k in msg for k in ["crédito", "credito", "financiamiento", "financiación", "mensualidades"]):
        return "Crédito"
    return None


def _detect_disinterest(text: str) -> bool:
    """
    V2: Detecta si el lead expresa desinterés explícito.
    Retorna True si el mensaje indica que el lead quiere parar.
    """
    if not text:
        return False

    t = text.strip()

    # Exact matches (case-sensitive for STOP/BAJA)
    if t in ("STOP", "BAJA"):
        return True

    t_lower = t.lower()
    disinterest_phrases = [
        "no me interesa",
        "ya no quiero",
        "no gracias",
        "no, gracias",
        "cancela",
        "cancelar",
        "ya no me interesa",
        "no estoy interesado",
        "no quiero nada",
        "dejen de escribirme",
        "no me escriban",
        "basta",
    ]

    return any(phrase in t_lower for phrase in disinterest_phrases)


def _normalize_spanish(text: str) -> str:
    t = (text or "").lower()

    # Typos de marca
    t = t.replace("miller", "miler")
    t = t.replace("vanesa", "toano")
    t = t.replace("freight liner", "freightliner")
    t = t.replace("freigthliner", "freightliner")
    t = t.replace("freighliner", "freightliner")

    # Typos de modelo
    t = re.sub(r"\btunlan\b", "tunland", t)
    t = re.sub(r"\btunlad\b", "tunland", t)
    t = re.sub(r"\btunlnad\b", "tunland", t)
    t = re.sub(r"\bcascadía\b", "cascadia", t)
    t = re.sub(r"\bcaskadia\b", "cascadia", t)

    # Aliases naturales → nombre de modelo para matching
    # Pickups / Tunland
    alias_map = [
        # E5
        (r"\bla e5\b", "tunland e5"),
        (r"\bel e5\b", "tunland e5"),
        # G7
        (r"\bla g7\b", "tunland g7"),
        (r"\bel g7\b", "tunland g7"),
        # G9
        (r"\bla g9\b", "tunland g9"),
        (r"\bel g9\b", "tunland g9"),
        # Genéricos pickup → no mapear a modelo específico, solo normalizar
        (r"\bla pickup\b", "tunland"),
        (r"\bla troca\b", "tunland"),
        (r"\bla camioneta\b", "tunland"),
        (r"\bla doble cabina\b", "tunland"),
        # Toano
        (r"\bla van\b", "toano panel"),
        (r"\bla panel\b", "toano panel"),
        (r"\bla combi\b", "toano panel"),
        # Miler
        (r"\bel camioncito\b", "miler"),
        (r"\bel miler\b", "miler"),
        (r"\bel de 3 toneladas\b", "miler"),
        (r"\bel de carga\b", "miler"),
        # EST-A / tractocamión
        (r"\bel tracto\b", "6x4"),
        (r"\bel tractocamion\b", "6x4"),
        (r"\bel tractocamión\b", "6x4"),
        (r"\bla esta\b", "6x4"),
        (r"\bel camion grande\b", "6x4"),
        (r"\bel camión grande\b", "6x4"),
        # Cascadia
        (r"\bla cascadia\b", "cascadia"),
        (r"\bel cascadia\b", "cascadia"),
    ]

    for pattern, replacement in alias_map:
        t = re.sub(pattern, replacement, t)

    return t


def _strip_accents(text: str) -> str:
    """Remove accents for comparison: León → Leon, Querétaro → Queretaro."""
    if not text:
        return text
    nfkd = unicodedata.normalize('NFKD', text)
    return ''.join(c for c in nfkd if not unicodedata.combining(c))


def _detect_model_switch(user_message: str, current_interest: str, inventory_service) -> Optional[str]:
    """
    Detect if the client wants to switch to a different vehicle model during a campaign.
    Returns the new model label if a switch is detected, None otherwise.

    Signals:
    - Negation of current model: "no quiero Cascadia", "que no, una E5"
    - Explicit mention of a different model in the same message
    """
    if not user_message or not current_interest:
        return None

    msg_lower = user_message.lower()
    current_norm = _normalize_spanish(current_interest).lower()

    # Detect negation patterns that suggest rejection of current interest
    _negation_phrases = [
        "no quiero", "que no quiero", "no me interesa", "no busco",
        "ya no quiero", "no, quiero", "mas bien", "más bien",
        "me equivoqué", "me equivoque", "quise decir", "prefiero",
    ]
    has_negation = any(p in msg_lower for p in _negation_phrases)

    # Try to extract a new interest from the current message
    new_interest = _extract_interest_from_messages(user_message, "", inventory_service)
    if not new_interest:
        return None

    new_norm = _normalize_spanish(new_interest).lower()

    # Check if the new interest is genuinely different from current
    # Compare significant tokens (strip brand names that appear in both)
    _brand_noise = {"foton", "freightliner"}
    current_tokens = set(current_norm.split()) - _brand_noise
    new_tokens = set(new_norm.split()) - _brand_noise

    # If they share significant tokens, it's the same model
    if current_tokens & new_tokens:
        return None

    # Different model detected — if there's negation OR the user message
    # doesn't mention the current model at all, treat it as a switch
    current_keywords = [t for t in current_tokens if len(t) > 2]
    current_mentioned = any(kw in msg_lower for kw in current_keywords)

    if has_negation or not current_mentioned:
        logger.info(f"🔄 Model switch detectado: {current_interest} → {new_interest} "
                     f"(negation={has_negation}, current_mentioned={current_mentioned})")
        return new_interest

    return None


def _extract_interest_from_messages(user_message: str, reply: str, inventory_service) -> Optional[str]:
    """Infer model interest by matching inventory model tokens in user message or bot reply."""
    items = getattr(inventory_service, "items", None) or []
    if not items:
        return None

    msg_norm = _normalize_spanish(user_message)
    rep_norm = _normalize_spanish(reply)

    # Palabras comunes en español que NO deben usarse como tokens de matching
    # "esta/este/estan" causan falsos positivos con el modelo EST-A
    _noise = {
        "foton", "freightliner", "camion", "camión",
        "esta", "este", "estos", "estas", "estan", "están",
        "gris", "azul", "rojo", "negro", "blanco", "plata",
        "at", "mt", "diesel",
    }

    best: Optional[str] = None
    best_score = 0
    best_anio: str = ""

    # Strip trade-in / customer's own vehicle context before extracting year
    # Phrases like "mi carro X 2016", "tengo un Nissan 2018", "recibirían mi auto 2020"
    # contain years that belong to the customer's car, not our inventory
    _tradein_patterns = [
        r"(?:recib[ií]r[ií]an|aceptan|toman|reciben)\s+mi\s+\w+[\w\s]*?\d{4}",
        r"mi\s+(?:carro|auto|coche|camioneta|vehiculo|vehículo|unidad|pickup|troca)\s+[\w\s]*?\d{4}",
        r"tengo\s+(?:un|una|mi)\s+[\w\s]*?\d{4}",
        r"(?:doy|dejo|entrego)\s+(?:mi|un|una)\s+[\w\s]*?\d{4}",
    ]
    msg_for_year = msg_norm
    for tp in _tradein_patterns:
        msg_for_year = re.sub(tp, "", msg_for_year, flags=re.IGNORECASE)

    # Detect year mentioned in user message (e.g. "ESTA 2023"), excluding trade-in context
    year_in_msg = re.search(r'\b(20\d{2})\b', msg_for_year)

    for item in items:
        modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"]).strip()
        if not modelo:
            continue

        modelo_norm = _normalize_spanish(modelo)
        anio = _safe_get(item, ["Anio", "Año", "anio"], default="").strip()
        # Permitir tokens de 2 caracteres para detectar G9, E5, G7, etc.
        tokens = [t for t in modelo_norm.split() if len(t) >= 2 and t not in _noise]
        if not tokens:
            continue

        score = 0
        for tok in tokens:
            # Usar word boundary para evitar "esta" matcheando "estaría"
            pat = re.compile(r'\b' + re.escape(tok) + r'\b')
            if pat.search(msg_norm):
                score += 2
            if pat.search(rep_norm):
                score += 1

        # Bonus score when user mentions a year and item's year matches
        if year_in_msg and anio == year_in_msg.group(1):
            score += 3
        # Penalize when user mentions a year but item's year doesn't match
        elif year_in_msg and anio and anio != year_in_msg.group(1):
            score -= 2

        if score > best_score:
            best_score = score
            best = modelo
            best_anio = anio

    if best_score >= 2:
        # Append year to interest so downstream functions can filter by it
        if best_anio:
            return f"{best} {best_anio}"
        return best

    return None


def _extract_appointment_from_text(text: str) -> Optional[str]:
    """Basic Spanish appointment extractor for day/time."""
    t = (text or "").strip().lower()
    if not t:
        return None

    day: Optional[str] = None
    if "mañana" in t:
        day = "Mañana"
    else:
        days = ["lunes", "martes", "miércoles", "miercoles", "jueves", "viernes", "sábado", "sabado", "domingo"]
        for d in days:
            if d in t:
                day = d.capitalize().replace("Miercoles", "Miércoles").replace("Sabado", "Sábado")
                break

    time_str: Optional[str] = None

    # "medio dia" o "mediodía"
    if "medio dia" in t or "mediodía" in t or "medio día" in t:
        time_str = "12:00"

    if not time_str:
        m = re.search(r"\b(\d{1,2})\s*y\s*media\b", t)
        if m:
            h = int(m.group(1))
            time_str = f"{h}:30"

    if not time_str:
        m = re.search(r"\b(\d{1,2})\s*:\s*(\d{2})\b", t)
        if m:
            h = int(m.group(1))
            mm = int(m.group(2))
            if 0 <= h <= 23 and 0 <= mm <= 59:
                time_str = f"{h}:{mm:02d}"

    if not time_str:
        m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", t)
        if m:
            h = int(m.group(1))
            mer = m.group(2)
            if 1 <= h <= 12:
                hh = h % 12
                if mer == "pm":
                    hh += 12
                time_str = f"{hh}:00"

    if not time_str:
        if "en la tarde" in t or "por la tarde" in t:
            time_str = "(tarde)"
        elif "en la mañana" in t or "por la mañana" in t:
            time_str = "(mañana)"
        elif "en la noche" in t or "por la noche" in t:
            time_str = "(noche)"

    def _pretty_time_24_to_12(h24: int, mm: str) -> str:
        if h24 == 0:
            return f"12:{mm} AM"
        if 1 <= h24 <= 11:
            return f"{h24}:{mm} AM"
        if h24 == 12:
            return f"12:{mm} PM"
        return f"{h24 - 12}:{mm} PM"

    if day and time_str:
        if re.fullmatch(r"\d{1,2}:\d{2}", time_str):
            h24 = int(time_str.split(":")[0])
            mm = time_str.split(":")[1]
            return f"{day} {_pretty_time_24_to_12(h24, mm)}"
        return f"{day} {time_str}"

    if day and not time_str:
        return day

    if time_str and not day:
        if re.fullmatch(r"\d{1,2}:\d{2}", time_str):
            h24 = int(time_str.split(":")[0])
            mm = time_str.split(":")[1]
            return _pretty_time_24_to_12(h24, mm)
        return time_str

    return None


def _message_confirms_appointment(text: str) -> bool:
    """
    Detecta si el mensaje es una confirmación de cita.
    Solo coincidencias exactas para evitar falsos positivos.
    """
    t = (text or "").strip().lower()
    if not t:
        return False

    confirmations = [
        "vale", "ok", "okey", "si", "sí", "listo", "perfecto",
        "nos vemos", "ahí nos vemos", "mañana nos vemos",
        "de acuerdo", "confirmo", "gracias", "está bien",
        "entendido", "excelente", "claro", "bien", "sale"
    ]

    return t in confirmations


# ============================================================
# PHOTOS LOGIC (🔥 CON MEMORIA DE ÍNDICE)
# ============================================================
def _pick_media_urls(
    user_message: str,
    reply: str,
    inventory_service,
    context: Dict[str, Any],
) -> List[str]:
    """
    Devuelve lista de URLs de fotos según el modelo detectado.
    Ahora con MEMORIA: guarda en context['photo_index'] para saber cuál foto va.
    """
    msg = _normalize_spanish(user_message)

    # 1) Si piden ubicación, no mandar fotos
    gps_keywords = ["ubicacion", "ubicación", "donde estan", "dónde están", "direccion", "dirección", "mapa", "donde se ubican"]
    if any(k in msg for k in gps_keywords):
        return []

    items = getattr(inventory_service, "items", None) or []
    if not items:
        return []

    # 2) Verificar si piden fotos EXPLÍCITAMENTE (con word boundaries)
    # Usa regex \b para evitar falsos positivos como "¿esta foto es real?"
    # que no es una petición sino una pregunta sobre una foto ya enviada
    explicit_photo_patterns = [
        r"\b(mandame|mándame|pasame|pásame|enviame|envíame|comparteme|compárteme)\b.{0,10}\b(foto|fotos|photos?|imagen|imagenes|imágenes)\b",
        r"\b(ver|quiero)\b.{0,10}\b(foto|fotos|photos?|imagen|imagenes|imágenes)\b",
        r"\b(enseñame|enséñame|muestrame|muéstrame)\b.{0,10}\b(foto|fotos|photos?)\b",
        r"\bfotos\b",  # "fotos" plural casi siempre es petición
        r"\bphotos?\b",  # English variants: "photo" / "photos"
        r"\buna\s+foto\b",  # "una foto por fa" es petición explícita
        r"\bfoto\b.{0,15}\b(por\s*fa|porfa|por\s*favor|porfavor|please|plis|plz)\b",  # "foto por fa/favor"
    ]
    # Foto singular solo cuenta si NO es pregunta sobre foto ya enviada
    singular_photo_question = bool(re.search(r"\b(esta|esa|la|cual|cuál)\s+(foto|photo)\b", msg))

    # Keywords que SOLO funcionan si ya hay contexto de fotos (photo_model existe)
    context_photo_keywords = ["otra foto", "mas fotos", "más fotos", "siguiente foto", "otra imagen", "more photos", "another photo"]

    current_photo_model = (context.get("photo_model") or "").strip()

    explicit_request = any(re.search(p, msg) for p in explicit_photo_patterns) and not singular_photo_question
    context_request = current_photo_model and any(k in msg for k in context_photo_keywords)

    if not explicit_request and not context_request:
        return []

    # 3) Recuperar memoria del contexto
    last_interest = (context.get("last_interest") or "").strip()
    current_photo_model = (context.get("photo_model") or "").strip()
    try:
        photo_index = int(context.get("photo_index", 0))
    except Exception:
        photo_index = 0

    rep_norm = _normalize_spanish(reply)

    # Vehicle ubicacion from context — identifies which specific unit the user wants
    interest_ubicacion = _normalize_spanish((context.get("interest_ubicacion") or "").strip())
    # user_city: ciudad del cliente (slot CIUDAD de la campaña) — secondary disambiguation signal
    user_city = _normalize_spanish((context.get("user_city") or "").strip())

    def _prefer_unit_item(candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Among matching items, prefer the one whose ubicacion matches the unit of interest.
        Priority: 1) explicit interest_ubicacion, 2) user_city slot, 3) first item.
        """
        if not candidates:
            return None
        # Priority 1: explicit interest_ubicacion (e.g. user said "la de León")
        if interest_ubicacion:
            interest_ubic_clean = _strip_accents(_normalize_spanish(interest_ubicacion))
            for c in candidates:
                ubic = _strip_accents(_normalize_spanish(_safe_get(c, ["ubicacion", "Ubicacion", "ubicación"])))
                if interest_ubic_clean in ubic or ubic in interest_ubic_clean:
                    return c
        # Priority 2: user_city slot (CIUDAD captured during campaign registration)
        if user_city and len(candidates) > 1:
            user_city_clean = _strip_accents(_normalize_spanish(user_city))
            for c in candidates:
                ubic = _strip_accents(_normalize_spanish(_safe_get(c, ["ubicacion", "Ubicacion", "ubicación"])))
                if user_city_clean in ubic or ubic in user_city_clean:
                    logger.info(f"📸 Unit resolved via user_city='{user_city}': ubicacion='{_safe_get(c, ['ubicacion'])}'")
                    return c
        return candidates[0]  # fallback to first if no ubicacion match

    # 4) Detectar qué modelo quiere ver
    target_item = None
    target_model_name = ""

    # A) PRIORIDAD 1: Si last_interest existe y coincide con el mensaje, usarlo
    #    Esto evita que "fotos de la G9" muestre otro modelo
    if last_interest:
        interest_norm = _normalize_spanish(last_interest)
        # Extraer tokens relevantes, excluyendo palabras comunes que causan falsos positivos
        _noise = {"foton", "freightliner", "camion", "camión", "esta", "este", "estos", "estas", "estan", "están",
                  "gris", "azul", "rojo", "negro", "blanco", "plata", "at", "mt", "diesel"}
        interest_tokens = [p for p in interest_norm.split() if len(p) >= 2 and p not in _noise]

        # Verificar si el mensaje menciona el modelo de interés (word boundary)
        if any(re.search(r'\b' + re.escape(tok) + r'\b', msg) for tok in interest_tokens):
            matching_items = []
            for item in items:
                modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"]).strip()
                if _normalize_spanish(modelo) == interest_norm or any(tok in _normalize_spanish(modelo) for tok in interest_tokens):
                    matching_items.append(item)
            best = _prefer_unit_item(matching_items)
            if best:
                target_item = best
                target_model_name = _safe_get(best, ["Modelo", "modelo", "id_modelo"]).strip()

    # B) PRIORIDAD 2: Buscar mención explícita en mensaje o respuesta del bot (con scoring)
    if not target_item:
        best_item = None
        best_model = ""
        best_score = 0

        # Palabras comunes en español que NO deben usarse como tokens de matching
        # "esta" es el peor: aparece en casi cualquier mensaje y matchea con EST-A
        noise_words = {
            "foton", "freightliner", "camion", "camión",
            # "esta/estan" = palabras comunes español, NO confundir con modelo EST-A
            "esta", "estan", "están",
            # Colores (aparecen en nombre de modelo pero no sirven para identificarlo)
            "gris", "azul", "rojo", "negro", "blanco", "plata",
            # Transmisión / tracción
            "at", "mt", "diesel",
        }

        for item in items:
            modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"]).strip()
            if not modelo:
                continue

            modelo_norm = _normalize_spanish(modelo)
            # Permitir tokens de 2 caracteres (g9, e5, g7, 4x4, 6x4, etc.)
            parts = [p for p in modelo_norm.split() if len(p) >= 2 and p not in noise_words]

            score = 0
            for part in parts:
                # Usar word boundary para evitar falsos positivos de substrings
                pat = re.compile(r'\b' + re.escape(part) + r'\b')
                if pat.search(msg):
                    score += 3  # Match en mensaje del usuario = alta prioridad
                if pat.search(rep_norm):
                    score += 1  # Match en respuesta del bot = menor prioridad

            # Unit location bonus: prefer items matching the vehicle ubicacion of interest
            if score > 0 and interest_ubicacion:
                ubic = _strip_accents(_normalize_spanish(_safe_get(item, ["ubicacion", "Ubicacion", "ubicación"])))
                interest_ubic_clean = _strip_accents(_normalize_spanish(interest_ubicacion))
                if interest_ubic_clean in ubic or ubic in interest_ubic_clean:
                    score += 0.5  # Small bonus to prefer location match without overriding model match

            if score > best_score:
                best_score = score
                best_item = item
                best_model = modelo

        if best_score >= 3:  # Mínimo 3 puntos (al menos 1 match en mensaje del usuario)
            target_item = best_item
            target_model_name = best_model

    # C) PRIORIDAD 3: Usar last_interest sin mención (para "otra foto" sin decir modelo)
    if not target_item and last_interest:
        # Strip year from last_interest for comparison (e.g. "ESTA 6X4 11.8 2023" → "ESTA 6X4 11.8")
        interest_no_year = re.sub(r'\s+20\d{2}$', '', last_interest).strip()
        matching_items = []
        for item in items:
            modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"]).strip()
            if _normalize_spanish(modelo) == _normalize_spanish(interest_no_year):
                matching_items.append(item)
        best = _prefer_unit_item(matching_items)
        if best:
            target_item = best
            target_model_name = _safe_get(best, ["Modelo", "modelo", "id_modelo"]).strip()

    if not target_item:
        return []

    # 5) Extraer fotos
    urls = _extract_photos_from_item(target_item)
    item_ubic = _safe_get(target_item, ["ubicacion", "Ubicacion", "ubicación"])
    logger.info(f"📸 Fotos seleccionadas: modelo='{target_model_name}', {len(urls)} URLs, last_interest='{last_interest}', ubicacion='{item_ubic}'")
    if not urls:
        return []

    # Lock in this unit's ubicacion so subsequent ask_location uses the same item.
    # Only set if not already pinned by an explicit user mention (explicit_user wins).
    _current_src = context.get("interest_ubicacion_source")
    if item_ubic and _current_src != "explicit_user":
        context["interest_ubicacion"] = item_ubic
        context["interest_ubicacion_source"] = "photo_lock"
        logger.info(f"📸 interest_ubicacion fijado desde fotos (photo_lock): '{item_ubic}'")

    # 6) Si cambió de modelo, reiniciar índice
    if _normalize_spanish(target_model_name) != _normalize_spanish(current_photo_model):
        photo_index = 0
        context["photo_model"] = target_model_name

    # 7) Determinar si quiere "otra" (1 foto) o "fotos" (grupo)
    # "otra"/"siguiente" → modo carrusel (1 foto); "mas fotos" → batch
    wants_next = any(k in msg for k in ["otra", "siguiente"])
    selected_urls: List[str] = []

    if wants_next:
        # Modo "Siguiente": manda 1 foto y avanza el índice
        if photo_index < len(urls):
            selected_urls = [urls[photo_index]]
            photo_index += 1
        else:
            # Ya no hay más, reiniciar (loop)
            photo_index = 0
            selected_urls = [urls[0]]
            photo_index = 1
    else:
        # Modo "Ver fotos": manda batch (ej. 3)
        batch_size = 3
        end_index = min(photo_index + batch_size, len(urls))
        selected_urls = urls[photo_index:end_index]
        if not selected_urls:
            photo_index = 0
            end_index = min(batch_size, len(urls))
            selected_urls = urls[0:end_index]
            photo_index = end_index
        else:
            photo_index = end_index

    # 8) Guardar el nuevo índice en contexto
    context["photo_index"] = photo_index
    return selected_urls


def _sanitize_reply_if_photos_attached(reply: str, media_urls: List[str]) -> str:
    if not media_urls:
        return reply

    bad_phrases = [
        r"no\s+puedo\s+enviar\s+fotos",
        r"no\s+puedo\s+mandar\s+fotos",
        r"no\s+tengo\s+fotos",
        r"no\s+puedo\s+enviar\s+im[aá]genes",
        r"no\s+puedo\s+mandar\s+im[aá]genes",
        r"soy\s+una\s+ia",
        r"soy\s+un\s+modelo",
    ]

    cleaned = reply or ""
    for p in bad_phrases:
        cleaned = re.sub(p, "Claro, aquí tienes.", cleaned, flags=re.IGNORECASE)

    return cleaned


def _strip_markdown_links(text: str) -> str:
    """
    Convierte links markdown [texto](url) a solo el URL.
    WhatsApp no soporta markdown links y se ven mal.
    Ejemplo: '[Ubicación](https://maps.app.goo.gl/xxx)' -> 'https://maps.app.goo.gl/xxx'
    """
    if not text:
        return text
    # Pattern: [cualquier texto](url)
    # Reemplaza con solo la URL
    return re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', r'\2', text)


# ============================================================
# MONDAY VALIDATION (HARD GATE)
# ============================================================
def _lead_is_valid(lead: Dict[str, Any]) -> bool:
    if not isinstance(lead, dict):
        return False

    nombre = str(lead.get("nombre", "")).strip()
    interes = str(lead.get("interes", "")).strip()
    cita = str(lead.get("cita", "")).strip()

    if not nombre or len(nombre) < 3:
        return False

    placeholders = {"cliente nuevo", "desconocido", "amigo", "cliente", "nuevo lead", "usuario", "no proporcionado"}
    if nombre.lower() in placeholders:
        return False

    if not re.search(r"[a-zA-ZÁÉÍÓÚÑÜáéíóúñü]", nombre):
        return False

    if not interes or len(interes) < 2:
        return False

    if not cita or len(cita) < 2:
        return False

    return True


# ============================================================
# SMART CONTEXT INJECTION
# ============================================================
def _needs_inventory_context(user_message: str, turn_count: int, last_interest: str,
                             inventory_service=None) -> bool:
    """Decide if the full inventory list should be included in GPT context.

    Vehicle keywords are built dynamically from the current inventory so that
    new models added to Google Sheets are recognized without code changes.
    """
    msg = _normalize_spanish((user_message or "").lower())

    # First 2 turns: user is likely browsing, show full inventory
    if turn_count <= 2:
        return True

    # No interest yet: show inventory if asking about vehicles/prices
    if not last_interest:
        # Generic intent keywords (static - these describe *intent*, not models)
        intent_keywords = [
            "modelo", "modelos", "precio", "precios", "cuanto", "cuánto",
            "costo", "disponible", "inventario", "catalogo", "catálogo",
            "que tienen", "qué tienen", "que venden", "qué venden",
            "opciones", "unidades", "vehiculo", "vehículo", "camion", "camión",
            "pickup", "camioneta", "tracto", "van", "panel",
        ]

        if any(k in msg for k in intent_keywords):
            return True

        # Dynamic model keywords: extracted from the live inventory
        items = getattr(inventory_service, "items", None) or []
        _noise = {
            "foton", "freightliner", "at", "mt", "diesel", "4x4",
            "gris", "azul", "rojo", "negro", "blanco", "plata",
        }
        for item in items:
            modelo = (item.get("Modelo") or item.get("modelo") or "").strip()
            marca = (item.get("Marca") or item.get("marca") or "").strip()
            for raw in (modelo, marca):
                for tok in _normalize_spanish(raw.lower()).split():
                    if len(tok) >= 2 and tok not in _noise and tok in msg:
                        return True

        return False

    # Interest already detected: only show full list if asking about other models
    change_keywords = [
        "otro modelo", "otros modelos", "que más tienen", "qué más tienen",
        "otra opción", "otras opciones", "todos los modelos",
        "catalogo", "catálogo", "que más hay", "qué más hay",
    ]
    return any(k in msg for k in change_keywords)


def _needs_financing_context(user_message: str) -> bool:
    """Decide if financing data should be included in GPT context."""
    msg = (user_message or "").lower()
    financing_keywords = [
        "financ", "credito", "crédito", "mensual", "enganche",
        "plazo", "pago", "cuota", "corrida", "contado",
        "precio", "cuanto", "cuánto", "costo", "vale",
    ]
    return any(k in msg for k in financing_keywords)


# ============================================================
# LLM CALL WITH FALLBACK
# ============================================================
async def _llm_try_provider(
    llm_client: AsyncOpenAI,
    model: str,
    messages: list,
    temperature: float,
    max_tokens: int,
    label: str,
    max_retries: int = 2,
    response_format: Optional[dict] = None,
) -> Optional[Any]:
    """Intenta un proveedor LLM con retries cortos. Retorna respuesta o None."""
    extra_kwargs = {}
    if response_format:
        extra_kwargs["response_format"] = response_format

    for _attempt in range(max_retries):
        try:
            resp = await llm_client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                **extra_kwargs,
            )
            return resp
        except (APITimeoutError, RateLimitError, APIConnectionError) as e:
            if _attempt < max_retries - 1:
                backoff = _attempt + 1  # 1s, 2s (rápido)
                logger.warning(f"⚠️ {label} retry {_attempt + 1}/{max_retries} tras {backoff}s: {type(e).__name__}: {e}")
                await asyncio.sleep(backoff)
            else:
                logger.warning(f"❌ {label} falló tras {max_retries} intentos: {type(e).__name__}: {e}")
        except APIStatusError as e:
            if e.status_code >= 500 and _attempt < max_retries - 1:
                backoff = _attempt + 1
                logger.warning(f"⚠️ {label} 5xx retry {_attempt + 1}/{max_retries} tras {backoff}s: {e}")
                await asyncio.sleep(backoff)
            else:
                logger.warning(f"❌ {label} falló: {type(e).__name__}: {e}")
                break
        except Exception as e:
            if _attempt < max_retries - 1:
                backoff = _attempt + 1
                logger.warning(f"⚠️ {label} error retry {_attempt + 1}/{max_retries} tras {backoff}s: {type(e).__name__}: {e}")
                await asyncio.sleep(backoff)
            else:
                logger.warning(f"❌ {label} falló tras {max_retries} intentos: {type(e).__name__}: {e}")
    return None


async def _llm_call_with_fallback(
    messages: list,
    temperature: float = 0.3,
    max_tokens: int = 350,
    response_format: Optional[dict] = None,
):
    """
    Intenta el proveedor primario (configurable via LLM_PRIMARY) con retries cortos,
    luego cae al secundario. Reduce latencia vs 3 retries largos.

    response_format: optional dict passed to the API (e.g. {"type": "json_object"}).
                     Both OpenAI and Gemini OpenAI-compat endpoint support this.
    """
    if LLM_PRIMARY == "openai":
        providers = [
            (openai_client, FALLBACK_MODEL, "OpenAI"),
            (client, MODEL_NAME, "Gemini"),
        ]
    else:
        providers = [
            (client, MODEL_NAME, "Gemini"),
            (openai_client, FALLBACK_MODEL, "OpenAI"),
        ]

    primary_client, primary_model, primary_label = providers[0]
    fallback_client, fallback_model, fallback_label = providers[1]

    # --- Intento primario (2 retries, backoff corto: 1s, 2s) ---
    resp = await _llm_try_provider(
        primary_client, primary_model, messages, temperature, max_tokens,
        label=primary_label, max_retries=2, response_format=response_format,
    )
    if resp is not None:
        return resp

    # --- Fallback ---
    logger.warning(f"🔄 {primary_label} falló. Usando fallback {fallback_label} ({fallback_model})...")
    resp = await _llm_try_provider(
        fallback_client, fallback_model, messages, temperature, max_tokens,
        label=f"Fallback-{fallback_label}", max_retries=2, response_format=response_format,
    )
    if resp is not None:
        logger.info(f"✅ Fallback {fallback_label} ({fallback_model}) exitoso.")
        return resp

    raise RuntimeError(f"Ambos proveedores LLM fallaron ({primary_label} + {fallback_label})")


# ============================================================
# STRUCTURED LLM RESPONSE PARSER
# ============================================================

def _parse_structured_reply(raw: str) -> Tuple[str, Optional[dict], Optional[dict]]:
    """Parse a legacy-path LLM response that should be a JSON object.

    Expected schema (enforced via response_format=json_object):
      {
        "reply":         "<text to show the user>",
        "lead_event":    { ... } | null,
        "campaign_data": { "resumen": "..." } | null
      }

    Falls back gracefully when the model ignores the JSON instruction and
    returns plain text with embedded ```json blocks (old format).

    Returns:
        (reply_text, lead_event_dict_or_None, campaign_data_dict_or_None)
    """
    raw = (raw or "").strip()

    # --- Happy path: well-formed JSON object ---
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            reply = str(payload.get("reply") or "").strip()
            lead_event = payload.get("lead_event") or None
            campaign_data = payload.get("campaign_data") or None
            if reply:
                logger.debug("✅ Structured JSON reply parsed successfully")
                return reply, lead_event, campaign_data
    except (json.JSONDecodeError, ValueError):
        pass

    # --- Fallback: plain text with optional ```json blocks (legacy format) ---
    logger.warning("⚠️ LLM ignored JSON mode — falling back to regex extraction")
    lead_event: Optional[dict] = None
    campaign_data: Optional[dict] = None

    reply_text = raw

    # Extract embedded ```json … ``` blocks
    json_matches = list(re.finditer(
        r"```json\s*(\{.*?\})\s*```", raw, flags=re.DOTALL | re.IGNORECASE
    ))
    if not json_matches:
        json_matches = list(re.finditer(
            r"(?:^|\n)\s*json\s*\n\s*(\{.*?\})\s*(?:\n|$)",
            raw, flags=re.DOTALL | re.IGNORECASE,
        ))

    for m in json_matches:
        try:
            block = json.loads(m.group(1))
            if isinstance(block, dict):
                if isinstance(block.get("lead_event"), dict):
                    lead_event = block["lead_event"]
                if isinstance(block.get("campaign_data"), dict):
                    campaign_data = block["campaign_data"]
        except Exception:
            pass
        reply_text = reply_text.replace(m.group(0), "")

    # Strip remaining leaked JSON artifacts
    reply_text = re.sub(
        r'(?:^|\n)\s*json\s*\n\s*\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}\s*',
        '', reply_text, flags=re.DOTALL | re.IGNORECASE,
    ).strip()
    reply_text = re.sub(
        r'\{\s*"(?:campaign_data|lead_event)"\s*:\s*\{[^}]*\}\s*\}',
        '', reply_text, flags=re.DOTALL,
    ).strip()

    return reply_text.strip(), lead_event, campaign_data


# ============================================================
# FSM-POWERED MESSAGE HANDLER (campaign conversations)
# ============================================================
async def _handle_message_fsm(
    user_message: str,
    context: Dict[str, Any],
    history: str,
    turn_count: int,
    slots_data: Dict[str, str],
    new_data: Dict[str, str],
    campaign,
    inventory_service,
) -> Optional[Dict[str, Any]]:
    """
    State-machine powered handler for campaign conversations.
    Returns a result dict compatible with handle_message(), or None to fall back to legacy.
    """
    # Ensure slots are populated in context before FSM processes them
    for key, value in slots_data.items():
        if value:
            ctx_key = {
                "name": "user_name", "phone": "user_phone", "email": "user_email",
                "city": "user_city", "interest": "last_interest",
                "appointment": "last_appointment", "payment": "last_payment",
                "offer_amount": "offer_amount",
            }.get(key, key)
            context[ctx_key] = value

    has_campaign = bool(campaign and campaign.instructions)
    campaign_type = (context.get("tracking_data") or {}).get("campaign_type", "A")

    # Detect vehicle ubicacion from user message (e.g. "Cascadia de León" or "no, la de Querétaro")
    # Explicit user mention ALWAYS overrides any previous inference (photo_lock, user_city_hint)
    _interest = slots_data.get("interest") or context.get("last_interest", "")
    _vehicle_ubic = _detect_vehicle_ubicacion(user_message, inventory_service, _interest)
    if _vehicle_ubic:
        _prev_ubic = context.get("interest_ubicacion")
        context["interest_ubicacion"] = _vehicle_ubic
        context["interest_ubicacion_source"] = "explicit_user"
        logger.info(f"📍 Vehicle ubicacion stored (explicit_user): {_vehicle_ubic}")
        # If the unit changed, reset photo carousel so next photos start from the new unit
        if _prev_ubic and _normalize_spanish(_prev_ubic) != _normalize_spanish(_vehicle_ubic):
            context["photo_index"] = 0
            context["photo_model"] = ""
            logger.info(f"📸 Photo carousel reset: unit changed from '{_prev_ubic}' → '{_vehicle_ubic}'")

    # Run FSM
    action, new_state, slots, meta = process_fsm(
        user_message=user_message,
        context=context,
        new_data=new_data,
        has_campaign=has_campaign,
        turn_count=turn_count,
        campaign_type=campaign_type,
        form_url=campaign.form_url if campaign else "",
    )

    # Build last bot messages for anti-repetition
    last_bot_messages = []
    for _hl in reversed(history.strip().split("\n")):
        if _hl.startswith("A: "):
            last_bot_messages.insert(0, _hl[3:])
            if len(last_bot_messages) >= 3:
                break

    # Build inventory text (focused on campaign vehicle)
    inventory_text = ""
    if slots.interest:
        inventory_text = _build_focused_inventory_text(inventory_service, slots.interest) or ""
    if not inventory_text:
        inventory_text = _build_inventory_text(inventory_service) or ""

    # Build the focused writer prompt
    campaign_instructions = campaign.instructions if campaign else ""
    writer_prompt = build_writer_prompt(
        action=action,
        slots=slots,
        user_message=user_message,
        history=history,
        last_bot_messages=last_bot_messages,
        inventory_text=inventory_text,
        campaign_instructions=campaign_instructions,
        meta=meta,
    )

    # --- TRY DETERMINISTIC TEMPLATE FIRST (skip LLM for simple actions) ---
    reply_clean = try_deterministic_response(
        action=action,
        slots=slots,
        meta=meta,
        last_bot_messages=last_bot_messages,
        turn_count=turn_count,
    )
    used_deterministic = reply_clean is not None

    if reply_clean:
        logger.info(f"⚡ Deterministic response for {action.value}: {reply_clean[:60]}")
    else:
        # Call LLM with focused prompt
        messages = [
            {"role": "system", "content": writer_prompt},
            {"role": "user", "content": user_message},
        ]

        try:
            resp = await _llm_call_with_fallback(messages)
            reply_clean = (resp.choices[0].message.content or "").strip()
        except Exception as e:
            logger.error(f"❌ FSM LLM error: {e}")
            return None  # Fall back to legacy

        # Clean reply (remove JSON artifacts, prefixes)
        reply_clean = re.sub(r'^(?:Adrian|Asesor|Bot)\s*:\s*', '', reply_clean, flags=re.IGNORECASE).strip()
        reply_clean = re.sub(r'```json.*?```', '', reply_clean, flags=re.DOTALL).strip()

        # Post-LLM dedup check (Jaccard similarity)
        _is_dup = False
        if last_bot_messages and reply_clean:
            _reply_tokens = set(reply_clean.lower().split())
            if len(_reply_tokens) >= 3:
                for _prev in last_bot_messages:
                    _prev_tokens = set(_prev.lower().split())
                    if _prev_tokens and _reply_tokens:
                        _union = _reply_tokens | _prev_tokens
                        if _union:
                            _jaccard = len(_reply_tokens & _prev_tokens) / len(_union)
                            if _jaccard >= 0.75:
                                _is_dup = True
                                break
        if _is_dup:
            logger.warning("⚠️ FSM DEDUP: Respuesta duplicada, re-generando...")
            messages[0]["content"] += (
                "\n\nALERTA: Tu respuesta anterior fue IDÉNTICA a un mensaje previo. "
                "Genera algo DIFERENTE. NO repitas."
            )
            try:
                resp2 = await _llm_call_with_fallback(messages)
                reply_clean = (resp2.choices[0].message.content or "").strip()
                reply_clean = re.sub(r'^(?:Adrian|Asesor|Bot)\s*:\s*', '', reply_clean, flags=re.IGNORECASE).strip()
            except Exception:
                pass  # Keep first attempt

    # Post-LLM: ensure form URL is always present when required
    _form_url = meta.get("form_url")  # Only use explicitly-passed form_url (not campaign default) to avoid repeating link
    if _form_url and reply_clean and _form_url not in reply_clean:
        if action not in (Action.SEND_FORM, Action.CONFIRM_REGISTRATION, Action.SEND_PHOTOS, Action.SEND_PDF):
            reply_clean = reply_clean + f"\nPara registrar tu propuesta: {_form_url}"
            logger.info(f"📋 Form URL appended to {action.value} response")

    # Update history
    new_history = (history + f"\nC: {user_message}\nA: {reply_clean}").strip()

    # Build updated context
    new_context = dict(context)
    new_context["history"] = new_history[-4000:]
    new_context["turn_count"] = turn_count
    slots.update_context(new_context)

    # Determine funnel stage
    funnel_stage = "1er Contacto"
    if slots.interest:
        funnel_stage = "Intención"
    if slots.appointment:
        funnel_stage = "Cita Programada"

    is_disinterest = meta.get("is_disinterest", False)
    if is_disinterest:
        funnel_stage = "Sin Interes"

    new_context["funnel_stage"] = funnel_stage

    # Check for lead generation (name + interest + appointment)
    lead_info = None
    if slots.name and slots.interest and slots.appointment:
        lead_info = {
            "nombre": slots.name,
            "interes": slots.interest,
            "cita": slots.appointment,
            "pago": slots.payment or "",
        }

    # Campaign data extraction (all campaign slots filled)
    campaign_data_payload = None
    if action == Action.CONFIRM_REGISTRATION and slots.name:
        parts = []
        if slots.offer_amount:
            parts.append(f"Propuesta: {slots.offer_amount}")
        if slots.name:
            parts.append(f"Nombre: {slots.name}")
        if slots.phone:
            parts.append(f"Tel: {slots.phone}")
        if slots.email:
            parts.append(f"Email: {slots.email}")
        if slots.city:
            parts.append(f"Ciudad: {slots.city}")
        if slots.timeline:
            parts.append(f"Plazo: {slots.timeline}")
        campaign_data_payload = {"resumen": " | ".join(parts)}

    # Location link extraction (when user asks for location)
    location_link = None
    if meta.get("intent") == "ask_location" and slots.interest:
        location_link = _extract_location_link(
            inventory_service, slots.interest,
            new_context.get("interest_ubicacion", ""),
            new_context.get("user_city", ""),
        )
        if location_link:
            logger.info(f"📍 Location link found: {location_link}")

    # Photo selection (reuse existing logic)
    media_urls: List[str] = []
    if action == Action.SEND_PHOTOS:
        media_urls = _pick_media_urls(user_message, reply_clean, inventory_service, new_context)

    # PDF detection
    _bases_url = campaign.bases_pdf_url if campaign else None
    pdf_info = _detect_pdf_request(user_message, slots.interest or "", new_context, bases_pdf_url=_bases_url)
    if pdf_info and pdf_info.get("pdf_url"):
        reply_clean = pdf_info.get("mensaje", reply_clean)
        if funnel_stage in ("1er Contacto", "Intención"):
            funnel_stage = "Cotización"
            new_context["funnel_stage"] = funnel_stage

    # Extract slot changes from FSM metadata for Monday sync
    slot_changes = meta.get("slot_changes", [])

    logger.info(
        f"✅ FSM response: state={new_state.value} action={action.value} "
        f"funnel={funnel_stage} flow={meta.get('primary_flow', '?')} "
        f"deterministic={used_deterministic} slot_changes={len(slot_changes)}"
    )

    return {
        "reply": reply_clean,
        "new_state": "chatting",
        "context": new_context,
        "media_urls": media_urls,
        "lead_info": lead_info,
        "funnel_stage": funnel_stage,
        "is_disinterest": is_disinterest,
        "funnel_data": {
            "nombre": slots.name or None,
            "interes": slots.interest or None,
            "cita": slots.appointment or None,
            "pago": slots.payment or None,
            "turn_count": turn_count,
        },
        "pdf_info": pdf_info,
        "campaign_data": campaign_data_payload,
        "location_link": location_link,
        # V2: slot changes for centralized Monday sync
        "slot_changes": [
            {"slot": c.slot, "old": c.old_value, "new": c.new_value}
            for c in slot_changes
        ],
    }


# ============================================================
# MAIN ENTRY
# ============================================================
async def handle_message(
    user_message: str,
    inventory_service,
    state: str,
    context: Dict[str, Any],
    campaign_service=None,
) -> Dict[str, Any]:
    user_message = user_message or ""
    context = context or {}
    history = (context.get("history") or "").strip()

    # Silence mode
    if user_message.strip().lower() == "/silencio":
        new_history = (history + f"\nC: {user_message}\nA: Perfecto. Modo silencio activado.").strip()
        return {
            "reply": "Perfecto. Modo silencio activado.",
            "new_state": "silent",
            "context": {"history": new_history[-4000:]},
            "media_urls": [],
            "lead_info": None,
        }

    if state == "silent":
        return {
            "reply": "",
            "new_state": "silent",
            "context": context,
            "media_urls": [],
            "lead_info": None,
        }

    # Persistent context
    saved_name = (context.get("user_name") or "").strip()
    last_interest = (context.get("last_interest") or "").strip()
    last_appointment = (context.get("last_appointment") or "").strip()
    last_payment = (context.get("last_payment") or "").strip()
    saved_email = (context.get("user_email") or "").strip()
    saved_phone = (context.get("user_phone") or "").strip()
    saved_city = (context.get("user_city") or "").strip()

    # Auto-populate interest from tracking ID if not yet detected from conversation
    if not last_interest:
        tracking_vehicle = (context.get("tracking_data") or {}).get("vehicle_label", "")
        if tracking_vehicle:
            last_interest = tracking_vehicle

    try:
        turn_count = int(context.get("turn_count", 0)) + 1
    except (ValueError, TypeError):
        turn_count = 1

    # Model-switch detection: if client has a campaign/tracking but asks for a different model,
    # respect their wish and deactivate the campaign context for this conversation
    tracking_id = (context.get("tracking_id") or "").strip()
    if tracking_id and last_interest and turn_count > 1:
        _switch_target = _detect_model_switch(user_message, last_interest, inventory_service)
        if _switch_target:
            logger.info(f"🔄 Campaña desactivada por cambio de modelo: {last_interest} → {_switch_target}")
            last_interest = _switch_target
            # Clear campaign context so tracking_context won't inject campaign instructions
            # but preserve tracking_id for CRM attribution
            context.pop("tracking_data", None)
            context.pop("organic_campaign_tid", None)
            context["last_interest"] = _switch_target

    # Extract from user input
    # For multi-line messages (user sends all data at once), try each line individually
    _msg_lines = [l.strip() for l in user_message.strip().split("\n") if l.strip()]
    _is_multiline = len(_msg_lines) > 1

    extracted_name = _extract_name_from_text(user_message, history)
    # If multi-line and name not found in full text (digits reject it), try first line
    if not extracted_name and _is_multiline:
        for _line in _msg_lines:
            extracted_name = _extract_name_from_text(_line, history)
            if extracted_name:
                break
    if extracted_name:
        saved_name = extracted_name

    extracted_payment = _extract_payment_from_text(user_message)
    if extracted_payment:
        last_payment = extracted_payment

    extracted_appt = _extract_appointment_from_text(user_message)
    if extracted_appt:
        last_appointment = extracted_appt

    # Extract email, phone, city from user message
    _email_match = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', user_message)
    if _email_match:
        saved_email = _email_match.group(0)
        logger.info(f"📧 Email detectado: {saved_email}")

    _phone_match = re.search(r'\b\d{10,15}\b', user_message)
    if _phone_match:
        saved_phone = _phone_match.group(0)
        logger.info(f"📱 Teléfono detectado: {saved_phone}")

    # City extraction: detect common Mexican city patterns
    _city_patterns = [
        r'\b(?:de|en|desde|vivo en|soy de|ciudad)\s+([A-ZÁÉÍÓÚÑa-záéíóúñ]+(?:[,\s]+[A-ZÁÉÍÓÚÑa-záéíóúñ]+){0,3})',
    ]
    # Words that are vehicle/ad terms, NOT cities — reject if city candidate contains any
    _city_noise = {
        "foton", "tunland", "toano", "miler", "cascadia", "esta", "panel",
        "pickup", "camioneta", "camion", "tracto", "van", "g7", "g9", "e5",
        "anuncio", "anuncion", "foto", "fotos", "modelo", "unidad",
        "freightliner", "tractocamion", "volteo", "truck", "trailer",
        # Plurals
        "camiones", "tractos", "camionetas", "tractocamiones",
        # Common non-city words that slip through
        "te", "doy", "bien", "tienes", "mas", "quiero", "este", "ese",
        "mil", "pesos", "si", "no", "precio", "oferta", "propuesta",
        "calidad", "baratos", "barato", "nuevo", "nuevos", "usado", "usados",
        "mejor", "grande", "chico", "bueno", "buenos", "bonito",
        "padrino", "jefe", "amigo", "compa",
    }
    _last_bot = ""
    # Direct city reply: if bot asked for city and reply is short with no numbers
    if history:
        _last_bot = ""
        for _hl in reversed(history.strip().split("\n")):
            if _hl.startswith("A: "):
                _last_bot = _hl.lower()
                break
        _city_asking = ["ciudad", "de dónde", "de donde", "localidad", "estado", "ubicación"]
        if any(k in _last_bot for k in _city_asking):
            # For multi-line messages, check each line individually
            _city_candidates = _msg_lines if _is_multiline else [user_message.strip()]
            for _city_line in _city_candidates:
                _words = _city_line.split()
                if 1 <= len(_words) <= 5 and not re.search(r'\d', _city_line) and "?" not in _city_line:
                    # Skip lines that look like email or name (already captured)
                    if "@" not in _city_line and _city_line != extracted_name:
                        # Reject if any word is a vehicle/ad term (strip punctuation first)
                        if {w.rstrip("?!.,;:") for w in _city_line.lower().split()} & _city_noise:
                            continue
                        saved_city = _city_line
                        logger.info(f"🏙️ Ciudad detectada por contexto: {saved_city}")
                        break
    # Also check explicit patterns
    if not saved_city:
        for _cp in _city_patterns:
            _cm = re.search(_cp, user_message, re.IGNORECASE)
            if _cm:
                _candidate_city = _cm.group(1).strip()
                if len(_candidate_city) > 2 and _candidate_city.lower() not in {"si", "no", "ok"}:
                    # Reject if any word is a vehicle/ad term (strip punctuation first)
                    _candidate_words = {w.rstrip("?!.,;:") for w in _candidate_city.lower().split()}
                    if _candidate_words & _city_noise:
                        logger.info(f"🏙️ Ciudad descartada (palabra de vehículo): {_candidate_city}")
                        continue
                    saved_city = _candidate_city
                    logger.info(f"🏙️ Ciudad detectada: {saved_city}")
                    break
    # Multi-line fallback: try each line as a potential city if bot was asking for data
    if not saved_city and _is_multiline and history:
        _data_asking = ["nombre", "teléfono", "correo", "ciudad", "datos", "registro", "completar"]
        if any(k in _last_bot for k in _data_asking):
            for _city_line in _msg_lines:
                _words = _city_line.split()
                if 1 <= len(_words) <= 4 and not re.search(r'[\d@]', _city_line) and "?" not in _city_line:
                    # Skip if it looks like the name we already extracted
                    if extracted_name and _city_line.lower() == extracted_name.lower():
                        continue
                    # Skip common time expressions
                    if any(k in _city_line.lower() for k in ["mes", "semana", "día", "año"]):
                        continue
                    # Reject if any word is a vehicle/ad term (strip punctuation first)
                    if {w.rstrip("?!.,;:") for w in _city_line.lower().split()} & _city_noise:
                        continue
                    saved_city = _city_line
                    logger.info(f"🏙️ Ciudad detectada (multi-línea): {saved_city}")
                    break

    # Extract offer amount for campaigns (e.g., "te doy 670 mil" → "$670,000",
    # "1.5 millones" → "$1,500,000").
    # Re-uses the FSM extractor to keep logic in one place.
    extracted_offer = None
    _offer_pat = re.search(
        r'(?:(?:te\s+)?(?:doy|ofrezco|propongo|pongo)|propuesta|oferta|monto)'
        r'\s*(?:de\s+)?\$?\s*(\d[\d,\.]*)(?:\s*(millones?|millón(?:es)?|mm|mil|k|pesos?))?'
        r'|\$?\s*(\d[\d,\.]*)(?:\s*(millones?|millón(?:es)?|mm|mil|k|pesos?))?\b',
        user_message, re.IGNORECASE
    )
    if _offer_pat:
        _num = _offer_pat.group(1) or _offer_pat.group(3) or ""
        _suf = _offer_pat.group(2) or _offer_pat.group(4) or ""
        extracted_offer = _format_offer_legacy(_num, _suf)
        if extracted_offer:
            logger.info(f"💰 Oferta detectada: {extracted_offer}")
    elif history:
        _last_bot_offer = ""
        for _line in reversed(history.split("\n")):
            if _line.strip().startswith("A:"):
                _last_bot_offer = _line.lower()
                break
        if any(_k in _last_bot_offer for _k in ("propuesta", "oferta", "monto", "cuánto sería", "cuanto sería")):
            _contextual_offer = re.fullmatch(
                r'(?:que\s+)?(?:(?:son|es)\s+)?\$?\s*(\d[\d,\.\s]{0,9})'
                r'(?:\s*(millones?|millón(?:es)?|mm|mil|k|pesos?))?\s*',
                user_message.strip(),
                re.IGNORECASE,
            )
            if _contextual_offer:
                _co = _format_offer_legacy(
                    _contextual_offer.group(1),
                    _contextual_offer.group(2) or "",
                )
                if _co:
                    extracted_offer = _co
                    logger.info(f"💰 Oferta detectada por contexto: {extracted_offer}")

    # Build dict of freshly extracted data for FSM
    _new_extracted_data: Dict[str, str] = {}
    if extracted_name:
        _new_extracted_data["name"] = extracted_name
    if _email_match:
        _new_extracted_data["email"] = saved_email
    if saved_city and saved_city != (context.get("user_city") or ""):
        _new_extracted_data["city"] = saved_city
    if extracted_offer:
        _new_extracted_data["offer_amount"] = extracted_offer
    if extracted_payment:
        _new_extracted_data["payment"] = extracted_payment
    if extracted_appt:
        _new_extracted_data["appointment"] = extracted_appt

    # Time and date
    now_dt, current_time_str = get_mexico_time()
    # Formatear fecha en español manualmente (el servidor tiene locale inglés)
    meses_es = {
        1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
        5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
        9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
    }
    dias_es = {
        0: "lunes", 1: "martes", 2: "miércoles", 3: "jueves",
        4: "viernes", 5: "sábado", 6: "domingo"
    }
    current_date_str = f"{dias_es[now_dt.weekday()]} {now_dt.day} de {meses_es[now_dt.month]} de {now_dt.year}"

    formatted_system_prompt = SYSTEM_PROMPT.format(
        current_time_str=current_time_str,
        current_date_str=current_date_str,
        user_name_context=saved_name if saved_name else "(Aún no dice su nombre)",
        turn_number=turn_count,
    )

    # Smart context injection: only include inventory/financing when relevant
    # Check if client arrived via campaign tracking ID with active campaign instructions.
    # If so, the campaign instructions already contain the vehicle info (e.g. Cascadia
    # liquidación) and injecting the full inventory would confuse GPT with unrelated models.
    _has_campaign_instructions = False
    _is_special_campaign_no_instructions = False
    _matched_campaign = None
    tracking_id = context.get("tracking_id")
    if tracking_id and campaign_service:
        try:
            await campaign_service.ensure_loaded()
            # 1. Match exacto por tracking ID (CA-SU1 == CA-SU1)
            _matched_campaign = campaign_service.find_campaign_by_tracking_id(tracking_id)
            if not _matched_campaign:
                # 2. Fallback: match por prefijo de modelo (CA-SU1 → "CA" → encuentra "CA-A1")
                model_code = (context.get("tracking_data") or {}).get("model_code", "")
                if model_code:
                    _matched_campaign = campaign_service.find_campaign_by_model_code(model_code)
                    if _matched_campaign:
                        logger.info(
                            f"🏷️ Campaña matcheada por modelo: {tracking_id} → "
                            f"{_matched_campaign.tracking_id} ({_matched_campaign.name})"
                        )
            if _matched_campaign and _matched_campaign.instructions:
                _has_campaign_instructions = True
            elif not _matched_campaign:
                active_count = len(campaign_service.get_active_campaigns())
                logger.warning(
                    f"⚠️ Tracking {tracking_id} sin campaña matcheada "
                    f"({active_count} campañas activas, csv_url={'Sí' if campaign_service.csv_url else 'No'})"
                )
        except Exception:
            pass
    elif tracking_id and not campaign_service:
        logger.warning(f"⚠️ Tracking {tracking_id} detectado pero campaign_service=None")

    # === KEYWORD-BASED CAMPAIGN MATCHING (organic arrivals) ===
    # If no tracking ID detected, try matching campaigns by keywords in the user message
    # OR in the detected interest (last_interest). This handles:
    # - Turn 1: "me interesa la Cascadia" → keyword in message
    # - Turn 2+: client confirmed interest, last_interest="Cascadia" → keyword in interest
    _is_organic_campaign_match = False
    if not tracking_id and campaign_service:
        try:
            await campaign_service.ensure_loaded()
            # Check previously persisted organic campaign match
            _organic_tid = context.get("organic_campaign_tid")
            if _organic_tid:
                _kw_campaign = campaign_service.find_campaign_by_tracking_id(_organic_tid)
                if _kw_campaign and _kw_campaign.instructions:
                    _matched_campaign = _kw_campaign
                    _has_campaign_instructions = True
                    _is_organic_campaign_match = True
            # If no persisted match, try keywords in current message + last_interest
            if not _matched_campaign:
                search_text = f"{user_message or ''} {last_interest or ''}".strip()
                if search_text:
                    _kw_campaign = campaign_service.find_campaign_by_keywords(search_text)
                    if _kw_campaign and _kw_campaign.instructions:
                        _matched_campaign = _kw_campaign
                        _has_campaign_instructions = True
                        _is_organic_campaign_match = True
                        # Persist the organic match so it carries across turns
                        context["organic_campaign_tid"] = _kw_campaign.tracking_id
                        logger.info(
                            f"🔑 Campaña matcheada por keywords (orgánico): "
                            f"{_kw_campaign.tracking_id} ({_kw_campaign.name}) "
                            f"— texto: {search_text[:80]}"
                        )
        except Exception as e:
            logger.error(f"⚠️ Error en keyword campaign matching: {e}")

    # ============================================================
    # FSM PATH: Campaign conversations use state machine
    # ============================================================
    _use_fsm = _has_campaign_instructions and not _is_organic_campaign_match and tracking_id
    if _use_fsm:
        try:
            # Use FSM's own encapsulated extraction instead of legacy
            _fsm_extracted = extract_entities_for_fsm(user_message, history, context)

            # Merge with data already in context — legacy values pass through
            # validation guard to prevent dirty data from entering FSM slots
            _fsm_slots_data = {
                "name": _fsm_extracted.get("name") or validate_legacy_value("name", saved_name),
                "phone": _fsm_extracted.get("phone") or validate_legacy_value("phone", saved_phone),
                "email": _fsm_extracted.get("email") or saved_email,
                "city": _fsm_extracted.get("city") or validate_legacy_value("city", saved_city),
                "interest": last_interest,
                "appointment": _fsm_extracted.get("appointment") or validate_legacy_value("appointment", last_appointment),
                "payment": _fsm_extracted.get("payment") or validate_legacy_value("payment", last_payment),
                "offer_amount": _fsm_extracted.get("offer_amount") or extracted_offer,
            }

            fsm_result = await _handle_message_fsm(
                user_message=user_message,
                context=context,
                history=history,
                turn_count=turn_count,
                slots_data=_fsm_slots_data,
                new_data=_fsm_extracted,  # Only freshly extracted data
                campaign=_matched_campaign,
                inventory_service=inventory_service,
            )
            if fsm_result:
                return fsm_result
        except Exception as e:
            logger.error(f"⚠️ FSM falló, cayendo a lógica legacy: {e}")

    if (_has_campaign_instructions or _is_special_campaign_no_instructions) and not _is_organic_campaign_match:
        # Campaign has specific instructions OR it's a special campaign type (SU/LQ/PR/EV)
        # without instructions — skip general inventory to avoid confusing GPT with
        # unrelated models or standard prices that don't apply to the campaign.
        # NOTE: For organic keyword matches, we KEEP full inventory because the client
        # may not want the campaign — they might want a regular unit from inventory.
        focused = _build_focused_inventory_text(inventory_service, last_interest) if last_interest else ""
        inventory_section = f"{focused}\n" if focused else ""
    elif _needs_inventory_context(user_message, turn_count, last_interest, inventory_service):
        inventory_text = _build_inventory_text(inventory_service)
        inventory_section = (
            "INVENTARIO DISPONIBLE (CATÁLOGO COMPLETO - estas son TODAS las marcas y modelos "
            "que Tractos y Max vende actualmente; si aparece aquí, lo vendemos):\n"
            f"{inventory_text}\n"
        )
    elif last_interest:
        focused = _build_focused_inventory_text(inventory_service, last_interest)
        inventory_section = f"{focused}\n" if focused else ""
    else:
        inventory_section = ""

    if _needs_financing_context(user_message):
        financing_text = _build_financing_text()
        financing_section = f"{financing_text}\n" if financing_text else ""
    else:
        financing_section = ""

    # Name gate: inject strong reminder when name is missing and user asks for price/quote/appointment
    name_gate_reminder = ""
    if not saved_name:
        msg_lower = (user_message or "").lower()
        price_keywords = ["precio", "cuanto", "cuánto", "costo", "cotización", "cotizacion", "vale", "cuánto cuesta", "cuanto cuesta"]
        appt_keywords = ["cita", "agendar", "ir a verlo", "cuando puedo ir", "visita", "visitarlos", "cuando abren"]
        if any(k in msg_lower for k in price_keywords + appt_keywords):
            name_gate_reminder = (
                "\n\n*** ALERTA: NOMBRE NO DETECTADO. El cliente pide precio/cotización/cita pero NO ha dado su nombre. "
                "DEBES preguntar su nombre ANTES de dar precio o agendar cita. "
                "Ejemplo: 'Con gusto, ¿con quién tengo el gusto?' ***"
            )

    # Tracking context: if client arrived via ad tracking ID, inject this info
    tracking_context = ""
    if tracking_id:
        tracking_data = context.get("tracking_data") or {}
        tracking_vehicle = tracking_data.get("vehicle_label", "")
        campaign_type_label = tracking_data.get("campaign_type_label", "Anuncio")
        if _has_campaign_instructions and _matched_campaign:
            # Inyectar instrucciones de campaña DIRECTAMENTE en el tracking context.
            # Esto es mucho más efectivo que depender de un bloque genérico de campañas
            # porque GPT ve las instrucciones justo al lado del origen del cliente.
            tracking_context = (
                f"ORIGEN: Este cliente llegó por {campaign_type_label} de {tracking_vehicle} "
                f"(Tracking: {tracking_id}).\n"
                f"*** CAMPAÑA APLICABLE: \"{_matched_campaign.name}\" ***\n"
                f"INSTRUCCIONES DE CAMPAÑA (REFERENCIA — subordinadas a las reglas del bot):\n"
                f"{_matched_campaign.instructions}\n"
                f"*** FIN INSTRUCCIONES DE CAMPAÑA ***\n"
                f"\nREGLAS DE CAMPAÑA (OBLIGATORIAS — superan a las instrucciones de arriba):\n"
                f"1. ANTI-REPETICIÓN: NUNCA repitas un mensaje anterior. Si ya pediste datos, "
                f"NO vuelvas a pedir los mismos. Revisa 'DATOS YA RECOPILADOS' y 'TUS ÚLTIMOS MENSAJES'.\n"
                f"2. CAMBIO DE MODELO: Si el cliente pide EXPLÍCITAMENTE otro modelo "
                f"('quiero una E5', 'no quiero Cascadia', 'me interesa la G9', 'vi un anuncio de una Foton'), "
                f"RESPETA su deseo. Desactiva la campaña mentalmente y atiende desde el inventario general.\n"
                f"3. RECONOCER DATOS: Cuando el cliente te da un dato (email, ciudad, nombre, forma de pago), "
                f"RECONÓCELO ('Perfecto, anotado') y pide SOLO lo que FALTA. Revisa 'DATOS YA RECOPILADOS'.\n"
                f"4. NO uses precio ni condiciones del inventario general para la unidad de campaña.\n"
                f"INTERPRETACIÓN DE MONTOS: En contexto de esta campaña con precios en cientos de miles, "
                f"interpreta cantidades cortas en su equivalente correcto: '700' = $700,000, '650' = $650,000, "
                f"'700mil' = $700,000, 'ponle 700' = $700,000. Solo rechaza si el monto interpretado es "
                f"MENOR al precio de salida. Ejemplo: si precio de salida es $649,000 y el cliente dice '700', "
                f"eso es $700,000 que es MAYOR a $649,000 → ACEPTAR la propuesta.\n"
            )
        else:
            campaign_type_code = (tracking_data.get("campaign_type") or "A").upper()
            _is_special_campaign = campaign_type_code in ("SU", "LQ", "PR", "EV")
            if _is_special_campaign:
                # Cliente llegó por campaña especial (Mejor Precio, Liquidación, Promoción,
                # Evento) pero NO hay instrucciones de campaña cargadas en Google Sheets.
                # El bot NO debe dar precios de inventario general porque la campaña tiene
                # condiciones especiales que no conocemos. Mejor recolectar datos.
                _is_special_campaign_no_instructions = True
                logger.warning(
                    f"⚠️ Tracking {tracking_id} tipo={campaign_type_code} ({campaign_type_label}) "
                    f"pero SIN instrucciones de campaña. ¿CAMPAIGNS_CSV_URL configurado?"
                )
                tracking_context = (
                    f"ORIGEN: Este cliente llegó por {campaign_type_label} de {tracking_vehicle} "
                    f"(Tracking: {tracking_id}).\n"
                    f"*** CAMPAÑA ESPECIAL SIN INSTRUCCIONES DETALLADAS ***\n"
                    f"Este cliente viene de una campaña de tipo '{campaign_type_label}' para el "
                    f"{tracking_vehicle}. Esta campaña tiene condiciones especiales (precio, "
                    f"dinámica, reglas) que NO están en el inventario general.\n"
                    f"REGLAS CRÍTICAS PARA ESTE CLIENTE:\n"
                    f"1. NO des el precio del inventario general. El precio de esta campaña puede "
                    f"ser diferente al del inventario.\n"
                    f"2. NO uses el disclaimer de intermediario (regla 2) con este cliente.\n"
                    f"3. Confirma su interés en el {tracking_vehicle} y pregunta: "
                    f"'¿Me compartes tu nombre para registrarte en la dinámica?'\n"
                    f"4. Recolecta: nombre, teléfono, correo, ciudad.\n"
                    f"5. Si pregunta por precio o condiciones de la campaña, di: "
                    f"'Un asesor te contacta con los detalles de esta dinámica.'\n"
                    f"6. Tu objetivo es capturar datos del cliente y pasarlo a un asesor, "
                    f"NO cotizar desde inventario.\n"
                )
            else:
                tracking_context = (
                    f"ORIGEN: Este cliente llegó por {campaign_type_label} de {tracking_vehicle} "
                    f"(Tracking: {tracking_id}). Sabemos su modelo de interés. "
                    f"Si es el primer mensaje, confirma brevemente que le interesa el {tracking_vehicle} "
                    f"antes de dar precio, ubicación y condiciones completas.\n"
                )
    elif _is_organic_campaign_match and _matched_campaign:
        # Cliente llegó orgánicamente pero mencionó un vehículo con campaña activa.
        # Inyectar contexto de campaña de forma MÁS SUAVE que para tracking ID:
        # - NO forzar la dinámica de inmediato
        # - SÍ incluir las instrucciones para que el bot las conozca
        # - Dejar que la conversación fluya naturalmente
        campaign_vehicle = _matched_campaign.tracking_id.split("-")[0] if _matched_campaign.tracking_id else ""
        # Resolve model code to label for display
        from src.monday_service import MODEL_CODE_MAP
        campaign_vehicle_label = MODEL_CODE_MAP.get(campaign_vehicle.upper(), campaign_vehicle)
        tracking_context = (
            f"CONTEXTO CAMPAÑA (cliente orgánico): Este cliente llegó por su cuenta "
            f"(NO por un anuncio con tracking ID). Mencionó interés en {campaign_vehicle_label}.\n"
            f"*** HAY UNA CAMPAÑA ACTIVA para este vehículo: \"{_matched_campaign.name}\" ***\n"
            f"INSTRUCCIONES DE CAMPAÑA (REFERENCIA — aplícalas solo cuando sea natural en la conversación):\n"
            f"{_matched_campaign.instructions}\n"
            f"*** FIN INSTRUCCIONES DE CAMPAÑA ***\n"
            f"REGLAS PARA CLIENTE ORGÁNICO (PRIORIDAD MÁXIMA):\n"
            f"1. SÉ IMPARCIAL. NO menciones la campaña ni la dinámica de entrada. El cliente no sabe que existe.\n"
            f"2. Pregunta de forma ABIERTA y NEUTRA: '¿Cuál {campaign_vehicle_label} te interesa?' o "
            f"'¿Tienes algún año o versión en mente?' — deja que EL CLIENTE se perfile solo.\n"
            f"3. NUNCA ofrezcas menú tipo '¿quieres la regular o la de la dinámica?' — eso sesga.\n"
            f"4. ESPERA a que el cliente dé señales claras (mencione año, ciudad, precio, dinámica, "
            f"'vi un anuncio', 'la de mejor propuesta', 'la de León', 'la de 649'). "
            f"Solo ENTONCES confirma y aplica las instrucciones de campaña.\n"
            f"5. Si el cliente NO da señales de campaña, sigue por inventario normal como cualquier cliente.\n"
            f"6. Sigue ofreciendo el inventario completo si el cliente pregunta por otros modelos.\n"
            f"INTERPRETACIÓN DE MONTOS (solo cuando campaña ya esté confirmada): "
            f"'700' = $700,000, '650' = $650,000.\n"
        )

    # Build ad context section if referral has externalAdReply info
    ad_context_section = ""
    referral_data = context.get("referral_data") or {}
    if referral_data and turn_count <= 3:
        ad_reply_raw = referral_data.get("externalAdReply", "")
        if ad_reply_raw:
            # Parse externalAdReply dict if it was stored as string repr
            ad_title = ""
            ad_body = ""
            if isinstance(ad_reply_raw, dict):
                ad_title = ad_reply_raw.get("title", "")
                ad_body = ad_reply_raw.get("body", "")
            elif isinstance(ad_reply_raw, str):
                # Try to extract title and body from string representation
                import ast
                try:
                    ad_dict = ast.literal_eval(ad_reply_raw)
                    if isinstance(ad_dict, dict):
                        ad_title = ad_dict.get("title", "")
                        ad_body = ad_dict.get("body", "")
                except (ValueError, SyntaxError):
                    pass
            if ad_title or ad_body:
                ad_context_section = (
                    f"CONTEXTO DEL ANUNCIO (el cliente llegó por este anuncio de Facebook/Instagram):\n"
                    f"  Título: {ad_title}\n"
                    f"  Descripción: {ad_body}\n"
                    f"  IMPORTANTE: Este contexto te indica cómo llegó el cliente, pero NO asumas que quiere exactamente esa unidad. "
                    f"Si el cliente no ha mencionado un modelo específico, confirma su interés antes de ofrecer detalles de una unidad concreta.\n"
                )

    # Campañas activas del Sheet
    # Campaign instructions are injected via tracking_context for both:
    # 1. Tracking ID matches (ad arrivals) — with PRIORITY instructions
    # 2. Keyword matches (organic arrivals) — with softer DISAMBIGUATION instructions
    # Never inject generic campaign block for non-campaign clients — it confuses GPT.
    campaigns_section = ""
    if _has_campaign_instructions and _matched_campaign:
        # Ya inyectada en tracking_context (tracking ID o keyword match) → omitir bloque genérico
        pass
    elif tracking_id and campaign_service:
        # Client has tracking ID but no matched campaign — try generic block
        try:
            await campaign_service.ensure_loaded()
            campaigns_section = campaign_service.build_campaigns_prompt_block()
            if campaigns_section:
                campaigns_section += "\n"
        except Exception as e:
            logger.error(f"⚠️ Error cargando campañas para prompt: {e}")

    # Extraer últimos 2 mensajes del bot del historial para anti-repetición
    last_bot_msgs = []
    if history:
        for _line in reversed(history.strip().split("\n")):
            if _line.startswith("A: ") and len(last_bot_msgs) < 2:
                last_bot_msgs.append(_line[3:].strip())
    last_bot_msg = last_bot_msgs[0] if last_bot_msgs else ""

    last_bot_section = ""
    if last_bot_msgs:
        last_bot_section = f"TUS ÚLTIMOS MENSAJES (NO REPETIR NI PARAFRASEAR):\n"
        for i, _bm in enumerate(last_bot_msgs):
            last_bot_section += f"  [{i+1}]: {_bm[:200]}\n"

    # Build collected-data section so GPT knows what it already has
    _collected_items = []
    if saved_name:
        _collected_items.append(f"NOMBRE: {saved_name}")
    if saved_email:
        _collected_items.append(f"EMAIL: {saved_email}")
    if saved_phone:
        _collected_items.append(f"TELÉFONO: {saved_phone} (ya lo tienes, NO lo pidas)")
    if saved_city:
        _collected_items.append(f"CIUDAD: {saved_city}")
    _collected_section = ""
    if _collected_items:
        _collected_section = (
            "*** DATOS YA RECOPILADOS (NO volver a pedir estos datos): "
            + " | ".join(_collected_items) + " ***\n"
        )

    # Context block assembly — ORDER MATTERS for LLM attention.
    #
    # Rule: the model pays most attention to content near the END of a long
    # prompt ("recency bias").  We exploit this with a deliberate layout:
    #
    #   1. Static metadata (turn, time, detected data)   ← low attention OK
    #   2. Origin / campaign context                     ← medium
    #   3. Conversation history                          ← medium
    #   4. Critical collected-data reminder              ← high
    #   5. Inventory / financing                         ← HIGHEST — right before
    #                                                       the user message
    #
    # This avoids the "lost in the middle" failure mode where inventory buried
    # between tracking context and history gets ignored by the model.
    context_block = (
        # ── 1. Static metadata ──
        f"TURNO: {turn_count} {'(PRIMER MENSAJE - puedes saludar)' if turn_count == 1 else '(NO saludes, ve directo al punto)'}\n"
        f"MOMENTO ACTUAL: {current_time_str}\n"
        f"CLIENTE DETECTADO: {saved_name or '(Desconocido)'}\n"
        f"INTERÉS DETECTADO: {last_interest or '(Sin modelo)'}\n"
        f"CITA DETECTADA: {last_appointment or '(Sin cita)'}\n"
        f"PAGO DETECTADO: {last_payment or '(Por definir)'}\n"
        # ── 2. Origin / campaign ──
        f"{tracking_context}"
        f"{ad_context_section}"
        f"{campaigns_section}"
        # ── 3. Conversation history ──
        f"HISTORIAL DE CHAT:\n{history[-3000:]}\n"
        # ── 4. Critical reminder (collected data + anti-repetition) ──
        f"\n*** SECCIÓN CRÍTICA — LEE ESTO ANTES DE RESPONDER ***\n"
        f"{_collected_section}"
        f"{last_bot_section}"
        f"{name_gate_reminder}"
        # ── 5. Inventory / financing — LAST, closest to the user message ──
        f"{inventory_section}"
        f"{financing_section}"
    )

    messages = [
        {"role": "system", "content": formatted_system_prompt},
        {"role": "user", "content": context_block},
        {"role": "user", "content": user_message},
    ]

    lead_info: Optional[Dict[str, Any]] = None
    campaign_data_payload: Optional[Dict[str, Any]] = None
    reply_clean = "Hubo un error técnico."

    # Placeholder markers used in prompt examples — reject if found in extracted data
    _PLACEHOLDER_MARKERS = [
        "x@y.com", "5551234567", "821,000", "Juan Perez",
        "juan@correo.com", "Nayarit", "[nombre real", "[modelo del",
        "[fecha/hora", "[Contado o",
    ]

    try:
        # JSON mode: forces the model to always return a valid JSON object.
        # _parse_structured_reply() extracts reply/lead_event/campaign_data and
        # falls back to regex parsing if the model ignores the format instruction.
        resp = await _llm_call_with_fallback(
            messages,
            max_tokens=450,
            response_format={"type": "json_object"},
        )

        raw_reply = resp.choices[0].message.content or ""
        reply_clean, _lead_candidate, _campaign_candidate = _parse_structured_reply(raw_reply)

        # Update interest using user+bot text
        inferred_interest = _extract_interest_from_messages(user_message, reply_clean, inventory_service)
        if inferred_interest:
            last_interest = inferred_interest

        # --- Validate lead_event ---
        if isinstance(_lead_candidate, dict):
            # Fill in slots already known from context
            if not str(_lead_candidate.get("nombre", "")).strip() and saved_name:
                _lead_candidate["nombre"] = saved_name
            if not str(_lead_candidate.get("interes", "")).strip() and last_interest:
                _lead_candidate["interes"] = last_interest
            if not str(_lead_candidate.get("cita", "")).strip() and last_appointment:
                _lead_candidate["cita"] = last_appointment
            if not str(_lead_candidate.get("pago", "")).strip() and last_payment:
                _lead_candidate["pago"] = last_payment

            # Reject placeholders from the prompt template
            _lead_str = json.dumps(_lead_candidate, ensure_ascii=False).lower()
            _has_placeholder = any(p.lower() in _lead_str for p in _PLACEHOLDER_MARKERS)
            if _has_placeholder:
                logger.warning(f"⚠️ lead_event RECHAZADO (contiene placeholder): {_lead_candidate}")
            elif _lead_is_valid(_lead_candidate):
                lead_info = _lead_candidate
                logger.info(f"✅ Lead extraído (JSON mode): {_lead_candidate}")
            else:
                logger.warning(f"lead_event descartado (incompleto): {_lead_candidate}")

        # --- Validate campaign_data ---
        if isinstance(_campaign_candidate, dict) and _campaign_candidate.get("resumen"):
            _resumen = _campaign_candidate["resumen"]
            _has_placeholder = any(p.lower() in _resumen.lower() for p in _PLACEHOLDER_MARKERS)
            if _has_placeholder:
                logger.warning(f"⚠️ campaign_data RECHAZADO (placeholder): {_resumen}")
            else:
                campaign_data_payload = _campaign_candidate
                logger.info(f"📋 campaign_data extraído: {_resumen}")

    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        reply_clean = "Dame un momento, estoy consultando sistema..."

    # Clean prefixes
    reply_clean = re.sub(
        r"^(Adrian|Asesor|Bot)\s*:\s*",
        "",
        reply_clean.strip(),
        flags=re.IGNORECASE,
    ).strip()

    # === POST-LLM: Duplicate response detection ===
    # Check if the bot is about to send the same message it already sent recently
    if reply_clean and history:
        _last_bot_replies = []
        for _hline in reversed(history.strip().split("\n")):
            if _hline.startswith("A: ") and len(_last_bot_replies) < 3:
                _last_bot_replies.append(_hline[3:].strip().lower())
        _reply_tokens = set(reply_clean.lower().split())
        _is_dup = False
        if len(_reply_tokens) >= 3:
            for _prev in _last_bot_replies:
                _prev_tokens = set(_prev.split())
                if _prev_tokens:
                    _jaccard = len(_reply_tokens & _prev_tokens) / len(_reply_tokens | _prev_tokens)
                    if _jaccard >= 0.75:
                        _is_dup = True
                        break
        if _is_dup:
            logger.warning(f"⚠️ DEDUP: Respuesta duplicada detectada (Jaccard >= 0.75), re-generando...")
            _anti_repeat = [
                {"role": "assistant", "content": reply_clean},
                {"role": "user", "content": (
                    "*** ALERTA: Tu respuesta anterior fue IDÉNTICA a un mensaje previo. ***\n"
                    f"RESPUESTA RECHAZADA: \"{reply_clean[:200]}\"\n"
                    f"{_collected_section}"
                    "GENERA UNA RESPUESTA COMPLETAMENTE DIFERENTE. "
                    "Si ya pediste datos, reconoce los que el cliente ya dio y pide SOLO los que faltan. "
                    "Si no faltan datos, avanza la conversación."
                )},
            ]
            try:
                _retry_resp = await _llm_call_with_fallback(
                    messages + _anti_repeat,
                    max_tokens=450,
                    response_format={"type": "json_object"},
                )
                _raw_retry = (_retry_resp.choices[0].message.content or "").strip()
                _retry_reply, _, _ = _parse_structured_reply(_raw_retry)
                _retry_reply = re.sub(
                    r"^(Adrian|Asesor|Bot)\s*:\s*", "", _retry_reply.strip(), flags=re.IGNORECASE,
                ).strip()
                # Verify retry is actually different
                _retry_tokens = set(_retry_reply.lower().split())
                _still_dup = False
                if len(_retry_tokens) >= 3:
                    for _prev in _last_bot_replies:
                        _prev_tokens = set(_prev.split())
                        if _prev_tokens and len(_retry_tokens & _prev_tokens) / len(_retry_tokens | _prev_tokens) >= 0.75:
                            _still_dup = True
                            break
                if _retry_reply and not _still_dup:
                    reply_clean = _retry_reply
                    logger.info(f"✅ DEDUP: Re-generación exitosa")
                else:
                    logger.warning(f"⚠️ DEDUP: Re-generación también duplicada, usando fallback determinístico")
                    _missing = []
                    if not saved_name: _missing.append("nombre")
                    if not saved_email: _missing.append("correo")
                    if not saved_city: _missing.append("ciudad")
                    if _missing:
                        reply_clean = f"Gracias por tu información. Solo me falta: {', '.join(_missing)}."
                    else:
                        reply_clean = "Perfecto, ya tengo tus datos registrados. Un asesor se pone en contacto contigo en breve."
            except Exception as _dup_err:
                logger.error(f"❌ DEDUP re-gen error: {_dup_err}")

    # 🔥 CAMBIO CLAVE: Construir new_context ANTES de llamar a _pick_media_urls
    new_context: Dict[str, Any] = {
        "history": (history + f"\nC: {user_message}\nA: {reply_clean}").strip()[-4000:],
        "user_name": saved_name,
        "last_interest": last_interest,
        "last_appointment": last_appointment,
        "last_payment": last_payment,
        "user_email": saved_email,
        "user_phone": saved_phone,
        "user_city": saved_city,
        "turn_count": turn_count,
        # Mantener valores previos de fotos si existen
        "photo_model": context.get("photo_model"),
        "photo_index": context.get("photo_index", 0),
        # Mantener tipo de PDF solicitado para peticiones genéricas
        "last_pdf_request_type": context.get("last_pdf_request_type"),
        # Preservar referral + tracking data across turns (BUG FIX: se perdían)
        "referral_source": context.get("referral_source"),
        "referral_data": context.get("referral_data"),
        "tracking_id": context.get("tracking_id"),
        "tracking_data": context.get("tracking_data"),
        # Vehicle ubicacion: which specific unit is of interest (for photo/location filtering)
        "interest_ubicacion": context.get("interest_ubicacion"),
        "interest_ubicacion_source": context.get("interest_ubicacion_source"),
    }

    # Detect vehicle ubicacion in legacy path too — explicit user mention always wins
    _vehicle_ubic = _detect_vehicle_ubicacion(user_message, inventory_service, last_interest)
    if _vehicle_ubic:
        _prev_ubic_legacy = new_context.get("interest_ubicacion")
        new_context["interest_ubicacion"] = _vehicle_ubic
        new_context["interest_ubicacion_source"] = "explicit_user"
        logger.info(f"📍 Vehicle ubicacion stored (legacy, explicit_user): {_vehicle_ubic}")
        if _prev_ubic_legacy and _normalize_spanish(_prev_ubic_legacy) != _normalize_spanish(_vehicle_ubic):
            new_context["photo_index"] = 0
            new_context["photo_model"] = ""
            logger.info(f"📸 Photo carousel reset (legacy): unit changed '{_prev_ubic_legacy}' → '{_vehicle_ubic}'")

    # Pasamos new_context (la función lo modificará)
    media_urls = _pick_media_urls(user_message, reply_clean, inventory_service, new_context)
    reply_clean = _sanitize_reply_if_photos_attached(reply_clean, media_urls)

    # Si el bot prometió fotos pero no se encontraron, corregir la respuesta
    if not media_urls:
        photo_promise_patterns = [
            r"aqu[ií]\s+tienes",
            r"te\s+(mando|envío|comparto)\s+(las\s+)?fotos",
        ]
        msg_lower = (user_message or "").lower()
        photo_requested = any(k in msg_lower for k in ["foto", "fotos", "imagen", "imágenes", "imagenes"])
        if photo_requested:
            for p in photo_promise_patterns:
                if re.search(p, reply_clean, re.IGNORECASE):
                    reply_clean = "Por el momento no tengo fotos de ese modelo en sistema. Un asesor te las puede compartir."
                    logger.warning(f"⚠️ Bot prometió fotos pero no se encontraron, respuesta corregida")
                    break

    # Quitar markdown links que WhatsApp no soporta
    reply_clean = _strip_markdown_links(reply_clean)

    # ============================================================
    # MONDAY FAILSAFE (MEJORADO - AGRESIVO)
    # ============================================================
    if lead_info is None:
        candidate = {
            "nombre": saved_name,
            "interes": last_interest,
            "cita": last_appointment,
            "pago": last_payment or "Por definir",
        }

        # CAMBIO 1: Validar ANTES de esperar confirmación
        if _lead_is_valid(candidate):
            lead_info = candidate
            logger.info(f"✅ FAILSAFE: Lead válido encontrado sin JSON de OpenAI - {candidate}")
        
        # CAMBIO 2: Si hay nombre + interés + cita, Y el mensaje es corto (posible confirmación)
        elif saved_name and last_interest and last_appointment:
            # Verificar si el mensaje es una confirmación o respuesta corta
            if _message_confirms_appointment(user_message) or len(user_message.strip()) <= 15:
                # Forzar registro aunque falte algo
                candidate["pago"] = candidate.get("pago") or "Por definir"
                if _lead_is_valid(candidate):
                    lead_info = candidate
                    logger.info(f"✅ FAILSAFE AGRESIVO: Mensaje corto '{user_message}' después de cita confirmada - {candidate}")

    # Log para debugging de leads
    if saved_name and last_interest and last_appointment:
        if lead_info:
            logger.info(f"🎯 LEAD SERÁ ENVIADO A MONDAY: {lead_info}")
        else:
            logger.warning(
                f"⚠️ LEAD NO GENERADO aunque hay datos: "
                f"nombre={saved_name}, interes={last_interest}, cita={last_appointment}, "
                f"mensaje_usuario='{user_message}'"
            )

    # ============================================================
    # FUNNEL STAGE CALCULATION (V2)
    # ============================================================
    # V2 Labels: 1er Contacto → Intención → Cotización → Cita Programada
    # "Sin Interes" can override any stage
    funnel_stage = "1er Contacto"  # Default: primer contacto (V2: merges Mensaje+Enganche)

    if last_interest:
        funnel_stage = "Intención"  # Modelo específico mencionado

    # Cotización: se marca cuando se envía PDF (ver pdf_info más abajo)
    # Se maneja después de la detección de PDF

    if last_appointment:
        funnel_stage = "Cita Programada"  # V2: renamed from "Cita agendada"

    # V2: Sin Interes overrides everything
    is_disinterest = _detect_disinterest(user_message)
    if is_disinterest:
        funnel_stage = "Sin Interes"

    # Agregar etapa al contexto para tracking
    new_context["funnel_stage"] = funnel_stage

    # ============================================================
    # PDF DETECTION (FICHA TÉCNICA / CORRIDA)
    # ============================================================
    _bases_url_legacy = _matched_campaign.bases_pdf_url if _matched_campaign else None
    pdf_info = _detect_pdf_request(user_message, last_interest, new_context, bases_pdf_url=_bases_url_legacy)
    if pdf_info:
        # Guardar tipo de PDF solicitado para peticiones genéricas posteriores
        if pdf_info.get("tipo"):
            new_context["last_pdf_request_type"] = pdf_info.get("tipo")

        if pdf_info.get("sin_modelo"):
            # No hay modelo detectado, el bot debe preguntar
            logger.info(f"📄 PDF solicitado ({pdf_info.get('tipo')}) pero sin modelo detectado")
        elif pdf_info.get("sin_pdf"):
            # No tenemos el PDF de ese modelo
            logger.info(f"📄 PDF solicitado ({pdf_info.get('tipo')}) pero no disponible para {pdf_info.get('modelo')}")
        elif pdf_info.get("pdf_url"):
            # Tenemos el PDF, lo vamos a enviar
            logger.info(f"📄 PDF detectado: {pdf_info.get('tipo')} - {pdf_info.get('modelo')} - {pdf_info.get('filename')}")
            # Reemplazar la respuesta del bot con el mensaje apropiado
            reply_clean = pdf_info.get("mensaje", reply_clean)

            # V2: Sending PDF/ficha = Cotización stage (only advance, not regress)
            if funnel_stage != "Sin Interes" and funnel_stage in ("1er Contacto", "Intención"):
                funnel_stage = "Cotización"
                new_context["funnel_stage"] = funnel_stage

    return {
        "reply": reply_clean,
        "new_state": "chatting",
        "context": new_context,
        "media_urls": media_urls,
        "lead_info": lead_info,
        "funnel_stage": funnel_stage,
        "is_disinterest": is_disinterest,
        "funnel_data": {
            "nombre": saved_name or None,
            "interes": last_interest or None,
            "cita": last_appointment or None,
            "pago": last_payment or None,
            "turn_count": turn_count,
        },
        "pdf_info": pdf_info,
        "campaign_data": campaign_data_payload,
    }
