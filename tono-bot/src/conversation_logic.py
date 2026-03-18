import os
import re
import json
import logging
import asyncio
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

import httpx
import pytz
from openai import AsyncOpenAI, APITimeoutError, RateLimitError, APIStatusError, APIConnectionError

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
  * "El Cascadia 2014 está en liquidación en León a $600,000 de contado." (saltó a unidad, precio, ubicación y condiciones sin confirmación)
- NUNCA menciones liquidaciones, subastas, precios de salida, fechas límite, ni condiciones especiales hasta que el cliente confirme que se refiere a ESA unidad específica.
- Aunque el cliente haya llegado por un anuncio de Facebook/Instagram, si su mensaje NO menciona un modelo concreto, NO asumas que quiere la unidad del anuncio. Enfoca por tipo de vehículo y confirma.
- TRACKING ID: Si el cliente envía un Tracking ID de campaña (ej. CA-LQ1, TG9-A1), SÍ sabemos su interés. Confirma brevemente ("¿Te refieres al [modelo] del anuncio?") y al confirmar, ahora sí da los detalles de la campaña.
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

6) ANTI-REPETICIÓN:
- NUNCA preguntes algo que ya sabes.
- Revisa HISTORIAL antes de responder.

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

14) LEAD (JSON):
- SOLO genera JSON si hay: NOMBRE + MODELO + CITA CONFIRMADA.
```json
{{
  "lead_event": {{
    "nombre": "Juan Perez",
    "interes": "[modelo del inventario]",
    "cita": "Lunes 10 AM",
    "pago": "Contado"
  }}
}}
```

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


def _detect_pdf_request(user_message: str, last_interest: str, context: Dict[str, Any] = None) -> Optional[Dict[str, Any]]:
    """
    Detecta si el usuario pide un PDF (ficha técnica o corrida).
    Retorna dict con: tipo, pdf_url, filename, mensaje_previo
    O None si no pide PDF.

    Ahora con soporte de contexto para:
    - Typos comunes ("fiche", "fixa", "corrda")
    - Peticiones genéricas ("pásamela", "mándamela") si hubo PDF previo
    """
    msg = (user_message or "").lower()
    context = context or {}

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
    return [u.strip() for u in raw.split("|") if u.strip().startswith("http")]


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
            for item in items:
                modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"]).strip()
                if _normalize_spanish(modelo) == interest_norm or any(tok in _normalize_spanish(modelo) for tok in interest_tokens):
                    target_item = item
                    target_model_name = modelo
                    break

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
        for item in items:
            modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"]).strip()
            if _normalize_spanish(modelo) == _normalize_spanish(interest_no_year):
                target_item = item
                target_model_name = modelo
                break

    if not target_item:
        return []

    # 5) Extraer fotos
    urls = _extract_photos_from_item(target_item)
    logger.info(f"📸 Fotos seleccionadas: modelo='{target_model_name}', {len(urls)} URLs, last_interest='{last_interest}'")
    if not urls:
        return []

    # 6) Si cambió de modelo, reiniciar índice
    if _normalize_spanish(target_model_name) != _normalize_spanish(current_photo_model):
        photo_index = 0
        context["photo_model"] = target_model_name

    # 7) Determinar si quiere "otra" (1 foto) o "fotos" (grupo)
    wants_next = any(k in msg for k in ["otra", "mas", "más", "siguiente"])
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
) -> Optional[Any]:
    """Intenta un proveedor LLM con retries cortos. Retorna respuesta o None."""
    for _attempt in range(max_retries):
        try:
            resp = await llm_client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
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


async def _llm_call_with_fallback(messages: list, temperature: float = 0.3, max_tokens: int = 350):
    """
    Intenta el proveedor primario (configurable via LLM_PRIMARY) con retries cortos,
    luego cae al secundario. Reduce latencia vs 3 retries largos.
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
        label=primary_label, max_retries=2,
    )
    if resp is not None:
        return resp

    # --- Fallback ---
    logger.warning(f"🔄 {primary_label} falló. Usando fallback {fallback_label} ({fallback_model})...")
    resp = await _llm_try_provider(
        fallback_client, fallback_model, messages, temperature, max_tokens,
        label=f"Fallback-{fallback_label}", max_retries=2,
    )
    if resp is not None:
        logger.info(f"✅ Fallback {fallback_label} ({fallback_model}) exitoso.")
        return resp

    raise RuntimeError(f"Ambos proveedores LLM fallaron ({primary_label} + {fallback_label})")


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

    # Auto-populate interest from tracking ID if not yet detected from conversation
    if not last_interest:
        tracking_vehicle = (context.get("tracking_data") or {}).get("vehicle_label", "")
        if tracking_vehicle:
            last_interest = tracking_vehicle
    try:
        turn_count = int(context.get("turn_count", 0)) + 1
    except (ValueError, TypeError):
        turn_count = 1

    # Extract from user input
    extracted_name = _extract_name_from_text(user_message, history)
    if extracted_name:
        saved_name = extracted_name

    extracted_payment = _extract_payment_from_text(user_message)
    if extracted_payment:
        last_payment = extracted_payment

    extracted_appt = _extract_appointment_from_text(user_message)
    if extracted_appt:
        last_appointment = extracted_appt

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
    if _needs_inventory_context(user_message, turn_count, last_interest, inventory_service):
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
    tracking_id = context.get("tracking_id")
    if tracking_id:
        tracking_data = context.get("tracking_data") or {}
        tracking_vehicle = tracking_data.get("vehicle_label", "")
        campaign_type_label = tracking_data.get("campaign_type_label", "Anuncio")
        tracking_context = (
            f"ORIGEN: Este cliente llegó por {campaign_type_label} de {tracking_vehicle} "
            f"(Tracking: {tracking_id}). Sabemos su modelo de interés. "
            f"Si es el primer mensaje, confirma brevemente que le interesa el {tracking_vehicle} "
            f"antes de dar precio, ubicación y condiciones completas.\n"
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
    campaigns_section = ""
    if campaign_service:
        try:
            await campaign_service.ensure_loaded()
            campaigns_section = campaign_service.build_campaigns_prompt_block()
            if campaigns_section:
                campaigns_section += "\n"
        except Exception as e:
            logger.error(f"⚠️ Error cargando campañas para prompt: {e}")

    context_block = (
        f"TURNO: {turn_count} {'(PRIMER MENSAJE - puedes saludar)' if turn_count == 1 else '(NO saludes, ve directo al punto)'}\n"
        f"MOMENTO ACTUAL: {current_time_str}\n"
        f"CLIENTE DETECTADO: {saved_name or '(Desconocido)'}\n"
        f"INTERÉS DETECTADO: {last_interest or '(Sin modelo)'}\n"
        f"CITA DETECTADA: {last_appointment or '(Sin cita)'}\n"
        f"PAGO DETECTADO: {last_payment or '(Por definir)'}\n"
        f"{tracking_context}"
        f"{ad_context_section}"
        f"{campaigns_section}"
        f"{inventory_section}"
        f"{financing_section}"
        f"HISTORIAL DE CHAT:\n{history[-3000:]}"
        f"{name_gate_reminder}"
    )

    messages = [
        {"role": "system", "content": formatted_system_prompt},
        {"role": "user", "content": context_block},
        {"role": "user", "content": user_message},
    ]

    lead_info: Optional[Dict[str, Any]] = None
    reply_clean = "Hubo un error técnico."

    try:
        resp = await _llm_call_with_fallback(messages)

        raw_reply = resp.choices[0].message.content or ""
        reply_clean = raw_reply

        # Update interest using user+bot text
        inferred_interest = _extract_interest_from_messages(user_message, raw_reply, inventory_service)
        if inferred_interest:
            last_interest = inferred_interest

        # Extract optional JSON from the model (inside ```json ... ```)
        json_match = re.search(r"```json\s*({.*?})\s*```", raw_reply, flags=re.DOTALL | re.IGNORECASE)
        if json_match:
            try:
                payload = json.loads(json_match.group(1))
                candidate = payload.get("lead_event") if isinstance(payload, dict) else None

                if isinstance(candidate, dict):
                    # Inject what we already know
                    if not str(candidate.get("nombre", "")).strip() and saved_name:
                        candidate["nombre"] = saved_name
                    if not str(candidate.get("interes", "")).strip() and last_interest:
                        candidate["interes"] = last_interest
                    if not str(candidate.get("cita", "")).strip() and last_appointment:
                        candidate["cita"] = last_appointment
                    if not str(candidate.get("pago", "")).strip() and last_payment:
                        candidate["pago"] = last_payment

                    if _lead_is_valid(candidate):
                        lead_info = candidate
                        logger.info(f"✅ Lead extraído del JSON de OpenAI: {candidate}")
                    else:
                        logger.warning(f"Lead JSON discarded (incomplete): {candidate}")

                # Hide JSON from user-facing message
                reply_clean = raw_reply.replace(json_match.group(0), "").strip()
            except Exception as e:
                logger.error(f"Error parseando JSON de lead: {e}")
                reply_clean = raw_reply.replace(json_match.group(0), "").strip()

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

    # 🔥 CAMBIO CLAVE: Construir new_context ANTES de llamar a _pick_media_urls
    new_context: Dict[str, Any] = {
        "history": (history + f"\nC: {user_message}\nA: {reply_clean}").strip()[-4000:],
        "user_name": saved_name,
        "last_interest": last_interest,
        "last_appointment": last_appointment,
        "last_payment": last_payment,
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
    }

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
    pdf_info = _detect_pdf_request(user_message, last_interest, new_context)
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
    }
