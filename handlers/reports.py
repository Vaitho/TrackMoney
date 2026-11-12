"""Resoconti: grafico + riepilogo a comando, invio automatico, esportazione CSV."""
import csv
import io
from datetime import datetime, timedelta

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import db
from config import TZ
from summaries import build_period_report, month_key, period

from .common import btn, edit, kb, reply

CAPTION_LIMIT = 1024  # massimo di Telegram per la didascalia di una foto


def _period_kb() -> InlineKeyboardMarkup:
    return kb(
        [btn("Questo mese", "r:m:0"), btn("Mese scorso", "r:m:-1"), btn("Anno", "r:y")],
        [btn("📬 Invio automatico", "r:a")],
    )


async def _send_report(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str, offset: int) -> None:
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id, "upload_photo")
    report = await build_period_report(chat_id, mode, offset)
    if report is None:
        _, _, title = period(mode, offset)
        await reply(update, f"Non ci sono spese da mostrare ({title.lower()}).", _period_kb())
        return
    message = update.effective_message
    if len(report.text) <= CAPTION_LIMIT:
        await message.reply_photo(report.image, caption=report.text, parse_mode="HTML", reply_markup=_period_kb())
    else:  # riepilogo troppo lungo per stare sotto la foto
        await message.reply_photo(report.image, caption=f"📈 {report.title}")
        await reply(update, report.text, _period_kb())


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = context.args[0].lower() if context.args else ""
    mode = "year" if arg.startswith("ann") else "month"
    await _send_report(update, context, mode, -1 if arg.startswith("scors") else 0)


# ---------- invio automatico ----------

async def _auto_view(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    prefs = await db.get_report_prefs(chat_id) or {"weekly": 0, "monthly": 0}

    def mark(on) -> str:
        return "✅" if on else "⬜️"

    text = (
        "📬 <b>Invio automatico</b>\n"
        "• <b>Settimanale</b>: ogni lunedì, il riepilogo della settimana appena finita.\n"
        "• <b>Mensile</b>: il 1° del mese, il grafico del mese appena chiuso.\n"
        "Li ricevi dalle 9:00. Tocca per attivare o disattivare."
    )
    markup = kb([
        btn(f"{mark(prefs['weekly'])} Settimanale", "r:tw"),
        btn(f"{mark(prefs['monthly'])} Mensile", "r:tm"),
    ])
    return text, markup


async def _toggle(chat_id: int, which: str) -> None:
    prefs = await db.get_report_prefs(chat_id) or {"weekly": 0, "monthly": 0}
    weekly = not prefs["weekly"] if which == "weekly" else bool(prefs["weekly"])
    monthly = not prefs["monthly"] if which == "monthly" else bool(prefs["monthly"])
    # Segno il periodo corrente come "già inviato": il primo invio sarà al prossimo lunedì / 1° del mese.
    today = datetime.now(TZ).date()
    monday = (today - timedelta(days=today.weekday())).isoformat()
    await db.set_report_prefs(chat_id, weekly, monthly, monday, month_key())


async def auto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, *await _auto_view(update.effective_chat.id))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    parts = query.data.split(":")
    action = parts[1]

    if action == "m":
        await _send_report(update, context, "month", int(parts[2]))
    elif action == "y":
        await _send_report(update, context, "year", 0)
    elif action == "a":  # il bottone sta sotto una foto: le impostazioni vanno in un messaggio nuovo
        await reply(update, *await _auto_view(chat_id))
    elif action in ("tw", "tm"):
        await _toggle(chat_id, "weekly" if action == "tw" else "monthly")
        await edit(update, *await _auto_view(chat_id))


# ---------- esportazione ----------

async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    expenses = await db.expenses_between(chat_id, 0, 2**40)
    incomes = await db.incomes_between(chat_id, 0, 2**40)
    if not expenses and not incomes:
        await reply(update, "Non hai ancora niente da esportare.")
        return
    entries = [(r["spent_at"], "Spesa", r["amount_cents"], r["category"], r["note"]) for r in expenses]
    entries += [(r["received_at"], "Entrata", r["amount_cents"], "Entrata", r["note"]) for r in incomes]
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")  # separatore adatto a Excel in italiano
    writer.writerow(["Tipo", "Data", "Importo", "Categoria", "Nota"])
    for ts, kind, cents, category, note in sorted(entries):
        when = datetime.fromtimestamp(ts, TZ).strftime("%d/%m/%Y %H:%M")
        writer.writerow([kind, when, f"{cents / 100:.2f}".replace(".", ","), category, note])
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    data.name = "spese.csv"
    await update.effective_message.reply_document(
        data, caption=f"{len(expenses)} spese e {len(incomes)} entrate esportate."
    )
