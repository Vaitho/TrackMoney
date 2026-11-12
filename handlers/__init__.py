"""Registrazione di comandi, bottoni e testo libero.

Schema dei dati dei bottoni (callback_data): <modulo>:<azione>[:<id>[:<valore>]]
  e = spese · i = entrate · r = report · b = budget · rc = ricorrenti · p = prezzi · x = cancellazione dati
"""
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from . import account, budget, common, expenses, income, prices, recurring, reports

# Comandi mostrati nel menu di Telegram.
MENU = [
    ("spese", "Vedi, modifica o elimina le spese"),
    ("report", "Grafico e riepilogo"),
    ("budget", "Limite mensile, totale o per categoria"),
    ("ricorrenti", "Spese che si ripetono ogni mese"),
    ("prezzi", "Prodotti monitorati"),
    ("export", "Scarica spese ed entrate in CSV"),
    ("cancella_dati", "Elimina tutti i tuoi dati"),
    ("help", "Come funziona"),
]

# (nomi del comando, funzione). I nomi dopo il primo sono i vecchi comandi, tenuti come alias.
COMMANDS = [
    (["start", "help"], common.help_cmd),
    (["annulla"], common.cancel_cmd),
    (["spese", "modifica"], expenses.spese_cmd),
    (["report", "riepilogo"], reports.report_cmd),
    (["report_auto"], reports.auto_cmd),
    (["export"], reports.export_cmd),
    (["budget"], budget.budget_cmd),
    (["ricorrenti", "ricorrente"], recurring.ricorrenti_cmd),
    (["prezzi", "lista", "nuova", "storico", "obiettivo"], prices.prezzi_cmd),
    (["cancella_dati"], account.cancella_cmd),
]

# (prefisso dei dati del bottone, funzione)
CALLBACKS = [
    (r"^e:", expenses.on_callback),
    (r"^r:", reports.on_callback),
    (r"^b:", budget.on_callback),
    (r"^rc:", recurring.on_callback),
    (r"^p:", prices.on_callback),
    (r"^i:", income.on_callback),
    (r"^x:", account.on_callback),
    (r"^cancel$", common.cancel_cb),
]

# Risposte alle domande in sospeso (vedi common.set_pending).
PENDING = {**expenses.PENDING, **budget.PENDING, **recurring.PENDING, **prices.PENDING}


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Testo libero: risposta a una domanda in sospeso, link di un prodotto, oppure una spesa."""
    pending = common.get_pending(context)
    if pending:
        await PENDING[pending["action"]](update, context, pending)
        return
    text = update.effective_message.text.strip()
    if text.lower().startswith(("http://", "https://")):
        await prices.add_from_url(update, context, text)
    else:
        await expenses.add_from_text(update, context)


def register(app: Application) -> None:
    # Gruppo -1: viene prima di tutto il resto e può fermare l'elaborazione per gli utenti non autorizzati.
    app.add_handler(TypeHandler(Update, common.access_guard), group=-1)
    for names, callback in COMMANDS:
        app.add_handler(CommandHandler(names, callback))
    for pattern, callback in CALLBACKS:
        app.add_handler(CallbackQueryHandler(callback, pattern=pattern))
    app.add_handler(CallbackQueryHandler(common.stale_button))  # bottoni di vecchi messaggi
    app.add_handler(MessageHandler(filters.COMMAND, common.unknown_command))
    # Solo messaggi nuovi: un messaggio modificato non deve registrare una seconda spesa.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.UpdateType.MESSAGE, on_text))
    app.add_error_handler(common.on_error)
