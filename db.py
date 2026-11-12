"""Accesso al database SQLite. Una sola connessione condivisa, aperta con connect() e chiusa con close()."""
import json
import time

import aiosqlite

from config import MIN_CHECK_INTERVAL, settings

PRICE_SOURCE = "prezzo"

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    params TEXT NOT NULL,
    interval_min INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    last_check INTEGER,
    last_state TEXT,
    fail_count INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subs_chat ON subscriptions(chat_id);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sub_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    price REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hist_sub ON price_history(sub_id, ts);

CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    category TEXT NOT NULL,
    note TEXT NOT NULL,
    spent_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exp_chat_date ON expenses(chat_id, spent_at);

CREATE TABLE IF NOT EXISTS incomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    note TEXT NOT NULL,
    received_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inc_chat_date ON incomes(chat_id, received_at);

CREATE TABLE IF NOT EXISTS recurring (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    category TEXT NOT NULL,
    note TEXT NOT NULL,
    day INTEGER NOT NULL,
    last_run TEXT,
    income INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS budgets (
    chat_id INTEGER PRIMARY KEY,
    amount_cents INTEGER NOT NULL,
    alert_month TEXT,
    alert_level INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS category_budgets (
    chat_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    alert_month TEXT,
    alert_level INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, category)
);

CREATE TABLE IF NOT EXISTS report_prefs (
    chat_id INTEGER PRIMARY KEY,
    weekly INTEGER NOT NULL DEFAULT 0,
    monthly INTEGER NOT NULL DEFAULT 0,
    last_weekly TEXT,
    last_monthly TEXT
);
"""

_conn: aiosqlite.Connection | None = None


async def connect(path: str | None = None) -> None:
    global _conn
    await close()
    _conn = await aiosqlite.connect(path or settings.db_path)
    _conn.row_factory = aiosqlite.Row
    await _conn.executescript(SCHEMA)
    await _conn.commit()
    await _migrate()


async def _migrate() -> None:
    """Adegua i database creati dalle versioni precedenti."""
    columns = {r["name"] for r in await _all("PRAGMA table_info(recurring)")}
    if "income" not in columns:
        await _run("ALTER TABLE recurring ADD COLUMN income INTEGER NOT NULL DEFAULT 0")
    # Frequenza minima dei controlli: i prodotti impostati su 15 o 30 minuti passano a quella minima.
    await _run("UPDATE subscriptions SET interval_min = ? WHERE interval_min < ?", (MIN_CHECK_INTERVAL, MIN_CHECK_INTERVAL))


async def close() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


def _db() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("Database non aperto: chiama db.connect() all'avvio.")
    return _conn


async def _all(query: str, args: tuple = ()) -> list[dict]:
    async with _db().execute(query, args) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def _one(query: str, args: tuple = ()) -> dict | None:
    rows = await _all(query, args)
    return rows[0] if rows else None


async def _insert(query: str, args: tuple = ()) -> int:
    """Esegue un INSERT e ritorna l'id della nuova riga."""
    async with _db().execute(query, args) as cur:
        rowid = cur.lastrowid
    await _db().commit()
    return rowid


async def _run(query: str, args: tuple = ()) -> int:
    """Esegue UPDATE/DELETE e ritorna il numero di righe toccate."""
    async with _db().execute(query, args) as cur:
        count = cur.rowcount
    await _db().commit()
    return count


# ---------- prodotti monitorati ----------

def _sub(row: dict | None) -> dict | None:
    if row is None:
        return None
    row["params"] = json.loads(row["params"])
    row["last_state"] = json.loads(row["last_state"]) if row["last_state"] else None
    return row


async def add_subscription(chat_id: int, params: dict, interval_min: int) -> int:
    return await _insert(
        "INSERT INTO subscriptions (chat_id, source, params, interval_min, created_at) VALUES (?, ?, ?, ?, ?)",
        (chat_id, PRICE_SOURCE, json.dumps(params), interval_min, int(time.time())),
    )


async def list_subscriptions(chat_id: int) -> list[dict]:
    rows = await _all(
        "SELECT * FROM subscriptions WHERE chat_id = ? AND source = ? ORDER BY id", (chat_id, PRICE_SOURCE)
    )
    return [_sub(r) for r in rows]


async def count_subscriptions(chat_id: int) -> int:
    row = await _one(
        "SELECT COUNT(*) AS n FROM subscriptions WHERE chat_id = ? AND source = ?", (chat_id, PRICE_SOURCE)
    )
    return row["n"]


async def get_subscription(sub_id: int, chat_id: int) -> dict | None:
    return _sub(await _one(
        "SELECT * FROM subscriptions WHERE id = ? AND chat_id = ? AND source = ?", (sub_id, chat_id, PRICE_SOURCE)
    ))


async def delete_subscription(sub_id: int, chat_id: int) -> bool:
    deleted = await _run("DELETE FROM subscriptions WHERE id = ? AND chat_id = ?", (sub_id, chat_id)) > 0
    if deleted:
        await _run("DELETE FROM price_history WHERE sub_id = ?", (sub_id,))
    return deleted


async def set_active(sub_id: int, chat_id: int, active: bool) -> None:
    await _run(
        "UPDATE subscriptions SET active = ?, fail_count = 0 WHERE id = ? AND chat_id = ?",
        (int(active), sub_id, chat_id),
    )


async def set_interval(sub_id: int, chat_id: int, interval_min: int) -> None:
    await _run(
        "UPDATE subscriptions SET interval_min = ? WHERE id = ? AND chat_id = ?", (interval_min, sub_id, chat_id)
    )


async def update_subscription(sub_id: int, chat_id: int, params: dict, state: dict | None) -> None:
    await _run(
        "UPDATE subscriptions SET params = ?, last_state = ? WHERE id = ? AND chat_id = ?",
        (json.dumps(params), json.dumps(state) if state is not None else None, sub_id, chat_id),
    )


async def due_subscriptions() -> list[dict]:
    rows = await _all(
        "SELECT * FROM subscriptions WHERE active = 1 AND source = ? "
        "AND (last_check IS NULL OR last_check + interval_min * 60 <= ?)",
        (PRICE_SOURCE, int(time.time())),
    )
    return [_sub(r) for r in rows]


async def save_check_ok(sub_id: int, state: dict) -> None:
    await _run(
        "UPDATE subscriptions SET last_check = ?, last_state = ?, fail_count = 0 WHERE id = ?",
        (int(time.time()), json.dumps(state), sub_id),
    )


async def save_check_failed(sub_id: int) -> int:
    """Segna un controllo fallito e ritorna il numero di fallimenti consecutivi."""
    await _run(
        "UPDATE subscriptions SET last_check = ?, fail_count = fail_count + 1 WHERE id = ?",
        (int(time.time()), sub_id),
    )
    row = await _one("SELECT fail_count FROM subscriptions WHERE id = ?", (sub_id,))
    return row["fail_count"] if row else 0


async def touch_check(sub_id: int) -> None:
    """Rimanda il prossimo controllo senza cambiare lo stato (es. invio della notifica fallito)."""
    await _run("UPDATE subscriptions SET last_check = ? WHERE id = ?", (int(time.time()), sub_id))


async def deactivate(sub_id: int) -> None:
    await _run("UPDATE subscriptions SET active = 0 WHERE id = ?", (sub_id,))


async def deactivate_chat(chat_id: int) -> None:
    await _run("UPDATE subscriptions SET active = 0 WHERE chat_id = ?", (chat_id,))


async def add_price_point(sub_id: int, price: float) -> None:
    await _insert(
        "INSERT INTO price_history (sub_id, ts, price) VALUES (?, ?, ?)", (sub_id, int(time.time()), price)
    )


async def price_points(sub_id: int) -> list[tuple[int, float]]:
    rows = await _all("SELECT ts, price FROM price_history WHERE sub_id = ? ORDER BY ts, id", (sub_id,))
    return [(r["ts"], r["price"]) for r in rows]


# ---------- spese ----------

async def add_expense(
    chat_id: int, amount_cents: int, category: str, note: str, spent_at: int | None = None
) -> int:
    return await _insert(
        "INSERT INTO expenses (chat_id, amount_cents, category, note, spent_at) VALUES (?, ?, ?, ?, ?)",
        (chat_id, amount_cents, category, note, spent_at or int(time.time())),
    )


async def get_expense(expense_id: int, chat_id: int) -> dict | None:
    return await _one("SELECT * FROM expenses WHERE id = ? AND chat_id = ?", (expense_id, chat_id))


async def update_expense(expense_id: int, chat_id: int, fields: dict) -> bool:
    allowed = {"amount_cents", "category", "note", "spent_at"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return False
    sets = ", ".join(f"{k} = ?" for k in fields)
    return await _run(
        f"UPDATE expenses SET {sets} WHERE id = ? AND chat_id = ?", (*fields.values(), expense_id, chat_id)
    ) > 0


async def delete_expense(expense_id: int, chat_id: int) -> bool:
    return await _run("DELETE FROM expenses WHERE id = ? AND chat_id = ?", (expense_id, chat_id)) > 0


async def list_expenses(chat_id: int, limit: int = 10, offset: int = 0) -> list[dict]:
    return await _all(
        "SELECT * FROM expenses WHERE chat_id = ? ORDER BY spent_at DESC, id DESC LIMIT ? OFFSET ?",
        (chat_id, limit, offset),
    )


async def count_expenses(chat_id: int) -> int:
    row = await _one("SELECT COUNT(*) AS n FROM expenses WHERE chat_id = ?", (chat_id,))
    return row["n"]


async def expenses_between(chat_id: int, start_ts: int, end_ts: int) -> list[dict]:
    return await _all(
        "SELECT * FROM expenses WHERE chat_id = ? AND spent_at >= ? AND spent_at < ? ORDER BY spent_at, id",
        (chat_id, start_ts, end_ts),
    )


async def user_categories(chat_id: int) -> list[str]:
    """Tutte le categorie che l'utente ha già usato (spese, budget, ricorrenti)."""
    rows = await _all(
        "SELECT category FROM expenses WHERE chat_id = ? "
        "UNION SELECT category FROM category_budgets WHERE chat_id = ? "
        "UNION SELECT category FROM recurring WHERE chat_id = ? ORDER BY 1",
        (chat_id, chat_id, chat_id),
    )
    return [r["category"] for r in rows]


# ---------- entrate ----------

async def add_income(chat_id: int, amount_cents: int, note: str, received_at: int | None = None) -> int:
    return await _insert(
        "INSERT INTO incomes (chat_id, amount_cents, note, received_at) VALUES (?, ?, ?, ?)",
        (chat_id, amount_cents, note, received_at or int(time.time())),
    )


async def get_income(income_id: int, chat_id: int) -> dict | None:
    return await _one("SELECT * FROM incomes WHERE id = ? AND chat_id = ?", (income_id, chat_id))


async def delete_income(income_id: int, chat_id: int) -> bool:
    return await _run("DELETE FROM incomes WHERE id = ? AND chat_id = ?", (income_id, chat_id)) > 0


async def list_incomes(chat_id: int, limit: int = 8, offset: int = 0) -> list[dict]:
    return await _all(
        "SELECT * FROM incomes WHERE chat_id = ? ORDER BY received_at DESC, id DESC LIMIT ? OFFSET ?",
        (chat_id, limit, offset),
    )


async def count_incomes(chat_id: int) -> int:
    return (await _one("SELECT COUNT(*) AS n FROM incomes WHERE chat_id = ?", (chat_id,)))["n"]


async def incomes_between(chat_id: int, start_ts: int, end_ts: int) -> list[dict]:
    return await _all(
        "SELECT * FROM incomes WHERE chat_id = ? AND received_at >= ? AND received_at < ? ORDER BY received_at, id",
        (chat_id, start_ts, end_ts),
    )


# ---------- spese ricorrenti ----------

async def add_recurring(
    chat_id: int, amount_cents: int, category: str, note: str, day: int, last_run: str | None,
    income: bool = False,
) -> int:
    return await _insert(
        "INSERT INTO recurring (chat_id, amount_cents, category, note, day, last_run, income) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (chat_id, amount_cents, category, note, day, last_run, int(income)),
    )


async def list_recurring(chat_id: int) -> list[dict]:
    return await _all("SELECT * FROM recurring WHERE chat_id = ? ORDER BY day, id", (chat_id,))


async def all_recurring() -> list[dict]:
    return await _all("SELECT * FROM recurring")


async def delete_recurring(rec_id: int, chat_id: int) -> bool:
    return await _run("DELETE FROM recurring WHERE id = ? AND chat_id = ?", (rec_id, chat_id)) > 0


async def mark_recurring_run(rec_id: int, month_key: str) -> None:
    await _run("UPDATE recurring SET last_run = ? WHERE id = ?", (month_key, rec_id))


# ---------- budget ----------

async def set_budget(chat_id: int, amount_cents: int) -> None:
    await _run(
        "INSERT INTO budgets (chat_id, amount_cents) VALUES (?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET amount_cents = excluded.amount_cents, "
        "alert_month = NULL, alert_level = 0",
        (chat_id, amount_cents),
    )


async def get_budget(chat_id: int) -> dict | None:
    return await _one("SELECT * FROM budgets WHERE chat_id = ?", (chat_id,))


async def remove_budget(chat_id: int) -> bool:
    return await _run("DELETE FROM budgets WHERE chat_id = ?", (chat_id,)) > 0


async def set_budget_alert(chat_id: int, month: str, level: int) -> None:
    await _run("UPDATE budgets SET alert_month = ?, alert_level = ? WHERE chat_id = ?", (month, level, chat_id))


async def set_cat_budget(chat_id: int, category: str, amount_cents: int) -> None:
    await _run(
        "INSERT INTO category_budgets (chat_id, category, amount_cents) VALUES (?, ?, ?) "
        "ON CONFLICT(chat_id, category) DO UPDATE SET amount_cents = excluded.amount_cents, "
        "alert_month = NULL, alert_level = 0",
        (chat_id, category, amount_cents),
    )


async def get_cat_budgets(chat_id: int) -> list[dict]:
    return await _all("SELECT * FROM category_budgets WHERE chat_id = ? ORDER BY category", (chat_id,))


async def remove_cat_budget(chat_id: int, category: str) -> bool:
    return await _run(
        "DELETE FROM category_budgets WHERE chat_id = ? AND category = ?", (chat_id, category)
    ) > 0


async def set_cat_alert(chat_id: int, category: str, month: str, level: int) -> None:
    await _run(
        "UPDATE category_budgets SET alert_month = ?, alert_level = ? WHERE chat_id = ? AND category = ?",
        (month, level, chat_id, category),
    )


# ---------- dati dell'utente e copie di sicurezza ----------

USER_TABLES = ("subscriptions", "expenses", "incomes", "recurring", "budgets", "category_budgets", "report_prefs")


async def user_data_counts(chat_id: int) -> dict[str, int]:
    counts = {}
    for table in USER_TABLES:
        counts[table] = (await _one(f"SELECT COUNT(*) AS n FROM {table} WHERE chat_id = ?", (chat_id,)))["n"]
    return counts


async def delete_user_data(chat_id: int) -> None:
    """Cancella tutto ciò che il bot sa di un utente, in un'unica operazione."""
    conn = _db()
    try:
        await conn.execute(
            "DELETE FROM price_history WHERE sub_id IN (SELECT id FROM subscriptions WHERE chat_id = ?)", (chat_id,)
        )
        for table in USER_TABLES:
            await conn.execute(f"DELETE FROM {table} WHERE chat_id = ?", (chat_id,))
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise


async def backup_to(path: str) -> None:
    """Copia consistente del database in un nuovo file (che non deve esistere già)."""
    await _db().execute("VACUUM INTO ?", (path,))


# ---------- resoconti automatici ----------

async def get_report_prefs(chat_id: int) -> dict | None:
    return await _one("SELECT * FROM report_prefs WHERE chat_id = ?", (chat_id,))


async def get_report_prefs_all() -> list[dict]:
    return await _all("SELECT * FROM report_prefs WHERE weekly = 1 OR monthly = 1")


async def set_report_prefs(
    chat_id: int, weekly: bool, monthly: bool, last_weekly: str | None, last_monthly: str | None
) -> None:
    await _run(
        "INSERT INTO report_prefs (chat_id, weekly, monthly, last_weekly, last_monthly) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET weekly = excluded.weekly, "
        "monthly = excluded.monthly, last_weekly = excluded.last_weekly, last_monthly = excluded.last_monthly",
        (chat_id, int(weekly), int(monthly), last_weekly, last_monthly),
    )


async def mark_report_sent(
    chat_id: int, last_weekly: str | None = None, last_monthly: str | None = None
) -> None:
    if last_weekly:
        await _run("UPDATE report_prefs SET last_weekly = ? WHERE chat_id = ?", (last_weekly, chat_id))
    if last_monthly:
        await _run("UPDATE report_prefs SET last_monthly = ? WHERE chat_id = ?", (last_monthly, chat_id))
