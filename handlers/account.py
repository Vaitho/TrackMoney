"""I dati dell'utente: cancellazione completa."""
from telegram import Update
from telegram.ext import ContextTypes

import db

from . import reports
from .common import btn, clear_pending, edit, kb, reply


async def _summary(chat_id: int) -> str:
    c = await db.user_data_counts(chat_id)
    parts = [
        (c["expenses"], "spese"), (c["incomes"], "entrate"), (c["subscriptions"], "prodotti monitorati"),
        (c["recurring"], "voci ricorrenti"), (c["budgets"] + c["category_budgets"], "budget"),
    ]
    found = [f"{n} {label}" for n, label in parts if n]
    return ", ".join(found) if found else "niente"


async def cancella_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    summary = await _summary(update.effective_chat.id)
    await reply(
        update,
        f"🗑 <b>Cancellare i tuoi dati</b>\nIn questo momento conservo per te: {summary}.\n\n"
        "Posso eliminare tutto: spese, entrate, budget, spese ricorrenti, prodotti monitorati e storico dei prezzi. "
        "Se vuoi tenerne una copia, scaricala prima.",
        kb(
            [btn("📥 Scarica un CSV", "x:csv")],
            [btn("🗑 Cancella tutto…", "x:ask")],
            [btn("✖️ Lascia stare", "x:no")],
        ),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    action = query.data.split(":")[1]

    if action == "csv":
        await reports.export_cmd(update, context)
    elif action == "ask":
        await edit(
            update,
            f"⚠️ Stai per cancellare definitivamente: {await _summary(chat_id)}.\nNon si può annullare. Sei sicuro?",
            kb([btn("Sì, cancella definitivamente", "x:go")], [btn("No, tieni i miei dati", "x:no")]),
        )
    elif action == "go":
        await db.delete_user_data(chat_id)
        clear_pending(context)
        context.user_data.clear()
        await edit(
            update,
            "✅ Fatto: ho cancellato tutti i tuoi dati.\nSe mi riscrivi, ricomincio da zero. "
            "(Le copie di sicurezza giornaliere che tengo sul server li contengono ancora per qualche giorno, poi vengono sostituite.)",
        )
    elif action == "no":
        await edit(update, "Va bene, non ho cancellato nulla.")
