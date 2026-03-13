# 👑 CREAM VIP — Bot de Telegram

Tu tracker personal de Telegram Stars, ahora en Telegram.

---

## ✅ PASO 1 — Crear tu bot en Telegram (2 minutos)

1. Abre Telegram y busca **@BotFather**
2. Escríbele: `/newbot`
3. Ponle nombre: `CREAM VIP`
4. Ponle usuario: `creamvip_tuNombre_bot` (debe terminar en `bot`)
5. BotFather te dará un **token** así:
   ```
   1234567890:ABCDefGhIJKlmNoPQRsTUvwXyz
   ```
   **Guárdalo** — lo necesitas ahora.

---

## ✅ PASO 2 — Obtener tu Telegram User ID

1. Busca **@userinfobot** en Telegram
2. Escríbele cualquier cosa
3. Te responde con tu **Id** (un número como `987654321`)
   **Guárdalo** también.

---

## ✅ PASO 3 — Subir el bot a Railway (gratis)

### 3a. Crear cuenta en GitHub
1. Ve a **github.com** desde Safari en tu iPhone
2. Crea una cuenta gratuita (o inicia sesión)
3. Crea un repositorio nuevo llamado `creamvip-bot`
4. Sube los 4 archivos de esta carpeta:
   - `bot.py`
   - `requirements.txt`
   - `Procfile`
   - `runtime.txt`

### 3b. Desplegar en Railway
1. Ve a **railway.app** desde Safari
2. Crea cuenta gratis con tu GitHub
3. Haz clic en **"New Project"**
4. Selecciona **"Deploy from GitHub repo"**
5. Elige tu repositorio `creamvip-bot`
6. Railway detecta el `Procfile` automáticamente ✅

---

## ✅ PASO 4 — Configurar variables de entorno

En Railway, ve a tu proyecto → pestaña **Variables** → agrega:

| Variable   | Valor                          |
|------------|-------------------------------|
| `BOT_TOKEN` | El token que te dio BotFather |
| `OWNER_ID`  | Tu Telegram User ID (número)  |

Haz clic en **Deploy** — ¡listo!

---

## 🤖 Comandos del bot

| Comando        | Qué hace                              |
|----------------|---------------------------------------|
| `/start`       | Menú principal                        |
| `/resumen`     | Ver total de stars y USD              |
| `/calendario`  | Ver TODAS las fechas de retiro        |
| `/agregar`     | Registrar nuevo ingreso               |
| `/retirar`     | Marcar un retiro realizado            |
| `/stats`       | Estadísticas detalladas               |
| `/retiros`     | Historial de ingresos y retiros       |
| `/meta 500`    | Establecer meta mensual ($500 USD)    |
| `/nombre Val`  | Cambiar tu nombre en el bot           |

---

## 🔔 Alertas automáticas

El bot te enviará un mensaje **todos los días a las 7:17 PM** cuando tengas Stars disponibles para retirar.

Cada vez que agregas un ingreso, el bot programa automáticamente una notificación exacta para el día 21 a las 7:17 PM.

---

## 💡 Notas

- Railway plan gratuito incluye 500 horas/mes (suficiente para el bot)
- Los datos se guardan en `data.json` en el servidor
- Para hacer backup, usa `/resumen` y copia el texto

---

*CREAM VIP Bot — Stars Tracker Premium* 👑
