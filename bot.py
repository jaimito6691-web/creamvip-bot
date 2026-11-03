import asyncio
import html
import imaplib
import logging
import os
import re
import sqlite3
import unicodedata
from datetime import date, datetime, time as dtime, timedelta, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from functools import wraps
from zoneinfo import ZoneInfo

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ───────────────────────── CONFIG (variables de entorno) ─────────────────────────
TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TOKEN")
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("ADMIN_IDS", ""))}
DB_PATH = os.getenv("DB_PATH", "netflix.db")  # en Railway: /data/netflix.db con un Volume
CHECK_MINUTES = int(os.getenv("CHECK_MINUTES", "5"))  # cada cuánto revisa los correos
TZ = ZoneInfo(os.getenv("BOT_TZ", "America/Lima"))
AUTO_EXTENDER = os.getenv("AUTO_EXTENDER", "1") == "1"  # extiende vencimiento al detectar pago OK
WAIT_SECONDS = int(os.getenv("WAIT_SECONDS", "45"))  # espera máx. al pedir un código
VIGENCIA_DIAS = 30
CODE_MAX_AGE_MIN = 15  # los códigos de Netflix duran ~15 min

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("netflix-bot")

NOPREV = {"parse_mode": ParseMode.HTML, "link_preview_options": LinkPreviewOptions(is_disabled=True)}
e = html.escape

# ───────────────────────── BASE DE DATOS ─────────────────────────


def q(sql, params=(), fetch=None):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute(sql, params)
        if fetch == "all":
            res = cur.fetchall()
        elif fetch == "one":
            res = cur.fetchone()
        else:
            res = cur.lastrowid
        con.commit()
        return res
    finally:
        con.close()


def init_db():
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    q(
        """CREATE TABLE IF NOT EXISTS cuentas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            correo TEXT NOT NULL,
            clave TEXT NOT NULL,
            imap_clave TEXT NOT NULL DEFAULT '',
            imap_host TEXT NOT NULL DEFAULT '',
            inicio TEXT NOT NULL,
            vence TEXT NOT NULL,
            creada TEXT NOT NULL,
            baseline INTEGER NOT NULL DEFAULT 0,
            estado TEXT NOT NULL DEFAULT 'ok'
        )"""
    )
    q(
        """CREATE TABLE IF NOT EXISTS eventos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cuenta_id INTEGER NOT NULL,
            msgid TEXT NOT NULL,
            tipo TEXT,
            UNIQUE(cuenta_id, msgid)
        )"""
    )


# ───────────────────────── FECHAS ─────────────────────────


def hoy():
    return datetime.now(TZ).date()


def fmt(d):
    return d.strftime("%d/%m/%Y")


def parse_fecha(s):
    s = s.strip()
    for f in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    raise ValueError("Fecha inválida. Usa DD/MM/AAAA, por ejemplo 28/10/2026.")


def _entero(v):
    try:
        n = int(str(v).strip())
    except ValueError:
        raise ValueError("Debes escribir un número entero de días (ej: 4).")
    if not 0 <= n <= 365:
        raise ValueError("El número de días debe estar entre 0 y 365.")
    return n


def calc_fechas(modo, valor=None):
    """Devuelve (fecha_inicio, fecha_vencimiento)."""
    h = hoy()
    if modo == "hoy":
        ini = h
    elif modo == "hace":  # la cuenta ya lleva N días activa
        ini = h - timedelta(days=_entero(valor))
    elif modo == "inicio":  # fecha de inicio exacta
        ini = parse_fecha(valor)
    elif modo == "restan":  # le quedan N días
        ini = h + timedelta(days=_entero(valor)) - timedelta(days=VIGENCIA_DIAS)
    else:
        raise ValueError("Opción de fecha no válida.")
    return ini, ini + timedelta(days=VIGENCIA_DIAS)


def dias_restantes(c):
    return (date.fromisoformat(c["vence"]) - hoy()).days


def txt_dias(dl):
    if dl > 1:
        return f"en {dl} días"
    if dl == 1:
        return "mañana"
    if dl == 0:
        return "hoy"
    if dl == -1:
        return "venció ayer"
    return f"venció hace {-dl} días"


def emoji_dias(dl):
    if dl > 3:
        return "🟢"
    if dl >= 1:
        return "🟡"
    if dl == 0:
        return "🟠"
    return "🔴"


# ───────────────────────── IMAP ─────────────────────────

HOSTS = {
    "gmail.com": "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "outlook.com": "outlook.office365.com",
    "hotmail.com": "outlook.office365.com",
    "live.com": "outlook.office365.com",
    "msn.com": "outlook.office365.com",
    "yahoo.com": "imap.mail.yahoo.com",
    "yahoo.es": "imap.mail.yahoo.com",
    "icloud.com": "imap.mail.me.com",
    "me.com": "imap.mail.me.com",
    "gmx.com": "imap.gmx.com",
    "gmx.es": "imap.gmx.com",
    "zoho.com": "imap.zoho.com",
    "aol.com": "imap.aol.com",
}
MESES_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def host_para(correo):
    dom = correo.split("@")[-1].lower()
    return HOSTS.get(dom, "imap." + dom)


def _imap_fecha(d):
    return f"{d.day:02d}-{MESES_EN[d.month - 1]}-{d.year}"


def _decode(h):
    try:
        return str(make_header(decode_header(h or "")))
    except Exception:
        return h or ""


def _extraer(msg):
    textos, links = [], []
    for part in msg.walk():
        ct = part.get_content_type()
        if ct not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        s = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if ct == "text/html":
            links += re.findall(r'href=["\']([^"\']+)["\']', s, flags=re.I)
            s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
            s = re.sub(r"<[^>]+>", " ", s)
            s = html.unescape(s)
        else:
            links += re.findall(r"https?://[^\s<>\"]+", s)
        textos.append(s)
    texto = re.sub(r"\s+", " ", " ".join(textos)).strip()
    links = [html.unescape(x) for x in links if x.startswith("http")]
    return texto, links


def imap_fetch(host, user, pwd, dias=3, limite=30):
    """Lee (sin marcar como leídos) los correos recientes de Netflix. Más nuevos primero."""
    m = imaplib.IMAP4_SSL(host, 993, timeout=30)
    try:
        m.login(user, pwd)
        m.select("INBOX", readonly=True)
        desde = _imap_fecha(date.today() - timedelta(days=dias))
        typ, data = m.search(None, "SINCE", desde, "FROM", "netflix")
        ids = data[0].split()[-limite:] if data and data[0] else []
        out = []
        for num in reversed(ids):
            typ, d = m.fetch(num, "(BODY.PEEK[])")
            if typ != "OK" or not d or not isinstance(d[0], tuple):
                continue
            msg = message_from_bytes(d[0][1])
            remit = _decode(msg.get("From", ""))
            if "netflix.com" not in remit.lower():
                continue
            asunto = _decode(msg.get("Subject", ""))
            try:
                fecha = parsedate_to_datetime(msg.get("Date"))
                if fecha.tzinfo is None:
                    fecha = fecha.replace(tzinfo=timezone.utc)
            except Exception:
                fecha = datetime.now(timezone.utc)
            texto, links = _extraer(msg)
            msgid = (msg.get("Message-ID") or "").strip() or f"{asunto}|{fecha.isoformat()}"
            out.append({"msgid": msgid, "asunto": asunto, "fecha": fecha, "texto": texto, "links": links})
        return out
    finally:
        try:
            m.logout()
        except Exception:
            pass


# ───────────────────────── CLASIFICACIÓN DE CORREOS ─────────────────────────
# Ajusta estas listas si Netflix cambia los textos. Se comparan sin tildes y en minúsculas.


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


KEYWORDS = [
    ("hogar", [
        "hogar con netflix", "hogar de netflix", "actualizar tu hogar", "codigo de acceso temporal",
        "netflix household", "update your netflix household", "temporary access code",
        "not part of your household", "no forma parte de tu hogar",
    ]),
    ("login", [
        "codigo de inicio de sesion", "sign-in code", "sign in code", "login code",
        "codigo de acceso para iniciar", "codigo de verificacion de inicio",
    ]),
    ("correo", [
        "cambio de correo", "cambio de email", "cambiar tu correo", "cambiar tu email",
        "verifica tu correo", "verifica tu email", "confirma tu correo", "confirma tu email",
        "confirmar tu correo", "verify your email", "confirm your email", "email change",
        "change your email", "change of email",
    ]),
    ("suspendida", [
        "cuenta suspendida", "hemos suspendido", "suspendimos", "cuenta bloqueada", "bloqueamos",
        "account suspended", "account locked", "account on hold", "membresia cancelada",
        "membresia ha sido cancelada", "cancelamos tu membresia", "se ha cancelado tu membresia",
        "tu membresia ha finalizado", "membership canceled", "membership cancelled",
        "membership has ended", "actividad inusual", "actividad sospechosa", "unusual activity",
        "suspicious activity", "acceso no autorizado", "tu cuenta ha sido cerrada",
    ]),
    ("pago_fallido", [
        "no pudimos procesar", "problema con tu pago", "problema con el pago", "problema de pago",
        "problema al procesar", "pago rechazado", "actualiza tu metodo de pago",
        "actualiza tu informacion de pago", "actualiza la informacion de pago", "no se pudo procesar",
        "no pudimos cobrar", "payment failed", "payment issue", "problem with your payment",
        "unable to process", "update your payment", "declined",
    ]),
    ("reembolso", ["reembolso", "refund", "reintegro", "devolucion de tu pago"]),
    ("pago_ok", [
        "recibo", "receipt", "pago recibido", "hemos recibido tu pago", "gracias por tu pago",
        "pago exitoso", "payment received", "thanks for your payment", "thank you for your payment",
        "factura", "invoice",
    ]),
    ("cobro", [
        "proximo cobro", "proxima fecha de facturacion", "fecha de facturacion", "proximo pago",
        "cambio de precio", "aumento de precio", "se renovara", "renovacion de tu membresia",
        "next billing date", "next payment", "price change", "will renew", "renewal",
    ]),
]
ALERTAS = {"suspendida", "pago_fallido", "reembolso", "pago_ok", "cobro"}


def clasificar(asunto, texto):
    s, b = norm(asunto), norm(texto[:2000])
    for tipo, kws in KEYWORDS:
        if any(k in s for k in kws):
            return tipo
    for tipo, kws in KEYWORDS:
        if any(k in b for k in kws):
            return tipo
    return None


def extraer_codigo(texto):
    t = norm(texto)
    m = re.search(r"(?:codigo|code)[^0-9]{0,80}?(\d{4,8})\b", t)
    if m:
        return m.group(1)
    t2 = re.sub(r"\b(?:19|20)\d{2}\b", " ", t[:600])
    m = re.search(r"\b(\d{4}|\d{6})\b", t2)
    return m.group(1) if m else None


def elegir_links(links, claves):
    out = []
    for c in claves:
        for l in links:
            if "netflix.com" in l.lower() and c in l.lower() and l not in out:
                out.append(l)
    return out[:2]


MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def extraer_fecha(texto):
    t = norm(texto)
    hallados = []
    for m in re.finditer(r"(\d{1,2}) de ([a-z]+)(?: de)? (\d{4})", t):
        if m.group(2) in MESES:
            hallados.append((m.start(), (int(m.group(3)), MESES[m.group(2)], int(m.group(1)))))
    for m in re.finditer(r"([a-z]+) (\d{1,2}),? (\d{4})", t):
        if m.group(1) in MESES:
            hallados.append((m.start(), (int(m.group(3)), MESES[m.group(1)], int(m.group(2)))))
    for m in re.finditer(r"(\d{1,2})/(\d{1,2})/(\d{4})", t):
        hallados.append((m.start(), (int(m.group(3)), int(m.group(2)), int(m.group(1)))))
    for m in re.finditer(r"(\d{4})-(\d{2})-(\d{2})", t):
        hallados.append((m.start(), (int(m.group(1)), int(m.group(2)), int(m.group(3)))))
    for _, (y, mo, d) in sorted(hallados):
        try:
            return date(y, mo, d)
        except ValueError:
            continue
    return None


ETIQUETAS = {
    "login": "🔑 Código de inicio de sesión",
    "hogar": "🏠 Código / enlace de Hogar",
    "correo": "✉️ Cambio de correo / verificación",
}


def buscar_codigos(c, tipos):
    msgs = imap_fetch(c["imap_host"], c["correo"], c["imap_clave"], dias=2, limite=25)
    ahora = datetime.now(timezone.utc)
    res = {t: None for t in tipos}
    for m in msgs:  # más nuevos primero
        t = clasificar(m["asunto"], m["texto"])
        if t in res and res[t] is None:
            if t == "login":
                codigo, links = extraer_codigo(m["texto"]), []
            elif t == "hogar":
                codigo, links = None, elegir_links(m["links"], ("update-primary-location", "travel", "household", "verify"))
            else:
                codigo = extraer_codigo(m["texto"])
                links = elegir_links(m["links"], ("verifyemail", "verify", "confirm", "emailchange", "changeemail"))
            res[t] = {
                "codigo": codigo,
                "links": links,
                "asunto": m["asunto"],
                "edad_min": (ahora - m["fecha"]).total_seconds() / 60,
            }
    return res


def fmt_res(tipo, r):
    t = ETIQUETAS[tipo]
    if not r:
        return f"{t}\n   — No encontré correos de este tipo en los últimos 2 días."
    mins = r["edad_min"]
    if mins < 60:
        edad = f"hace {int(mins)} min"
    elif mins < 1440:
        edad = f"hace {mins / 60:.1f} h"
    else:
        edad = f"hace {mins / 1440:.1f} días"
    vigente = mins <= CODE_MAX_AGE_MIN
    s = f"{t}\n   {'✅' if vigente else '⚠️'} {edad}{'' if vigente else ' (probablemente vencido)'}"
    if r["codigo"]:
        s += f"\n   Código: <code>{e(r['codigo'])}</code>"
    for l in r["links"]:
        s += f"\n   🔗 {e(l)}"
    if not r["codigo"] and not r["links"]:
        s += "\n   (Encontré el correo pero no pude leer el código/enlace; revísalo manualmente)"
    return s


# ───────────────────────── UTILIDADES BOT ─────────────────────────
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def solo_admin(fn):
    @wraps(fn)
    async def w(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **k):
        u = update.effective_user
        if not u or u.id not in ADMIN_IDS:
            if update.callback_query:
                await update.callback_query.answer("⛔ No autorizado", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text(f"⛔ No autorizado. Tu ID de Telegram es {u.id if u else '?'}.")
            return ConversationHandler.END
        return await fn(update, context, *a, **k)

    return w


async def avisar(bot, texto):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, texto, **NOPREV)
        except Exception as ex:
            log.warning("No pude avisar a %s: %s", aid, ex)


async def borrar_mensaje(update):
    try:
        await update.message.delete()
    except Exception:
        pass


def buscar_cuenta(arg):
    arg = (arg or "").strip().lstrip("#")
    if not arg:
        return None
    if arg.isdigit():
        return q("SELECT * FROM cuentas WHERE id=?", (int(arg),), "one")
    r = q("SELECT * FROM cuentas WHERE lower(correo) LIKE ?", (f"%{arg.lower()}%",), "all")
    return r[0] if len(r) == 1 else None


def resumen_alta(cid, correo, ini, ven, imap_clave):
    dl = (ven - hoy()).days
    return (
        f"✅ <b>Cuenta #{cid} registrada</b>\n"
        f"📧 {e(correo)}\n"
        f"🗓 Inicio: {fmt(ini)}\n"
        f"⏳ Vence: {fmt(ven)} ({txt_dias(dl)})\n"
        f"📬 IMAP: {'activo' if imap_clave else 'sin clave (usa /editar_imap para activarlo)'}"
    )


def crear_cuenta(correo, clave, imap_clave, modo, valor=None):
    if q("SELECT id FROM cuentas WHERE lower(correo)=?", (correo.lower(),), "one"):
        raise ValueError("Ese correo ya está registrado.")
    ini, ven = calc_fechas(modo, valor)
    cid = q(
        "INSERT INTO cuentas (correo, clave, imap_clave, imap_host, inicio, vence, creada) VALUES (?,?,?,?,?,?,?)",
        (correo, clave, imap_clave or "", host_para(correo), ini.isoformat(), ven.isoformat(), hoy().isoformat()),
    )
    return cid, ini, ven


AYUDA = (
    "<b>🎬 Gestor de cuentas Netflix</b>\n\n"
    "<b>Registro</b>\n"
    "/agregar — asistente guiado (correo, clave, fecha)\n"
    "/agregar correo clave [clave_imap] — rápido, vence en 30 días desde hoy\n"
    "/agregar correo clave clave_imap hace:4 — ya lleva 4 días activa\n"
    "/agregar correo clave clave_imap inicio:28/10/2026\n"
    "/agregar correo clave clave_imap restan:26 — le quedan 26 días\n"
    "(usa <code>-</code> como clave_imap si no quieres activar IMAP)\n\n"
    "<b>Gestión</b>\n"
    "/listar · /ver ID · /eliminar ID\n"
    "/editar_correo ID nuevo@correo.com [clave_imap]\n"
    "/editar_clave ID nueva_clave\n"
    "/editar_imap ID clave [servidor] (<code>-</code> la desactiva)\n"
    "/fecha ID hoy | hace:N | inicio:DD/MM/AAAA | restan:N\n\n"
    "<b>Códigos</b>\n"
    "/codigo [ID o correo] — inicio de sesión, Hogar, cambio de correo\n\n"
    "/id — muestra tu ID de Telegram · /cancelar"
)

# ───────────────────────── COMANDOS BÁSICOS ─────────────────────────


async def cmd_id(update, ctx):
    await update.message.reply_text(f"Tu ID de Telegram: {update.effective_user.id}")


@solo_admin
async def cmd_start(update, ctx):
    await update.message.reply_text(AYUDA, **NOPREV)


@solo_admin
async def listar(update, ctx):
    cs = q("SELECT * FROM cuentas ORDER BY vence ASC", fetch="all")
    if not cs:
        await update.message.reply_text("No hay cuentas registradas. Usa /agregar.")
        return
    bloques = []
    for c in cs:
        dl = dias_restantes(c)
        imap = "✅" if c["imap_clave"] else "❌ sin clave"
        if c["estado"] == "imap_error":
            imap = "⚠️ error de conexión"
        bloques.append(
            f"{emoji_dias(dl)} <b>#{c['id']}</b> {e(c['correo'])}\n"
            f"   Inicio {fmt(date.fromisoformat(c['inicio']))} · Vence {fmt(date.fromisoformat(c['vence']))} ({txt_dias(dl)})\n"
            f"   IMAP: {imap}"
        )
    texto = f"📋 <b>Cuentas ({len(cs)})</b>\n\n"
    for b in bloques:
        if len(texto) + len(b) > 3800:
            await update.message.reply_text(texto, **NOPREV)
            texto = ""
        texto += b + "\n\n"
    if texto.strip():
        await update.message.reply_text(texto, **NOPREV)


@solo_admin
async def ver(update, ctx):
    c = buscar_cuenta(" ".join(ctx.args))
    if not c:
        await update.message.reply_text("Uso: /ver ID (o parte del correo)")
        return
    dl = dias_restantes(c)
    await update.message.reply_text(
        f"<b>Cuenta #{c['id']}</b>\n📧 {e(c['correo'])}\n🔑 Clave Netflix: <code>{e(c['clave'])}</code>\n"
        f"🗓 Inicio: {fmt(date.fromisoformat(c['inicio']))}\n⏳ Vence: {fmt(date.fromisoformat(c['vence']))} ({txt_dias(dl)})\n"
        f"📬 IMAP: {e(c['imap_host'])} · {'con clave' if c['imap_clave'] else 'sin clave'}",
        **NOPREV,
    )


# ───────────────────────── /agregar ─────────────────────────
EMAIL, CLAVE, IMAPCLAVE, FECHA_MODO, FECHA_VALOR = range(5)


@solo_admin
async def agregar_inicio(update, ctx):
    ctx.user_data.pop("nuevo", None)
    if not ctx.args:
        await update.message.reply_text("➕ Nueva cuenta.\nEscribe el <b>correo</b> de la cuenta Netflix (o /cancelar):", **NOPREV)
        return EMAIL
    opcion, pos = None, []
    for t in ctx.args:
        if ":" in t and t.split(":", 1)[0].lower() in ("hace", "inicio", "restan"):
            k, v = t.split(":", 1)
            opcion = (k.lower(), v)
        else:
            pos.append(t)
    if len(pos) < 2:
        await update.message.reply_text("Uso: /agregar correo clave [clave_imap] [hace:N | inicio:DD/MM/AAAA | restan:N]\nO escribe /agregar solo para el asistente.")
        return ConversationHandler.END
    correo, clave = pos[0], pos[1]
    imap = pos[2].replace(" ", "") if len(pos) > 2 and pos[2] != "-" else ""
    if not EMAIL_RE.match(correo):
        await update.message.reply_text("⚠️ Ese correo no parece válido.")
        return ConversationHandler.END
    modo, valor = opcion or ("hoy", None)
    try:
        cid, ini, ven = crear_cuenta(correo, clave, imap, modo, valor)
    except ValueError as ex:
        await update.message.reply_text(f"⚠️ {ex}")
        return ConversationHandler.END
    await borrar_mensaje(update)
    await update.effective_chat.send_message(resumen_alta(cid, correo, ini, ven, imap), **NOPREV)
    return ConversationHandler.END


@solo_admin
async def paso_email(update, ctx):
    correo = update.message.text.strip()
    if not EMAIL_RE.match(correo):
        await update.message.reply_text("⚠️ Correo no válido. Inténtalo de nuevo o /cancelar.")
        return EMAIL
    if q("SELECT id FROM cuentas WHERE lower(correo)=?", (correo.lower(),), "one"):
        await update.message.reply_text("⚠️ Ese correo ya está registrado. Escribe otro o /cancelar.")
        return EMAIL
    ctx.user_data["nuevo"] = {"correo": correo}
    await update.message.reply_text("🔑 Escribe la <b>contraseña de Netflix</b> de esa cuenta:", **NOPREV)
    return CLAVE


@solo_admin
async def paso_clave(update, ctx):
    ctx.user_data["nuevo"]["clave"] = update.message.text.strip()
    await borrar_mensaje(update)
    await update.effective_chat.send_message(
        "📬 Escribe la <b>clave IMAP</b> del correo (en Gmail: contraseña de aplicación).\nEnvía <code>-</code> para omitir.",
        **NOPREV,
    )
    return IMAPCLAVE


@solo_admin
async def paso_imap(update, ctx):
    t = update.message.text.strip()
    ctx.user_data["nuevo"]["imap"] = "" if t == "-" else t.replace(" ", "")
    await borrar_mensaje(update)
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📅 Hoy (30 días)", callback_data="f:hoy")],
            [InlineKeyboardButton("⏳ Ya tiene días activa", callback_data="f:hace")],
            [InlineKeyboardButton("🗓 Fecha de inicio", callback_data="f:inicio")],
            [InlineKeyboardButton("⌛ Días restantes", callback_data="f:restan")],
        ]
    )
    await update.effective_chat.send_message("¿Cómo registramos la fecha?", reply_markup=kb)
    return FECHA_MODO


@solo_admin
async def paso_modo(update, ctx):
    qy = update.callback_query
    await qy.answer()
    modo = qy.data.split(":")[1]
    n = ctx.user_data.get("nuevo")
    if not n:
        await qy.edit_message_text("La sesión expiró. Usa /agregar otra vez.")
        return ConversationHandler.END
    if modo == "hoy":
        try:
            cid, ini, ven = crear_cuenta(n["correo"], n["clave"], n["imap"], "hoy")
        except ValueError as ex:
            await qy.edit_message_text(f"⚠️ {ex}")
            return ConversationHandler.END
        await qy.edit_message_text(resumen_alta(cid, n["correo"], ini, ven, n["imap"]), **NOPREV)
        return ConversationHandler.END
    ctx.user_data["modo"] = modo
    pregunta = {
        "hace": "¿Hace cuántos días se creó/activó la cuenta? (ej: 4)",
        "inicio": "Escribe la fecha de inicio (DD/MM/AAAA):",
        "restan": "¿Cuántos días le quedan? (ej: 26)",
    }[modo]
    await qy.edit_message_text(pregunta)
    return FECHA_VALOR


@solo_admin
async def paso_valor(update, ctx):
    n = ctx.user_data.get("nuevo")
    if not n:
        await update.message.reply_text("La sesión expiró. Usa /agregar otra vez.")
        return ConversationHandler.END
    try:
        cid, ini, ven = crear_cuenta(n["correo"], n["clave"], n["imap"], ctx.user_data["modo"], update.message.text)
    except ValueError as ex:
        await update.message.reply_text(f"⚠️ {ex}\nInténtalo de nuevo o /cancelar.")
        return FECHA_VALOR
    await update.message.reply_text(resumen_alta(cid, n["correo"], ini, ven, n["imap"]), **NOPREV)
    ctx.user_data.clear()
    return ConversationHandler.END


async def cancelar(update, ctx):
    ctx.user_data.clear()
    await update.message.reply_text("Cancelado.")
    return ConversationHandler.END


# ───────────────────────── ELIMINAR / EDITAR ─────────────────────────


@solo_admin
async def eliminar(update, ctx):
    c = buscar_cuenta(" ".join(ctx.args))
    if not c:
        await update.message.reply_text("Uso: /eliminar ID (o parte del correo). Mira los ID con /listar.")
        return
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🗑 Sí, eliminar", callback_data=f"del:{c['id']}:si"),
          InlineKeyboardButton("Cancelar", callback_data=f"del:{c['id']}:no")]]
    )
    await update.message.reply_text(f"¿Eliminar la cuenta #{c['id']} ({c['correo']})?", reply_markup=kb)


@solo_admin
async def cb_eliminar(update, ctx):
    qy = update.callback_query
    await qy.answer()
    _, cid, resp = qy.data.split(":")
    if resp == "si":
        q("DELETE FROM eventos WHERE cuenta_id=?", (int(cid),))
        q("DELETE FROM cuentas WHERE id=?", (int(cid),))
        await qy.edit_message_text(f"🗑 Cuenta #{cid} eliminada.")
    else:
        await qy.edit_message_text("Cancelado.")


@solo_admin
async def editar_correo(update, ctx):
    a = ctx.args
    if len(a) < 2:
        await update.message.reply_text("Uso: /editar_correo ID nuevo@correo.com [clave_imap_nueva]")
        return
    c = buscar_cuenta(a[0])
    if not c:
        await update.message.reply_text("No encontré esa cuenta. Revisa /listar.")
        return
    nuevo = a[1].strip()
    if not EMAIL_RE.match(nuevo):
        await update.message.reply_text("⚠️ Ese correo no parece válido.")
        return
    if q("SELECT id FROM cuentas WHERE lower(correo)=? AND id!=?", (nuevo.lower(), c["id"]), "one"):
        await update.message.reply_text("⚠️ Ya existe otra cuenta con ese correo.")
        return
    imap = a[2].replace(" ", "") if len(a) > 2 else c["imap_clave"]
    q(
        "UPDATE cuentas SET correo=?, imap_host=?, imap_clave=?, baseline=0, estado='ok' WHERE id=?",
        (nuevo, host_para(nuevo), imap, c["id"]),
    )
    q("DELETE FROM eventos WHERE cuenta_id=?", (c["id"],))
    extra = "" if len(a) > 2 else "\n⚠️ Si la clave IMAP del correo nuevo es distinta, actualízala con /editar_imap."
    await borrar_mensaje(update)
    await update.effective_chat.send_message(f"✅ Cuenta #{c['id']}: {c['correo']} → {nuevo}{extra}")


@solo_admin
async def editar_clave(update, ctx):
    if len(ctx.args) < 2:
        await update.message.reply_text("Uso: /editar_clave ID nueva_clave")
        return
    c = buscar_cuenta(ctx.args[0])
    if not c:
        await update.message.reply_text("No encontré esa cuenta.")
        return
    q("UPDATE cuentas SET clave=? WHERE id=?", (" ".join(ctx.args[1:]), c["id"]))
    await borrar_mensaje(update)
    await update.effective_chat.send_message(f"✅ Clave de Netflix actualizada (cuenta #{c['id']}).")


@solo_admin
async def editar_imap(update, ctx):
    if len(ctx.args) < 2:
        await update.message.reply_text("Uso: /editar_imap ID clave [servidor_imap]  (clave <code>-</code> = desactivar)", **NOPREV)
        return
    c = buscar_cuenta(ctx.args[0])
    if not c:
        await update.message.reply_text("No encontré esa cuenta.")
        return
    clave = "" if ctx.args[1] == "-" else ctx.args[1].replace(" ", "")
    host = ctx.args[2] if len(ctx.args) > 2 else c["imap_host"]
    q("UPDATE cuentas SET imap_clave=?, imap_host=?, baseline=0, estado='ok' WHERE id=?", (clave, host, c["id"]))
    await borrar_mensaje(update)
    await update.effective_chat.send_message(f"✅ IMAP de la cuenta #{c['id']}: {'activo en ' + host if clave else 'desactivado'}.")


@solo_admin
async def cmd_fecha(update, ctx):
    uso = "Uso: /fecha ID hoy | hace:N | inicio:DD/MM/AAAA | restan:N"
    if len(ctx.args) < 2:
        await update.message.reply_text(uso)
        return
    c = buscar_cuenta(ctx.args[0])
    if not c:
        await update.message.reply_text("No encontré esa cuenta.")
        return
    t = ctx.args[1]
    if t.lower() == "hoy":
        modo, valor = "hoy", None
    elif ":" in t and t.split(":", 1)[0].lower() in ("hace", "inicio", "restan"):
        modo, valor = t.split(":", 1)[0].lower(), t.split(":", 1)[1]
    else:
        await update.message.reply_text(uso)
        return
    try:
        ini, ven = calc_fechas(modo, valor)
    except ValueError as ex:
        await update.message.reply_text(f"⚠️ {ex}")
        return
    q("UPDATE cuentas SET inicio=?, vence=? WHERE id=?", (ini.isoformat(), ven.isoformat(), c["id"]))
    await update.message.reply_text(f"✅ Cuenta #{c['id']}: inicio {fmt(ini)} · vence {fmt(ven)} ({txt_dias((ven - hoy()).days)})")


# ───────────────────────── /codigo ─────────────────────────


def teclado_tipos(cid):
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔑 Inicio de sesión", callback_data=f"cod:{cid}:login"),
             InlineKeyboardButton("🏠 Hogar", callback_data=f"cod:{cid}:hogar")],
            [InlineKeyboardButton("✉️ Cambio de correo", callback_data=f"cod:{cid}:correo")],
            [InlineKeyboardButton("🔎 Todos", callback_data=f"cod:{cid}:todos")],
        ]
    )


@solo_admin
async def codigo_cmd(update, ctx):
    if not ctx.args:
        cs = q("SELECT id, correo FROM cuentas WHERE imap_clave != '' ORDER BY id", fetch="all")
        if not cs:
            await update.message.reply_text("No hay cuentas con IMAP activo. Usa /editar_imap.")
            return
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"#{c['id']} {c['correo']}", callback_data=f"cuenta:{c['id']}")] for c in cs])
        await update.message.reply_text("¿De qué cuenta?", reply_markup=kb)
        return
    c = buscar_cuenta(" ".join(ctx.args))
    if not c:
        await update.message.reply_text("No encontré esa cuenta (o hay varias coincidencias). Usa el ID de /listar.")
        return
    await update.message.reply_text(f"#{c['id']} {c['correo']}\n¿Qué código necesitas?", reply_markup=teclado_tipos(c["id"]))


@solo_admin
async def cb_cuenta(update, ctx):
    qy = update.callback_query
    await qy.answer()
    cid = int(qy.data.split(":")[1])
    c = q("SELECT * FROM cuentas WHERE id=?", (cid,), "one")
    if not c:
        await qy.edit_message_text("Esa cuenta ya no existe.")
        return
    await qy.edit_message_text(f"#{c['id']} {c['correo']}\n¿Qué código necesitas?", reply_markup=teclado_tipos(cid))


@solo_admin
async def cb_codigo(update, ctx):
    qy = update.callback_query
    await qy.answer()
    _, cid, tipo = qy.data.split(":")
    c = q("SELECT * FROM cuentas WHERE id=?", (int(cid),), "one")
    if not c:
        await qy.edit_message_text("Esa cuenta ya no existe.")
        return
    if not c["imap_clave"]:
        await qy.edit_message_text("Esa cuenta no tiene clave IMAP. Usa /editar_imap.")
        return
    tipos = ["login", "hogar", "correo"] if tipo == "todos" else [tipo]
    await qy.edit_message_text(f"🔎 Buscando en {c['correo']}…" + (f"\n(espero hasta {WAIT_SECONDS}s si aún no llega)" if len(tipos) == 1 else ""))
    limite = asyncio.get_running_loop().time() + WAIT_SECONDS
    res = {}
    try:
        while True:
            res = await asyncio.to_thread(buscar_codigos, c, tipos)
            listo = all(r and r["edad_min"] <= CODE_MAX_AGE_MIN for r in res.values())
            if listo or len(tipos) > 1 or asyncio.get_running_loop().time() >= limite:
                break
            await asyncio.sleep(6)
    except Exception as ex:
        log.warning("IMAP %s: %s", c["correo"], ex)
        await qy.edit_message_text(
            f"❌ No pude entrar por IMAP a {c['correo']}.\n{ex}\n\nRevisa la clave (en Gmail: contraseña de aplicación), "
            "que IMAP esté habilitado y el servidor (/editar_imap).",
        )
        return
    texto = f"<b>#{c['id']} {e(c['correo'])}</b>\n\n" + "\n\n".join(fmt_res(t, res.get(t)) for t in tipos)
    await qy.edit_message_text(texto, reply_markup=teclado_tipos(c["id"]), **NOPREV)


# ───────────────────────── TAREAS PROGRAMADAS ─────────────────────────


async def procesar_alerta(context, c, m, tipo):
    cid, correo, asunto = c["id"], e(c["correo"]), e(m["asunto"][:120])
    if tipo == "suspendida":
        txt = f"🚨 <b>Posible caída / bloqueo</b>\n#{cid} {correo}\nAsunto: {asunto}"
    elif tipo == "pago_fallido":
        txt = f"❌ <b>Falló la renovación / problema de pago</b>\n#{cid} {correo}\nAsunto: {asunto}"
    elif tipo == "reembolso":
        txt = f"💸 <b>Reembolso detectado</b>\n#{cid} {correo}\nAsunto: {asunto}"
    elif tipo == "pago_ok":
        f = m["fecha"].astimezone(TZ).date()
        txt = f"✅ <b>Renovación procesada</b>\n#{cid} {correo}\nPago detectado: {fmt(f)}"
        if AUTO_EXTENDER:
            nueva, actual = f + timedelta(days=VIGENCIA_DIAS), date.fromisoformat(c["vence"])
            if nueva > actual:
                q("UPDATE cuentas SET inicio=?, vence=? WHERE id=?", (f.isoformat(), nueva.isoformat(), cid))
                txt += f"\nNuevo vencimiento: {fmt(nueva)}"
    else:  # cobro
        d = extraer_fecha(m["texto"])
        txt = f"📅 <b>Aviso de cobro / fecha de pago</b>\n#{cid} {correo}\n" + (
            f"Fecha indicada en el correo: {fmt(d)}" if d else "No pude leer la fecha; revisa el correo."
        ) + f"\nAsunto: {asunto}"
    await avisar(context.bot, txt)


async def monitor_job(context: ContextTypes.DEFAULT_TYPE):
    cuentas = q("SELECT * FROM cuentas WHERE imap_clave != ''", fetch="all")
    for c in cuentas:
        try:
            msgs = await asyncio.to_thread(imap_fetch, c["imap_host"], c["correo"], c["imap_clave"], 3, 30)
        except Exception as ex:
            log.warning("IMAP %s: %s", c["correo"], ex)
            if c["estado"] != "imap_error":
                q("UPDATE cuentas SET estado='imap_error' WHERE id=?", (c["id"],))
                await avisar(context.bot, f"⚠️ No puedo leer el correo de #{c['id']} {e(c['correo'])}\n{e(str(ex))[:200]}")
            continue
        if c["estado"] == "imap_error":
            q("UPDATE cuentas SET estado='ok' WHERE id=?", (c["id"],))
            await avisar(context.bot, f"✅ IMAP restablecido en #{c['id']} {e(c['correo'])}")
        if not c["baseline"]:  # primera lectura: guarda lo existente sin alertar
            for m in msgs:
                q("INSERT OR IGNORE INTO eventos (cuenta_id, msgid, tipo) VALUES (?,?,?)",
                  (c["id"], m["msgid"], clasificar(m["asunto"], m["texto"])))
            q("UPDATE cuentas SET baseline=1 WHERE id=?", (c["id"],))
            continue
        for m in reversed(msgs):  # del más viejo al más nuevo
            if q("SELECT 1 FROM eventos WHERE cuenta_id=? AND msgid=?", (c["id"], m["msgid"]), "one"):
                continue
            tipo = clasificar(m["asunto"], m["texto"])
            q("INSERT OR IGNORE INTO eventos (cuenta_id, msgid, tipo) VALUES (?,?,?)", (c["id"], m["msgid"], tipo))
            if tipo in ALERTAS:
                fresca = q("SELECT * FROM cuentas WHERE id=?", (c["id"],), "one")
                await procesar_alerta(context, fresca, m, tipo)


async def recordatorios_job(context: ContextTypes.DEFAULT_TYPE):
    for c in q("SELECT * FROM cuentas ORDER BY vence", fetch="all"):
        dl = dias_restantes(c)
        if dl in (3, 1, 0, -1):
            await avisar(
                context.bot,
                f"⏰ <b>#{c['id']}</b> {e(c['correo'])}\nFecha de pago/vencimiento: {fmt(date.fromisoformat(c['vence']))} ({txt_dias(dl)})",
            )


# ───────────────────────── ARRANQUE ─────────────────────────


async def post_init(app: Application):
    await app.bot.set_my_commands(
        [
            BotCommand("agregar", "Registrar una cuenta"),
            BotCommand("listar", "Ver cuentas y vencimientos"),
            BotCommand("codigo", "Pedir código de Netflix"),
            BotCommand("ver", "Detalle de una cuenta"),
            BotCommand("eliminar", "Eliminar una cuenta"),
            BotCommand("editar_correo", "Cambiar el correo de una cuenta"),
            BotCommand("editar_clave", "Cambiar clave de Netflix"),
            BotCommand("editar_imap", "Cambiar clave/servidor IMAP"),
            BotCommand("fecha", "Corregir fecha de una cuenta"),
            BotCommand("ayuda", "Ayuda"),
        ]
    )


def main():
    if not TOKEN:
        raise SystemExit("Falta la variable de entorno BOT_TOKEN")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS vacío: nadie podrá usar el bot. Usa /id para ver tu ID y ponlo en ADMIN_IDS.")
    init_db()
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    txt = filters.TEXT & ~filters.COMMAND
    conv = ConversationHandler(
        entry_points=[CommandHandler("agregar", agregar_inicio)],
        states={
            EMAIL: [MessageHandler(txt, paso_email)],
            CLAVE: [MessageHandler(txt, paso_clave)],
            IMAPCLAVE: [MessageHandler(txt, paso_imap)],
            FECHA_MODO: [CallbackQueryHandler(paso_modo, pattern=r"^f:")],
            FECHA_VALOR: [MessageHandler(txt, paso_valor)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        allow_reentry=True,
        conversation_timeout=900,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler(["start", "ayuda"], cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("listar", listar))
    app.add_handler(CommandHandler("ver", ver))
    app.add_handler(CommandHandler("eliminar", eliminar))
    app.add_handler(CommandHandler("editar_correo", editar_correo))
    app.add_handler(CommandHandler("editar_clave", editar_clave))
    app.add_handler(CommandHandler("editar_imap", editar_imap))
    app.add_handler(CommandHandler("fecha", cmd_fecha))
    app.add_handler(CommandHandler("codigo", codigo_cmd))
    app.add_handler(CallbackQueryHandler(cb_eliminar, pattern=r"^del:\d+:(si|no)$"))
    app.add_handler(CallbackQueryHandler(cb_cuenta, pattern=r"^cuenta:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_codigo, pattern=r"^cod:\d+:\w+$"))

    app.job_queue.run_repeating(monitor_job, interval=CHECK_MINUTES * 60, first=20, name="monitor")
    app.job_queue.run_daily(recordatorios_job, time=dtime(9, 0, tzinfo=TZ), name="recordatorios")

    log.info("Bot iniciado. Revisión IMAP cada %s min.", CHECK_MINUTES)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
