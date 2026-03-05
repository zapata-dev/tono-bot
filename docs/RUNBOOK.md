# Runbook - Tono-Bot Operations

## Despliegue

### Plataforma
- **Render** PaaS
- **Puerto**: 8080
- **Dockerfile**: Raíz del repo (`/Dockerfile`)
- **Base de datos**: SQLite persistido en `/app/tono-bot/db/`

### Variables de Entorno
Todas las variables se configuran en el dashboard de Render. Ver `.env.example` para la lista completa.

### Health Check
```bash
curl https://tu-app.onrender.com/health
```

Respuesta incluye métricas del bot: uptime, sesiones activas, estado del LLM.

## Comandos de Desarrollo Local

### Instalar dependencias
```bash
cd tono-bot
pip install -r requirements.txt
```

### Ejecutar localmente
```bash
cd tono-bot
uvicorn src.main:app --host 0.0.0.0 --port 8080 --reload
```

### Docker
```bash
docker build -t tono-bot .
docker run -p 8080:8080 --env-file .env tono-bot
```

## Monitoreo

### Logs Clave

| Patrón de Log | Significado |
|---------------|-------------|
| `🚀 Startup` | Aplicación iniciando |
| `🔄 Smoke test` | Verificando conectividad Gemini |
| `⚠️ Gemini unreachable` | Fallback a OpenAI activado |
| `📩 Webhook received` | Mensaje entrante |
| `🤖 LLM response` | Respuesta generada |
| `👤 Human detected` | Handoff humano detectado |
| `🔇 Silenced` | Bot silenciado para un JID |
| `📋 Monday.com` | Operación CRM |
| `❌ Error` | Error que requiere atención |

### Métricas en /health
- Estado de la conexión HTTP
- Proveedor LLM activo (Gemini/OpenAI)
- Conteo de sesiones activas
- Conteo de JIDs silenciados

## Troubleshooting

### Bot no responde

1. Verificar `/health` endpoint
2. Revisar logs para errores de conexión
3. Verificar que `EVOLUTION_API_URL` y `EVOLUTION_API_KEY` sean correctos
4. Verificar que la instancia de WhatsApp esté conectada en Evolution API

### Gemini no funciona (fallback a OpenAI)

**Síntoma**: Log muestra "Gemini unreachable, switching to OpenAI"

**Causas comunes**:
- IPv6 en Render (se fuerza IPv4 pero puede fallar)
- `GEMINI_API_KEY` inválido o expirado
- Servicio de Gemini temporalmente caído

**Acción**: El bot funciona normalmente con OpenAI. Se auto-recupera en el siguiente restart.

### Mensajes duplicados

**Síntoma**: Bot responde dos veces al mismo mensaje

**Causas comunes**:
- Evolution API reenvía webhook (timeout en ACK)
- BoundedOrderedSet lleno (evicción FIFO)

**Acción**:
- Verificar que el webhook retorna 200 rápido (< 1s)
- Revisar tamaño de `processed_message_ids` en logs

### Bot responde a mensajes humanos

**Síntoma**: Bot interfiere cuando un asesor está atendiendo

**Causas comunes**:
- `TEAM_NUMBERS` no configurado
- Mensaje humano no detectado por heurísticas
- Timer de silencio expiró (60 min default)

**Acción**:
- Configurar `TEAM_NUMBERS` con los números de los asesores
- Ajustar `AUTO_REACTIVATE_MINUTES` si 60 min no es suficiente

### Google Sheets 403

**Síntoma**: Inventario no se actualiza

**Causa**: La URL de Google Sheets no es pública

**Acción**:
1. Ir a la hoja en Google Sheets
2. Archivo → Compartir → "Cualquier persona con el enlace"
3. Usar URL de exportación CSV: `https://docs.google.com/spreadsheets/d/{ID}/export?format=csv`

### Monday.com errores

**Error 401**: API key inválido → Regenerar en Monday.com

**Error 404**: Board ID incorrecto → Verificar `MONDAY_BOARD_ID`

**Dropdown no actualiza**: El label no coincide exactamente con los valores configurados en el tablero

**Lead duplicado**: Verificar que `MONDAY_DEDUPE_COLUMN_ID` apunta a la columna correcta

### Mensajes de audio no se transcriben

1. Verificar `OPENAI_API_KEY` (Whisper usa OpenAI)
2. Verificar que el archivo de audio es accesible desde Evolution API
3. Revisar logs para errores de transcripción

### Imágenes no se analizan

1. Verificar `GEMINI_API_KEY` o `OPENAI_API_KEY`
2. Verificar que la imagen se descarga correctamente de Evolution API
3. Revisar logs para errores de Vision API

## Operaciones de Emergencia

### Reiniciar el bot
En Render: Manual Deploy → Deploy latest commit

### Silenciar bot para un número específico
No hay endpoint dedicado. El bot se silencia automáticamente cuando detecta intervención humana. Se reactiva después de `AUTO_REACTIVATE_MINUTES`.

### Limpiar base de datos SQLite
```bash
# En el contenedor
rm /app/tono-bot/db/memory.db
# El bot recrea la DB automáticamente al reiniciar
```
**PRECAUCIÓN**: Esto borra todo el historial de conversaciones.

## Arquitectura de Red

```
Cliente WhatsApp → Evolution API → Webhook POST /webhook → Tono-Bot (Render)
                                                              ↓
                                                    Gemini API (primary)
                                                    OpenAI API (fallback)
                                                    Monday.com GraphQL API
                                                              ↓
                                                    Evolution API ← Respuesta WhatsApp
```
