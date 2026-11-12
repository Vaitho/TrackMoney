"""Entrate (stipendio, rimborsi…): si registrano con un "+" davanti all'importo; qui l'elenco e l'eliminazione."""
import html
from datetime import datetime

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import db
from config import TZ
from formatting import eur
from parsing import parse_income
from summaries import balance_text, month_totals

from .common import btn, date_label, edit, kb, reply

PAGE = 8
GONE = "Questa entrata non esiste più."


async def try_add(update: Update, text: str) -> bool:
    """Se il testo è un'entrata la registra e ritorna True; altrimenti False (è una spesa o altro)."""
    income = parse_income(text)
    if income is None:
        return False
    chat_id = update.effective_chat.id
    received_at = int(income.when.timestamp()) if income.when else None
    income_id = await db.add_income(chat_id, income.cents, income.note[:200], received_at)
    row = await db.get_income(income_id, chat_id)
    when = "" if date_label(row["received_at"]) == "oggi" else f"\n📅 {date_label(row['received_at'])}"
    earned, spent = await month_totals(chat_id)
    await reply(
        update,
        f"💰 <b>+{eur(income.cents)}</b> · {html.escape(income.note)}{when}\n\n"
        f"Saldo del mese: <b>{balance_text(earned, spent)}</b> "
        f"<i>(entrate {eur(earned)} · spese {eur(spent)})</i>",
        kb([btn("↩️ Annulla", f"i:u:{income_id}"), btn("💰 Tutte le entrate", "i:l:0")]),
    )
    return True


async def _list_view(chat_id: int, page: int = 0, notice: str = "") -> tuple[str, InlineKeyboardMarkup]:
    total = await db.count_incomes(chat_id)
    prefix = f"{notice}\n\n" if notice else ""
    back = [btn("« Spese", "e:l:0")]
    if total == 0:
        return prefix + "Nessuna entrata registrata.\nScrivimi ad esempio <code>+1500 stipendio</code>.", kb(back)

    pages = (total + PAGE - 1) // PAGE
    page = max(0, min(page, pages - 1))
    rows = await db.list_incomes(chat_id, PAGE, page * PAGE)
    lines = ["<b>Le tue entrate</b>" + (f" · pagina {page + 1} di {pages}" if pages > 1 else "")]
    buttons = []
    for r in rows:
        day = datetime.fromtimestamp(r["received_at"], TZ).strftime("%d/%m")
        lines.append(f"{day} · <b>+{eur(r['amount_cents'])}</b> · {html.escape(r['note'])}")
        buttons.append([btn(f"{day} · +{eur(r['amount_cents'])} · {r['note'][:24]}", f"i:v:{r['id']}")])
    lines.append("\nTocca un'entrata per eliminarla.")

    nav = []
    if page > 0:
        nav.append(btn("‹ Più recenti", f"i:l:{page - 1}"))
    if page < pages - 1:
        nav.append(btn("Più vecchie ›", f"i:l:{page + 1}"))
    return prefix + "\n".join(lines), kb(*buttons, nav, back)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    parts = query.data.split(":")  # i:<azione>:<id o pagina>
    action, number = parts[1], int(parts[2])

    if action == "l":
        await edit(update, *await _list_view(chat_id, number))
        return

    row = await db.get_income(number, chat_id)
    if row is None:
        await edit(update, GONE)
        return
    label = f"<b>+{eur(row['amount_cents'])}</b> · {html.escape(row['note'])}"

    if action == "v":
        await edit(update, f"{label}\n📅 {date_label(row['received_at'])}", kb(
            [btn("🗑 Elimina", f"i:d:{number}")], [btn("« Entrate", "i:l:0")],
        ))
    elif action == "u":
        await db.delete_income(number, chat_id)
        await edit(update, f"↩️ Entrata annullata: {label}")
    elif action == "d":
        await db.delete_income(number, chat_id)
        await edit(update, *await _list_view(chat_id, 0, f"🗑 Eliminata: {label}"))
