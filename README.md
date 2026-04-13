# socialbot

Bot de Telegram que descarga imágenes y videos de posts de **Instagram** y **Twitter/X** y los envía directamente al chat. No requiere APIs de pago ni cuentas de desarrollador.

## Arquitectura

```
[Telegram] ←polling→ [bot Python en Oracle Cloud VM]
                              ↓
                     yt-dlp / instaloader
```

El bot corre directamente en una VM de Oracle Cloud (free forever). No necesita webhook ni dominio ni HTTPS.

---

## Requisitos

- Cuenta en [Oracle Cloud](https://cloud.oracle.com) (free tier, requiere tarjeta para verificación)
- Bot de Telegram creado con [@BotFather](https://t.me/BotFather)
- Cookies de Instagram exportadas del navegador (para posts de Instagram)

---

## Paso 1 — Crear el bot de Telegram

1. Escribirle a [@BotFather](https://t.me/BotFather) en Telegram
2. Usar `/newbot` y seguir las instrucciones
3. Guardar el **token** (formato `1234567890:AAExxxxxxx`)

---

## Paso 2 — Crear la VM en Oracle Cloud

1. Ir a **Compute → Instances → Create Instance**
2. Imagen: **Ubuntu 22.04**
3. Shape: **VM.Standard.E2.1.Micro** (Always Free)
4. Descargar la clave SSH que genera Oracle
5. Una vez creada, ir a **Networking → VNIC → IP administration** y asignar una **Ephemeral Public IP**

### Conectarse por SSH

```bash
ssh -i Oracle\ ssh-key.key ubuntu@IP_PUBLICA
```

---

## Paso 3 — Configurar ZeroTier (SSH seguro)

Instalar ZeroTier en la VM y en tu PC para acceder por SSH sin exponer el puerto 22 a internet.

**En la VM:**
```bash
curl -s https://install.zerotier.com | sudo bash
sudo zerotier-cli join TU_NETWORK_ID
```

Autorizar el dispositivo en [my.zerotier.com](https://my.zerotier.com) y anotar la IP ZeroTier asignada (ej. `10.241.x.x`).

**Configurar firewall:**
```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 9993/udp          # ZeroTier
sudo ufw allow in on ztXXXXXX to any port 22   # SSH solo por ZeroTier
sudo ufw enable
```

`ztXXXXXX` es el nombre de la interfaz ZeroTier (`ip link show | grep zt`).

Verificar que SSH funciona por ZeroTier antes de cerrar el puerto 22 público en Oracle **Security Lists**.

---

## Paso 4 — Instalar el bot

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install python3-pip git -y
git clone https://github.com/pablopeu/socialbot.git
cd socialbot/telegrambot
pip3 install -r requirements.txt
```

---

## Paso 5 — Configurar

**Token del bot:**
```bash
cp config.json.example config.json
nano config.json   # reemplazar con tu token
```

**Usuarios permitidos** (un ID por línea):
```bash
cp allowed_users.txt.example allowed_users.txt
nano allowed_users.txt
```

Para saber tu ID de Telegram escribile a [@userinfobot](https://t.me/userinfobot).

**Cookies de Instagram** — exportar con la extensión Cookie-Editor del navegador (formato Netscape) y copiar a la VM:
```bash
# Desde tu PC (PowerShell):
scp -i "Oracle ssh-key.key" cookies.txt ubuntu@10.241.x.x:/home/ubuntu/socialbot/telegrambot/cookies.txt
```

---

## Paso 6 — Instalar como servicio

```bash
sudo cp /home/ubuntu/socialbot/telegrambot/socialbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable socialbot
sudo systemctl start socialbot
sudo systemctl status socialbot   # debe mostrar "active (running)"
```

El bot arranca automáticamente si la VM se reinicia.

---

## Uso

Mandar un link al bot en Telegram:

```
https://www.instagram.com/p/SHORTCODE/
https://www.instagram.com/reel/SHORTCODE/
https://x.com/usuario/status/1234567890
https://twitter.com/usuario/status/1234567890
```

El bot responde con las fotos y/o videos del post. Soporta posts individuales, carousels y reels.

Los usuarios no autorizados reciben: *"No tenés acceso. Contactate con el admin."*

---

## Agregar un usuario

```bash
nano ~/socialbot/telegrambot/allowed_users.txt   # agregar el ID
sudo systemctl restart socialbot
```

---

## Comandos útiles

```bash
# Ver logs en tiempo real
sudo journalctl -u socialbot -f

# Reiniciar el bot
sudo systemctl restart socialbot

# Actualizar el bot
cd ~/socialbot && git pull && sudo systemctl restart socialbot
```

---

## Notas

- Las cookies de Instagram expiran. Si deja de funcionar con Instagram, repetir el paso 5.
- Oracle Cloud Always Free no tiene límite de tiempo ni costo mientras se use el shape gratuito.
- Telegram tiene un límite de 50 MB por archivo. Videos más grandes no se pueden enviar.
