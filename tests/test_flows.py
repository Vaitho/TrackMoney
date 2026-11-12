"""Conversazioni complete con il bot simulate dall'inizio alla fine (testo, comandi, bottoni)."""
import datetime as dt
import time
from datetime import datetime, timedelta
from unittest.mock import patch

from telegram import Chat as TgChat, Message, Update, User
from telegram.ext import Application, MessageHandler

import db
import handlers
import pricing
from config import TZ
from handlers.common import PENDING_TTL
from pricing import PageInfo

from helpers import Chat, with_db


# ---------- spese ----------

@with_db
async def test_add_edit_and_delete_an_expense():
    chat = Chat()
    sent = await chat.say("caffè 1,20")
    assert "1,20 €" in sent.text and "Ristoranti e bar" in sent.text
    assert chat.labels() == ["↩️ Annulla", "✏️ Modifica"]

    detail = await chat.tap("Modifica")
    assert detail.kind == "edit" and "1,20 €" in detail.text

    await chat.tap("Importo")
    assert "nuovo importo" in chat.last.text
    await chat.say("boh")  # risposta non valida: la domanda resta aperta
    assert "non è un importo valido" in chat.last.text.lower()
    await chat.say("2,50")
    assert "2,50 €" in chat.last.text
    (expense,) = await db.list_expenses(chat.chat_id)
    assert expense["amount_cents"] == 250  # modificata, non registrata come nuova spesa

    await chat.tap("Categoria")
    await chat.tap("Svago")
    assert (await db.get_expense(expense["id"], chat.chat_id))["category"] == "Svago"

    await chat.tap("Data")
    await chat.tap("Ieri")
    spent = datetime.fromtimestamp((await db.get_expense(expense["id"], chat.chat_id))["spent_at"], TZ)
    assert spent.date() == (datetime.now(TZ) - timedelta(days=1)).date()

    await chat.tap("Descrizione")
    await chat.say("caffè al bar")
    assert (await db.get_expense(expense["id"], chat.chat_id))["note"] == "caffè al bar"

    await chat.cmd("spese")
    assert "caffè al bar" in chat.last.text
    await chat.tap("caffè al bar")
    after_delete = await chat.tap("Elimina")
    # l'elenco viene ridisegnato con la conferma, non sostituito da una frase sola
    assert after_delete.kind == "edit" and "Eliminata" in after_delete.text and "Nessuna spesa" in after_delete.text
    assert await db.count_expenses(chat.chat_id) == 0


@with_db
async def test_undo_right_after_adding():
    chat = Chat()
    await chat.say("45 benzina")
    sent = await chat.tap("Annulla")
    assert "annullata" in sent.text and await db.count_expenses(chat.chat_id) == 0
    # il bottone premuto una seconda volta non rompe nulla
    await chat.click(chat.last_query.data)
    assert "non esiste più" in chat.last.text


@with_db
async def test_new_category_typed_by_hand_reuses_an_existing_one():
    chat = Chat()
    await chat.say("30 fiori #regalo")
    await chat.say("20 libro")
    await chat.tap("Modifica")
    await chat.tap("Categoria")
    assert "Regalo" in " ".join(chat.labels())  # la categoria creata dall'utente è tra le scelte
    await chat.tap("Nuova categoria")
    await chat.say("regali")
    categories = {e["category"] for e in await db.list_expenses(chat.chat_id)}
    assert categories == {"Regalo"}  # 'regali' è stata ricondotta a 'Regalo'


@with_db
async def test_expense_list_is_paginated():
    chat = Chat()
    for i in range(20):
        await db.add_expense(chat.chat_id, 100 + i, "Altro", f"spesa {i}", int(time.time()) - i * 60)
    first = await chat.cmd("spese")
    assert "pagina 1 di 3" in first.text and "spesa 0" in first.text and "spesa 8" not in first.text
    second = await chat.tap("Più vecchie")
    assert "pagina 2 di 3" in second.text and "spesa 8" in second.text
    assert await chat.tap("Più recenti") and "pagina 1 di 3" in chat.last.text


@with_db
async def test_text_that_is_not_an_expense_gets_a_hint():
    chat = Chat()
    sent = await chat.say("ciao come va")
    assert "Non ho capito" in sent.text and await db.count_expenses(chat.chat_id) == 0


# ---------- domande in sospeso ----------

@with_db
async def test_cancel_and_expiry_of_a_pending_question():
    chat = Chat()
    await chat.say("caffè 1,20")
    await chat.tap("Modifica")
    await chat.tap("Importo")
    await chat.tap("Annulla")
    assert "annullata" in chat.last.text
    await chat.say("5 gelato")  # non c'è più una domanda aperta: è una spesa nuova
    assert await db.count_expenses(chat.chat_id) == 2

    await chat.tap("Modifica")
    await chat.tap("Importo")
    chat.context.user_data["pending"]["at"] -= PENDING_TTL + 1  # la domanda è scaduta
    await chat.say("3 brioche")
    assert await db.count_expenses(chat.chat_id) == 3

    assert "nessuna operazione" in (await chat.cmd("annulla")).text
    await chat.tap("Modifica")
    await chat.tap("Importo")
    assert "annullata" in (await chat.cmd("annulla")).text


@with_db
async def test_buttons_of_old_messages_get_an_explanation():
    chat = Chat()
    await chat.click("expdel:12")  # formato dei bottoni della versione precedente
    assert chat.last_query.alert and "non è più attivo" in chat.last_query.alert


# ---------- budget ----------

@with_db
async def test_budget_panel():
    chat = Chat()
    assert "Non hai ancora impostato un budget" in (await chat.cmd("budget")).text

    await chat.tap("Budget totale")
    await chat.say("100")
    assert "impostato a <b>100,00 €</b>" in chat.last.text
    assert (await db.get_budget(chat.chat_id))["amount_cents"] == 10000

    await chat.say("85 spesa")
    assert "80%" in chat.last.text  # avviso di soglia subito dopo la spesa

    await chat.cmd("budget")
    await chat.tap("Per categoria")
    await chat.tap("Alimentari")
    await chat.say("50")
    texts = " ".join(s.text for s in chat.log[-2:])
    assert "«Alimentari» impostato" in texts and "superato" in texts  # 85 € su 50 €: avviso immediato

    await chat.cmd("budget")
    await chat.tap("Rimuovi un budget")
    removed = await chat.tap("Alimentari")
    assert removed.kind == "edit" and "rimosso" in removed.text and "💰 Budget" in removed.text
    assert await db.get_cat_budgets(chat.chat_id) == []


@with_db
async def test_budget_shortcuts():
    chat = Chat()
    await chat.cmd("budget", "1.500")
    assert (await db.get_budget(chat.chat_id))["amount_cents"] == 150000
    await chat.cmd("budget", "ristoranti", "300")
    assert [(b["category"], b["amount_cents"]) for b in await db.get_cat_budgets(chat.chat_id)] == [("Ristoranti e bar", 30000)]
    await chat.cmd("budget", "ristoranti", "off")
    await chat.cmd("budget", "off")
    assert await db.get_budget(chat.chat_id) is None and await db.get_cat_budgets(chat.chat_id) == []
    assert "non valido" in (await chat.cmd("budget", "tanto")).text


# ---------- ricorrenti ----------

@with_db
async def test_recurring_panel():
    chat = Chat()
    assert "Non hai spese ricorrenti" in (await chat.cmd("ricorrenti")).text
    await chat.tap("Aggiungi")
    await chat.say("netflix")  # incompleto: la domanda resta aperta
    assert "Non ho capito" in chat.last.text
    await chat.say("12,99 netflix 15")
    assert "12,99 €" in chat.last.text and "Spese al mese" in chat.last.text
    assert await db.count_expenses(chat.chat_id) == 0  # nessuna delle due risposte è diventata una spesa

    after = await chat.tap("netflix")
    assert after.kind == "edit" and "eliminata" in after.text and "Non hai spese ricorrenti" in after.text

    await chat.cmd("ricorrente", "800", "affitto", "ogni", "1")  # vecchio comando, ancora valido
    assert [r["note"] for r in await db.list_recurring(chat.chat_id)] == ["affitto"]


# ---------- prezzi ----------

def shop(price: float | None, title: str = "Cuffie X", available: bool | None = True):
    async def load_page(url, use_browser=False):
        return PageInfo(price, title, available), False
    return patch.object(pricing, "load_page", load_page)


@with_db
async def test_price_monitor_from_a_pasted_link():
    chat = Chat()
    with shop(100.0):
        sent = await chat.say("https://shop.example/cuffie")
    assert "Prodotto aggiunto" in sent.text and "100,00 €" in sent.text and "ogni 1 ora" in sent.text
    (sub,) = await db.list_subscriptions(chat.chat_id)
    assert sub["interval_min"] == 60 and await db.count_expenses(chat.chat_id) == 0

    with shop(100.0):
        again = await chat.say("https://shop.example/cuffie")
    assert "già monitorando" in again.text and await db.count_subscriptions(chat.chat_id) == 1

    # obiettivo sopra il prezzo attuale: avviso subito (il primo controllo ha già salvato lo stato)
    await db.save_check_ok(sub["id"], {"price": 100.0, "min": 100.0, "available": True, "below_target": False})
    await chat.tap("Obiettivo")
    await chat.say("150")
    assert "già sotto l'obiettivo" in chat.last.text
    assert (await db.get_subscription(sub["id"], chat.chat_id))["params"]["target"] == 150.0

    await chat.tap("Obiettivo")
    removed = await chat.tap("Rimuovi obiettivo")
    assert "Obiettivo rimosso" in removed.text
    assert "target" not in (await db.get_subscription(sub["id"], chat.chat_id))["params"]

    await chat.tap("Frequenza")
    await chat.tap("6 ore")
    assert (await db.get_subscription(sub["id"], chat.chat_id))["interval_min"] == 360

    paused = await chat.tap("Metti in pausa")
    assert "in pausa" in paused.text and (await db.get_subscription(sub["id"], chat.chat_id))["active"] == 0
    await chat.tap("Riattiva")
    assert (await db.get_subscription(sub["id"], chat.chat_id))["active"] == 1

    await chat.tap("Storico")
    assert chat.last.kind == "text" and "non è ancora cambiato" in chat.last.text
    await db.add_price_point(sub["id"], 100.0)
    await db.add_price_point(sub["id"], 90.0)
    await chat.cmd("prezzi")
    await chat.tap("Cuffie X")
    await chat.tap("Storico")
    assert chat.last.kind == "photo"

    await chat.cmd("prezzi")
    await chat.tap("Cuffie X")
    confirm = await chat.tap("Elimina")
    assert "Eliminare" in confirm.text and await db.count_subscriptions(chat.chat_id) == 1  # prima chiede conferma
    gone = await chat.tap("Sì, elimina")
    assert "Prodotto eliminato" in gone.text and await db.count_subscriptions(chat.chat_id) == 0
    assert await db.price_points(sub["id"]) == []


@with_db
async def test_price_monitor_from_the_add_button_and_unreadable_pages():
    chat = Chat()
    await chat.cmd("prezzi")
    await chat.tap("Aggiungi prodotto")
    with shop(None, available=None):
        await chat.say("https://shop.example/strano")
    assert "Non riesco a leggere il prezzo" in chat.last.text
    with shop(59.9, "Lampada"):
        await chat.say("https://shop.example/lampada")  # la domanda era ancora aperta
    assert "Prodotto aggiunto" in chat.last.text
    listing = await chat.cmd("prezzi")
    assert "Lampada" in listing.text and "59,90 €" in listing.text


@with_db
async def test_price_monitor_limit():
    chat = Chat()
    for i in range(10):
        await db.add_subscription(chat.chat_id, {"url": f"http://shop/{i}", "title": f"P{i}"}, 60)
    with shop(10.0):
        sent = await chat.say("https://shop.example/undicesimo")
    assert "il massimo" in sent.text and await db.count_subscriptions(chat.chat_id) == 10


# ---------- report ----------

@with_db
async def test_report_and_automatic_delivery():
    chat = Chat()
    assert "Non ci sono spese" in (await chat.cmd("report")).text
    await chat.say("caffè 1,20")
    await chat.say("45 benzina")

    report = await chat.cmd("report")
    assert report.kind == "photo" and "Totale: <b>46,20 €</b> (2 spese)" in report.text
    assert await chat.tap("Anno") and chat.last.kind == "photo"
    assert "Non ci sono spese" in (await chat.tap("Mese scorso")).text

    await chat.tap("Invio automatico")
    assert "⬜️ Settimanale" in chat.labels()
    toggled = await chat.tap("Settimanale")
    assert toggled.kind == "edit" and "✅ Settimanale" in chat.labels()
    prefs = await db.get_report_prefs(chat.chat_id)
    assert (prefs["weekly"], prefs["monthly"]) == (1, 0)
    await chat.tap("Settimanale")
    assert (await db.get_report_prefs(chat.chat_id))["weekly"] == 0

    export = await chat.cmd("export")
    assert export.kind == "document" and "2 spese" in export.text


# ---------- registrazione dei comandi ----------

def test_handlers_register_and_ignore_edited_messages():
    app = Application.builder().token("123456:TEST-TOKEN").build()
    handlers.register(app)
    text_handler = app.handlers[0][-1]
    assert isinstance(text_handler, MessageHandler)

    message = Message(
        1, dt.datetime.now(dt.timezone.utc), TgChat(1, "private"), from_user=User(1, "u", False), text="caffè 1,20"
    )
    assert text_handler.check_update(Update(1, message=message))
    assert not text_handler.check_update(Update(2, edited_message=message))  # una modifica non crea una seconda spesa


def test_menu_lists_only_registered_commands():
    registered = {name for names, _ in handlers.COMMANDS for name in names}
    assert {name for name, _ in handlers.MENU} <= registered
    assert len(handlers.MENU) == 8
