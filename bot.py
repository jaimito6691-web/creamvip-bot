"""
╔══════════════════════════════════════╗
║      CREAM VIP — Telegram Bot        ║
║  Stars Tracker · by CREAM VIP        ║
╚══════════════════════════════════════╝
"""

import os, json, logging, asyncio
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes,
    filters
)

# ─── CONFIG ───────────────────────────────────────────────────
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "")
OWNER_ID   = int(os.environ.get("OWNER_ID", "0"))   # Tu Telegram user ID
TIMEZONE   = ZoneInfo("America/Lima")                # Cambia si es otro país
NOTIFY_H   = 19   # 7 PM
NOTIFY_M   = 17   # :17
RATE       = 0.013
WAIT_DAYS  = 21
DATA_FILE  = "data.json"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ─── CONVERSATION STATES ──────────────────────────────────────
ADD_DATE, ADD_STARS, ADD_NOTE, ADD_TAG = range(4)
WITHDRAW_DATE, WITHDRAW_STARS         = range(4, 6)

# ─── TAGS ─────────────────────────────────────────────────────
TAGS = {
    "donacion":    ("💝", "Donación"),
    "suscripcion": ("💜", "Suscripción"),
    "contenido":   ("🎬", "Contenido"),
    "propina":     ("💵", "Propina"),
    "otro":        ("✨", "Otro"),
}

# ─── LEVELS ───────────────────────────────────────────────────
LEVELS = [
    (0,    100,  "🥉 Bronce"),
    (100,  500,  "🥈 Plata"),
    (500,  1000, "🥇 Oro"),
    (1000, 5000, "💎 Diamante"),
    (5000, 9e9,  "👑 Élite"),
]

# ─── DATA LAYER ───────────────────────────────────────────────
def load() -> dict:
    try:
        with open(DATA_FILE) as f:
            return json.load(f)
    except Exception:
        return {"entries": [], "withdrawals": [], "name": "", "goal_month": 0, "goal_week": 0}

def dump(data: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def total_stars(data):
    return sum(e["stars"] for e in data["entries"])

def due_date(entry_date: str) -> datetime:
    d = datetime.fromisoformat(entry_date + "T12:00:00")
    return d + timedelta(days=WAIT_DAYS)

def days_left(entry_date: str) -> int:
    return max(0, (due_date(entry_date).date() - datetime.now(TIMEZONE).date()).days)

def is_ready(entry_date: str) -> bool:
    now = datetime.now(TIMEZONE)
    due = due_date(entry_date).replace(
        hour=NOTIFY_H, minute=NOTIFY_M, tzinfo=TIMEZONE
    )
    return now >= due

def get_level(usd: float) -> str:
    for mn, mx, name in LEVELS:
        if mn <= usd < mx:
            return name
    return LEVELS[-1][2]

def streak(entries: list) -> int:
    if not entries:
        return 0
    days_with_entry = set(e["date"] for e in entries)
    s = 0
    d = datetime.now(TIMEZONE).date()
    while True:
        if d.isoformat() in days_with_entry:
            s += 1
            d -= timedelta(days=1)
        else:
            break
    return s

# ─── KEYBOARDS ────────────────────────────────────────────────
def main_kb():
    return ReplyKeyboardMarkup([
        ["⭐ Agregar ingreso",  "📅 Mi calendario"],
        ["📊 Estadísticas",     "💰 Mis retiros"],
        ["🏠 Resumen",          "⚙️ Ajustes"],
    ], resize_keyboard=True)

def tag_kb():
    buttons = []
    for tid, (icon, label) in TAGS.items():
        buttons.append(InlineKeyboardButton(f"{icon} {label}", callback_data=f"tag:{tid}"))
    rows = [buttons[:3], buttons[3:]]
    rows.append([InlineKeyboardButton("❌ Cancelar", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)

def confirm_kb(action: str):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Sí", callback_data=f"confirm:{action}"),
        InlineKeyboardButton("❌ No", callback_data="cancel"),
    ]])

# ─── MESSAGES ─────────────────────────────────────────────────
def render_summary(data: dict, short=False) -> str:
    entries = data["entries"]
    total   = total_stars(data)
    usd     = total * RATE
    ready   = [e for e in entries if is_ready(e["date"])]
    pending = [e for e in entries if not is_ready(e["date"])]
    nxt     = sorted(pending, key=lambda e: e["date"])
    level   = get_level(usd)
    st      = streak(entries)
    name    = data.get("name") or "Creadora"

    if short:
        return (
            f"⭐ *{total:,} Stars* — `${usd:,.2f} USD`\n"
            f"✅ Listas: *{sum(e['stars'] for e in ready):,}★*  "
            f"⏳ En espera: *{sum(e['stars'] for e in pending):,}★*"
        )

    lines = [
        f"╔══ 👑 *CREAM VIP* ══╗",
        f"",
        f"Hola, *{name}* {level}",
        f"",
        f"⭐ *{total:,} Stars*",
        f"💵 `${usd:,.2f} USD` · `S/{usd*3.72:,.2f}` · `€{usd*0.92:,.2f}`",
        f"",
        f"✅ *Listas para retirar:* {sum(e['stars'] for e in ready):,}★",
        f"⏳ *En espera (21d):*  {sum(e['stars'] for e in pending):,}★",
        f"🔥 *Racha activa:* {st} día{'s' if st!=1 else ''}",
        f"",
    ]
    if nxt:
        nxt_e = nxt[0]
        dl = days_left(nxt_e["date"])
        dd = due_date(nxt_e["date"])
        lines.append(f"⏰ *Próximo retiro:* {dd.strftime('%d %b')} a las 7:17 PM ({dl}d)")
    if data.get("goal_month"):
        now = datetime.now(TIMEZONE)
        month_stars = sum(
            e["stars"] for e in entries
            if e["date"].startswith(now.strftime("%Y-%m"))
        )
        month_usd = month_stars * RATE
        pct = min(100, int((month_usd / data["goal_month"]) * 100))
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        lines += [
            f"",
            f"🎯 *Meta mensual:* `{bar}` {pct}%",
            f"   `${month_usd:,.2f}` / `${data['goal_month']:,.2f}` USD",
        ]
    lines.append(f"\n╚══════════════════╝")
    return "\n".join(lines)


def render_calendar(data: dict) -> str:
    entries     = data["entries"]
    withdrawals = data["withdrawals"]
    now         = datetime.now(TIMEZONE)
    wdr_set     = set(w["date"] for w in withdrawals)

    # Build maps: due_date -> [entries]
    due_map: dict[str, list] = {}
    entry_map: dict[str, list] = {}
    for e in entries:
        entry_map.setdefault(e["date"], []).append(e)
        dd = due_date(e["date"]).date().isoformat()
        due_map.setdefault(dd, []).append(e)

    lines = ["╔══ 📅 *CALENDARIO DE RETIROS* ══╗", ""]

    # Show next 30 days of due dates
    upcoming = []
    for dd_str, ents in sorted(due_map.items()):
        dd_obj = datetime.fromisoformat(dd_str + "T00:00:00").date()
        today  = now.date()
        delta  = (dd_obj - today).days
        total  = sum(e["stars"] for e in ents)
        usd    = total * RATE
        all_w  = all(e["date"] in wdr_set for e in ents)

        if delta < -7:
            continue

        if delta < 0 and not all_w:
            icon = "🔴"
            when = f"Vencido ({abs(delta)}d atrás)"
        elif delta < 0 and all_w:
            icon = "✅"
            when = f"Retirado ({dd_obj.strftime('%d %b')})"
        elif delta == 0:
            icon = "🔥"
            when = f"¡HOY a las 7:17 PM!"
        elif delta <= 3:
            icon = "🟡"
            when = f"{dd_obj.strftime('%d %b')} · en {delta}d · 7:17 PM"
        else:
            icon = "🟢"
            when = f"{dd_obj.strftime('%d %b')} · en {delta}d · 7:17 PM"

        upcoming.append(
            f"{icon} *★ {total:,}* = `${usd:,.2f}` USD\n"
            f"   └─ {when}"
        )

    if upcoming:
        lines += upcoming
    else:
        lines.append("_No hay retiros próximos._\n_Agrega un ingreso para empezar._")

    lines += ["", "╚═══════════════════════╝"]
    lines += [
        "",
        "🟢 Disponible  🟡 Próximo (≤3d)",
        "🔥 Hoy  🔴 Vencido  ✅ Retirado",
    ]
    return "\n".join(lines)


def render_stats(data: dict) -> str:
    entries = data["entries"]
    now     = datetime.now(TIMEZONE)

    if not entries:
        return "📊 _Sin datos aún. Agrega tu primer ingreso._"

    total  = total_stars(data)
    usd    = total * RATE

    # This month
    m_str   = now.strftime("%Y-%m")
    m_ents  = [e for e in entries if e["date"].startswith(m_str)]
    m_stars = sum(e["stars"] for e in m_ents)
    m_usd   = m_stars * RATE

    # This week
    wk_start = (now - timedelta(days=now.weekday())).date()
    wk_ents  = [e for e in entries if datetime.fromisoformat(e["date"]).date() >= wk_start]
    wk_stars = sum(e["stars"] for e in wk_ents)

    # Best day
    day_map: dict[str, int] = {}
    for e in entries:
        day_map[e["date"]] = day_map.get(e["date"], 0) + e["stars"]
    best_day, best_val = max(day_map.items(), key=lambda x: x[1], default=("—", 0))

    # Avg per day
    avg = total // max(len(day_map), 1)

    # Projection
    day_of_month  = now.day
    days_in_month = (now.replace(month=now.month % 12 + 1, day=1) - timedelta(days=1)).day if now.month < 12 else 31
    proj_usd = (m_usd / day_of_month) * days_in_month if day_of_month > 0 else 0

    # Category breakdown
    cat_map: dict[str, int] = {}
    for e in entries:
        cat_map[e.get("tag", "otro")] = cat_map.get(e.get("tag", "otro"), 0) + e["stars"]
    top_cats = sorted(cat_map.items(), key=lambda x: -x[1])[:3]
    cat_lines = []
    for tid, sv in top_cats:
        icon, label = TAGS.get(tid, ("✨", tid))
        pct = int((sv / total) * 100)
        cat_lines.append(f"  {icon} {label}: *{sv:,}★* ({pct}%)")

    level = get_level(usd)
    st    = streak(entries)

    lines = [
        f"╔══ 📊 *ESTADÍSTICAS* ══╗",
        f"",
        f"💰 *Total:* ★{total:,} = `${usd:,.2f}`",
        f"{level}  🔥 Racha: {st}d",
        f"",
        f"📅 *Este mes:* ★{m_stars:,} = `${m_usd:,.2f}`",
        f"📈 *Proyección mes:* `${proj_usd:,.2f}`",
        f"",
        f"🗓 *Esta semana:* ★{wk_stars:,}",
        f"⭐ *Promedio/día:* ★{avg:,}",
        f"🏆 *Mejor día:* ★{best_val:,} ({best_day})",
        f"",
        f"📂 *Por categoría:*",
    ]
    lines += cat_lines
    lines += [
        f"",
        f"📝 *Ingresos registrados:* {len(entries)}",
        f"💸 *Retiros realizados:* {len(data['withdrawals'])}",
        f"",
        f"╚══════════════════╝",
    ]
    return "\n".join(lines)


# ─── COMMAND HANDLERS ─────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load()
    name = data.get("name") or update.effective_user.first_name or "Creadora"
    data["name"] = name
    dump(data)

    await update.message.reply_text(
        f"👑 *Bienvenida a CREAM VIP*, {name}!\n\n"
        f"Tu tracker personal de *Telegram Stars* ⭐\n\n"
        f"Con este bot puedes:\n"
        f"• Registrar tus ingresos de Stars\n"
        f"• Ver el calendario de cuándo retirar\n"
        f"• Recibir alertas a las *7:17 PM* cuando un retiro esté listo\n"
        f"• Ver estadísticas y metas\n\n"
        f"Usa el menú de abajo para empezar 👇",
        parse_mode="Markdown",
        reply_markup=main_kb()
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *Comandos disponibles:*\n\n"
        "/start — Menú principal\n"
        "/resumen — Ver tu resumen rápido\n"
        "/calendario — Ver fechas de retiro\n"
        "/agregar — Registrar nuevo ingreso\n"
        "/stats — Estadísticas detalladas\n"
        "/retiros — Historial de retiros\n"
        "/meta — Configurar meta mensual\n"
        "/nombre — Cambiar tu nombre\n"
        "/help — Esta ayuda\n\n"
        "O usa los botones del menú 👇",
        parse_mode="Markdown",
        reply_markup=main_kb()
    )

# ─── RESUMEN ──────────────────────────────────────────────────
async def resumen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load()
    await update.message.reply_text(
        render_summary(data),
        parse_mode="Markdown",
        reply_markup=main_kb()
    )

# ─── CALENDARIO ───────────────────────────────────────────────
async def calendario(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load()
    await update.message.reply_text(
        render_calendar(data),
        parse_mode="Markdown",
        reply_markup=main_kb()
    )

# ─── STATS ────────────────────────────────────────────────────
async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load()
    await update.message.reply_text(
        render_stats(data),
        parse_mode="Markdown",
        reply_markup=main_kb()
    )

# ─── ADD ENTRY CONVERSATION ───────────────────────────────────
async def add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    ctx.user_data["add"] = {"date": today}
    await update.message.reply_text(
        f"⭐ *Nuevo ingreso de Stars*\n\n"
        f"📅 *Fecha del ingreso:*\n"
        f"Escribe la fecha en formato `YYYY-MM-DD`\n"
        f"o escribe *hoy* para usar hoy ({today})\n\n"
        f"_Escribe /cancelar para salir_",
        parse_mode="Markdown"
    )
    return ADD_DATE

async def add_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    if text in ("hoy", "today"):
        text = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    try:
        datetime.fromisoformat(text)
    except ValueError:
        await update.message.reply_text(
            "❌ Formato inválido. Usa `YYYY-MM-DD`\n"
            "Ejemplo: `2024-03-15` o escribe *hoy*",
            parse_mode="Markdown"
        )
        return ADD_DATE

    ctx.user_data["add"]["date"] = text
    due = due_date(text)
    await update.message.reply_text(
        f"✅ Fecha: *{text}*\n"
        f"📅 Retiro disponible: *{due.strftime('%d %b %Y')}* a las *7:17 PM*\n\n"
        f"⭐ *¿Cuántas Stars recibiste?*\n"
        f"Escribe solo el número (ej: `1500`)",
        parse_mode="Markdown"
    )
    return ADD_STARS

async def add_stars(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", "").replace(".", "")
    try:
        stars = int(text)
        if stars <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Número inválido. Escribe solo dígitos, ej: `1500`",
            parse_mode="Markdown"
        )
        return ADD_STARS

    ctx.user_data["add"]["stars"] = stars
    usd = stars * RATE
    due = due_date(ctx.user_data["add"]["date"])

    await update.message.reply_text(
        f"⭐ *{stars:,} Stars* = `${usd:,.2f} USD`\n"
        f"📅 Retiro: *{due.strftime('%d %b %Y')}* · 7:17 PM\n\n"
        f"🏷 *¿Tipo de ingreso?*",
        parse_mode="Markdown",
        reply_markup=tag_kb()
    )
    return ADD_TAG

async def add_tag_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data_cb = query.data

    if data_cb == "cancel":
        await query.edit_message_text("❌ Cancelado.")
        return ConversationHandler.END

    tid = data_cb.split(":")[1]
    ctx.user_data["add"]["tag"] = tid
    icon, label = TAGS.get(tid, ("✨", "Otro"))

    await query.edit_message_text(
        f"Tipo: {icon} *{label}*\n\n"
        f"📝 *¿Alguna nota?* (ej: nombre, evento)\n"
        f"Escribe algo o envía *-* para saltar",
        parse_mode="Markdown"
    )
    return ADD_NOTE

async def add_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    note = update.message.text.strip()
    if note == "-":
        note = ""
    ctx.user_data["add"]["note"] = note

    entry = ctx.user_data["add"]
    stars = entry["stars"]
    usd   = stars * RATE
    due   = due_date(entry["date"])
    icon, label = TAGS.get(entry.get("tag", "otro"), ("✨", "Otro"))

    # Save
    data = load()
    data["entries"].append({
        "id":    int(datetime.now().timestamp() * 1000),
        "date":  entry["date"],
        "stars": stars,
        "note":  note,
        "tag":   entry.get("tag", "otro"),
    })
    dump(data)

    # Schedule notification
    schedule_due_notification(ctx.application, entry["date"], stars)

    await update.message.reply_text(
        f"╔══ ✅ *INGRESO GUARDADO* ══╗\n\n"
        f"{icon} *{label}*\n"
        f"⭐ *{stars:,} Stars* = `${usd:,.2f} USD`\n"
        f"📅 Registrado: {entry['date']}\n"
        f"🔔 Retira el *{due.strftime('%d %b %Y')}* a las *7:17 PM*\n"
        + (f"📝 Nota: _{note}_\n" if note else "") +
        f"\n╚══════════════════════╝\n\n"
        f"_{render_summary(data, short=True)}_",
        parse_mode="Markdown",
        reply_markup=main_kb()
    )
    ctx.user_data.pop("add", None)
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "❌ Cancelado.",
        reply_markup=main_kb()
    )
    return ConversationHandler.END

# ─── WITHDRAWALS CONVERSATION ────────────────────────────────
async def withdraw_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load()
    ready = [e for e in data["entries"] if is_ready(e["date"])]
    if not ready:
        await update.message.reply_text(
            "⏳ No tienes Stars listas para retirar aún.\n\n"
            "Usa /calendario para ver cuándo estarán disponibles.",
            reply_markup=main_kb()
        )
        return ConversationHandler.END

    total_ready = sum(e["stars"] for e in ready)
    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    ctx.user_data["w"] = {"date": today}

    await update.message.reply_text(
        f"💸 *Registrar retiro*\n\n"
        f"✅ Tienes *{total_ready:,}★ = ${total_ready*RATE:,.2f}* disponibles\n\n"
        f"📅 Fecha del retiro:\n"
        f"Escribe *hoy* o una fecha `YYYY-MM-DD`",
        parse_mode="Markdown"
    )
    return WITHDRAW_DATE

async def withdraw_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    if text in ("hoy", "today"):
        text = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    try:
        datetime.fromisoformat(text)
    except ValueError:
        await update.message.reply_text("❌ Formato inválido. Usa `YYYY-MM-DD`", parse_mode="Markdown")
        return WITHDRAW_DATE
    ctx.user_data["w"]["date"] = text
    await update.message.reply_text(
        f"✅ Fecha: *{text}*\n\n⭐ *¿Cuántas Stars retiraste?*",
        parse_mode="Markdown"
    )
    return WITHDRAW_STARS

async def withdraw_stars(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", "")
    try:
        stars = int(text)
        assert stars > 0
    except Exception:
        await update.message.reply_text("❌ Número inválido.", parse_mode="Markdown")
        return WITHDRAW_STARS

    entry = ctx.user_data["w"]
    data  = load()
    data["withdrawals"].append({
        "id":    int(datetime.now().timestamp() * 1000),
        "date":  entry["date"],
        "stars": stars,
    })
    dump(data)

    total_w = sum(w["stars"] for w in data["withdrawals"])
    await update.message.reply_text(
        f"╔══ 💸 *RETIRO REGISTRADO* ══╗\n\n"
        f"⭐ *{stars:,} Stars* = `${stars*RATE:,.2f} USD`\n"
        f"📅 {entry['date']}\n\n"
        f"Total retirado histórico:\n"
        f"⭐ *{total_w:,} Stars* = `${total_w*RATE:,.2f} USD`\n\n"
        f"╚══════════════════════╝",
        parse_mode="Markdown",
        reply_markup=main_kb()
    )
    ctx.user_data.pop("w", None)
    return ConversationHandler.END

# ─── HISTORIAL ────────────────────────────────────────────────
async def historial(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load()
    entries  = sorted(data["entries"], key=lambda e: e["date"], reverse=True)[:10]
    wdrs     = sorted(data["withdrawals"], key=lambda w: w["date"], reverse=True)[:5]

    lines = ["╔══ 📋 *ÚLTIMOS INGRESOS* ══╗", ""]
    if entries:
        for e in entries:
            dl   = days_left(e["date"])
            rdy  = is_ready(e["date"])
            icon, label = TAGS.get(e.get("tag","otro"), ("✨","Otro"))
            due  = due_date(e["date"])
            status = "✅ Listo" if rdy else f"⏳ {dl}d"
            lines.append(
                f"{icon} *{e['stars']:,}★* — {status}\n"
                f"   {e['date']} → {due.strftime('%d %b')} 7:17PM"
                + (f"\n   _{e['note']}_" if e.get("note") else "")
            )
    else:
        lines.append("_Sin ingresos aún._")

    lines += ["", "╔══ 💸 *ÚLTIMOS RETIROS* ══╗", ""]
    if wdrs:
        for w in wdrs:
            lines.append(f"💸 *{w['stars']:,}★* = `${w['stars']*RATE:,.2f}` — {w['date']}")
    else:
        lines.append("_Sin retiros aún._")

    lines.append("\n╚══════════════════════╝")
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=main_kb()
    )

# ─── META ─────────────────────────────────────────────────────
async def meta_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    data = load()
    if args:
        try:
            val = float(args[0])
            data["goal_month"] = val
            dump(data)
            await update.message.reply_text(
                f"🎯 Meta mensual establecida: *${val:,.2f} USD*",
                parse_mode="Markdown",
                reply_markup=main_kb()
            )
        except:
            await update.message.reply_text("❌ Uso: /meta 500", reply_markup=main_kb())
    else:
        current = data.get("goal_month", 0)
        await update.message.reply_text(
            f"🎯 Meta mensual actual: *${current:,.2f} USD*\n\n"
            f"Para cambiarla: `/meta 500`",
            parse_mode="Markdown",
            reply_markup=main_kb()
        )

async def nombre_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if args:
        name = " ".join(args)
        data = load()
        data["name"] = name
        dump(data)
        await update.message.reply_text(
            f"✅ Nombre actualizado: *{name}*",
            parse_mode="Markdown",
            reply_markup=main_kb()
        )
    else:
        await update.message.reply_text(
            "Uso: `/nombre Valeria`",
            parse_mode="Markdown"
        )

# ─── TEXT ROUTER ──────────────────────────────────────────────
async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "Agregar"   in text: await add_start(update, ctx); return
    if "calendario" in text.lower() or "Calendario" in text: await calendario(update, ctx); return
    if "Estadísticas" in text or "stats" in text.lower(): await stats(update, ctx); return
    if "Retiros" in text or "retiro" in text.lower(): await historial(update, ctx); return
    if "Resumen"   in text: await resumen(update, ctx); return
    if "Ajustes"   in text:
        data = load()
        await update.message.reply_text(
            f"⚙️ *Ajustes CREAM VIP*\n\n"
            f"👤 Nombre: *{data.get('name','—')}*\n"
            f"🎯 Meta mensual: *${data.get('goal_month',0):,.2f} USD*\n\n"
            f"Comandos:\n"
            f"/nombre Valeria — cambiar nombre\n"
            f"/meta 500 — cambiar meta mensual",
            parse_mode="Markdown",
            reply_markup=main_kb()
        )
        return
    await resumen(update, ctx)

# ─── SCHEDULED NOTIFICATION ───────────────────────────────────
def schedule_due_notification(app: Application, entry_date: str, stars: int):
    """Schedule a notification for when this entry becomes available."""
    due = due_date(entry_date)
    now = datetime.now(TIMEZONE)
    notify_dt = due.replace(hour=NOTIFY_H, minute=NOTIFY_M, second=0, tzinfo=TIMEZONE)
    delay = (notify_dt - now).total_seconds()
    if delay > 0 and OWNER_ID:
        app.job_queue.run_once(
            send_due_notification,
            when=delay,
            data={"stars": stars, "date": entry_date},
            name=f"due_{entry_date}_{stars}"
        )
        log.info(f"Notificación programada en {delay:.0f}s para {notify_dt}")

async def send_due_notification(ctx: ContextTypes.DEFAULT_TYPE):
    job_data = ctx.job.data
    stars = job_data["stars"]
    usd   = stars * RATE
    await ctx.bot.send_message(
        chat_id=OWNER_ID,
        text=(
            f"🔔 *¡CREAM VIP — HORA DE RETIRAR!*\n\n"
            f"⭐ *{stars:,} Stars* están disponibles ahora\n"
            f"💵 `${usd:,.2f} USD`\n\n"
            f"🚀 Abre Telegram y retira ahora\n"
            f"⏰ Son las *7:17 PM* — ¡no esperes!\n\n"
            f"Usa /calendario para ver todos tus retiros."
        ),
        parse_mode="Markdown"
    )

async def daily_check(ctx: ContextTypes.DEFAULT_TYPE):
    """Daily check at 7:17 PM for due entries."""
    if not OWNER_ID:
        return
    data = load()
    today = datetime.now(TIMEZONE).date().isoformat()
    due_today = []
    for e in data["entries"]:
        dd = due_date(e["date"]).date().isoformat()
        if dd == today:
            due_today.append(e)

    if due_today:
        total = sum(e["stars"] for e in due_today)
        await ctx.bot.send_message(
            chat_id=OWNER_ID,
            text=(
                f"🔥 *¡CREAM VIP — {len(due_today)} RETIRO{'S' if len(due_today)>1 else ''} HOY!*\n\n"
                f"⭐ *{total:,} Stars* disponibles ahora\n"
                f"💵 `${total*RATE:,.2f} USD`\n\n"
                + "\n".join(
                    f"  • ★{e['stars']:,} ({TAGS.get(e.get('tag','otro'),('✨','?'))[0]})"
                    for e in due_today
                ) +
                f"\n\n⏰ Son las *7:17 PM* — ¡abre Telegram y retira!"
            ),
            parse_mode="Markdown"
        )

# ─── MAIN ─────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        raise ValueError("❌ Falta BOT_TOKEN en variables de entorno")

    app = Application.builder().token(BOT_TOKEN).build()

    # Add conversation: add entry
    add_conv = ConversationHandler(
        entry_points=[
            CommandHandler("agregar", add_start),
        ],
        states={
            ADD_DATE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, add_date)],
            ADD_STARS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_stars)],
            ADD_TAG:   [CallbackQueryHandler(add_tag_cb, pattern="^(tag:|cancel)")],
            ADD_NOTE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, add_note)],
        },
        fallbacks=[CommandHandler("cancelar", cancel)],
    )

    # Add conversation: withdraw
    w_conv = ConversationHandler(
        entry_points=[CommandHandler("retirar", withdraw_start)],
        states={
            WITHDRAW_DATE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_date)],
            WITHDRAW_STARS: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_stars)],
        },
        fallbacks=[CommandHandler("cancelar", cancel)],
    )

    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("help",       help_cmd))
    app.add_handler(CommandHandler("resumen",    resumen))
    app.add_handler(CommandHandler("calendario", calendario))
    app.add_handler(CommandHandler("stats",      stats))
    app.add_handler(CommandHandler("retiros",    historial))
    app.add_handler(CommandHandler("meta",       meta_cmd))
    app.add_handler(CommandHandler("nombre",     nombre_cmd))
    app.add_handler(add_conv)
    app.add_handler(w_conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    # Daily job at 7:17 PM
    if OWNER_ID:
        notify_time = time(hour=NOTIFY_H, minute=NOTIFY_M, tzinfo=TIMEZONE)
        app.job_queue.run_daily(daily_check, time=notify_time, name="daily_717")
        log.info(f"Alerta diaria programada a las {NOTIFY_H}:{NOTIFY_M:02d}")

    log.info("🚀 CREAM VIP Bot iniciado")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
