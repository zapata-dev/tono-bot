# Lógica de Negocio y Reglas de Automatización
# Asistente Virtual — Tractos y Max

Documento operativo que describe cómo funciona el asistente virtual de WhatsApp y cómo se refleja toda su actividad en el tablero de Monday.com. Dirigido a equipos de ventas, gerencia y dirección.

---

## 1. Introducción

El asistente virtual ("Adrian Jimenez") atiende clientes por WhatsApp las 24 horas. Su objetivo principal es **destrabar**: resolver dudas, compartir información y eliminar barreras para que el cliente visite la agencia.

**No es un bot de ventas agresivo.** Responde lo que el cliente pregunta, ofrece fichas técnicas, simulaciones de financiamiento y fotos del inventario. Cuando el cliente está listo, agenda la cita y registra todo automáticamente en Monday.com.

Todo lo que el bot hace durante la conversación queda reflejado en el tablero de Monday, sin intervención manual del equipo.

---

## 2. El Tablero en Monday.com — Vista General

**Tablero:** "Leads Tractos y Max"

### Organización por mes
Los leads se agrupan automáticamente por el mes en que llegaron. Cada mes se crea un grupo nuevo (ej. "MARZO 2026", "ABRIL 2026"). Esto permite filtrar y analizar el pipeline por periodo.

### Columnas del tablero

| Columna | Qué contiene | Quién la llena | Cuándo se llena |
|---------|-------------|----------------|-----------------|
| **Nombre del ítem** | Nombre del cliente + teléfono (ej. "Juan Pérez \| 5551234567") | Bot | Al detectar el nombre en la conversación |
| **Estado** (Embudo) | Etapa actual del lead en el embudo de ventas | Bot (etapas 1-5) / Equipo (etapas 6-10) | Se actualiza conforme avanza la conversación |
| **Teléfono** | Número de WhatsApp del cliente | Bot | Al primer contacto |
| **Vehículo de Interés** | Modelo que el cliente mencionó (ej. "Tunland G9") | Bot | Cuando el cliente pregunta por un modelo específico |
| **Esquema de Pago** | "De Contado", "Financiamiento" o "Por definir" | Bot | Cuando el cliente menciona cómo quiere pagar |
| **Agenda Citas (Día)** | Fecha de la cita programada | Bot | Cuando se confirma la cita |
| **Hora Cita** | Hora de la cita programada | Bot | Cuando se confirma la cita con horario |
| **Confirmación CMV** | Checkbox de confirmación interna | Equipo (manual) | Cuando el equipo lo valida internamente |
| **Origen Lead** | De dónde vino el cliente (ej. "Facebook Ad", "Instagram Post", "Directo") | Bot | Al primer mensaje, si viene de un anuncio |
| **Canal** | Red social de origen ("Facebook", "Instagram" o "Directo") | Bot | Al primer mensaje |
| **Tipo Origen** | Tipo de publicación ("Ad", "Post" o "Directo") | Bot | Al primer mensaje |
| **Ad ID** | Identificador del anuncio de Meta | Bot | Al primer mensaje (solo disponible con Cloud API) |
| **CTWA Click ID** | Identificador del clic en el anuncio | Bot | Al primer mensaje |
| **Campaign Name** | Nombre de la campaña en Meta Ads | Pendiente (enriquecimiento futuro) | — |
| **Ad Set Name** | Nombre del conjunto de anuncios | Pendiente (enriquecimiento futuro) | — |
| **Ad Name** | Nombre del anuncio específico | Pendiente (enriquecimiento futuro) | — |

### Notas del ítem
Cada vez que ocurre un evento importante, el bot agrega una nota al ítem en Monday con los detalles. Esto permite ver el historial completo de interacciones sin salir del tablero.

---

## 3. Embudo de Ventas — Cómo Avanza un Lead

El embudo tiene 10 etapas. Las primeras 5 las mueve el bot automáticamente. Las últimas 5 las mueve el equipo de ventas.

| # | Etapa | Quién la mueve | Qué significa |
|---|-------|---------------|---------------|
| 1 | **1er Contacto** | Bot | El cliente envió su primer mensaje. Se registró en Monday. |
| 2 | **Intención** | Bot | El cliente preguntó por un modelo específico (ej. "¿Cuánto cuesta la Tunland G9?"). |
| 3 | **Cotización** | Bot | El bot envió una ficha técnica o simulación de financiamiento en PDF. |
| 4 | **Cita Programada** | Bot | El cliente confirmó día, hora y nombre para visitar la agencia. |
| 5 | **Sin Interés** | Bot | El cliente expresó que no le interesa (ej. "No gracias", "Ya no quiero", "STOP"). |
| 6 | **Cita Atendida** | Equipo | El cliente llegó a la cita. |
| 7 | **Cita No Atendida** | Equipo | El cliente no se presentó. |
| 8 | **Venta Cerrada** | Equipo | Se concretó la venta. |
| 9 | **Financiamiento en Gestión** | Equipo | El trámite de crédito está en proceso. |
| 10 | **Venta Caída** | Equipo | La venta no se concretó. |

### Reglas del embudo

**Solo avanza, nunca retrocede.**
Un lead que ya está en "Cotización" no puede regresar a "Intención". Esto mantiene limpio el pipeline y refleja siempre el punto más avanzado de la relación con el cliente.

**Excepción: "Sin Interés" puede activarse en cualquier momento.**
Si el cliente dice explícitamente que no le interesa, se marca como "Sin Interés" sin importar en qué etapa estuviera.

**Estados terminales y nuevos ciclos.**
Cuando un lead llega a "Venta Cerrada", "Venta Caída" o "Sin Interés", se considera cerrado. Si ese mismo cliente vuelve a escribir después, el bot crea un **nuevo registro** en Monday (un nuevo ítem) para iniciar un ciclo de venta fresco, sin modificar el registro anterior.

### Ejemplo: Recorrido completo de un lead

> **Lunes 10:00 AM** — El cliente escribe "Hola, buenas tardes" por WhatsApp.
> - En Monday aparece un nuevo ítem: "Cliente Nuevo | 5551234567"
> - Estado: **1er Contacto**
> - Nota: "Primer contacto"
> - Si vino de un anuncio de Facebook, las columnas Origen, Canal y Tipo se llenan automáticamente.
>
> **Lunes 10:02 AM** — El cliente pregunta: "¿Cuánto cuesta la Tunland G9?"
> - Estado avanza a: **Intención**
> - Columna "Vehículo de Interés" se llena con: **Tunland G9**
> - Nota: "Interesado en: Tunland G9"
>
> **Lunes 10:05 AM** — El cliente pide: "¿Me mandas la ficha técnica?"
> - El bot envía el PDF por WhatsApp.
> - Estado avanza a: **Cotización**
> - Nota: "Cotización enviada: Tunland G9"
>
> **Lunes 10:08 AM** — El cliente dice: "Me gustaría ir el miércoles a las 10"
> - El bot pregunta: "Perfecto, ¿a nombre de quién agendo la cita?"
> - El cliente responde: "Juan Pérez"
> - Estado avanza a: **Cita Programada**
> - Nombre del ítem cambia a: "Juan Pérez | 5551234567"
> - Columna "Agenda Citas" se llena con: **miércoles (fecha ISO)**
> - Columna "Hora Cita" se llena con: **10:00**
> - Nota: "Cita programada: Miércoles 10:00"
> - El responsable recibe una alerta por WhatsApp con los datos del lead.

---

## 4. Acciones Automáticas del Bot (Reflejadas en Monday)

### 4.1 Registro inicial del lead

**Qué lo dispara:** El cliente envía su primer mensaje por WhatsApp.

**Qué aparece en Monday:**
- Se crea un nuevo ítem en el grupo del mes actual.
- Estado: "1er Contacto".
- El teléfono se registra automáticamente.
- Si el cliente llegó desde un anuncio de Facebook o Instagram, se llenan las columnas de atribución (Origen, Canal, Tipo, Ad ID, Click ID).

**Protección contra duplicados:** Si el mismo teléfono ya existe en el tablero, el bot actualiza el registro existente en lugar de crear uno nuevo. Solo crea uno nuevo si el registro anterior está en un estado terminal (Venta Cerrada, Venta Caída o Sin Interés).

---

### 4.2 Detección de vehículo de interés

**Qué lo dispara:** El cliente menciona un modelo del inventario durante la conversación.

**Qué aparece en Monday:**
- Estado avanza a "Intención".
- La columna "Vehículo de Interés" se llena con el modelo detectado.
- Nota agregada con el vehículo identificado.

**El bot reconoce variaciones y errores comunes:**

| El cliente escribe... | El bot entiende... |
|----------------------|-------------------|
| "la G9" | Tunland G9 |
| "la pickup" / "la troca" | Tunland (cualquier variante) |
| "la van" / "la panel" | Toano Panel |
| "el camioncito" | Miler |
| "el tracto" | ESTA 6x4 |
| "miller" (con doble L) | Miler |
| "tunlan" / "tunlad" | Tunland |

**Valores posibles en el dropdown de Monday:**
Tunland E5, ESTA 6x4 11.8, ESTA 6x4 X13, Miler, Toano Panel, Tunland G7, Tunland G9, Cascadia.

---

### 4.3 Envío de cotización o ficha técnica (PDF)

**Qué lo dispara:** El cliente solicita un documento, usando frases como:
- "Mándame la ficha técnica"
- "¿Me pasas la corrida financiera?"
- "Quiero ver las especificaciones"
- "Envíame la simulación de pagos"

**Qué aparece en Monday:**
- Estado avanza a "Cotización".
- Nota: "Cotización enviada: [Modelo]".

**Documentos disponibles:**
- **Ficha técnica:** Especificaciones del vehículo en PDF.
- **Corrida financiera:** Simulación de enganche, mensualidades y tasas en PDF.

---

### 4.4 Cita programada

**Qué lo dispara:** El cliente confirma una cita indicando día y hora, y el bot ya tiene su nombre y modelo de interés.

**Datos que el bot necesita antes de confirmar la cita:**
1. Nombre del cliente (lo pide si no lo tiene).
2. Modelo de interés (detectado de la conversación).
3. Día y hora de la visita.

**Qué aparece en Monday:**
- Estado avanza a "Cita Programada".
- Columna "Agenda Citas (Día)" con la fecha.
- Columna "Hora Cita" con el horario.
- Nombre del ítem actualizado con el nombre real del cliente.
- Nota con los detalles de la cita.

**El bot interpreta horarios naturales:**

| El cliente dice... | Monday registra... |
|-------------------|-------------------|
| "Mañana a las 10" | Fecha de mañana, 10:00 |
| "El viernes por la tarde" | Próximo viernes, 15:00 |
| "Miércoles a medio día" | Próximo miércoles, 12:00 |
| "Lunes a las 10 y media" | Próximo lunes, 10:30 |

**Nota importante:** El bot sabe que los domingos la agencia está cerrada. Si el cliente propone domingo, sugiere lunes o sábado como alternativa.

---

### 4.5 Detección de método de pago

**Qué lo dispara:** El cliente menciona cómo piensa pagar durante la conversación.

**Qué aparece en Monday:**
- La columna "Esquema de Pago" se actualiza con uno de estos valores:

| El cliente dice... | Monday registra... |
|-------------------|-------------------|
| "De contado" / "Cash" / "No quiero crédito" | **De Contado** |
| "A crédito" / "Financiamiento" / "Mensualidades" | **Financiamiento** |
| (No ha mencionado forma de pago) | **Por definir** |

---

### 4.6 Detección de desinterés

**Qué lo dispara:** El cliente expresa explícitamente que no le interesa, usando frases como:
- "No me interesa"
- "Ya no quiero"
- "No gracias"
- "Cancela"
- "Dejen de escribirme"
- "STOP" / "BAJA"

**Qué aparece en Monday:**
- Estado cambia a "Sin Interés" (sin importar en qué etapa estuviera).
- Nota: "Lead expresó desinterés".
- Si el mismo cliente vuelve a escribir en el futuro, se crea un **nuevo registro**.

---

### 4.7 Atribución de origen (Facebook / Instagram)

**Qué lo dispara:** Cuando el cliente llega al WhatsApp desde un anuncio o publicación de Facebook/Instagram (click-to-WhatsApp).

**Qué aparece en Monday (automático, sin intervención):**

| Columna | Ejemplo |
|---------|---------|
| **Origen Lead** | "Facebook Ad", "Instagram Post", "Directo" |
| **Canal** | "Facebook", "Instagram", "Directo" |
| **Tipo Origen** | "Ad" (anuncio pagado), "Post" (publicación orgánica), "Directo" |
| **Ad ID** | Identificador del anuncio en Meta |
| **CTWA Click ID** | Identificador del clic del usuario |

**Valores posibles de Origen:**
Facebook Ad, Facebook Post, Instagram Ad, Instagram Post, Facebook, Instagram, Directo.

Esto permite medir qué campañas y anuncios generan más leads directamente desde Monday.

---

## 5. Acciones que Requieren Intervención Humana

### Etapas manuales del embudo
Las siguientes etapas **solo las puede mover el equipo de ventas** desde Monday:

| Etapa | Cuándo moverla |
|-------|---------------|
| **Cita Atendida** | El cliente llegó a la agencia. |
| **Cita No Atendida** | El cliente no se presentó en la fecha acordada. |
| **Venta Cerrada** | Se firmó y se entregó la unidad. |
| **Financiamiento en Gestión** | El crédito está en trámite. |
| **Venta Caída** | El cliente decidió no comprar después de visitar. |

### Confirmación CMV
La columna "Confirmación CMV" es un checkbox que **solo marca el equipo**. El bot no lo toca.

### Handoff a humano
Cuando un asesor real toma la conversación (responde desde el celular), el bot lo detecta automáticamente y **se silencia durante 60 minutos** para no interferir. Después de ese tiempo, el bot se reactiva por si el cliente escribe y nadie lo atiende.

---

## 6. Capacidades del Bot en la Conversación

Además de registrar datos en Monday, el bot ofrece las siguientes funciones al cliente:

| Capacidad | Descripción |
|-----------|-------------|
| **Fotos del inventario** | Envía hasta 3 fotos exteriores del modelo solicitado. El cliente puede pedir "otra foto" para ver más. |
| **Fichas técnicas en PDF** | Envía el documento de especificaciones del vehículo. |
| **Simulación de financiamiento en PDF** | Envía corrida con enganche (20%), mensualidad, tasa y CAT. Siempre aclara que es ilustrativa. |
| **Información de financiamiento** | Responde preguntas sobre enganche mínimo, plazos y mensualidades. Si la unidad no aplica para crédito, lo indica. |
| **Transcripción de audios** | Si el cliente envía una nota de voz, el bot la transcribe y responde como si fuera texto. |
| **Análisis de imágenes** | Si el cliente envía una foto de un vehículo, el bot la analiza e identifica si es un modelo del inventario. |
| **Horarios y ubicación** | Informa horarios de atención (L-V 9-6, Sáb 9-2) y ubicación de la agencia. |
| **Derivación a asesor** | Cuando el cliente necesita atención personalizada (financiamiento especial, negociación), el bot avisa al responsable. |

### Reglas de financiamiento
- El enganche mínimo siempre es **20% del valor factura**.
- El plazo base es **48 meses**.
- Los montos incluyen interés, IVA y seguro.
- Siempre se aclara que los números son **ilustrativos**.
- Si la unidad no aplica para financiamiento, el bot lo dice y sugiere unidades que sí aplican.
- Si el cliente pide condiciones especiales (más plazo, menos enganche, quitar seguro), el bot lo deriva a un asesor humano.

---

## 7. Alertas al Responsable

El bot envía alertas por WhatsApp al responsable designado cuando ocurren eventos importantes:

### Alerta de Lead Calificado
**Cuándo:** El bot cerró una cita (nombre + modelo + fecha confirmada).

**Contenido:**
> **NUEVO LEAD EN MONDAY**
> Cliente: wa.me/5551234567
> El bot cerró una cita. Revisa el tablero.
> Origen: Facebook Ad *(si aplica)*

### Alerta de Interés Detectado
**Cuándo:** El cliente muestra interés activo (pregunta precio, quiere comprar, pide ubicación) pero aún no agenda cita.

**Contenido:**
> **Interés Detectado**
> Cliente: wa.me/5551234567
> Dijo: "¿Cuál es el precio de la Tunland G9?"
> Bot: "La Tunland G9 está en $499,000 MXN IVA incluido..."
> Origen: Instagram Ad *(si aplica)*

Esto permite al equipo intervenir proactivamente con leads calientes sin esperar a que el bot cierre la cita.

---

## 8. Protección y Calidad de Datos

### No se duplican leads
El bot busca el teléfono del cliente antes de crear un registro nuevo. Si ya existe, actualiza el mismo ítem. Solo crea uno nuevo si el registro anterior ya está en un estado cerrado (Venta Cerrada, Venta Caída o Sin Interés).

### Mensajes agrupados
Si el cliente envía varios mensajes seguidos en menos de 8 segundos ("Hola" → "Me interesa" → "la Tunland G9"), el bot los agrupa y responde una sola vez. Esto evita respuestas fragmentadas y mantiene la conversación natural.

### Inventario siempre actualizado
El catálogo de vehículos se sincroniza automáticamente desde la hoja de cálculo de Google Sheets cada 5 minutos. Si se agrega o elimina una unidad en la hoja, el bot lo refleja en sus respuestas sin necesidad de reiniciarlo.

### El bot nunca inventa información
Si un modelo no está en el inventario, el bot no lo menciona. Si no tiene un dato, dice "Eso lo confirmo y te aviso" en lugar de inventar. Los precios, especificaciones y disponibilidad siempre vienen del inventario real.

---

## Resumen: Flujo Completo de Datos

```
Cliente envía mensaje por WhatsApp
         |
         v
  Bot responde y extrae información
         |
         v
  Monday.com se actualiza automáticamente
  ┌─────────────────────────────────────────────┐
  │  Nuevo ítem → "1er Contacto"                │
  │  Menciona modelo → "Intención" + Vehículo   │
  │  Pide PDF → "Cotización"                    │
  │  Confirma cita → "Cita Programada" + Fecha  │
  │  Dice no → "Sin Interés"                    │
  └─────────────────────────────────────────────┘
         |
         v
  Alerta al responsable por WhatsApp
         |
         v
  Equipo toma el control en Monday
  ┌─────────────────────────────────────────────┐
  │  Cita Atendida / No Atendida                │
  │  Venta Cerrada / Financiamiento en Gestión  │
  │  Venta Caída                                │
  └─────────────────────────────────────────────┘
```

---

*Documento generado como referencia operativa para el equipo de Tractos y Max.*
*Última actualización: Marzo 2026.*
