# AI Architecture - Tono-Bot

## Resumen

Tono-Bot usa una arquitectura Dual LLM con Gemini como proveedor primario y OpenAI como fallback. Ambos se manejan a través del OpenAI SDK (`openai==1.59.7`), usando diferentes `base_url` para cada proveedor.

## Proveedores

### Gemini (Primario)
- **Modelo**: `gemini-2.5-flash-lite` (configurable con `OPENAI_MODEL`)
- **SDK**: OpenAI SDK con `base_url=https://generativelanguage.googleapis.com/v1beta/openai/`
- **API Key**: `GEMINI_API_KEY`
- **Transporte**: IPv4 forzado (`local_address="0.0.0.0"`) para evitar fallos de IPv6 en Render

### OpenAI (Fallback)
- **Modelo**: `gpt-4o-mini` (configurable con `OPENAI_FALLBACK_MODEL`)
- **SDK**: OpenAI SDK nativo
- **API Key**: `OPENAI_API_KEY`
- También usado para: Whisper (audio), Vision (imágenes)

## Smoke Test al Inicio

Al arrancar la aplicación (`lifespan()`):
1. **DNS check**: Resuelve `generativelanguage.googleapis.com`
2. **TCP check**: Conecta al puerto 443
3. **HTTPS check**: GET request al endpoint
4. **API call**: Llamada real al modelo Gemini

Si cualquier paso falla → OpenAI se convierte en primario automáticamente via `set_llm_primary("openai")`.

## Fallback Por Request

`_llm_call_with_fallback()` en `conversation_logic.py`:

1. Intenta con proveedor primario (Gemini)
2. Si falla → 2 reintentos con backoff (1s, 2s)
3. Si sigue fallando → cambia al proveedor secundario (OpenAI)
4. 2 reintentos con backoff para el secundario
5. Si todo falla → mensaje de error genérico al usuario

## Concurrencia y Rate Limiting

- `AsyncOpenAI` para llamadas no bloqueantes
- `max_retries=0` en el SDK (retries manejados manualmente)
- Timeout: 30s general, 10s para conexión
- Manejo explícito de 429 (Rate Limit) con backoff

## System Prompt

El bot tiene personalidad de "Adrian Jimenez":
- Máximo 2 oraciones por respuesta
- Sin emojis
- Tono profesional pero natural
- Respuestas en español
- Filosofía "DESTRABAR": resolver dudas, no vender agresivamente

## Optimización de Tokens

### Inyección Dinámica de Inventario
- **Turno 1-2**: Inventario completo en el contexto
- **Turno 3+**: Solo inventario enfocado (modelo detectado por el cliente)
- Ahorro: ~2000 tokens por llamada

### Inyección Condicional de Financiamiento
- Los datos de `financing.json` solo se inyectan cuando el mensaje contiene keywords de financiamiento
- Keywords: "financiamiento", "crédito", "mensualidades", "enganche", "corrida", etc.

### Truncado de Historial
- Conversación truncada a ~4000 caracteres para el contexto
- Se mantienen los turnos más recientes

## Message Accumulation (Debouncing)

Cuando un cliente envía varios mensajes rápidos seguidos:

1. Primer mensaje inicia un timer de 8 segundos
2. Mensajes adicionales se acumulan en `pending_messages[jid]`
3. Al expirar el timer, todos los mensajes se combinan: `"msg1 | msg2 | msg3"`
4. Una sola llamada al LLM con el mensaje combinado
5. **Drain loop**: Si llegan mensajes durante el procesamiento, se re-acumulan

### Protección de Concurrencia
- `asyncio.Lock()` por JID para evitar race conditions
- `pending_message_tasks[jid]` controla el timer activo

## Procesamiento de Audio

1. Webhook recibe mensaje de audio
2. Se descarga el archivo de audio desde Evolution API
3. Se transcribe con OpenAI Whisper API
4. El texto transcrito se procesa como mensaje de texto normal

## Procesamiento de Imágenes (Vision)

1. Webhook recibe mensaje con imagen
2. `_handle_image_analysis()` descarga la imagen (base64)
3. Se envía a Gemini Vision o OpenAI Vision para análisis
4. Se genera descripción breve enfocada en vehículos/documentos
5. Descripción inyectada al contexto: `"[El cliente envió una foto que muestra: ...]"`
6. El bot responde contextualmente

## Diagrama de Flujo Simplificado

```
Webhook → Dedup → Accumulate → [Timer 8s] → Combine Messages
                                                    ↓
                                              LLM Call (Gemini)
                                                    ↓ (fallo)
                                              Retry x2 (backoff)
                                                    ↓ (fallo)
                                              LLM Call (OpenAI)
                                                    ↓ (fallo)
                                              Retry x2 (backoff)
                                                    ↓ (fallo)
                                              Error Message
```
