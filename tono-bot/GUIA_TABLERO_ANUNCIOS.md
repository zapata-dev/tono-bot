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
| **Codigo** | **EL MAS IMPORTANTE** - Codigo unico para el bot | `TG9-A1` o `CA-SU1` |
| **Modelo** | Que vehiculo promueve este anuncio | Tunland G9 |

---

## Como funciona el Codigo (la columna mas importante)

El codigo es lo que conecta al cliente con el anuncio. Tiene este formato:

```
[MODELO]-[TIPO][NUMERO]
```

### Tipos de campaña

No todos los anuncios son iguales. El tipo de campaña va entre el modelo y el numero:

| Codigo | Tipo | Cuando usarlo | Ejemplo |
|--------|------|---------------|---------|
| `A` | **Anuncio** | Anuncio regular de Facebook/Instagram | `TG9-A1` |
| `SU` | **Mejor Precio** | Mejor Propuesta / Mejor Precio | `CA-SU1` |
| `LQ` | **Liquidacion** | Liquidacion / Precio especial | `ML-LQ1` |
| `PR` | **Promocion** | Promocion especial con condiciones | `TP-PR1` |
| `EV` | **Evento** | Evento / Open House | `E11-EV1` |

### Codigos de cada modelo

| Vehiculo | Codigo | Anuncio regular | Mejor Precio | Liquidacion |
|----------|--------|-----------------|---------|-------------|
| Tunland G7 | `TG7` | `TG7-A1` | `TG7-SU1` | `TG7-LQ1` |
| Tunland G9 | `TG9` | `TG9-A1` | `TG9-SU1` | `TG9-LQ1` |
| Tunland E5 | `TE5` | `TE5-A1` | `TE5-SU1` | `TE5-LQ1` |
| Miler | `ML` | `ML-A1` | `ML-SU1` | `ML-LQ1` |
| Toano Panel | `TP` | `TP-A1` | `TP-SU1` | `TP-LQ1` |
| ESTA 6x4 11.8 | `E11` | `E11-A1` | `E11-SU1` | `E11-LQ1` |
| ESTA 6x4 X13 | `EX` | `EX-A1` | `EX-SU1` | `EX-LQ1` |
| Cascadia | `CA` | `CA-A1` | `CA-SU1` | `CA-LQ1` |

### Reglas del codigo

1. **El numero es secuencial por modelo Y por tipo**. Si ya tienes `TG9-A1` y `TG9-A2`, el siguiente anuncio regular es `TG9-A3`. Si es la primera campaña de mejor precio de G9, es `TG9-SU1`.
2. **Nunca repitas un codigo**. Cada anuncio tiene su propio codigo unico.
3. **Nunca reutilices un codigo viejo**. Si desactivas `TG9-A1`, el siguiente anuncio regular de G9 es `TG9-A3` (o el que siga), NO vuelves a usar `TG9-A1`.
4. **Puedes llegar hasta 999** por modelo y tipo (`TG9-A999`, `CA-SU999`). No te vas a quedar sin numeros.
5. **Cada tipo tiene su propia numeracion**. `CA-A1` y `CA-SU1` son codigos diferentes para anuncios diferentes del mismo modelo.

---

## Como crear un anuncio nuevo (paso a paso)

### Paso 1: Registrar en Monday

1. Ve al tablero "Seguimiento de Anuncios de Tractos YMax"
2. En el grupo **"Anuncios Pendientes"**, haz clic en **"+ Agregar elemento"**
3. Llena las columnas:
   - **Elemento**: Nombre descriptivo (ej: "Tunland G9 - Reel Financiamiento Abril")
   - **Codigo**: El siguiente numero disponible (ej: `TG9-A3` para anuncio regular, `TG9-SU1` para campaña de mejor precio)
   - **Modelo**: Selecciona el vehiculo (ej: Tunland G9)
   - **Tipo de Anuncio**: Promocion, Evento, etc.
   - **Responsable**: Tu nombre
   - **Estado**: No Iniciado

### Paso 2: Configurar en Meta Ads Manager (Facebook/Instagram)

El codigo NO va en la imagen ni en el texto del anuncio que lee la gente. Va en la **Plantilla de Mensaje** (el mensaje automatico que se le escribe al cliente cuando le da clic al boton de WhatsApp).

#### 2.1 Crear la Campana y Conjunto de Anuncios

1. Crea tu campana normalmente (generalmente con el objetivo de **Interaccion** o **Ventas**)
2. En el nivel de **Conjunto de Anuncios**, asegurate de que el destino (App de mensajeria) este marcado como **WhatsApp**

#### 2.2 Disenar el Anuncio

1. Sube tu video o imagen
2. Escribe el texto principal (Copy) y el titulo normal ("Estrena tu Tunland G9 hoy", etc.)
3. **El codigo NO va aqui** - el copy del anuncio se mantiene normal

#### 2.3 Configurar la Plantilla de Mensaje (AQUI VA EL CODIGO)

1. Baja hasta la seccion que dice **"Plantilla de mensaje"** (Message Template)
2. Haz clic en **"+ Crear nueva"** (o edita una existente)
3. Ve a la seccion del **Mensaje inicial del cliente** (el texto pre-escrito que aparecera en el celular del cliente cuando se abra WhatsApp)
4. Borra las preguntas por defecto ("Quiero mas informacion", "Tienen disponibilidad?", etc.)
5. Escribe el mensaje incluyendo el codigo exacto de Monday:

```
Hola, me interesa TG9-A3
```

O para una campaña de mejor precio:
```
Hola CA-SU1
```

> **Opcional**: Puedes hacerlo mas natural, por ejemplo: "Hola, vi su anuncio en Facebook y me interesa TG9-A3. Me dan informes?" - el bot lo detectara igual mientras el codigo este bien escrito.

> **IMPORTANTE**: El codigo (`TG9-A3`, `CA-SU1`, etc.) debe estar EXACTAMENTE como lo registraste en Monday. Respeta mayusculas, el guion y el tipo de campaña.

6. Guarda la plantilla con un nombre que reconozcas (ej: "Plantilla G9 Anuncio 3")

#### 2.4 Probar antes de publicar

> **REGLA DE ORO**: Antes de encender la campana en Facebook, enviate un mensaje de prueba a ti mismo haciendo clic en la vista previa del anuncio. Comprueba que el bot reconoce el codigo y te responde enfocado en el modelo correcto. Si el bot no detecta el codigo, revisa el formato antes de gastar presupuesto.

7. Publica el anuncio

### Paso 3: Actualizar estado en Monday

1. Mueve el elemento al grupo **"Anuncios Activos"**
2. Cambia el **Estado** a **"En Progreso"**
3. Pon la **Fecha de Publicacion**

---

## Que pasa cuando un cliente hace clic en el anuncio?

1. El cliente hace clic en el anuncio de Facebook/Instagram
2. Se abre WhatsApp con el mensaje prellenado: "Hola, me interesa TG9-A3" (o "Hola CA-SU1" para campaña de mejor precio)
3. El bot **automaticamente**:
   - Detecta el codigo `TG9-A3` o `CA-SU1`
   - Sabe que el cliente quiere una **Tunland G9** (o **Cascadia**) y el tipo de campaña (Anuncio regular, Mejor Precio, etc.)
   - Le responde enfocandose en ese modelo y las reglas de esa campaña
   - Crea el lead en el tablero de Leads con el Tracking ID
   - Vincula el lead con el anuncio en Monday
4. El cliente **nunca ve el codigo** en la conversacion (el bot lo quita antes de responder)

### Que experimenta el cliente?

1. Ve un anuncio atractivo de la Tunland G9 en Facebook/Instagram
2. Le da clic al boton **"Enviar mensaje"**
3. Se abre su WhatsApp y en la caja de texto ya dice "Hola, me interesa TG9-A3" (o "Hola CA-SU1")
4. El cliente solo presiona el boton de enviar
5. El bot le responde sobre la Tunland G9 (o Cascadia) sin que el cliente vea ningun codigo raro

---

## Cuando desactivar un anuncio

Cuando dejes de correr un anuncio en Meta:

1. Mueve el elemento al grupo **"Anuncios Archivados"**
2. Cambia el **Estado** a **"Completado"**
3. **NO lo borres** - los leads historicos siguen vinculados a este anuncio

---

## Ejemplo completo

Digamos que vas a crear anuncios regulares y una campaña de mejor precio del Cascadia en marzo:

| Elemento | Codigo | Modelo | Tipo | Mensaje prellenado en WhatsApp |
|----------|--------|--------|------|-------------------------------|
| G9 - Video Testimonio Cliente | `TG9-A1` | Tunland G9 | Anuncio | "Hola, me interesa TG9-A1" |
| G9 - Comparativa vs Competencia | `TG9-A2` | Tunland G9 | Anuncio | "Hola, me interesa TG9-A2" |
| Cascadia - Mejor Propuesta Leon | `CA-SU1` | Cascadia | Mejor Precio | "Hola CA-SU1" |
| Miler - Liquidacion Marzo | `ML-LQ1` | Miler | Liquidacion | "Hola ML-LQ1" |

Despues de un mes, revisas en el tablero de **Leads** y filtras por Tracking ID:
- `TG9-A1` trajo 25 leads, 3 citas, 1 venta → **Buen anuncio regular**
- `TG9-A2` trajo 50 leads, 0 citas, 0 ventas → **Mucho trafico pero no convierte**
- `CA-SU1` trajo 15 leads, 8 propuestas, 1 venta → **Mejor Precio exitosa!**
- `ML-LQ1` trajo 10 leads, 4 citas, 2 ventas → **Liquidacion funciono bien**

---

## Preguntas frecuentes

### Puedo tener varios anuncios del mismo modelo al mismo tiempo?
**Si.** Es lo recomendable. Cada uno tiene su propio codigo (`TG9-A1`, `TG9-A2`, `TG9-SU1`, etc.) para medir cual funciona mejor.

### Que pasa si un cliente escribe sin codigo (mensaje directo)?
El bot lo atiende normalmente. En Monday aparecera sin Tracking ID, y el "Origen Lead" dira "Directo". No pasa nada malo.

### Que pasa si me equivoco en el codigo del anuncio?
Si el codigo no coincide con ningun modelo o tipo (ej: escribiste `TG9A1` sin guion, o `TG9-X1` con un tipo inexistente), el bot no lo detectara y tratara al cliente como si llegara directo. **Siempre verifica el formato: MODELO-TIPO# (ej: TG9-A1, CA-SU1, ML-LQ2)**

### Puedo usar el mismo codigo para Facebook e Instagram?
**No.** Si quieres medir por separado, crea un codigo para cada plataforma:
- `TG9-A1` para el anuncio en Facebook
- `TG9-A2` para el mismo contenido en Instagram

Si no te importa distinguir la plataforma, puedes usar el mismo codigo en ambas.

### Que numero sigue si ya tengo TG9-A1 y TG9-A2?
`TG9-A3`. Siempre el siguiente numero. Revisa en el tablero cual fue el ultimo que usaste para ese modelo y tipo.

### Puedo tener un anuncio regular y una campaña de mejor precio del mismo modelo?
**Si.** Cada tipo tiene su propia numeracion. Puedes tener `CA-A1` (anuncio regular de Cascadia) y `CA-SU1` (campaña de mejor precio de Cascadia) al mismo tiempo sin problema.

### Que tipos de campaña puedo usar?
- **A** = Anuncio regular (el de siempre)
- **SU** = Mejor Precio / Mejor Propuesta
- **LQ** = Liquidacion / Precio especial
- **PR** = Promocion especial
- **EV** = Evento / Open House

### Donde veo los resultados?
En el tablero de **"Leads Tractos y Max"**. Filtra la columna "Tracking ID" por el codigo que quieras analizar y veras todos los leads que llegaron por ese anuncio.
