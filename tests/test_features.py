"""Entrate e saldo, cancellazione dei dati, copie di sicurezza, migrazione del database."""
import asyncio
import sqlite3
from datetime import datetime, timedelta

import pytest

import charts
import db
import jobs
from config import MIN_CHECK_INTERVAL, TZ
from parsing import parse_income, parse_recurring
from summaries import balance_lines, build_period_report, month_totals

from helpers import Chat, FakeBot, with_db

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=TZ)


# ---------- interpretazione del testo ----------

@pytest.mark.parametrize("text, cents, note", [
    ("+1500 stipendio", 150000, "stipendio"),
    ("+ 200,50 rimborso", 20050, "rimborso"),
    ("stipendio 1.800", 180000, "stipendio"),  # la parola basta, anche senza il +
    ("1500 tredicesima", 150000, "tredicesima"),
    ("+50 vendita libro 10/11", 5000, "vendita libro"),
])
def test_incomes_are_recognised(text, cents, note):
    income = parse_income(text, now=NOW)
    assert income is not None and (income.cents, income.note) == (cents, note)


@pytest.mark.parametrize("text", ["caffè 1,20", "45 benzina", "rimborso 20", "+ciao", "+", "ciao"])
def test_spending_is_not_an_income(text):
    assert parse_income(text, now=NOW) is None


def test_income_date():
    assert parse_income("+50 vendita 10/11", now=NOW).when.date() == datetime(2026, 11, 10).date()


def test_recurring_incomes():
    rec = parse_recurring("+1500 stipendio 27")
    assert (rec.income, rec.cents, rec.day, rec.category) == (True, 150000, 27, "Entrata")
    assert parse_recurring("1500 stipendio 27").income  # riconosciuta dalla parola
    assert not parse_recurring("12,99 netflix 15").income


# ---------- entrate e saldo ----------

@with_db
async def test_income_flow_and_balance_in_the_report():
    chat = Chat()
    await chat.say("45 benzina")
    sent = await chat.say("+1500 stipendio")
    assert "+1.500,00 €" in sent.text and "Saldo del mese: <b>+1.455,00 €</b>" in sent.text
    assert await db.count_expenses(chat.chat_id) == 1 and await db.count_incomes(chat.chat_id) == 1

    report = await chat.cmd("report")
    assert report.kind == "photo"
    assert "Entrate: <b>1.500,00 €</b>" in report.text and "Spese: <b>45,00 €</b>" in report.text
    assert "Saldo: <b>+1.455,00 €</b> (risparmiato il 97%)" in report.text

    # elenco e eliminazione delle entrate dal pannello delle spese
    await chat.cmd("spese")
    listing = await chat.tap("Entrate")
    assert listing.kind == "edit" and "+1.500,00 €" in listing.text
    await chat.tap("stipendio")
    after = await chat.tap("Elimina")
    assert "Eliminata" in after.text and "Nessuna entrata" in after.text
    assert await db.count_incomes(chat.chat_id) == 0
    assert "Le tue spese" in (await chat.tap("Spese")).text  # il bottone riporta all'elenco delle spese


@with_db
async def test_undo_an_income():
    chat = Chat()
    await chat.say("+200 rimborso")
    assert "annullata" in (await chat.tap("Annulla")).text
    assert await db.count_incomes(chat.chat_id) == 0


@with_db
async def test_months_are_kept_separate():
    now = datetime.now(TZ)
    this_month = int(now.replace(day=1, hour=12).timestamp())
    last_month = int((now.replace(day=1, hour=12) - timedelta(days=5)).timestamp())
    await db.add_income(7, 10000, "regalo", last_month)
    await db.add_income(7, 5000, "rimborso", this_month)
    await db.add_expense(7, 4000, "Altro", "x", this_month)
    assert await month_totals(7, 0) == (5000, 4000)
    assert await month_totals(7, -1) == (10000, 0)


def test_balance_text():
    negative = "\n".join(balance_lines(10000, 14000))
    assert "📉 Saldo: <b>-40,00 €</b>" in negative and "risparmiato" not in negative
    assert "📈 Saldo: <b>+60,00 €</b> (risparmiato il 60%)" in "\n".join(balance_lines(10000, 4000))
    assert balance_lines(0, 5000) == []  # senza entrate non si parla di saldo


@with_db
async def test_recurring_income_is_registered_as_income():
    await db.add_recurring(7, 150000, "Entrata", "stipendio", 27, None, income=True)
    bot = FakeBot()
    await jobs.register_recurring(bot, datetime(2026, 11, 28, 8, tzinfo=TZ))
    assert await db.count_expenses(7) == 0 and await db.count_incomes(7) == 1
    assert "entrata ricorrente" in bot.log[0].text and "+1.500,00 €" in bot.log[0].text


@with_db
async def test_recurring_panel_shows_incomes_separately():
    chat = Chat()
    await chat.cmd("ricorrenti", "+1500", "stipendio", "27")
    await chat.cmd("ricorrenti", "12,99", "netflix", "15")
    listing = await chat.cmd("ricorrenti")
    assert "+1.500,00 €" in listing.text and "Spese al mese: <b>12,99 €</b>" in listing.text
    assert "entrate: <b>1.500,00 €</b>" in listing.text


@with_db
async def test_csv_contains_expenses_and_incomes():
    chat = Chat()
    await chat.say("45 benzina")
    await chat.say("+1500 stipendio")
    export = await chat.cmd("export")
    assert export.kind == "document" and "1 spese e 1 entrate" in export.text


def test_income_shows_in_the_report_image():
    rows = [{"category": "Casa", "amount_cents": 50000, "spent_at": int(NOW.timestamp())}]
    image = charts.build_report(rows, "Prova", "month", datetime(2026, 11, 1, tzinfo=TZ), None, income_cents=150000)
    assert image.getvalue().startswith(b"\x89PNG")


# ---------- cancellazione dei dati ----------

@with_db
async def test_delete_all_my_data_leaves_other_users_alone():
    me, other = Chat(1), Chat(2)
    for chat in (me, other):
        await chat.say("45 benzina")
        await chat.say("+1500 stipendio")
        await chat.cmd("budget", "1000")
        await chat.cmd("budget", "ristoranti", "100")
        await chat.cmd("ricorrenti", "12,99", "netflix", "15")
        sub_id = await db.add_subscription(chat.chat_id, {"url": f"http://shop/{chat.chat_id}", "title": "P"}, 60)
        await db.add_price_point(sub_id, 10.0)
        await db.set_report_prefs(chat.chat_id, True, False, None, None)

    ask = await me.cmd("cancella_dati")
    assert "1 spese, 1 entrate, 1 prodotti monitorati" in ask.text
    assert await db.count_expenses(1) == 1  # finché non confermi non cambia nulla

    sure = await me.tap("Cancella tutto")
    assert "Non si può annullare" in sure.text and await db.count_expenses(1) == 1
    done = await me.tap("Sì, cancella definitivamente")
    assert "ho cancellato tutti i tuoi dati" in done.text

    assert all(n == 0 for n in (await db.user_data_counts(1)).values())
    assert await db.get_report_prefs(1) is None
    assert await db.price_points(1) == [] and me.context.user_data == {}
    # l'altro utente è intatto, storico prezzi compreso
    assert all(n >= 1 for n in (await db.user_data_counts(2)).values())
    assert len(await db.price_points(2)) == 1

    assert "niente" in (await me.cmd("cancella_dati")).text  # e si può ricominciare


@with_db
async def test_delete_data_can_be_declined_and_csv_downloaded_first():
    chat = Chat()
    await chat.say("45 benzina")
    await chat.cmd("cancella_dati")
    await chat.tap("Scarica un CSV")
    assert chat.last.kind == "document"
    await chat.tap("Cancella tutto")
    assert "Va bene" in (await chat.tap("No, tieni")).text
    assert await db.count_expenses(chat.chat_id) == 1


# ---------- copie di sicurezza ----------

@with_db
async def test_daily_backup_is_complete_and_old_ones_are_removed(tmp_path):
    directory = tmp_path / "backups"
    await db.add_expense(7, 1234, "Altro", "prova")

    first = await jobs.make_backup(directory, keep=3, now=datetime(2026, 11, 12, 10, tzinfo=TZ))
    assert first == directory / "bot-2026-11-12.db"
    copy = sqlite3.connect(first)
    assert copy.execute("SELECT note FROM expenses").fetchall() == [("prova",)]
    copy.close()
    assert await jobs.make_backup(directory, keep=3, now=datetime(2026, 11, 12, 18, tzinfo=TZ)) is None  # una al giorno

    for day in (13, 14, 15, 16):
        await jobs.make_backup(directory, keep=3, now=datetime(2026, 11, day, 10, tzinfo=TZ))
    assert sorted(p.name for p in directory.glob("*.db")) == ["bot-2026-11-14.db", "bot-2026-11-15.db", "bot-2026-11-16.db"]
    assert not list(directory.glob("*.part"))


# ---------- database delle versioni precedenti ----------

def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE recurring (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
            amount_cents INTEGER NOT NULL, category TEXT NOT NULL, note TEXT NOT NULL, day INTEGER NOT NULL, last_run TEXT);
        INSERT INTO recurring (chat_id, amount_cents, category, note, day) VALUES (1, 1299, 'Svago', 'netflix', 15);
        CREATE TABLE subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, source TEXT NOT NULL,
            params TEXT NOT NULL, interval_min INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1, last_check INTEGER,
            last_state TEXT, fail_count INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);
        INSERT INTO subscriptions (chat_id, source, params, interval_min, created_at) VALUES (1, 'prezzo', '{}', 15, 0);
        INSERT INTO subscriptions (chat_id, source, params, interval_min, created_at) VALUES (1, 'prezzo', '{}', 360, 0);
    """)
    con.commit()
    con.close()

    async def go():
        await db.connect(str(path))
        try:
            (rec,) = await db.list_recurring(1)
            intervals = [s["interval_min"] for s in await db.list_subscriptions(1)]
            await db.connect(str(path))  # un secondo avvio non deve rompere nulla
            return rec["income"], intervals
        finally:
            await db.close()
    income, intervals = asyncio.run(go())
    assert income == 0  # le ricorrenti esistenti restano spese
    assert intervals == [MIN_CHECK_INTERVAL, 360]  # 15 minuti diventano 1 ora, 6 ore resta com'è
