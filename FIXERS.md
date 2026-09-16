# Runbook: verificación de fixers de Instagram

Proceso completo para encontrar, probar y habilitar mirrors tipo fixer
(instancias al estilo InstaFix que devuelven tags `og:image`/`og:video`
del CDN de Instagram). Documentado después del roundtrip de 2026-09-16.

## Contexto

El bot descarga de Instagram por rutas: acceso directo (instaloader,
bloqueado con 403 desde la VM), fixers, y APIs de terceros (instapdown,
downreels, fastvidl, nuelink, listnr). El `health check` diario (04:30
hora Buenos Aires, ver `telegrambot/downloader.py`) prueba todas las
rutas contra posts canarios, rankea las vivas por latencia y saltea las
muertas durante el día. Este runbook cubre cómo incorporar fixers
nuevos a ese sistema.

Un fixer sirve el HTML del post en `https://DOMINIO/p/CODIGO` (o
`/reel/CODIGO`, `/tv/CODIGO`) con tags OG apuntando a `cdninstagram.com`.

**Lección clave del 2026-09-16:** la red desde donde se testea miente.
Hosts que colgaban o daban DNS-fail desde la máquina local funcionaban
perfecto desde el VPS de Oracle (y viceversa: `instagramez.com` y
`kkinstagram.com` respondían 200 local pero servían shells sin OG desde
ambos lados). El veredicto solo vale lo que dice el test **en el VPS**.

## Paso 1 — Conseguir candidatos

- Addon tipo "XIG Copy Replace" (Firefox): su changelog documenta cada
  reemplazo de dominio de la comunidad (ej: ddinstagram → uuinstagram,
  kkinstagram → zzinstagram). Fuente de instancias nuevas.
- READMEs de bots que arreglan embeds (ej: `seriaati/embed-fixer`) —
  listan fixers soportados con sus dominios.
- Issues del repo `Wikidepia/InstaFix` (archivado 2026-04): los usuarios
  reportan instancias alternativas cuando una muere.

## Paso 2 — Pre-test local (opcional, orientativo)

```bash
curl -sL "https://DOMINIO/p/BsOGulcndj-" | grep -o 'og:image" content="[^"]*'
```

Si devuelve una URL de `cdninstagram.com` es candidato. Si falla, no
descartarlo: puede ser bloqueo de red local (ver lección arriba).

## Paso 3 — Test real en el VPS

Los canarios actuales (públicos y estables):
- Post: `BsOGulcndj-` (el post del huevo, 2019)
- Reel: `DdU5pjGikpX` (reel real del log del bot)

Si un canario dejara de existir, elegir otro post/reel público muy
conocido y actualizar `CANARY_POST`/`CANARY_REEL` en
`test_fixers_vps.py` (y `SOCIALBOT_INSTAGRAM_HEALTH_CANARY` en
`downloader.py` para el health check diario).

```bash
# en el VPS, dentro del repo
git pull
python3 test_fixers_vps.py
```

Para probar hosts nuevos, agregarlos a la lista `FIXER_HOSTS` al inicio
del script y volver a correr. El script es solo lectura: no toca el
estado del bot ni descarga medios.

## Paso 4 — Interpretar la salida

| Resultado | Significado |
|---|---|
| `200 ...ms og:N` con N ≥ 1, en post **y** reel | Sirve. Agregar a la lista. |
| `200 ...ms og:0` | Vivo pero inútil (devuelve shell sin medios). No sumar. |
| `403` / `502` | Bloqueado/roto para la IP del VPS. No sumar. |
| `X ConnectTimeout` / `X ConnectError` | Inaccesible desde el VPS. No sumar. |

Probar siempre post **y** reel: hay hosts que sirven OG para `/p/` pero
no para `/reel/` (o devuelven shells a algunas redes).

## Paso 5 — Actualizar el bot y deployar

1. Editar el default de `INSTAGRAM_FIXER_HOSTS` en
   `telegrambot/downloader.py` (los verificados primero). No borrar los
   muertos: quedan como fallback y el health check los saltea.
2. Commit + push.
3. En el VPS: `git pull && sudo systemctl restart socialbot`.

Al arrancar, el bot corre el health check solo (también el 04:30 de
cada día), rankea por latencia medida, y saltea los muertos hasta el
chequeo siguiente. Estado visible con `/instagram_status` en Telegram.

## Estado al 2026-09-16 (IP VPS 129.213.40.115)

- Fixers vivos: `zzinstagram.com`, `instagram7.com`,
  `toinstagram.com`, `uuinstagram.com` (~0.8-1.1s, OG real post+reel).
- Fixers muertos/inútiles: `vxinstagram.com` (502), `fxstagram.com`
  (cuelga), `eeinstagram.com`, `instagramez.com`, `kkinstagram.com`
  (200 sin OG), `dtoinstagram.com`, `ddinstagram.com` (inaccesibles),
  `oginstagram.com` (403).
- APIs vivas: instapdown (263ms en reels, la más rápida), downreels,
  listnr. Rotas: fastvidl (paywall), nuelink (422).
- `direct` (instaloader anónimo): muerto por 403 de Instagram.
