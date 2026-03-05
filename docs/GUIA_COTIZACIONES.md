# Guía de Cotizaciones y PDFs - Tono-Bot

## Resumen

El bot puede enviar documentos PDF (fichas técnicas y corridas financieras) a los clientes por WhatsApp. Los datos de financiamiento y URLs de los PDFs se almacenan en `tono-bot/data/financing.json`.

## Tipos de Documentos

### 1. Ficha Técnica (`pdf_ficha_tecnica`)
- Especificaciones técnicas del vehículo
- Dimensiones, motor, capacidades, equipamiento

### 2. Corrida Financiera (`pdf_corrida`)
- Simulación de financiamiento
- Incluye: enganche mínimo, plazo, tasa, pago mensual

## Modelos con PDFs Disponibles

| Modelo | Año | Ficha Técnica | Corrida Financiera |
|--------|-----|:-------------:|:------------------:|
| Foton Toano Panel | 2024 | Si | Si |
| Foton Toano Panel | 2025 | Si | Si |
| Foton Tunland G9 | 2025 | Si | Si |
| Foton Tunland E5 | 2024 | No | Si |
| Foton Auman EST-A 6x4 x13 | 2024 | Si | Si |
| Foton Miler 45T RS | 2024 | No | Si |

## Datos de Financiamiento por Modelo

| Modelo | Valor Factura | Enganche Mín | Plazo | Tasa Anual | Pago Mensual |
|--------|--------------|--------------|-------|------------|-------------|
| Toano Panel 2024 | $700,000 | $140,000 (20%) | 48 meses | 15.99% | $18,766.87 |
| Toano Panel 2025 | $720,000 | $144,000 (20%) | 48 meses | 15.99% | $19,291.50 |
| Tunland G9 2025 | $450,000 | $90,000 (20%) | 48 meses | 15.99% | $12,396.45 |
| Tunland E5 2024 | $300,000 | $60,000 (20%) | 48 meses | 15.99% | $8,419.32 |
| EST-A 6x4 X13 2024 | $1,845,000 | $369,000 (20%) | 48 meses | 15.99% | $39,503.67 |
| Miler 45T RS 2024 | $500,000 | $100,000 (20%) | 48 meses | 15.99% | $13,520.59 |

## Detección de Solicitud de PDF

El bot detecta solicitudes de documentos mediante palabras clave en el mensaje del cliente:

**Palabras clave de acción:** mándame, envíame, pásame, quiero, necesito, dame

**Palabras clave de documento:**
- Ficha técnica: "ficha", "especificaciones", "specs", "técnica"
- Corrida financiera: "corrida", "financiamiento", "simulación", "pagos", "mensualidades", "crédito"

**Ejemplos de frases que disparan envío:**
- "Mándame la ficha técnica de la G9"
- "¿Me pasas la corrida financiera?"
- "Quiero ver las especificaciones"
- "Envíame la simulación de pagos"

## Flujo de Envío

1. Cliente solicita documento (detectado por keywords)
2. Bot identifica el modelo del vehículo en la conversación
3. Bot busca el PDF correspondiente en `financing.json`
4. Bot envía mensaje introductorio
5. Bot envía el PDF por WhatsApp (vía Evolution API)
6. El lead avanza a estado **"Cotización"** en Monday.com
7. Se agrega nota: "Cotización enviada: [Modelo]"

## Inyección de Datos Financieros al Contexto

Los datos de financiamiento solo se inyectan al contexto del LLM cuando el mensaje del cliente contiene palabras clave de financiamiento. Esto ahorra tokens significativamente.

## Almacenamiento de PDFs

Los PDFs se almacenan en GitHub como archivos estáticos:
- Repositorio: `dgarduno-ZAPATA/foton-pdfs`
- Ruta fichas: `pdfs/fichas/`
- Ruta corridas: `pdfs/corridas/`

## Cómo Agregar un Nuevo PDF

1. Subir el PDF al repositorio de GitHub
2. Obtener la URL raw del archivo
3. Agregar la entrada correspondiente en `tono-bot/data/financing.json`
4. Incluir todos los campos: nombre, año, transmisión, valor_factura, enganche, plazo, tasa, pago mensual, URLs de PDFs
5. Hacer deploy

## Troubleshooting

| Problema | Solución |
|----------|----------|
| PDF no se envía | Verificar que la URL en financing.json sea accesible |
| Modelo no encontrado | Verificar que el key en financing.json coincida |
| Ficha técnica null | Ese modelo no tiene ficha técnica, solo corrida |
| Bot no detecta solicitud | Revisar que el mensaje contenga keywords correctos |
