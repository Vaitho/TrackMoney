"""Lavori periodici: controllo prezzi, spese ricorrenti, resoconti automatici."""
import asyncio
import calendar
import html
import logging
from datetime import datetime, timedelta
from pathlib import Path

from telegram.error import Forbidden, TelegramError
from telegram.ext import ContextTypes, JobQueue

import db
import pricing
from config import TZ, settings
from formatting import eur
from summaries import budget_lines, build_period_report, month_key, weekly_text

log = logging.getLogger(__name__)

MAX_FAILURES = 10  # dopo tanti errori consecutivi il prodotto viene messo in pausa
CONCURRENCY = 5
REPORT_HOUR = 9  # i resoconti automatici partono dalle 9:00


# ---------- controllo prezzi ----------

async def _check_one(sub: dict, bot, sem: asyncio.Semaphore) -> None:
    async with sem:
        try:
            result = await pricing.check(sub["params"], sub["last_state"])
        except Exception as exc:  # un sito che non risponde non deve fermare gli altri controlli
            log.warning("Controllo fallito per il prodotto %s: %s", sub["id"], exc)
            if await db.save_check_failed(sub["id"]) >= MAX_FAILURES:
                await db.deactivate(sub["id"])
                try:
                    await bot.send_message(
                        sub["chat_id"],
                        f"⚠️ Non riesco più a leggere il prezzo di «{html.escape(sub['params'].get('title', 'un prodotto'))}» "
                        "e l'ho messo in pausa. Puoi riattivarlo da /prezzi.",
                        parse_mode="HTML",
                    )
                except TelegramError:
                    pass
            return

    for text in result.notifications:
        try:
            await bot.send_message(sub["chat_id"], text, parse_mode="HTML")
        except Forbidden:  # l'utente ha bloccato il bot
            await db.deactivate_chat(sub["chat_id"])
            return
        except TelegramError as exc:
            # Lo stato non viene salvato, così l'avviso verrà ritentato al prossimo controllo
            # (tra un intervallo, non tra un minuto).
            log.warning("Invio fallito a %s: %s", sub["chat_id"], exc)
            await db.touch_check(sub["id"])
            return

    if result.new_price is not None:
        await db.add_price_point(sub["id"], result.new_price)
    await db.save_check_ok(sub["id"], result.state)


async def check_prices(bot) -> None:
    subs = await db.due_subscriptions()
    if subs:
        sem = asyncio.Semaphore(CONCURRENCY)
        await asyncio.gather(*(_check_one(s, bot, sem) for s in subs))


# ---------- spese ricorrenti ----------

def effective_day(day: int, year: int, month: int) -> int:
    """Il 31 diventa l'ultimo giorno nei mesi più corti."""
    return min(day, calendar.monthrange(year, month)[1])


async def register_recurring(bot, now: datetime | None = None) -> None:
    """Registra le spese ricorrenti arrivate a scadenza, anche in ritardo se il bot era spento."""
    now = now or datetime.now(TZ)
    key = month_key(now)
    for r in await db.all_recurring():
        day = effective_day(r["day"], now.year, now.month)
        if r["last_run"] == key or now.day < day:
            continue
        when = int(datetime(now.year, now.month, day, 12, tzinfo=TZ).timestamp())
        if r["income"]:
            await db.add_income(r["chat_id"], r["amount_cents"], r["note"], when)
        else:
            await db.add_expense(r["chat_id"], r["amount_cents"], r["category"], r["note"], when)
        await db.mark_recurring_run(r["id"], key)
        try:
            if r["income"]:
                status, alerts = "", []
                text = f"🔁 Registrata entrata ricorrente: <b>+{eur(r['amount_cents'])}</b> · {html.escape(r['note'])}"
            else:
                status, alerts = await budget_lines(r["chat_id"], check_alerts=True, category=r["category"])
                text = (
                    f"🔁 Registrata spesa ricorrente: <b>{eur(r['amount_cents'])}</b> · {html.escape(r['note'])} "
                    f"({html.escape(r['category'])})"
                )
            await bot.send_message(r["chat_id"], text + (f"\n\n{status}" if status else ""), parse_mode="HTML")
            for alert in alerts:
                await bot.send_message(r["chat_id"], alert)
        except TelegramError as exc:
            log.warning("Avviso di ricorrente non inviato a %s: %s", r["chat_id"], exc)


# ---------- resoconti automatici ----------

async def send_due_reports(bot, now: datetime | None = None) -> None:
    """Invia i resoconti dovuti. Gira ogni ora: se il bot era spento all'orario previsto, recupera dopo."""
    now = now or datetime.now(TZ)
    if now.hour < REPORT_HOUR:
        return
    today = now.date()
    monday = today - timedelta(days=today.weekday())
    this_month = month_key(now)
    for prefs in await db.get_report_prefs_all():
        chat_id = prefs["chat_id"]
        try:
            if prefs["weekly"] and (prefs["last_weekly"] or "") < monday.isoformat():
                week_start = datetime(monday.year, monday.month, monday.day, tzinfo=TZ) - timedelta(days=7)
                text = await weekly_text(chat_id, week_start)
                if text:
                    await bot.send_message(chat_id, text, parse_mode="HTML")
                await db.mark_report_sent(chat_id, last_weekly=monday.isoformat())
            if prefs["monthly"] and (prefs["last_monthly"] or "") < this_month:
                report = await build_period_report(chat_id, "month", offset=-1)
                if report:
                    await bot.send_photo(chat_id, report.image, caption=f"📈 {report.title}")
                    await bot.send_message(chat_id, report.text, parse_mode="HTML")
                await db.mark_report_sent(chat_id, last_monthly=this_month)
        except TelegramError as exc:
            log.warning("Resoconto automatico non inviato a %s: %s", chat_id, exc)


# ---------- copia di sicurezza ----------

async def make_backup(directory: Path, keep: int, now: datetime | None = None) -> Path | None:
    """Una copia del database al giorno, tenendo le ultime `keep`. Ritorna il file creato (None se c'era già)."""
    now = now or datetime.now(TZ)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"bot-{now.strftime('%Y-%m-%d')}.db"
    if target.exists():
        return None
    partial = target.with_suffix(".part")  # così una copia interrotta non viene scambiata per una completa
    partial.unlink(missing_ok=True)
    await db.backup_to(str(partial))
    partial.replace(target)
    for old in sorted(directory.glob("bot-*.db"), reverse=True)[keep:]:
        old.unlink(missing_ok=True)
    return target


# ---------- registrazione ----------

async def _backup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        created = await make_backup(settings.backups_path, settings.backup_keep)
        if created:
            log.info("Copia di sicurezza creata: %s", created)
    except Exception:
        log.exception("Copia di sicurezza fallita")


async def _prices_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await check_prices(context.bot)


async def _recurring_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await register_recurring(context.bot)


async def _reports_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_due_reports(context.bot)


def register(job_queue: JobQueue) -> None:
    job_queue.run_repeating(_prices_job, interval=60, first=10)
    job_queue.run_repeating(_recurring_job, interval=3600, first=30)
    job_queue.run_repeating(_reports_job, interval=3600, first=60)
    job_queue.run_repeating(_backup_job, interval=3600, first=90)
