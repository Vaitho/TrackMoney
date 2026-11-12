"""Spese ricorrenti: si registrano da sole ogni mese nel giorno indicato."""
import html
from datetime import datetime

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import db
from config import TZ
from formatting import eur
from jobs import effective_day
from parsing import parse_recurring
from summaries import month_key

from .common import CANCEL_KB, ask, btn, clear_pending, edit, kb, reply

MAX_RECURRING = 20
HOW_TO = (
    "Scrivi importo, descrizione e giorno del mese.\n"
    "Esempio: <code>12,99 netflix 15</code> (il 15 di ogni mese).\n"
    "Con <code>#categoria</code> scegli tu la categoria.\n"
    "Per un'entrata mensile metti un +: <code>+1500 stipendio 27</code>."
)


async def _list_view(chat_id: int, notice: str = "") -> tuple[str, InlineKeyboardMarkup]:
    items = await db.list_recurring(chat_id)
    prefix = f"{notice}\n\n" if notice else ""
    add = [btn("➕ Aggiungi", "rc:a")]
    if not items:
        return prefix + "Non hai spese ricorrenti.\nSono utili per affitto, abbonamenti e bollette: le registro io ogni mese.", kb(add)
    lines = ["<b>Spese ricorrenti</b>"]
    for r in items:
        if r["income"]:
            lines.append(f"• il {r['day']} · 💰 <b>+{eur(r['amount_cents'])}</b> · {html.escape(r['note'])} <i>(entrata)</i>")
        else:
            lines.append(
                f"• il {r['day']} · <b>{eur(r['amount_cents'])}</b> · {html.escape(r['note'])} "
                f"<i>({html.escape(r['category'])})</i>"
            )
    spent = sum(r["amount_cents"] for r in items if not r["income"])
    earned = sum(r["amount_cents"] for r in items if r["income"])
    lines.append(f"\nSpese al mese: <b>{eur(spent)}</b>" + (f" · entrate: <b>{eur(earned)}</b>" if earned else ""))
    delete = [[btn(f"🗑 {r['note'][:28]}", f"rc:d:{r['id']}")] for r in items]
    return prefix + "\n".join(lines), kb(*delete, add)


async def _add(update: Update, chat_id: int, text: str) -> bool:
    """Crea la spesa ricorrente descritta da text. Ritorna False se il testo non è valido."""
    rec = parse_recurring(text, await db.user_categories(chat_id))
    if rec is None:
        return False
    if len(await db.list_recurring(chat_id)) >= MAX_RECURRING:
        await reply(update, f"Hai raggiunto il limite di {MAX_RECURRING} spese ricorrenti.")
        return True
    now = datetime.now(TZ)
    # Se il giorno di questo mese è già passato, la prima registrazione sarà il mese prossimo.
    already_passed = now.day >= effective_day(rec.day, now.year, now.month)
    await db.add_recurring(
        chat_id, rec.cents, rec.category, rec.note, rec.day, month_key(now) if already_passed else None, rec.income
    )
    start = "dal mese prossimo" if already_passed else "già da questo mese"
    what = f"💰 entrata <b>+{eur(rec.cents)}</b>" if rec.income else f"<b>{eur(rec.cents)}</b>"
    category = "" if rec.income else f" ({html.escape(rec.category)})"
    notice = f"🔁 Aggiunta: {what} · {html.escape(rec.note)}{category}, il {rec.day} di ogni mese, {start}."
    await reply(update, *await _list_view(chat_id, notice))
    return True


async def ricorrenti_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ricorrenti apre l'elenco. Scorciatoia: /ricorrenti 12,99 netflix 15"""
    chat_id = update.effective_chat.id
    if not context.args:
        await reply(update, *await _list_view(chat_id))
    elif not await _add(update, chat_id, " ".join(context.args)):
        await reply(update, f"Non ho capito.\n{HOW_TO}")


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    parts = query.data.split(":")
    if parts[1] == "a":
        await ask(update, context, "rec_add", f"<b>Nuova spesa ricorrente</b>\n{HOW_TO}")
    elif parts[1] == "d":
        removed = await db.delete_recurring(int(parts[2]), chat_id)
        await edit(update, *await _list_view(chat_id, "🗑 Spesa ricorrente eliminata." if removed else ""))


async def _pending_add(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict) -> None:
    if await _add(update, update.effective_chat.id, update.effective_message.text):
        clear_pending(context)
    else:
        await reply(update, f"Non ho capito.\n{HOW_TO}", CANCEL_KB)


PENDING = {"rec_add": _pending_add}
