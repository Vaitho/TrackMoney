"""Calcoli condivisi tra comandi e job: periodi, budget, testo e immagine dei resoconti."""
import asyncio
import html
import io
from dataclasses import dataclass
from datetime import datetime, timedelta

import charts
import db
from config import TZ
from formatting import MESI, bar, eur, n_spese
from parsing import KNOWN_CATEGORIES

WARN_LEVEL = 80  # % del budget oltre il quale scatta il primo avviso
TOP_CATEGORIES = 8


@dataclass
class Report:
    title: str
    image: io.BytesIO
    text: str


def month_bounds(offset: int = 0, now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or datetime.now(TZ)
    index = now.year * 12 + now.month - 1 + offset
    start = datetime(index // 12, index % 12 + 1, 1, tzinfo=TZ)
    end = datetime((index + 1) // 12, (index + 1) % 12 + 1, 1, tzinfo=TZ)
    return start, end


def month_key(now: datetime | None = None) -> str:
    return (now or datetime.now(TZ)).strftime("%Y-%m")


def period(mode: str, offset: int = 0) -> tuple[datetime, datetime, str]:
    """(inizio, fine, titolo) del periodo richiesto: 'month' con offset, oppure 'year' (anno corrente)."""
    if mode == "year":
        year = datetime.now(TZ).year
        return datetime(year, 1, 1, tzinfo=TZ), datetime(year + 1, 1, 1, tzinfo=TZ), f"Spese {year}"
    start, end = month_bounds(offset)
    return start, end, f"Spese di {MESI[start.month - 1]} {start.year}"


async def rows_between(chat_id: int, start: datetime, end: datetime) -> list[dict]:
    return await db.expenses_between(chat_id, int(start.timestamp()), int(end.timestamp()))


async def incomes_total(chat_id: int, start: datetime, end: datetime) -> int:
    rows = await db.incomes_between(chat_id, int(start.timestamp()), int(end.timestamp()))
    return sum(r["amount_cents"] for r in rows)


async def month_totals(chat_id: int, offset: int = 0) -> tuple[int, int]:
    """(entrate, spese) del mese, in centesimi."""
    start, end = month_bounds(offset)
    spent = sum(r["amount_cents"] for r in await rows_between(chat_id, start, end))
    return await incomes_total(chat_id, start, end), spent


def balance_text(income: int, spent: int) -> str:
    balance = income - spent
    sign = "+" if balance > 0 else ""
    return f"{sign}{eur(balance)}"


def balance_lines(income: int, spent: int) -> list[str]:
    """Entrate, spese e saldo. Vuoto se nel periodo non ci sono entrate."""
    if not income:
        return []
    balance = income - spent
    saldo = f"{'📈' if balance >= 0 else '📉'} Saldo: <b>{balance_text(income, spent)}</b>"
    if balance > 0:
        saldo += f" (risparmiato il {balance / income * 100:.0f}%)"
    return ["", f"💰 Entrate: <b>{eur(income)}</b>", f"💸 Spese: <b>{eur(spent)}</b>", saldo]


async def categories_for(chat_id: int) -> list[str]:
    """Categorie predefinite più quelle create dall'utente."""
    extra = [c for c in await db.user_categories(chat_id) if c not in KNOWN_CATEGORIES]
    return [*KNOWN_CATEGORIES, *extra]


# ---------- budget ----------

def _alert_text(label: str, level: int, spent: int, limit: int) -> str:
    if level == 100:
        if spent > limit:
            return f"🚨 Hai superato il budget {label} di {eur(spent - limit)}!"
        return f"🚨 Hai raggiunto il budget {label}: non ti resta più nulla da spendere."
    return f"⚠️ Hai già usato l'{WARN_LEVEL}% del budget {label}: ti restano {eur(limit - spent)}."


async def budget_lines(
    chat_id: int, check_alerts: bool, category: str | None = None, all_categories: bool = False
) -> tuple[str, list[str]]:
    """Righe di stato del budget (totale + categorie richieste) ed eventuali avvisi da inviare.
    Con check_alerts=True gli avvisi nuovi vengono segnati come inviati (uno per livello al mese)."""
    start, end = month_bounds()
    rows = await rows_between(chat_id, start, end)
    key = month_key()
    lines: list[str] = []
    alerts: list[str] = []

    async def handle(stored: dict, spent: int, label: str, title: str, save) -> None:
        limit = stored["amount_cents"]
        pct = spent / limit * 100
        lines.append(f"{title}: {eur(spent)} / {eur(limit)} ({pct:.0f}%)\n{bar(pct)}")
        if check_alerts:
            level = 100 if pct >= 100 else WARN_LEVEL if pct >= WARN_LEVEL else 0
            already = stored["alert_level"] if stored["alert_month"] == key else 0
            if level > already:
                await save(level)
                alerts.append(_alert_text(label, level, spent, limit))

    budget = await db.get_budget(chat_id)
    if budget:
        total = sum(r["amount_cents"] for r in rows)
        await handle(budget, total, "mensile", "💰 Budget", lambda lv: db.set_budget_alert(chat_id, key, lv))

    for cb in await db.get_cat_budgets(chat_id):
        name = cb["category"]
        if all_categories or name == category:
            spent = sum(r["amount_cents"] for r in rows if r["category"] == name)
            await handle(
                cb, spent, f"«{name}»", f"📂 {html.escape(name)}",
                lambda lv, c=name: db.set_cat_alert(chat_id, c, key, lv),
            )
    return "\n".join(lines), alerts


# ---------- resoconti ----------

def breakdown(rows: list[dict]) -> list[tuple[str, int]]:
    totals: dict[str, int] = {}
    for r in rows:
        totals[r["category"]] = totals.get(r["category"], 0) + r["amount_cents"]
    return sorted(totals.items(), key=lambda kv: -kv[1])


def _category_lines(rows: list[dict], limit: int) -> list[str]:
    total = sum(r["amount_cents"] for r in rows)
    cats = breakdown(rows)
    lines = [f"• {html.escape(c)}: {eur(v)} ({v / total * 100:.0f}%)" for c, v in cats[:limit]]
    rest = sum(v for _, v in cats[limit:])
    if rest:
        lines.append(f"• Altre: {eur(rest)} ({rest / total * 100:.0f}%)")
    return lines


def _versus(total: int, previous_rows: list[dict], label: str) -> list[str]:
    if not previous_rows:
        return []
    prev = sum(r["amount_cents"] for r in previous_rows)
    return ["", f"{label}: {eur(prev)} ({(total - prev) / prev * 100:+.0f}%)"]


async def build_period_report(chat_id: int, mode: str = "month", offset: int = 0) -> Report | None:
    """Grafico + riepilogo testuale del periodo. None se non ci sono spese."""
    start, end, title = period(mode, offset)
    rows = await rows_between(chat_id, start, end)
    if not rows:
        return None
    budget = await db.get_budget(chat_id)
    total = sum(r["amount_cents"] for r in rows)
    income = await incomes_total(chat_id, start, end)
    image = await asyncio.to_thread(
        charts.build_report, rows, title, mode, start, budget["amount_cents"] if budget else None, income
    )
    lines = [f"📊 <b>{title}</b>", f"Totale: <b>{eur(total)}</b> ({n_spese(len(rows))})", ""]
    lines += _category_lines(rows, TOP_CATEGORIES)
    lines += balance_lines(income, total)
    if mode == "month":
        prev_start, prev_end = month_bounds(offset - 1)
        lines += _versus(total, await rows_between(chat_id, prev_start, prev_end), "Mese precedente")
        if offset == 0:
            status, _ = await budget_lines(chat_id, check_alerts=False, all_categories=True)
            if status:
                lines += ["", status]
    return Report(title, image, "\n".join(lines))


async def weekly_text(chat_id: int, week_start: datetime) -> str | None:
    """Riepilogo testuale della settimana che inizia a week_start. None se non ci sono spese."""
    week_end = week_start + timedelta(days=7)
    rows = await rows_between(chat_id, week_start, week_end)
    if not rows:
        return None
    total = sum(r["amount_cents"] for r in rows)
    last_day = (week_end - timedelta(days=1)).strftime("%d/%m")
    lines = [
        f"📅 <b>Settimana {week_start.strftime('%d/%m')} – {last_day}</b>",
        f"Totale: <b>{eur(total)}</b> ({n_spese(len(rows))})",
        "",
        *_category_lines(rows, 5),
        *_versus(total, await rows_between(chat_id, week_start - timedelta(days=7), week_start), "Settimana precedente"),
    ]
    status, _ = await budget_lines(chat_id, check_alerts=False)
    if status:
        lines += ["", status]
    return "\n".join(lines)
