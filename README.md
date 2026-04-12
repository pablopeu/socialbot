# socialbot

Bot de Telegram que descarga imágenes y videos de posts de Instagram y Twitter/X usando un microservicio Python con `yt-dlp`. No requiere APIs de pago ni cuentas de desarrollador.

## Arquitectura

```
[Telegram] → [bot/ en shared hosting PHP] → [service/ en Railway] → [yt-dlp]
```

- **`bot/`** — script PHP que recibe el webhook de Telegram y envía los medios al usuario
- **`service/`** — microservicio FastAPI/Python que extrae URLs de medios usando `yt-dlp`

---

## 1. Microservicio Python en Railway

### Requisitos
- Cuenta gratuita en [Railway](https://railway.app)
- Este repositorio en tu GitHub

### Pasos

1. En Railway, crear un nuevo proyecto → **Deploy from GitHub repo**
2. Seleccionar este repositorio y elegir el directorio raíz: **`service/`**
3. Railway detecta `requirements.txt` automáticamente e instala las dependencias
4. El `Procfile` le indica a Railway cómo correr el servidor:
   ```
   web: uvicorn main:app --host 0.0.0.0 --port $PORT
   ```
5. (Opcional pero recomendado) En **Variables** del proyecto Railway, agregar:
   ```
   SECRET_TOKEN=una_clave_secreta_larga
   ```
   Si se define, el servicio exigirá el header `X-Secret: <token>` en cada request.

6. Una vez desplegado, Railway te da una URL pública (ej. `https://socialbot-production.up.railway.app`). Anotarla.

### Verificar que funciona
```
https://tu-servicio.railway.app/extract?url=https://www.instagram.com/p/SHORTCODE/
```
Debe devolver algo como:
```json
{"media": [{"type": "image", "url": "https://..."}]}
```

---

## 2. Bot PHP en shared hosting

### Requisitos
- Hosting PHP con soporte de `curl` (cualquier hosting compartido básico)
- Bot de Telegram creado con [@BotFather](https://t.me/BotFather)

### Pasos

1. **Crear el bot en Telegram**: hablar con [@BotFather](https://t.me/BotFather), usar `/newbot` y guardar el token.

2. **Editar `bot/config.php`** con tus datos:
   ```php
   <?php
   return [
       'token'             => 'TOKEN_DEL_BOT_DE_TELEGRAM',
       'ytdlp_service_url' => 'https://tu-servicio.railway.app',
       'ytdlp_secret'      => 'la_misma_clave_secreta_de_railway', // vacío si no usás SECRET_TOKEN
   ];
   ```

3. **Subir al hosting** los dos archivos de la carpeta `bot/`:
   - `peuigbot.php`
   - `config.php`

4. **Registrar el webhook** de Telegram visitando esta URL en el navegador (reemplazar los valores):
   ```
   https://api.telegram.org/bot[TOKEN]/setWebhook?url=https://[TU_DOMINIO]/peuigbot.php
   ```
   Por ejemplo:
   ```
   https://api.telegram.org/bot7435666643:AAE86MML8pGjvovfdhhhdT/setWebhook?url=https://midominio.com/peuigbot.php
   ```
   Si responde `{"ok":true}` el bot está activo.

---

## Uso

Abrí el bot en Telegram y mandá un link de Instagram o Twitter/X:

```
https://www.instagram.com/p/SHORTCODE/
https://www.instagram.com/reel/SHORTCODE/
https://x.com/usuario/status/1234567890
https://twitter.com/usuario/status/1234567890
```

El bot responde con las imágenes y/o videos del post. Funciona con posts individuales, carousels y videos.

---

## Estructura del repositorio

```
socialbot/
├── service/            ← subir a Railway
│   ├── main.py         # FastAPI + yt-dlp
│   ├── requirements.txt
│   └── Procfile
├── bot/                ← subir al shared hosting
│   ├── peuigbot.php    # bot de Telegram
│   └── config.php      # configuración (token, URL del servicio)
└── README.md
```

---

## Notas

- El microservicio en Railway extrae URLs directas del CDN de Instagram/X sin descargar los archivos; el bot PHP luego le pasa esas URLs a Telegram, que las descarga por su cuenta.
- El plan gratuito de Railway es suficiente para uso personal.
- Si un post es privado, yt-dlp no podrá extraerlo y el bot lo informará.
- El log del bot se guarda en `combined_bot.log` junto al script PHP.
