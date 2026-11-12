"""Spese: registrazione dal testo libero, elenco, modifica ed eliminazione con i bottoni."""
import html
from datetime import datetime, timedelta

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import db
from config import TZ
from formatting import eur
from parsing import extract_date, parse_amount, parse_expense, resolve_category
from summaries import budget_lines, categories_for

from . import income
from .common import CANCEL_KB, ask, btn, clear_pending, date_label, edit, fits, grid, kb, reply

PAGE = 8  # spese per pagina in /spese

NOT_UNDERSTOOD = (
    "Non ho capito 🤔\n"
    "Per registrare una spesa scrivi ad esempio <code>caffè 1,20</code> o <code>45 benzina</code>.\n"
    "Per un'entrata metti un + davanti: <code>+1500 stipendio</code>.\n"
    "Per monitorare un prezzo incollami il link del prodotto.\n"
    "Tutti i comandi: /help"
)
GONE = "Questa spesa non esiste più."


def _day(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, TZ)


# ---------- viste ----------

def _detail(e: dict) -> tuple[str, InlineKeyboardMarkup]:
    i = e["id"]
    text = (
        f"<b>{eur(e['amount_cents'])}</b> · {html.escape(e['note'])}\n"
        f"📂 {html.escape(e['category'])}\n"
        f"📅 {date_label(e['spent_at'])}"
    )
    markup = kb(
        [btn("💶 Importo", f"e:f:{i}:a"), btn("📂 Categoria", f"e:f:{i}:c")],
        [btn("📅 Data", f"e:f:{i}:d"), btn("📝 Descrizione", f"e:f:{i}:n")],
        [btn("🗑 Elimina", f"e:d:{i}")],
        [btn("« Tutte le spese", "e:l:0")],
    )
    return text, markup


async def _list_view(chat_id: int, page: int = 0, notice: str = "") -> tuple[str, InlineKeyboardMarkup | None]:
    total = await db.count_expenses(chat_id)
    prefix = f"{notice}\n\n" if notice else ""
    income_button = [btn("💰 Entrate", "i:l:0")]
    if total == 0:
        return prefix + "Nessuna spesa registrata. Scrivimi ad esempio: <code>caffè 1,20</code>", kb(income_button)

    pages = (total + PAGE - 1) // PAGE
    page = max(0, min(page, pages - 1))
    rows = await db.list_expenses(chat_id, PAGE, page * PAGE)

    lines = [f"<b>Le tue spese</b>" + (f" · pagina {page + 1} di {pages}" if pages > 1 else "")]
    buttons = []
    for e in rows:
        day = _day(e["spent_at"]).strftime("%d/%m")
        lines.append(
            f"{day} · <b>{eur(e['amount_cents'])}</b> · {html.escape(e['note'])} "
            f"<i>({html.escape(e['category'])})</i>"
        )
        buttons.append([btn(f"{day} · {eur(e['amount_cents'])} · {e['note'][:24]}", f"e:v:{e['id']}")])
    lines.append("\nTocca una spesa per modificarla o eliminarla.")

    nav = []
    if page > 0:
        nav.append(btn("‹ Più recenti", f"e:l:{page - 1}"))
    if page < pages - 1:
        nav.append(btn("Più vecchie ›", f"e:l:{page + 1}"))
    return prefix + "\n".join(lines), kb(*buttons, nav, income_button)


async def _send_alerts(update: Update, chat_id: int, category: str) -> None:
    _, alerts = await budget_lines(chat_id, check_alerts=True, category=category)
    for alert in alerts:
        await reply(update, alert)


async def _reply_detail(update: Update, chat_id: int, expense_id: int, notice: str) -> None:
    e = await db.get_expense(expense_id, chat_id)
    if e is None:
        await reply(update, GONE)
        return
    text, markup = _detail(e)
    await reply(update, f"{notice}\n\n{text}", markup)
    await _send_alerts(update, chat_id, e["category"])


# ---------- comandi e testo libero ----------

async def add_from_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await income.try_add(update, update.effective_message.text):  # "+1500 stipendio" è un'entrata
        return
    chat_id = update.effective_chat.id
    expense = parse_expense(update.effective_message.text, await db.user_categories(chat_id))
    if expense is None:
        await reply(update, NOT_UNDERSTOOD)
        return

    spent_at = int(expense.when.timestamp()) if expense.when else None
    exp_id = await db.add_expense(chat_id, expense.cents, expense.category, expense.note, spent_at)
    e = await db.get_expense(exp_id, chat_id)
    when = "" if date_label(e["spent_at"]) == "oggi" else f"\n📅 {date_label(e['spent_at'])}"
    status, alerts = await budget_lines(chat_id, check_alerts=True, category=expense.category)
    await reply(
        update,
        f"✅ <b>{eur(expense.cents)}</b> · {html.escape(expense.note)}\n📂 {html.escape(expense.category)}{when}"
        + (f"\n\n{status}" if status else ""),
        kb([btn("↩️ Annulla", f"e:u:{exp_id}"), btn("✏️ Modifica", f"e:v:{exp_id}")]),
    )
    for alert in alerts:
        await reply(update, alert)


async def spese_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, markup = await _list_view(update.effective_chat.id)
    await reply(update, text, markup)


# ---------- bottoni ----------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    parts = query.data.split(":", 3)  # e:<azione>:<id o pagina>[:<valore>]
    action, number = parts[1], int(parts[2])
    value = parts[3] if len(parts) > 3 else ""

    if action == "l":
        await edit(update, *await _list_view(chat_id, number))
        return

    expense = await db.get_expense(number, chat_id)
    if expense is None:
        await edit(update, GONE)
        return

    if action == "v":
        await edit(update, *_detail(expense))
    elif action == "u":
        await db.delete_expense(number, chat_id)
        await edit(update, f"↩️ Spesa annullata: {eur(expense['amount_cents'])} · {html.escape(expense['note'])}")
    elif action == "d":
        await db.delete_expense(number, chat_id)
        notice = f"🗑 Eliminata: {eur(expense['amount_cents'])} · {html.escape(expense['note'])}"
        await edit(update, *await _list_view(chat_id, 0, notice))
    elif action == "f":
        await _edit_field(update, context, expense, value)
    elif action == "sd":  # data scelta dai bottoni: 0 = oggi, 1 = ieri, 2 = altroieri
        day = datetime.now(TZ) - timedelta(days=int(value))
        when = day if value == "0" else day.replace(hour=12, minute=0, second=0, microsecond=0)
        await db.update_expense(number, chat_id, {"spent_at": int(when.timestamp())})
        await edit(update, *_detail(await db.get_expense(number, chat_id)))
    elif action == "sc":  # categoria scelta dai bottoni
        await db.update_expense(number, chat_id, {"category": value})
        await edit(update, *_detail(await db.get_expense(number, chat_id)))
        await _send_alerts(update, chat_id, value)


async def _edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE, e: dict, field: str) -> None:
    i = e["id"]
    what = f"<b>{eur(e['amount_cents'])}</b> · {html.escape(e['note'])}"
    back = [btn("« Indietro", f"e:v:{i}")]
    if field == "a":
        await ask(update, context, "exp_amount", f"Scrivi il nuovo importo per {what}", id=i)
    elif field == "n":
        await ask(update, context, "exp_note", f"Scrivi la nuova descrizione per {what}", id=i)
    elif field == "d":
        await edit(update, f"Quando hai fatto questa spesa?\n{what}", kb(
            [btn("Oggi", f"e:sd:{i}:0"), btn("Ieri", f"e:sd:{i}:1"), btn("Altroieri", f"e:sd:{i}:2")],
            [btn("Un'altra data…", f"e:f:{i}:D")],
            back,
        ))
    elif field == "D":
        await ask(update, context, "exp_date", f"Scrivi la data di {what}\nEsempio: <code>15/11</code> o <code>15/11/2025</code>", id=i)
    elif field == "c":
        names = [c for c in await categories_for(update.effective_chat.id) if fits(f"e:sc:{i}:{c}")]
        await edit(update, f"Scegli la categoria per {what}", kb(
            *grid([btn(("✓ " if c == e["category"] else "") + c, f"e:sc:{i}:{c}") for c in names]),
            [btn("Nuova categoria…", f"e:f:{i}:C")],
            back,
        ))
    elif field == "C":
        await ask(update, context, "exp_category", f"Scrivi il nome della nuova categoria per {what}", id=i)


# ---------- risposte alle domande in sospeso ----------

async def _pending_amount(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict) -> None:
    cents = parse_amount(update.effective_message.text)
    if cents is None:
        await reply(update, "Non è un importo valido. Scrivi ad esempio <code>12,50</code>.", CANCEL_KB)
        return
    clear_pending(context)
    chat_id = update.effective_chat.id
    await db.update_expense(pending["id"], chat_id, {"amount_cents": cents})
    await _reply_detail(update, chat_id, pending["id"], "✏️ Importo aggiornato.")


async def _pending_note(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict) -> None:
    clear_pending(context)
    chat_id = update.effective_chat.id
    await db.update_expense(pending["id"], chat_id, {"note": update.effective_message.text.strip()[:200]})
    await _reply_detail(update, chat_id, pending["id"], "✏️ Descrizione aggiornata.")


async def _pending_date(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict) -> None:
    _, when = extract_date(update.effective_message.text)
    if when is None:
        await reply(
            update, "Non è una data valida (o è nel futuro). Scrivi ad esempio <code>15/11</code> oppure <code>ieri</code>.",
            CANCEL_KB,
        )
        return
    clear_pending(context)
    chat_id = update.effective_chat.id
    await db.update_expense(pending["id"], chat_id, {"spent_at": int(when.timestamp())})
    await _reply_detail(update, chat_id, pending["id"], "✏️ Data aggiornata.")


async def _pending_category(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict) -> None:
    clear_pending(context)
    chat_id = update.effective_chat.id
    name = update.effective_message.text.strip().lstrip("#")[:40]
    category = resolve_category(name, await db.user_categories(chat_id))
    await db.update_expense(pending["id"], chat_id, {"category": category})
    await _reply_detail(update, chat_id, pending["id"], "✏️ Categoria aggiornata.")


PENDING = {
    "exp_amount": _pending_amount,
    "exp_note": _pending_note,
    "exp_date": _pending_date,
    "exp_category": _pending_category,
}
