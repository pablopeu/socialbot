# socialbot

Bot de Telegram que descarga imágenes y videos de posts de **Instagram** y **Twitter/X** y los envía directamente al chat. No requiere APIs de pago ni cuentas de desarrollador.

## Cómo funciona

```
[Telegram] → [bot/ en hosting PHP] → [service/ en Railway] → [yt-dlp / instaloader]
```

1. El usuario manda un link al bot de Telegram.
2. El bot PHP llama al microservicio en Railway.
3. Railway descarga el archivo usando `yt-dlp` (o `instaloader` como fallback para Instagram) y lo devuelve al bot.
4. El bot sube el archivo directamente a Telegram.

**`bot/`** — script PHP, corre en cualquier hosting compartido con cURL.  
**`service/`** — microservicio FastAPI/Python, se despliega gratis en Railway.

---

## Paso 1 — Crear el bot de Telegram

1. Hablar con [@BotFather](https://t.me/BotFather) en Telegram.
2. Usar el comando `/newbot` y seguir las instrucciones.
3. Guardar el **token** que entrega BotFather (formato `1234567890:AAExxxxxxx`).

---

## Paso 2 — Desplegar el microservicio en Railway

### Requisitos
- Cuenta gratuita en [Railway](https://railway.app)
- Este repositorio forkeado o clonado en tu GitHub

### Pasos

1. En Railway: **New Project** → **Deploy from GitHub repo** → seleccionar este repo.
2. Cuando Railway pida el directorio raíz, elegir **`service/`**.
3. Railway detecta el `Procfile` y `requirements.txt` automáticamente.
4. En la pestaña **Variables** del proyecto, agregar:

   | Variable | Valor | Obligatorio |
   |---|---|---|
   | `SECRET_TOKEN` | cualquier cadena larga y aleatoria | recomendado |

5. Una vez desplegado, ir a **Settings → Networking → Generate Domain** para obtener la URL pública.  
   Ejemplo: `https://socialbot-production-xxxx.up.railway.app`

### Verificar

```
curl https://tu-servicio.railway.app/health
# → {"status":"ok"}
```

---

## Paso 3 — Obtener cookies de Instagram (necesario para Instagram)

Instagram bloquea requests sin sesión. Hay que exportar las cookies del navegador con una sesión iniciada.

1. Instalar la extensión **Cookie-Editor** en Chrome o Firefox.
2. Ir a [instagram.com](https://www.instagram.com) con tu cuenta iniciada.
3. Abrir Cookie-Editor → **Export** → **Export as Netscape**.
4. Copiar todo el contenido del archivo.
5. Convertirlo a base64. En Linux/Mac:
   ```bash
   cat cookies.txt | base64 -w 0
   ```
   En Windows (PowerShell):
   ```powershell
   [Convert]::ToBase64String([IO.File]::ReadAllBytes("cookies.txt"))
   ```
6. Guardar el resultado (una cadena larga en una sola línea).

> Para Twitter/X no se necesitan cookies.

---

## Paso 4 — Configurar el bot PHP

Editar `bot/config.php` con tus datos:

```php
<?php
return [
    'token'             => 'TOKEN_DEL_BOT_DE_TELEGRAM',
    'ytdlp_service_url' => 'https://tu-servicio.railway.app',
    'ytdlp_secret'      => 'el_mismo_SECRET_TOKEN_de_railway',
    'ig_cookies'        => 'PEGAR_ACA_EL_BASE64_DE_LAS_COOKIES',
];
```

Si no usás `SECRET_TOKEN` en Railway, dejar `ytdlp_secret` como string vacío `''`.

---

## Paso 5 — Subir el bot al hosting

Subir por FTP o panel de control los dos archivos de la carpeta `bot/`:

```
bot/peuigbot.php
bot/config.php
```

Deben quedar accesibles por HTTPS, por ejemplo en `https://tudominio.com/peuigbot.php`.

---

## Paso 6 — Registrar el webhook

Abrir esta URL en el navegador (reemplazar `TOKEN` y `TU_DOMINIO`):

```
https://api.telegram.org/botTOKEN/setWebhook?url=https://TU_DOMINIO/peuigbot.php
```

Ejemplo:
```
https://api.telegram.org/bot1234567890:AAExxxxxxx/setWebhook?url=https://midominio.com/peuigbot.php
```

Si responde `{"ok":true,"result":true}` el bot está activo.

---

## Uso

Abrir el bot en Telegram y mandar un link:

```
https://www.instagram.com/p/SHORTCODE/
https://www.instagram.com/reel/SHORTCODE/
https://x.com/usuario/status/1234567890
https://twitter.com/usuario/status/1234567890
```

El bot responde con las fotos y/o videos del post. Funciona con posts individuales, carousels y reels.

---

## Estructura del repositorio

```
socialbot/
├── bot/                 ← subir al hosting PHP
│   ├── peuigbot.php     # webhook del bot de Telegram
│   └── config.php       # token, URL del servicio, cookies
├── service/             ← desplegar en Railway
│   ├── main.py          # FastAPI + yt-dlp + instaloader
│   ├── requirements.txt
│   └── Procfile
└── version_vieja/       ← versión original (referencia)
```

---

## Notas

- Las cookies de Instagram expiran. Si el bot deja de funcionar con Instagram, repetir el paso 3.
- El plan gratuito de Railway incluye suficientes horas para uso personal.
- Si un post es privado y las cookies son de una cuenta que no lo sigue, no se puede descargar.
- El log del bot se guarda en `combined_bot.log` junto al script PHP en el hosting.
