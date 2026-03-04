# LÓGICA DE NEGOCIO, FLUJOS Y REGLAS DE ESTADO
# Tono-Bot / "Adrian Jimenez" – Tractos y Max
# Documento técnico-operativo de arquitectura funcional y operación comercial

Este documento unifica y consolida la lógica de negocio, reglas de estado, validaciones y flujos transaccionales del asistente virtual de WhatsApp (en adelante "el bot"), diseñado para gestionar conversaciones con clientes potenciales, detectar intención comercial, responder solicitudes de información, enviar documentación (PDF), compartir fotos del inventario, gestionar citas y automatizar la progresión de leads dentro del embudo comercial conectado a Monday.com, manteniendo control estricto sobre calidad de datos y evitando interpretaciones erróneas o información inventada.

────────────────────────────────────────────────────────────
## 1) PROPÓSITO Y FILOSOFÍA OPERATIVA
────────────────────────────────────────────────────────────

### 1.1 Objetivo principal
El bot opera 24/7 y su objetivo principal es **destrabar**: resolver dudas, compartir información y eliminar fricciones para que el cliente visite la agencia.

### 1.2 Enfoque comercial (no agresivo)
El bot NO actúa como vendedor insistente. Responde en función de lo que el cliente pregunta, ofrece información útil (fichas, simulaciones, fotos), y cuando detecta señales suficientes, propone o confirma cita. Cuando es necesario, deriva a un asesor humano.

### 1.3 Principio de trazabilidad total
Toda acción relevante del bot queda reflejada automáticamente en Monday.com (campos del lead y notas de historial), sin intervención manual, para que ventas y dirección tengan visibilidad completa.

────────────────────────────────────────────────────────────
## 2) ARQUITECTURA DE DATOS EN MONDAY.COM
────────────────────────────────────────────────────────────

### 2.1 Tablero
Nombre del tablero: **"Leads Tractos y Max"**

### 2.2 Organización automática por mes
Los leads se agrupan por el mes en que llegan. Se crea dinámicamente el grupo mensual correspondiente (por ejemplo: "FEBRERO 2026", "MARZO 2026", "ABRIL 2026").

### 2.3 Campos típicos del registro (columna y función)
A continuación se describe qué se registra, quién lo registra y cuándo ocurre:

**Identidad del lead**
- **Nombre del ítem:** Nombre del cliente + teléfono (ejemplo "Juan Pérez | 5551234567"). Se actualiza cuando el bot detecta y valida el nombre real.
- **Teléfono:** Número de WhatsApp del cliente. Se captura desde el primer contacto.

**Estado del embudo (Embudo comercial)**
- **Estado:** Etapa del lead. El bot mueve etapas 1 a 5; el equipo mueve etapas 6 a 10.

**Interés**
- **Vehículo de Interés:** Modelo detectado y normalizado. Se llena al detectar mención explícita o implícita del modelo.

**Pago**
- **Esquema de Pago:** "De Contado", "Financiamiento" o "Por definir". Se llena cuando el cliente expresa intención de pago o se infiere que aún no lo mencionó.

**Cita**
- **Agenda Citas (Día):** Fecha de la cita. Se llena cuando el cliente confirma.
- **Hora Cita:** Hora de la cita. Se llena cuando el cliente confirma.

**Confirmación interna**
- **Confirmación CMV** (checkbox): Solo lo marca el equipo, el bot nunca lo modifica.

**Atribución (Referral Tracking / Meta)**
- **Origen Lead:** Ejemplo "Facebook Ad", "Instagram Post", "Directo".
- **Canal:** "Facebook", "Instagram", "Directo".
- **Tipo de Origen:** "Ad", "Post", "Directo".
- **Ad ID:** Identificador del anuncio (cuando está disponible).
- **CTWA Click ID:** Identificador del clic.

**Campos futuros** (pendientes de enriquecimiento):
- Campaign Name, Ad Set Name, Ad Name.

### 2.4 Notas del ítem (historial)
Cada evento importante (primer contacto, interés detectado, cotización enviada, cita programada, desinterés, etc.) genera una nota con detalles. Esto sirve como "bitácora" sin salir del tablero.

────────────────────────────────────────────────────────────
## 3) EMBUDO COMERCIAL Y REGLAS DE PROGRESIÓN (FUNNEL V2)
────────────────────────────────────────────────────────────

### 3.1 Jerarquía del embudo (10 etapas)

**Etapas automáticas (bot):**
1. 1er Contacto
2. Intención
3. Cotización
4. Cita Programada
5. Sin Interés

**Etapas manuales (equipo):**
6. Cita Atendida
7. Cita No Atendida
8. Venta Cerrada
9. Financiamiento en Gestión
10. Venta Caída

### 3.2 Regla de oro: solo avance
Un lead **nunca retrocede** de estado. La jerarquía se valida mediante una estructura de control (conceptualmente STAGE_HIERARCHY). Ejemplo: si ya está en "Cotización", no puede volver a "Intención".

### 3.3 Override global: Sin Interés
"Sin Interés" puede activarse en cualquier momento si el cliente expresa rechazo explícito (STOP/BAJA/cancela/no me interesa). En cuanto se detecta, el lead se marca como terminal.

### 3.4 Estados terminales y reinicio de ciclo

**Estados terminales:**
- Venta Cerrada
- Venta Caída
- Sin Interés

Cuando el lead está en estado terminal, si ese mismo cliente vuelve a escribir, el bot **NO actualiza el registro cerrado**. En su lugar crea un **nuevo registro** (nuevo ítem) para iniciar un ciclo fresco.

────────────────────────────────────────────────────────────
## 4) ACCIONES TRANSACCIONALES EN TIEMPO REAL (DURANTE LA CONVERSACIÓN)
────────────────────────────────────────────────────────────

### 4.1 Creación de Lead en Monday.com

**Evento disparador:**
- Primer mensaje recibido del cliente por WhatsApp.

**Condiciones necesarias (filtros de seguridad):**
- Identificador remoteJid válido.
- El mensaje no proviene de un grupo.
- El mensaje no es broadcast.

**Acción ejecutada:**
- Se crea un nuevo ítem en Monday.com.
- Se asigna automáticamente el estado "1er Contacto".
- Se coloca el registro dentro del grupo mensual dinámico correspondiente.
- Se almacenan variables de atribución de origen si vienen en el primer mensaje (Referral Tracking).
- Se registra el teléfono desde el primer contacto.

**Protección contra duplicados:**
- Antes de crear, el bot busca si el teléfono ya existe en el tablero.
- Si existe y NO está en estado terminal, actualiza el mismo ítem (no crea duplicado).
- Solo crea un nuevo ítem si el registro anterior está en estado terminal.

---

### 4.2 Detección de intención de compra (interés por modelo)

**Evento disparador:**
- El cliente menciona un modelo específico o hace referencia clara al inventario ("la G9", "la van", "el tracto", etc.).

**Condiciones necesarias:**
- Variable de interés detectada (conceptualmente last_interest).
- Score de coincidencia igual o mayor a 2 entre tokens del mensaje y el inventario activo.
- El sistema aplica normalización (mapeo de sinónimos / errores comunes).

**Acción ejecutada:**
- El lead avanza a estado "Intención".
- Se actualiza "Vehículo de Interés".
- Se normaliza el nombre del modelo usando un mapa de equivalencias (conceptualmente VEHICLE_DROPDOWN_MAP).

**Reconocimiento de variaciones y errores comunes (ejemplos):**

| El cliente escribe... | El bot entiende... |
|----------------------|-------------------|
| "la G9" | Tunland G9 |
| "la pickup" / "la troca" | Tunland (variante según disponibilidad) |
| "la van" / "la panel" | Toano Panel |
| "el camioncito" | Miler |
| "el tracto" | ESTA 6x4 |
| "miller" (doble L) | Miler |
| "tunlan" / "tunlad" | Tunland |

**Valores posibles típicos en el dropdown:**
Tunland E5, ESTA 6x4 11.8, ESTA 6x4 X13, Miler, Toano Panel, Tunland G7, Tunland G9, Cascadia.

---

### 4.3 Envío de cotización o ficha técnica (PDF)

**Evento disparador:**
- El cliente solicita documentación técnica o financiera.

**Ejemplos de frases:**
- "Mándame la ficha técnica"
- "¿Me pasas la corrida financiera?"
- "Quiero ver especificaciones"
- "Envíame la simulación de pagos"

**Condiciones necesarias:**
- Detección de palabras clave (acción + tipo de documento).
- Modelo de vehículo identificado.
- Documento disponible en la fuente de documentos (financing.json).

**Acción ejecutada:**
- El bot envía un mensaje introductorio.
- Envía el PDF correspondiente.
- El estado del lead cambia a "Cotización".
- Se agrega nota: "Cotización enviada: [Modelo]".

**Tipos de documentos:**
- Ficha técnica (PDF).
- Corrida financiera (PDF: enganche, mensualidades, tasas).

---

### 4.4 Cita Programada (agendamiento)

**Evento disparador:**
- El cliente confirma día y hora para una reunión / visita.

**Condiciones necesarias:**
- Nombre válido:
  - Mínimo 3 caracteres.
  - Sin palabras de rechazo o evasión.
- Interés identificado (modelo):
  - Mínimo 2 caracteres (o equivalente validado por catálogo).
- Cita confirmada:
  - Datos mínimos interpretables (día y hora).

**Acción ejecutada:**
- El lead se actualiza en Monday.com.
- Estado cambia a "Cita Programada".
- La fecha se formatea a estándar ISO.
- Se registra "Agenda Citas (Día)" y "Hora Cita".
- El nombre del ítem se actualiza con el nombre real del cliente.
- Se agrega nota con el detalle de la cita.
- Si existe un teléfono de asesor responsable (conceptualmente OWNER_PHONE), se envía notificación automática por WhatsApp al responsable.

**Interpretación de horarios en lenguaje natural (ejemplos):**

| El cliente dice... | Monday registra... |
|-------------------|-------------------|
| "Mañana a las 10" | Fecha de mañana, 10:00 |
| "El viernes por la tarde" | Próximo viernes, 15:00 |
| "Miércoles a medio día" | Próximo miércoles, 12:00 |
| "Lunes a las 10 y media" | Próximo lunes, 10:30 |

**Restricción operativa:**
- El bot sabe que domingo la agencia está cerrada.
- Si el cliente propone domingo, el bot sugiere alternativas (sábado o lunes).

---

### 4.5 Detección de método de pago

**Evento disparador:**
- El cliente menciona su forma de pago.

**Palabras clave detectadas (ejemplos):**
contado, cash, crédito, financiamiento, mensualidades, no quiero crédito.

**Acción ejecutada:**
- Se actualiza la memoria contextual de la conversación.
- Se actualiza la columna "Esquema de Pago" en Monday.

**Valores posibles:**

| El cliente dice... | Monday registra... |
|-------------------|-------------------|
| "De contado" / "Cash" / "No quiero crédito" | **De Contado** |
| "A crédito" / "Financiamiento" / "Mensualidades" | **Financiamiento** |
| (No ha mencionado forma de pago) | **Por definir** |

---

### 4.6 Detección de desinterés (rechazo explícito)

**Evento disparador:**
- El cliente expresa rechazo explícito.

**Frases típicas:**
"no me interesa", "ya no quiero", "no gracias", "cancela", "dejen de escribirme", "STOP", "BAJA" y variantes.

**Acción ejecutada:**
- El estado cambia inmediatamente a "Sin Interés" (override global).
- Se agrega nota: "Lead expresó desinterés".
- Se considera terminal.
- Si el cliente regresa posteriormente, se crea un nuevo lead.

---

### 4.7 Envío de fotos de vehículos (carrusel)

**Evento disparador:**
- El cliente solicita imágenes o pide continuar ("mándame fotos", "otra", "siguiente").

**Condiciones necesarias:**
- Modelo identificado en inventario.

**Acción ejecutada:**
- Envío inicial: lote de 3 fotos exteriores del modelo solicitado.
- Bajo demanda: envíos individuales posteriores.
- Control de navegación: se mantiene un índice (conceptualmente photo_index) almacenado en SQLite para saber cuál es la "siguiente foto" por usuario y por modelo.

---

### 4.8 Procesamiento de multimedia entrante

**4.8.1 Notas de voz**

Proceso:
- Descarga del archivo.
- Desencriptado.
- Transcripción mediante Whisper API.
- El texto transcrito se incorpora al motor conversacional como si el cliente lo hubiera escrito.

**4.8.2 Imágenes enviadas por el cliente**

Proceso:
- Análisis mediante Gemini Vision o OpenAI Vision.
- Se genera una descripción breve enfocada en vehículos o documentos.
- Esa descripción se incorpora al contexto para responder mejor (por ejemplo: identificar si la imagen parece un vehículo del inventario).

---

### 4.9 Detección de intervención humana (handoff)

**Evento disparador:**
- Mensaje enviado desde el mismo número (canal) pero no generado por el bot.

**Condiciones (anti-falsos positivos):**
- fromMe = true.
- El ID del mensaje no existe en el registro de mensajes enviados por el bot.
- El texto no coincide con caché reciente (evitar confundir eco o reintentos).
- No es un mensaje automático de WhatsApp Business.

**Acción ejecutada:**
- El bot se silencia para ese usuario durante 60 minutos (no interfiere con el asesor).

---

### 4.10 Tracking de origen del lead (Referral Tracking)

**Evento disparador:**
- Primer mensaje que incluye información de referencia.

**Datos detectables:**
referral, conversionSource, ad_id, ctwa_clid.

**Acción ejecutada:**
- Se almacenan los datos en la sesión.
- Se actualizan columnas en Monday: Origen Lead, Canal, Tipo de Origen (y cuando aplique: Ad ID, Click ID).

────────────────────────────────────────────────────────────
## 5) ACCIONES ASÍNCRONAS Y AUTOMATIZACIONES (GESTIÓN DE ESTADOS)
────────────────────────────────────────────────────────────

### 5.1 Acumulación de mensajes (debouncing)

**Objetivo:**
Si el cliente manda varios mensajes seguidos, se agrupan y se procesan como una sola entrada para evitar respuestas fragmentadas.

**Temporizador:** 8 segundos.

**Protección adicional:**
Locks de concurrencia por JID (remoteJid) para evitar condiciones de carrera (mensajes simultáneos, reintentos de webhook, etc.).

---

### 5.2 Reactivación automática del bot

**Temporizador:** 60 minutos.

**Condición:**
El bot fue silenciado por intervención humana o por comando (por ejemplo /silencio).

**Acción:**
Se elimina al usuario de la lista de silenciados y el bot vuelve a responder.

---

### 5.3 Actualización del inventario

**Temporizador:** 300 segundos (5 minutos).

**Acciones:**
- Descarga el catálogo desde Google Sheets o un CSV local.
- Filtra unidades agotadas o no disponibles.

**Condiciones de exclusión:**
- Cantidad menor o igual a 0.
- Status distinto de "disponible".

**Resultado:**
El bot siempre responde con base en inventario activo, minimizando errores de disponibilidad.

────────────────────────────────────────────────────────────
## 6) REGLAS DE VALIDACIÓN, SEGURIDAD Y CALIDAD DE INFORMACIÓN
────────────────────────────────────────────────────────────

### 6.1 Candado de nombre (Name Gate)

El bot **NO puede:**
- Dar precios.
- Enviar cotizaciones.
- Agendar citas.

...hasta haber identificado un nombre válido del cliente.

Esto previene cotizaciones "a nadie" y asegura formalidad del lead.

---

### 6.2 Anti-alucinación estricta

El bot tiene prohibido inventar vehículos, precios, disponibilidad o datos no presentes.

Si el modelo solicitado no aparece en el inventario, el bot debe:
- Informar que no se maneja ese modelo.
- Sugerir alternativas reales disponibles.

---

### 6.3 Interpretación de vehículos de carga (aclaración obligatoria)

Si el cliente pregunta "¿cuántos caben?" y el vehículo detectado es panel/chasis/carga:
- El bot debe aclarar que la unidad es para carga.
- Indicar que los asientos disponibles son los de cabina (no "pasajeros tipo van de turismo").

---

### 6.4 Control de memoria y deduplicación (robustez)

Estructuras de control (ejemplo conceptual):
- BoundedOrderedSet para rastrear:
  - IDs de mensajes procesados (límite 4000).
  - IDs de leads procesados (límite 8000).

Previene:
- Fugas de memoria.
- Bucles de webhook.
- Reprocesamiento de eventos.
- Duplicación de acciones (ej. crear lead 2 veces).

────────────────────────────────────────────────────────────
## 7) ALERTAS AL RESPONSABLE (WHATSAPP)
────────────────────────────────────────────────────────────

### 7.1 Alerta de Lead Calificado (cita cerrada)

**Cuándo:**
Cuando el bot confirma cita (ya tiene nombre + modelo + fecha/hora).

**Contenido típico:**
> **NUEVO LEAD EN MONDAY**
> Enlace wa.me del cliente
> Indicación de que se cerró una cita
> Origen del lead (si aplica)

---

### 7.2 Alerta de Interés Detectado (lead caliente sin cita)

**Cuándo:**
El cliente pregunta precio, muestra intención fuerte o pide ubicación, pero todavía no agenda.

**Contenido típico:**
> **Interés Detectado**
> Enlace wa.me del cliente
> Texto exacto del cliente
> Respuesta del bot
> Origen (si aplica)

**Objetivo:**
Permite intervención proactiva de ventas para acelerar cierre.

────────────────────────────────────────────────────────────
## 8) FUENTES DEL SISTEMA (ARCHIVOS Y RESPONSABILIDADES)
────────────────────────────────────────────────────────────

**conversation_logic.py**
- Prompt principal.
- Reglas de IA.
- Extracción de variables (nombre, cita, pago).
- Manejo de PDFs y carrusel de imágenes.

**main.py**
- Gestión de webhooks.
- Debouncing.
- Procesamiento de audio con Whisper.
- Análisis de imágenes.
- Tracking de referral.
- Detección de handoff humano.

**monday_service.py**
- Mutaciones GraphQL hacia Monday.com.
- Mapeo de dropdowns de vehículos.
- Formateo de fechas ISO.
- Control de jerarquía del embudo (solo avance).

**inventory_service.py**
- Parser del catálogo de vehículos.
- Filtrado de unidades agotadas.
- Control de caché y refresco del inventario.

────────────────────────────────────────────────────────────
## 9) RESUMEN EJECUTIVO DEL FLUJO COMPLETO
────────────────────────────────────────────────────────────

1. Cliente escribe por WhatsApp (primer mensaje).
2. Se crea lead en Monday, estado "1er Contacto", se captura teléfono y origen.
3. Cliente menciona modelo: se detecta interés, estado "Intención", se normaliza el vehículo.
4. Cliente pide PDF: se envía ficha/corrida, estado "Cotización".
5. Cliente confirma día/hora: se valida nombre y se agenda, estado "Cita Programada", se notifica al asesor.
6. Si el cliente rechaza: "Sin Interés" inmediato (terminal).
7. Equipo mueve estados finales: cita atendida/no atendida, financiamiento, venta cerrada o caída.
8. Si el cliente regresa después de un estado terminal: se crea un nuevo ciclo con un nuevo lead.

────────────────────────────────────────────────────────────
