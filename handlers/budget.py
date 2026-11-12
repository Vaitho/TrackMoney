"""Budget mensile, totale e per categoria."""
import html

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import db
from formatting import eur
from parsing import parse_amount, resolve_category
from summaries import WARN_LEVEL, budget_lines, categories_for

from .common import CANCEL_KB, ask, btn, clear_pending, edit, fits, grid, kb, reply

INTRO = (
    "Non hai ancora impostato un budget.\n"
    f"Puoi fissare un limite mensile totale, uno per categoria, o entrambi: "
    f"ti avviso all'{WARN_LEVEL}% e quando lo superi."
)


async def _overview(chat_id: int, notice: str = "") -> tuple[str, InlineKeyboardMarkup]:
    status, _ = await budget_lines(chat_id, check_alerts=False, all_categories=True)
    rows = [[btn("💰 Budget totale", "b:t"), btn("📂 Per categoria", "b:c")]]
    if status:
        rows.append([btn("🗑 Rimuovi un budget", "b:r")])
    text = f"<b>Budget di questo mese</b>\n{status}" if status else INTRO
    return (f"{notice}\n\n" if notice else "") + text, kb(*rows)


async def _apply(update: Update, chat_id: int, category: str | None, cents: int) -> None:
    if category:
        await db.set_cat_budget(chat_id, category, cents)
        notice = f"✅ Budget «{html.escape(category)}» impostato a <b>{eur(cents)}</b>."
    else:
        await db.set_budget(chat_id, cents)
        notice = f"✅ Budget mensile impostato a <b>{eur(cents)}</b>."
    _, alerts = await budget_lines(chat_id, check_alerts=True, category=category)
    await reply(update, *await _overview(chat_id, notice))
    for alert in alerts:
        await reply(update, alert)


async def budget_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/budget apre il pannello. Scorciatoie: /budget 1500 · /budget ristoranti 300 · /budget [categoria] off"""
    chat_id = update.effective_chat.id
    args = context.args
    if not args:
        await reply(update, *await _overview(chat_id))
        return

    value = args[-1].lower()
    category = resolve_category(" ".join(args[:-1]), await db.user_categories(chat_id)) if len(args) > 1 else None

    if value in ("off", "rimuovi", "elimina"):
        removed = await (db.remove_cat_budget(chat_id, category) if category else db.remove_budget(chat_id))
        label = f"«{html.escape(category)}»" if category else "mensile"
        await reply(update, f"Budget {label} rimosso." if removed else f"Non c'era nessun budget {label}.")
        return

    cents = parse_amount(value)
    if cents is None:
        await reply(update, "Importo non valido. Esempi: <code>/budget 1500</code> · <code>/budget ristoranti 300</code>")
        return
    await _apply(update, chat_id, category, cents)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    parts = query.data.split(":", 2)  # b:<azione>[:<categoria>]
    action = parts[1]
    name = parts[2] if len(parts) > 2 else ""
    back = [btn("« Indietro", "b:o")]

    if action == "o":
        await edit(update, *await _overview(chat_id))
    elif action == "t":
        await ask(update, context, "budget_total", "Quanto vuoi spendere al massimo ogni mese? Scrivi l'importo, es. <code>1500</code>")
    elif action == "c":
        names = [c for c in await categories_for(chat_id) if fits(f"b:cs:{c}")]
        await edit(update, "Per quale categoria vuoi fissare un limite?", kb(
            *grid([btn(c, f"b:cs:{c}") for c in names]), back,
        ))
    elif action == "cs":
        await ask(
            update, context, "budget_cat",
            f"Quanto vuoi spendere al massimo ogni mese per «{html.escape(name)}»? Scrivi l'importo, es. <code>300</code>",
            category=name,
        )
    elif action == "r":
        rows = []
        if await db.get_budget(chat_id):
            rows.append([btn("💰 Budget totale", "b:rt")])
        rows += [[btn(f"📂 {cb['category']}", f"b:rc:{cb['category']}")]
                 for cb in await db.get_cat_budgets(chat_id) if fits(f"b:rc:{cb['category']}")]
        await edit(update, "Quale budget vuoi rimuovere?", kb(*rows, back))
    elif action == "rt":
        await db.remove_budget(chat_id)
        await edit(update, *await _overview(chat_id, "🗑 Budget mensile rimosso."))
    elif action == "rc":
        await db.remove_cat_budget(chat_id, name)
        await edit(update, *await _overview(chat_id, f"🗑 Budget «{html.escape(name)}» rimosso."))


async def _pending_amount(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict) -> None:
    cents = parse_amount(update.effective_message.text)
    if cents is None:
        await reply(update, "Non è un importo valido. Scrivi ad esempio <code>1500</code>.", CANCEL_KB)
        return
    clear_pending(context)
    await _apply(update, update.effective_chat.id, pending.get("category"), cents)


PENDING = {"budget_total": _pending_amount, "budget_cat": _pending_amount}
