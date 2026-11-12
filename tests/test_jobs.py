from datetime import datetime
from unittest.mock import patch

from telegram.error import Forbidden, TelegramError

import db
import jobs
import pricing
from config import TZ
from pricing import CheckResult
from summaries import budget_lines, month_bounds

from helpers import FakeBot, with_db

CHAT = 7
PARAMS = {"url": "http://shop/x", "title": "Cuffie X", "initial_price": 100.0}


def test_month_bounds_cross_the_year():
    january = datetime(2026, 1, 15, tzinfo=TZ)
    start, end = month_bounds(-1, january)
    assert (start.year, start.month, end.year, end.month) == (2025, 12, 2026, 1)
    start, end = month_bounds(0, datetime(2026, 12, 3, tzinfo=TZ))
    assert (start.month, end.year, end.month) == (12, 2027, 1)


# ---------- budget ----------

@with_db
async def test_budget_alerts_fire_once_per_level():
    await db.set_budget(CHAT, 10000)  # 100 €
    await db.add_expense(CHAT, 5000, "Altro", "a")
    text, alerts = await budget_lines(CHAT, check_alerts=True)
    assert "50,00 € / 100,00 €" in text and alerts == []

    await db.add_expense(CHAT, 3500, "Altro", "b")  # 85%
    _, alerts = await budget_lines(CHAT, check_alerts=True)
    assert len(alerts) == 1 and "80%" in alerts[0]
    assert (await budget_lines(CHAT, check_alerts=True))[1] == []  # non si ripete

    await db.add_expense(CHAT, 2000, "Altro", "c")  # 105%
    _, alerts = await budget_lines(CHAT, check_alerts=True)
    assert len(alerts) == 1 and "superato" in alerts[0] and "5,00 €" in alerts[0]


@with_db
async def test_category_budget_counts_only_its_category():
    await db.set_cat_budget(CHAT, "Svago", 2000)
    await db.add_expense(CHAT, 9000, "Casa", "affitto")
    await db.add_expense(CHAT, 1000, "Svago", "cinema")
    text, alerts = await budget_lines(CHAT, check_alerts=True, category="Svago")
    assert "📂 Svago: 10,00 € / 20,00 € (50%)" in text and alerts == []
    # il budget di un'altra categoria non compare se non richiesto
    assert (await budget_lines(CHAT, check_alerts=False, category="Casa"))[0] == ""


# ---------- spese ricorrenti ----------

@with_db
async def test_recurring_is_registered_once_a_month():
    await db.add_recurring(CHAT, 1299, "Svago", "netflix", 15, None)
    bot = FakeBot()

    await jobs.register_recurring(bot, datetime(2026, 11, 14, 8, tzinfo=TZ))
    assert await db.count_expenses(CHAT) == 0  # non è ancora il 15

    await jobs.register_recurring(bot, datetime(2026, 11, 17, 8, tzinfo=TZ))  # bot spento il 15 e 16
    (expense,) = await db.list_expenses(CHAT)
    assert expense["note"] == "netflix"
    assert datetime.fromtimestamp(expense["spent_at"], TZ).day == 15  # con la data giusta, non quella del recupero
    assert len(bot.log) == 1 and "ricorrente" in bot.log[0].text

    await jobs.register_recurring(bot, datetime(2026, 11, 20, 8, tzinfo=TZ))
    assert await db.count_expenses(CHAT) == 1  # niente doppioni

    await jobs.register_recurring(bot, datetime(2026, 12, 15, 8, tzinfo=TZ))
    assert await db.count_expenses(CHAT) == 2


@with_db
async def test_recurring_on_the_31st_runs_on_the_last_day_of_short_months():
    await db.add_recurring(CHAT, 80000, "Casa", "affitto", 31, None)
    await jobs.register_recurring(FakeBot(), datetime(2026, 2, 28, 8, tzinfo=TZ))
    assert await db.count_expenses(CHAT) == 1


@with_db
async def test_recurring_is_registered_even_if_the_notice_cannot_be_sent():
    await db.add_recurring(CHAT, 1299, "Svago", "netflix", 1, None)
    await jobs.register_recurring(FakeBot(fail=Forbidden("blocked")), datetime(2026, 11, 2, 8, tzinfo=TZ))
    assert await db.count_expenses(CHAT) == 1


# ---------- resoconti automatici ----------

@with_db
async def test_reports_wait_for_nine_and_catch_up_later():
    await db.add_expense(CHAT, 2500, "Svago", "cinema", int(datetime(2026, 11, 4, 12, tzinfo=TZ).timestamp()))
    await db.add_expense(CHAT, 9000, "Casa", "bolletta", int(datetime(2026, 10, 20, 12, tzinfo=TZ).timestamp()))
    await db.set_report_prefs(CHAT, True, True, "2026-11-02", "2026-10")
    bot = FakeBot()

    await jobs.send_due_reports(bot, datetime(2026, 11, 9, 8, 30, tzinfo=TZ))  # lunedì, ma prima delle 9
    assert bot.log == []

    await jobs.send_due_reports(bot, datetime(2026, 11, 9, 14, 0, tzinfo=TZ))  # bot acceso solo nel pomeriggio
    kinds = [s.kind for s in bot.log]
    assert kinds == ["bot", "photo", "bot"]  # settimanale (testo) + mensile (grafico e riepilogo)
    assert "Settimana 02/11 – 08/11" in bot.log[0].text and "25,00 €" in bot.log[0].text
    assert "ottobre" in bot.log[1].text

    await jobs.send_due_reports(bot, datetime(2026, 11, 10, 9, 0, tzinfo=TZ))
    assert len(bot.log) == 3  # già inviati: non si ripetono


# ---------- controllo prezzi ----------

async def _fresh_sub() -> dict:
    sub_id = await db.add_subscription(CHAT, dict(PARAMS), 60)
    return await db.get_subscription(sub_id, CHAT)


@with_db
async def test_price_check_saves_state_history_and_notifies():
    sub = await _fresh_sub()
    bot = FakeBot()

    async def result(params, state):
        return CheckResult(["📉 sceso"], {"price": 90.0, "min": 90.0, "available": True}, new_price=90.0)
    with patch.object(pricing, "check", result):
        await jobs.check_prices(bot)

    saved = await db.get_subscription(sub["id"], CHAT)
    assert saved["last_state"]["price"] == 90.0 and saved["last_check"] is not None
    assert [p for _, p in await db.price_points(sub["id"])] == [90.0]
    assert [s.text for s in bot.log] == ["📉 sceso"]
    assert await db.due_subscriptions() == []  # il prossimo controllo è tra un intervallo


@with_db
async def test_failed_send_keeps_the_alert_for_the_next_interval():
    sub = await _fresh_sub()

    async def result(params, state):
        return CheckResult(["📉 sceso"], {"price": 90.0}, new_price=90.0)
    with patch.object(pricing, "check", result):
        await jobs.check_prices(FakeBot(fail=TelegramError("timeout")))

    saved = await db.get_subscription(sub["id"], CHAT)
    assert saved["last_state"] is None  # stato non salvato: l'avviso verrà ritentato
    assert await db.price_points(sub["id"]) == []
    assert await db.due_subscriptions() == []  # ma non ogni minuto


@with_db
async def test_blocked_user_gets_deactivated():
    sub = await _fresh_sub()

    async def result(params, state):
        return CheckResult(["📉 sceso"], {"price": 90.0})
    with patch.object(pricing, "check", result):
        await jobs.check_prices(FakeBot(fail=Forbidden("blocked")))
    assert (await db.get_subscription(sub["id"], CHAT))["active"] == 0


@with_db
async def test_repeated_failures_pause_the_product():
    sub = await _fresh_sub()
    bot = FakeBot()

    async def broken(params, state):
        raise RuntimeError("prezzo non trovato")
    with patch.object(pricing, "check", broken):
        for _ in range(jobs.MAX_FAILURES):
            await db._run("UPDATE subscriptions SET last_check = NULL WHERE id = ?", (sub["id"],))
            await jobs.check_prices(bot)

    assert (await db.get_subscription(sub["id"], CHAT))["active"] == 0
    assert len(bot.log) == 1 and "in pausa" in bot.log[0].text
