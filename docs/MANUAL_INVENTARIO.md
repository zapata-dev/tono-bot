# Manual de Inventario - Tono-Bot

## Resumen

El sistema de inventario gestiona el catálogo de vehículos disponibles. Soporta dos fuentes de datos: un archivo CSV local (`data/inventory.csv`) y una hoja de Google Sheets publicada como CSV.

## Fuentes de Datos

### 1. Google Sheets (Producción)
- Se configura con la variable `SHEET_CSV_URL`
- Debe ser una URL de exportación CSV pública
- Formato: `https://docs.google.com/spreadsheets/d/{ID}/export?format=csv`
- Si la URL no está configurada o falla, se usa el CSV local como respaldo

### 2. CSV Local (Respaldo)
- Ubicación: `tono-bot/data/inventory.csv`
- Se usa automáticamente si Google Sheets no está disponible

## Formato del CSV

| Columna | Tipo | Descripción |
|---------|------|-------------|
| Marca | texto | Fabricante (ej. "Foton") |
| Modelo | texto | Nombre del modelo (ej. "TUNLAND G9") |
| Anio | entero | Año del modelo |
| Precio | número | Precio en MXN |
| Cantidad | entero | Unidades disponibles |
| Colores | texto | Colores separados por `;` |
| TipoCabina | texto | Tipo de cabina |
| Asientos | entero | Número de asientos |
| Traccion | texto | Tipo de tracción (4x2, 4x4, 6x4) |

## Modelos Actuales (8 modelos)

| Modelo | Año | Precio | Cantidad | Tracción |
|--------|-----|--------|----------|----------|
| MILER 45T RS | 2024 | $500,000 | 7 | 4x2 |
| TOANO PANEL | 2025 | $720,000 | 5 | 4x2 |
| TOANO PANEL | 2024 | $700,000 | 4 | 4x2 |
| TUNLAND G7 AT 4X4 | 2025 | $430,000 | 1 | 4x4 |
| TUNLAND G7 MT 4X4 | 2024 | $390,000 | 1 | 4x4 |
| TUNLAND G9 | 2025 | $450,000 | 8 | 4x4 |
| ESTA 6X4 11.8 | 2023 | $1,675,000 | 1 | 6x4 |
| ESTA 6X4 X13 | 2024 | $1,845,000 | 3 | 6x4 |

## Caché y Refresco

- **TTL**: 300 segundos (5 minutos), configurable con `INVENTORY_REFRESH_SECONDS`
- Al expirar el caché, se descarga de nuevo desde Google Sheets
- Si la descarga falla, se mantiene el último inventario válido en memoria

## Filtrado Automático

Se excluyen del inventario activo las unidades que:
- Tienen `Cantidad <= 0`
- Tienen status diferente de "disponible" (si la columna existe)

## Inyección al Contexto del LLM

- **Turno 1-2**: Se inyecta el inventario completo al contexto del LLM
- **Turno 3+**: Solo se inyecta el inventario enfocado (modelo detectado) para ahorrar tokens
- El formato semántico incluye marca, modelo, año, precio, cantidad, colores, tracción

## Cómo Actualizar el Inventario

### En Google Sheets (recomendado)
1. Editar directamente la hoja de Google Sheets
2. Los cambios se reflejan en máximo 5 minutos (siguiente refresco de caché)

### En CSV Local
1. Editar `tono-bot/data/inventory.csv`
2. Mantener el formato exacto de columnas
3. Hacer deploy para que tome efecto

## Troubleshooting

| Problema | Solución |
|----------|----------|
| Google Sheets 403 | Verificar que la URL sea de exportación CSV pública |
| Inventario vacío | Revisar que el CSV tenga datos y formato correcto |
| Modelo no aparece | Verificar que Cantidad > 0 en el CSV |
| Caché no refresca | Reiniciar el servicio o esperar al siguiente TTL |
