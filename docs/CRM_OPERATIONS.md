# CRM Operations - Monday.com Integration

## Resumen

Tono-Bot se integra con Monday.com vía GraphQL API para gestionar leads automáticamente. El servicio CRM (`monday_service.py`, ~800 líneas) maneja creación de leads, actualización de estados, deduplicación y progresión del embudo.

## Configuración

### Variables de Entorno Requeridas

```bash
MONDAY_API_KEY=                    # API key de Monday.com
MONDAY_BOARD_ID=18396811838        # ID del tablero "Leads Tractos y Max"
```

### Columnas del Tablero

| Variable | Column ID | Tipo | Descripción |
|----------|-----------|------|-------------|
| MONDAY_DEDUPE_COLUMN_ID | text_mkzw7xjz | Text | Deduplicación por teléfono |
| MONDAY_LAST_MSG_ID_COLUMN_ID | text_mkzwndf | Text | Tracking de último mensaje |
| MONDAY_PHONE_COLUMN_ID | phone_mkzwh34a | Phone | Teléfono del cliente |
| MONDAY_STAGE_COLUMN_ID | status | Status | Estado del embudo |
| MONDAY_VEHICLE_COLUMN_ID | dropdown_mm0gq48r | Dropdown | Vehículo de interés |
| MONDAY_PAYMENT_COLUMN_ID | color_mm0gbjea | Status | Esquema de pago |
| MONDAY_APPOINTMENT_COLUMN_ID | date_mm0grgky | Date | Fecha de cita |
| MONDAY_APPOINTMENT_TIME_COLUMN_ID | hour_mm0hfk47 | Hour | Hora de cita |
| MONDAY_CMV_COLUMN_ID | boolean_mm0g2zf3 | Checkbox | Confirmación CMV (manual) |
| MONDAY_SOURCE_COLUMN_ID | color_mm0wb2gm | Status | Origen del lead |
| MONDAY_CHANNEL_COLUMN_ID | color_mm0wf5zn | Status | Canal (Facebook/Instagram/Directo) |
| MONDAY_SOURCE_TYPE_COLUMN_ID | color_mm0w1mtn | Status | Tipo origen (Ad/Post/Directo) |
| MONDAY_AD_ID_COLUMN_ID | text_mm0wcdmz | Text | Ad ID de Meta |
| MONDAY_CTWA_CLID_COLUMN_ID | text_mm0wwwg1 | Text | CTWA Click ID |
| MONDAY_CAMPAIGN_NAME_COLUMN_ID | text_mm0w77pn | Text | Nombre de campaña (futuro) |
| MONDAY_ADSET_NAME_COLUMN_ID | text_mm0wtebg | Text | Nombre de Ad Set (futuro) |
| MONDAY_AD_NAME_COLUMN_ID | text_mm0wtpwb | Text | Nombre de Ad (futuro) |

## Embudo Comercial (Funnel V2)

### Jerarquía de Estados (10 etapas)

| # | Estado | Disparador | Quién |
|---|--------|-----------|-------|
| 1 | 1er Contacto | Primer mensaje del cliente | Bot |
| 2 | Intención | Mención de modelo específico | Bot |
| 3 | Cotización | PDF enviado (ficha/corrida) | Bot |
| 4 | Cita Programada | Fecha de cita confirmada | Bot |
| 5 | Sin Interés | Cliente expresa desinterés | Bot |
| 6 | Cita Atendida | Cliente asistió | Humano |
| 7 | Cita No Atendida | Cliente no asistió | Humano |
| 8 | Venta Cerrada | Venta completada | Humano |
| 9 | Financiamiento en Gestión | Crédito en proceso | Humano |
| 10 | Venta Caída | Venta no se concretó | Humano |

### Reglas de Progresión

1. **Solo avance**: Un lead NUNCA retrocede de estado. Controlado por `STAGE_HIERARCHY`.
2. **Override global**: "Sin Interés" puede activarse desde cualquier estado (excepción a la regla de avance).
3. **Estados terminales**: `Venta Cerrada`, `Venta Caída`, `Sin Interés`.
4. **Reinicio de ciclo**: Si un cliente en estado terminal vuelve a escribir, se crea un nuevo lead.

## Operaciones Principales

### Creación de Lead

- Se dispara en el primer mensaje del cliente
- Se busca primero si ya existe por teléfono (deduplicación)
- Si existe y NO está en estado terminal → se actualiza
- Si existe en estado terminal → se crea nuevo lead
- Se asigna al grupo mensual dinámico (ej. "MARZO 2026")

### Actualización de Vehículo de Interés

- Se detecta el modelo mencionado por el cliente
- Se normaliza usando `VEHICLE_DROPDOWN_MAP`
- Valores válidos: `Tunland E5`, `ESTA 6x4 11.8`, `ESTA 6x4 X13`, `Miler`, `Toano Panel`, `Tunland G7`, `Tunland G9`, `Cascadia`

### Actualización de Esquema de Pago

- Se detecta intención de pago del cliente
- Valores: `De Contado`, `Financiamiento`, `Por definir`
- Se resuelve con `resolve_payment_to_label()`

### Programación de Cita

- Se parsea fecha/hora del lenguaje natural del cliente
- Fecha se convierte a ISO con `resolve_appointment_to_iso()`
- Se actualiza "Agenda Citas (Día)" y "Hora Cita"
- Se envía alerta al OWNER_PHONE

### Notas del Lead

Cada transición de estado genera una nota en Monday.com con detalles:
- "Lead creado desde WhatsApp"
- "Interés detectado: [Modelo]"
- "Cotización enviada: [Modelo]"
- "Cita programada: [Fecha] [Hora]"
- "Lead expresó desinterés"

## Atribución de Origen (CTWA)

### Flujo de Referral Tracking

1. Primer mensaje llega con datos de referral (Facebook/Instagram)
2. Se extraen datos de `contextInfo` (Baileys) o `referral` (Cloud API)
3. Se almacenan temporalmente en `GlobalState.pending_referrals`
4. Al crear el lead, se escriben en columnas de Monday.com

### Labels Automáticos

| Origen Lead | Canal | Tipo Origen |
|-------------|-------|-------------|
| Facebook Ad | Facebook | Ad |
| Facebook Post | Facebook | Post |
| Instagram Ad | Instagram | Ad |
| Instagram Post | Instagram | Post |
| Facebook | Facebook | Directo |
| Instagram | Instagram | Directo |
| Directo | Directo | Directo |

## Sistema de Tracking ID (V3)

### Formato
`<MODELO>-<TIPO_CAMPAÑA><NUMERO>` — Embebido en mensajes pre-llenados de anuncios de WhatsApp.

### Códigos de Modelo
`TG7` (Tunland G7), `TG9` (Tunland G9), `TE5` (Tunland E5), `ML` (Miler), `TP` (Toano Panel), `E11` (ESTA 6x4 11.8), `EX` (ESTA 6x4 X13), `CA` (Cascadia)

### Tipos de Campaña

| Código | Tipo | Descripción |
|--------|------|-------------|
| `A` | Anuncio | Anuncio regular de Facebook/Instagram |
| `SU` | Mejor Precio | Mejor Propuesta / Precio especial |
| `LQ` | Liquidación | Liquidación / Precio especial |
| `PR` | Promoción | Promoción especial |
| `EV` | Evento | Evento / Open House |

Ejemplos: `TG9-A1`, `CA-SU1`, `ML-LQ2`, `TP-PR1`, `E11-EV1`

### Flujo de Tracking ID

1. Anuncio en Meta tiene mensaje pre-llenado: "Hola CA-SU1"
2. Bot detecta patrón `[A-Z][A-Z0-9]{1,3}-(A|SU|LQ|PR|EV)\d{1,3}` en primer mensaje
3. Modelo auto-resuelto → `last_interest` = etiqueta del vehículo
4. Tipo de campaña resuelto → contexto incluye tipo (ej: "Mejor Precio de Cascadia")
5. Tracking ID eliminado del mensaje antes de enviar a GPT
6. Lead creado en Monday.com con columna Tracking ID populada
7. Lead vinculado a ítem en tablero Anuncios via Connect Boards
8. Alerta al owner incluye Tracking ID

### Columnas en Monday.com

| Variable | Descripción |
|----------|-------------|
| MONDAY_TRACKING_ID_COLUMN_ID | Columna Text para Tracking ID en Leads |
| MONDAY_ADS_BOARD_ID | ID del tablero Anuncios |
| MONDAY_ADS_TRACKING_COLUMN_ID | Columna Text de Tracking ID en Anuncios |
| MONDAY_LEADS_CONNECT_ADS_COLUMN_ID | Columna Connect Boards (Leads → Anuncios) |

### Coexistencia con CTWA
- Tracking ID funciona con **Baileys** (no necesita Meta API)
- CTWA requiere Meta Cloud API
- Ambos sistemas coexisten: si un lead tiene CTWA Y Tracking ID, ambos se guardan
- Si solo hay Tracking ID, `referral_source` = `"Ad Tracking: CA-SU1"`

## Retry Logic

- Todas las mutaciones GraphQL tienen retry con backoff exponencial
- 2-3 intentos antes de fallar
- Manejo de rate limits (429)

## Troubleshooting

| Problema | Solución |
|----------|----------|
| Lead duplicado | Verificar normalización de teléfono y DEDUPE_COLUMN_ID |
| Estado no avanza | Verificar STAGE_HIERARCHY - solo avance permitido |
| Dropdown no se actualiza | Verificar que el label coincida exactamente con VEHICLE_DROPDOWN_MAP |
| Error 401 | Verificar MONDAY_API_KEY |
| Grupo mensual no se crea | Verificar permisos del API key en el tablero |
