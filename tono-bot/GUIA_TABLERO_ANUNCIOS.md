# Guia para el Equipo de Marketing - Tablero de Anuncios

## Que es este tablero?

El tablero **"Seguimiento de Anuncios de Tractos YMax"** es donde registramos TODOS los anuncios que publicamos en Facebook e Instagram. Cada anuncio tiene un **codigo unico** que conecta automaticamente a los clientes que llegan por WhatsApp con el anuncio que los trajo.

Esto nos permite saber **exactamente que anuncio funciona y cual no**.

---

## Las columnas del tablero

| Columna | Que poner | Ejemplo |
|---------|-----------|---------|
| **Elemento** (Nombre) | Nombre descriptivo del anuncio | "Tunland G9 - Video Testimonio Marzo" |
| **Fecha de Publicacion** | Dia que se publica o se planeo publicar | 15 mar 2026 |
| **Tipo de Anuncio** | Categoria del contenido | Promocion / Evento / Lanzamiento / Noticia |
| **Descripcion** | Breve descripcion de que trata el anuncio | "Video de cliente satisfecho con su G9" |
| **Responsable** | Quien creo o maneja este anuncio | (Tu nombre) |
| **Estado** | En que fase esta el anuncio | No Iniciado / En Progreso / Completado / Bloqueado |
| **Codigo** | **EL MAS IMPORTANTE** - Codigo unico para el bot | `TG9-A1` |
| **Modelo** | Que vehiculo promueve este anuncio | Tunland G9 |

---

## Como funciona el Codigo (la columna mas importante)

El codigo es lo que conecta al cliente con el anuncio. Tiene este formato:

```
[MODELO]-A[NUMERO]
```

### Codigos de cada modelo

| Vehiculo | Codigo | Ejemplo 1er anuncio | Ejemplo 2do | Ejemplo 3ro |
|----------|--------|---------------------|-------------|-------------|
| Tunland G7 | `TG7` | `TG7-A1` | `TG7-A2` | `TG7-A3` |
| Tunland G9 | `TG9` | `TG9-A1` | `TG9-A2` | `TG9-A3` |
| Tunland E5 | `TE5` | `TE5-A1` | `TE5-A2` | `TE5-A3` |
| Miler | `ML` | `ML-A1` | `ML-A2` | `ML-A3` |
| Toano Panel | `TP` | `TP-A1` | `TP-A2` | `TP-A3` |
| ESTA 6x4 11.8 | `E11` | `E11-A1` | `E11-A2` | `E11-A3` |
| ESTA 6x4 X13 | `EX` | `EX-A1` | `EX-A2` | `EX-A3` |
| Cascadia | `CA` | `CA-A1` | `CA-A2` | `CA-A3` |

### Reglas del codigo

1. **El numero es secuencial por modelo**. Si ya tienes `TG9-A1` y `TG9-A2`, el siguiente es `TG9-A3`.
2. **Nunca repitas un codigo**. Cada anuncio tiene su propio codigo unico.
3. **Nunca reutilices un codigo viejo**. Si desactivas `TG9-A1`, el siguiente anuncio de G9 es `TG9-A3` (o el que siga), NO vuelves a usar `TG9-A1`.
4. **Puedes llegar hasta 999** por modelo (`TG9-A999`). No te vas a quedar sin numeros.

---

## Como crear un anuncio nuevo (paso a paso)

### Paso 1: Registrar en Monday

1. Ve al tablero "Seguimiento de Anuncios de Tractos YMax"
2. En el grupo **"Anuncios Pendientes"**, haz clic en **"+ Agregar elemento"**
3. Llena las columnas:
   - **Elemento**: Nombre descriptivo (ej: "Tunland G9 - Reel Financiamiento Abril")
   - **Codigo**: El siguiente numero disponible (ej: `TG9-A3`)
   - **Modelo**: Selecciona el vehiculo (ej: Tunland G9)
   - **Tipo de Anuncio**: Promocion, Evento, etc.
   - **Responsable**: Tu nombre
   - **Estado**: No Iniciado

### Paso 2: Configurar en Facebook/Instagram Ads

Cuando crees la campana en Meta Ads:

1. En la seccion del anuncio, elige **"Enviar mensaje por WhatsApp"** como destino
2. En el **mensaje prellenado** (el texto que el cliente envia automaticamente al hacer clic), escribe:

```
Hola, me interesa TG9-A3
```

> **IMPORTANTE**: El codigo (`TG9-A3`) debe estar EXACTAMENTE como lo registraste en Monday. Respeta mayusculas y el guion.

3. Publica el anuncio

### Paso 3: Actualizar estado en Monday

1. Mueve el elemento al grupo **"Anuncios Activos"**
2. Cambia el **Estado** a **"En Progreso"**
3. Pon la **Fecha de Publicacion**

---

## Que pasa cuando un cliente hace clic en el anuncio?

1. El cliente hace clic en el anuncio de Facebook/Instagram
2. Se abre WhatsApp con el mensaje prellenado: "Hola, me interesa TG9-A3"
3. El bot **automaticamente**:
   - Detecta el codigo `TG9-A3`
   - Sabe que el cliente quiere una **Tunland G9**
   - Le responde enfocandose en ese modelo
   - Crea el lead en el tablero de Leads con el Tracking ID `TG9-A3`
   - Vincula el lead con el anuncio en Monday
4. El cliente **nunca ve el codigo** en la conversacion (el bot lo quita antes de responder)

---

## Cuando desactivar un anuncio

Cuando dejes de correr un anuncio en Meta:

1. Mueve el elemento al grupo **"Anuncios Archivados"**
2. Cambia el **Estado** a **"Completado"**
3. **NO lo borres** - los leads historicos siguen vinculados a este anuncio

---

## Ejemplo completo

Digamos que vas a crear 3 anuncios para la Tunland G9 en marzo:

| Elemento | Codigo | Modelo | Tipo | Mensaje prellenado en WhatsApp |
|----------|--------|--------|------|-------------------------------|
| G9 - Video Testimonio Cliente | `TG9-A1` | Tunland G9 | Promocion | "Hola, me interesa TG9-A1" |
| G9 - Comparativa vs Competencia | `TG9-A2` | Tunland G9 | Promocion | "Hola, me interesa TG9-A2" |
| G9 - Promo Financiamiento | `TG9-A3` | Tunland G9 | Promocion | "Hola, me interesa TG9-A3" |

Despues de un mes, revisas en el tablero de **Leads** y filtras por Tracking ID:
- `TG9-A1` trajo 25 leads, 3 citas, 1 venta → **Buen anuncio**
- `TG9-A2` trajo 50 leads, 0 citas, 0 ventas → **Mucho trafico pero no convierte**
- `TG9-A3` trajo 10 leads, 4 citas, 2 ventas → **El mejor! Meter mas presupuesto**

---

## Preguntas frecuentes

### Puedo tener varios anuncios del mismo modelo al mismo tiempo?
**Si.** Es lo recomendable. Cada uno tiene su propio codigo (`TG9-A1`, `TG9-A2`, etc.) para medir cual funciona mejor.

### Que pasa si un cliente escribe sin codigo (mensaje directo)?
El bot lo atiende normalmente. En Monday aparecera sin Tracking ID, y el "Origen Lead" dira "Directo". No pasa nada malo.

### Que pasa si me equivoco en el codigo del anuncio?
Si el codigo no coincide con ningun modelo (ej: escribiste `TG9A1` sin guion), el bot no lo detectara y tratara al cliente como si llegara directo. **Siempre verifica el formato: MODELO-A#**

### Puedo usar el mismo codigo para Facebook e Instagram?
**No.** Si quieres medir por separado, crea un codigo para cada plataforma:
- `TG9-A1` para el anuncio en Facebook
- `TG9-A2` para el mismo contenido en Instagram

Si no te importa distinguir la plataforma, puedes usar el mismo codigo en ambas.

### Que numero sigue si ya tengo TG9-A1 y TG9-A2?
`TG9-A3`. Siempre el siguiente numero. Revisa en el tablero cual fue el ultimo que usaste para ese modelo.

### Donde veo los resultados?
En el tablero de **"Leads Tractos y Max"**. Filtra la columna "Tracking ID" por el codigo que quieras analizar y veras todos los leads que llegaron por ese anuncio.
