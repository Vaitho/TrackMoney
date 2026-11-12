"""Monitor prezzi: elenco dei prodotti, aggiunta da link, obiettivo, frequenza, storico."""
import asyncio
import html

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import charts
import db
import pricing
from config import MIN_CHECK_INTERVAL, settings
from parsing import parse_amount
from pricing import price_text

from .common import CANCEL_KB, ask, btn, clear_pending, edit, get_pending, kb, reply, set_pending

DEFAULT_INTERVAL = MIN_CHECK_INTERVAL  # minuti
# Niente controlli più frequenti di un'ora: i negozi (Amazon in testa) bloccano chi chiede troppo spesso.
INTERVALS = [(MIN_CHECK_INTERVAL, "1 ora"), (180, "3 ore"), (360, "6 ore"), (1440, "1 giorno")]
INTERVAL_LABELS = {m: f"ogni {label}" for m, label in INTERVALS}
GONE = "Questo prodotto non è più tra quelli monitorati."


def _interval_label(minutes: int) -> str:
    return INTERVAL_LABELS.get(minutes, f"ogni {minutes} min")


def _status(sub: dict) -> str:
    if not sub["active"]:
        return "⏸ in pausa"
    if (sub["last_state"] or {}).get("available") is False:
        return "⛔ non disponibile"
    return "▶️ attivo"


# ---------- viste ----------

async def _list_view(chat_id: int, notice: str = "") -> tuple[str, InlineKeyboardMarkup]:
    subs = await db.list_subscriptions(chat_id)
    prefix = f"{notice}\n\n" if notice else ""
    add = [btn("➕ Aggiungi prodotto", "p:a")]
    if not subs:
        return prefix + "Non stai monitorando nessun prodotto.\nIncollami il link di un prodotto e ti avviso quando il prezzo scende.", kb(add)

    lines, buttons = ["<b>Prodotti monitorati</b>"], []
    for s in subs:
        params, price = s["params"], pricing.current_price(s["params"], s["last_state"])
        title = params.get("title", params["url"])
        line = f"• {html.escape(title[:60])} — <b>{price_text(price) if price else '?'}</b>"
        if params.get("target"):
            line += f" (🎯 {price_text(params['target'])})"
        if _status(s) != "▶️ attivo":
            line += f" · {_status(s)}"
        lines.append(line)
        buttons.append([btn(title[:40], f"p:v:{s['id']}")])
    lines.append("\nTocca un prodotto per obiettivo, storico e impostazioni.")
    return prefix + "\n".join(lines), kb(*buttons, add)


def _detail(sub: dict, notice: str = "") -> tuple[str, InlineKeyboardMarkup]:
    i, params, state = sub["id"], sub["params"], sub["last_state"] or {}
    price = pricing.current_price(params, state)
    link = html.escape(params["url"], quote=True)
    lines = [f"<b><a href=\"{link}\">{html.escape(params.get('title', params['url']))}</a></b>"]
    price_line = f"Prezzo: <b>{price_text(price) if price else '?'}</b>"
    if state.get("min") is not None and price and state["min"] < price - pricing.EPS:
        price_line += f" · minimo visto {price_text(state['min'])}"
    lines.append(price_line)
    if params.get("target"):
        lines.append(f"🎯 Obiettivo: {price_text(params['target'])} — ti avviso quando il prezzo arriva lì o sotto")
    else:
        lines.append("Nessun obiettivo: ti avviso a ogni calo di prezzo")
    lines.append(f"⏱ Controllo {_interval_label(sub['interval_min'])} · {_status(sub)}")

    toggle = btn("⏸ Metti in pausa", f"p:p:{i}") if sub["active"] else btn("▶️ Riattiva", f"p:p:{i}")
    markup = kb(
        [btn("🎯 Obiettivo", f"p:t:{i}"), btn("📈 Storico", f"p:h:{i}")],
        [btn("⏱ Frequenza", f"p:i:{i}"), toggle],
        [btn("🗑 Elimina", f"p:d:{i}")],
        [btn("« Tutti i prodotti", "p:l")],
    )
    return (f"{notice}\n\n" if notice else "") + "\n".join(lines), markup


# ---------- azioni ----------

async def add_from_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    """Aggiunge un prodotto a partire dal link (incollato in chat o chiesto dal bottone 'Aggiungi')."""
    chat_id = update.effective_chat.id
    asked = get_pending(context) is not None  # True se stiamo rispondendo a "incolla il link"
    retry_kb = CANCEL_KB if asked else None

    if await db.count_subscriptions(chat_id) >= settings.max_subs_per_user:
        clear_pending(context)
        await reply(update, f"Stai già monitorando {settings.max_subs_per_user} prodotti, il massimo. Eliminane uno da /prezzi.")
        return
    for sub in await db.list_subscriptions(chat_id):
        if sub["params"]["url"] == url:
            clear_pending(context)
            await reply(update, *_detail(sub, "Stai già monitorando questo prodotto."))
            return

    await reply(update, "🔎 Controllo il link… può richiedere qualche secondo.")
    params, error = await pricing.validate(url)
    if error:
        await reply(update, f"❌ {error}" + ("\nProva con un altro link." if asked else ""), retry_kb)
        return

    clear_pending(context)
    sub_id = await db.add_subscription(chat_id, params, DEFAULT_INTERVAL)
    sub = await db.get_subscription(sub_id, chat_id)
    await reply(update, *_detail(sub, "✅ Prodotto aggiunto! Qui sotto puoi fissare un prezzo obiettivo o cambiare la frequenza."))


async def _set_target(sub: dict, chat_id: int, target: float | None) -> str:
    """Imposta o toglie il prezzo obiettivo. Ritorna il messaggio di conferma."""
    params, state = dict(sub["params"]), sub["last_state"]
    if target is None:
        params.pop("target", None)
        notice = "Obiettivo rimosso: ti avviso a ogni calo di prezzo."
    else:
        params["target"] = target
        notice = f"🎯 Obiettivo impostato a {price_text(target)}."
    if state:
        state["below_target"] = pricing.is_below_target(state["price"], target)
        if state["below_target"] and state.get("available", True):
            notice += f"\nIl prezzo attuale ({price_text(state['price'])}) è già sotto l'obiettivo! Ti riavviso se scende ancora."
    await db.update_subscription(sub["id"], chat_id, params, state)
    return notice


async def _send_history(update: Update, sub: dict) -> None:
    points = await db.price_points(sub["id"])
    title = sub["params"].get("title", "Prodotto")
    if len(points) < 2:
        current = f" Per ora è fermo a {price_text(points[0][1])}." if points else ""
        await reply(update, f"📈 Il prezzo di «{html.escape(title)}» non è ancora cambiato da quando lo monitoro.{current}")
        return
    image = await asyncio.to_thread(charts.build_price_history, points, title, sub["params"].get("target"))
    await update.effective_message.reply_photo(image, caption=f"📈 {title[:200]}")


async def prezzi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, *await _list_view(update.effective_chat.id))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    parts = query.data.split(":")  # p:<azione>[:<id>[:<valore>]]
    action = parts[1]

    if action == "l":
        await edit(update, *await _list_view(chat_id))
        return
    if action == "a":
        await ask(update, context, "price_url", "Incolla il link del prodotto (Amazon, un negozio online…).")
        return

    sub = await db.get_subscription(int(parts[2]), chat_id)
    if sub is None:
        await edit(update, GONE)
        return
    i = sub["id"]
    back = [btn("« Indietro", f"p:v:{i}")]

    if action == "v":
        await edit(update, *_detail(sub))
    elif action == "t":
        remove = [btn("Rimuovi obiettivo", f"p:tr:{i}")] if sub["params"].get("target") else []
        price = pricing.current_price(sub["params"], sub["last_state"])
        set_pending(context, "price_target", id=i)
        await reply(
            update,
            f"A quale prezzo vuoi essere avvisato? Ora costa {price_text(price) if price else '?'}.\n"
            "Scrivi l'importo, es. <code>80</code>",
            kb(remove, [btn("✖️ Annulla", "cancel")]),
        )
    elif action == "tr":
        clear_pending(context)
        notice = await _set_target(sub, chat_id, None)
        await edit(update, *_detail(await db.get_subscription(i, chat_id), notice))
    elif action == "h":
        await _send_history(update, sub)
    elif action == "i":
        await edit(update, "Ogni quanto devo controllare il prezzo?", kb(
            [btn(("✓ " if m == sub["interval_min"] else "") + label, f"p:is:{i}:{m}") for m, label in INTERVALS[:2]],
            [btn(("✓ " if m == sub["interval_min"] else "") + label, f"p:is:{i}:{m}") for m, label in INTERVALS[2:]],
            back,
        ))
    elif action == "is":
        await db.set_interval(i, chat_id, int(parts[3]))
        await edit(update, *_detail(await db.get_subscription(i, chat_id)))
    elif action == "p":
        await db.set_active(i, chat_id, not sub["active"])
        await edit(update, *_detail(await db.get_subscription(i, chat_id)))
    elif action == "d":
        await edit(
            update,
            f"Eliminare «{html.escape(sub['params'].get('title', '')[:80])}»?\nPerderai anche lo storico dei prezzi.",
            kb([btn("Sì, elimina", f"p:dc:{i}"), btn("No, tienilo", f"p:v:{i}")]),
        )
    elif action == "dc":
        await db.delete_subscription(i, chat_id)
        await edit(update, *await _list_view(chat_id, "🗑 Prodotto eliminato."))


# ---------- risposte alle domande in sospeso ----------

async def _pending_url(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict) -> None:
    await add_from_url(update, context, update.effective_message.text.strip())


async def _pending_target(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict) -> None:
    cents = parse_amount(update.effective_message.text)
    if cents is None:
        await reply(update, "Non è un importo valido. Scrivi ad esempio <code>80</code>.", CANCEL_KB)
        return
    clear_pending(context)
    chat_id = update.effective_chat.id
    sub = await db.get_subscription(pending["id"], chat_id)
    if sub is None:
        await reply(update, GONE)
        return
    notice = await _set_target(sub, chat_id, cents / 100)
    await reply(update, *_detail(await db.get_subscription(sub["id"], chat_id), notice))


PENDING = {"price_url": _pending_url, "price_target": _pending_target}
