"""Mattoni condivisi dai comandi: tastiere, risposte, "domanda in sospeso", aiuto ed errori."""
import logging
import time
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ApplicationHandlerStop, ContextTypes

from config import TZ, settings

log = logging.getLogger(__name__)

PENDING_TTL = 600  # secondi: dopo 10 minuti una domanda in sospeso viene dimenticata

HELP = (
    "Ciao! Tengo traccia delle tue spese e controllo i prezzi dei prodotti che ti interessano.\n\n"
    "💸 <b>Registrare una spesa</b>\n"
    "Scrivimi l'importo e cosa hai comprato: <code>caffè 1,20</code> oppure <code>45 benzina</code>.\n"
    "Puoi aggiungere una data (<code>ieri</code>, <code>15/11</code>) e una categoria (<code>#regali</code>).\n\n"
    "💰 <b>Registrare un'entrata</b>\n"
    "Metti un <code>+</code> davanti: <code>+1500 stipendio</code>. Nel /report vedi entrate, spese e saldo.\n\n"
    "🏷 <b>Monitorare un prezzo</b>\n"
    "Incollami il link di un prodotto: ti avviso quando il prezzo scende.\n\n"
    "<b>Comandi</b>\n"
    "/spese – vedi, modifica o elimina le spese\n"
    "/report – grafico e riepilogo, anche automatico\n"
    "/budget – limite mensile, totale o per categoria\n"
    "/ricorrenti – spese che si ripetono ogni mese\n"
    "/prezzi – prodotti monitorati\n"
    "/export – scarica spese ed entrate in CSV\n"
    "/cancella_dati – elimina tutto ciò che so di te\n"
    "/annulla – interrompe l'operazione in corso"
)


def date_label(ts: int) -> str:
    """'oggi', 'ieri' oppure la data per esteso."""
    day, today = datetime.fromtimestamp(ts, TZ).date(), datetime.now(TZ).date()
    if day == today:
        return "oggi"
    if day == today - timedelta(days=1):
        return "ieri"
    return day.strftime("%d/%m/%Y")


# ---------- tastiere ----------

def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([list(r) for r in rows if r])


def fits(data: str) -> bool:
    """Telegram accetta al massimo 64 byte nei dati di un bottone."""
    return len(data.encode("utf-8")) <= 64


def grid(buttons: list[InlineKeyboardButton], per_row: int = 2) -> list[list[InlineKeyboardButton]]:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


CANCEL_KB = kb([btn("✖️ Annulla", "cancel")])


# ---------- risposte ----------

async def reply(update: Update, text: str, markup: InlineKeyboardMarkup | None = None):
    return await update.effective_message.reply_text(
        text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True
    )


async def edit(update: Update, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Sostituisce il messaggio su cui è stato premuto il bottone."""
    try:
        await update.callback_query.edit_message_text(
            text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():  # doppio tocco sullo stesso bottone: niente da fare
            raise


# ---------- domanda in sospeso ----------
# Quando il bot chiede un valore ("scrivi il nuovo importo"), il prossimo messaggio di testo
# dell'utente è la risposta. Lo stato vive in context.user_data["pending"].

def set_pending(context: ContextTypes.DEFAULT_TYPE, action: str, **data) -> None:
    context.user_data["pending"] = {"action": action, "at": time.time(), **data}


def get_pending(context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    pending = context.user_data.get("pending")
    if pending and time.time() - pending["at"] > PENDING_TTL:
        context.user_data.pop("pending", None)
        return None
    return pending


def clear_pending(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("pending", None)


async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, prompt: str, **data) -> None:
    set_pending(context, action, **data)
    await reply(update, prompt, CANCEL_KB)


# ---------- comandi generali ----------

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, HELP)


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    had_pending = get_pending(context) is not None
    clear_pending(context)
    await reply(update, "Operazione annullata." if had_pending else "Non c'è nessuna operazione in corso.")


async def cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    clear_pending(context)
    await edit(update, "Operazione annullata.")


async def access_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Se è impostato ALLOWED_USERS, risponde solo a quegli utenti. Senza, il bot è aperto a tutti."""
    if not settings.allowed_users.strip():
        return
    user = update.effective_user
    if user and user.id in settings.allowed_ids:
        return
    user_id = user.id if user else "sconosciuto"
    if update.callback_query:
        await update.callback_query.answer("Bot privato.", show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(
            f"🔒 Questo bot è privato.\nSe vuoi usarlo, comunica il tuo ID a chi lo gestisce: {user_id}"
        )
    raise ApplicationHandlerStop


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, "Non conosco questo comando. Scrivi /help per vedere cosa posso fare.")


async def stale_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer(
        "Questo bottone non è più attivo. Usa i comandi del menu (/help).", show_alert=True
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Errore durante la gestione di un aggiornamento", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Qualcosa è andato storto. Riprova tra poco.")
        except Exception:
            pass
