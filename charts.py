"""Immagini dei resoconti (matplotlib, senza interfaccia grafica)."""
import calendar
import functools
import io
import threading
from collections import defaultdict
from datetime import datetime

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from config import TZ  # noqa: E402
from formatting import MESI_BREVI, eur, n_spese  # noqa: E402

# Palette (tema chiaro)
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
BLUE = "#2a78d6"
ORANGE = "#eb6834"

MAX_CATEGORIES = 7
DASHED = (0, (5, 4))


_render_lock = threading.Lock()


def _one_at_a_time(fn):
    """pyplot ha uno stato globale: due grafici disegnati insieme (da thread diversi) si rovinerebbero a vicenda."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _render_lock:
            return fn(*args, **kwargs)
    return wrapper


def _style(ax) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _png(fig, name: str) -> io.BytesIO:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    plt.close(fig)
    buf.seek(0)
    buf.name = name
    return buf


def category_totals(rows: list[dict]) -> list[tuple[str, int]]:
    """Totale per categoria, dal più alto; le categorie minori confluiscono in 'Altre'."""
    totals: dict[str, int] = defaultdict(int)
    for r in rows:
        totals[r["category"]] += r["amount_cents"]
    ordered = sorted(totals.items(), key=lambda kv: -kv[1])
    if len(ordered) > MAX_CATEGORIES:
        rest = sum(v for _, v in ordered[MAX_CATEGORIES - 1:])
        ordered = ordered[: MAX_CATEGORIES - 1] + [("Altre", rest)]
    return ordered


@_one_at_a_time
def build_report(
    rows: list[dict], title: str, mode: str, start: datetime, budget_cents: int | None, income_cents: int = 0
) -> io.BytesIO:
    """Resoconto spese. mode = 'month' (spesa cumulata nel mese) oppure 'year' (spesa per mese)."""
    total = sum(r["amount_cents"] for r in rows)
    fig, (ax1, ax2) = plt.subplots(
        1, 2, figsize=(11, 5.6), dpi=150, gridspec_kw={"width_ratios": [1, 1.25], "wspace": 0.35}
    )
    fig.patch.set_facecolor(SURFACE)
    fig.text(0.04, 0.945, title, fontsize=17, fontweight="bold", color=INK, va="center")
    subtitle = f"Totale speso: {eur(total, decimals=False)}  ·  {n_spese(len(rows))}"
    if income_cents:
        balance = income_cents - total
        subtitle = (
            f"Entrate {eur(income_cents, decimals=False)}  ·  Spese {eur(total, decimals=False)}  ·  "
            f"Saldo {'+' if balance > 0 else ''}{eur(balance, decimals=False)}"
        )
    fig.text(0.04, 0.885, subtitle, fontsize=11, color=INK_2, va="center")

    # ---- sinistra: spesa per categoria (barre orizzontali ordinate) ----
    cats = category_totals(rows)
    _style(ax1)
    ax1.grid(axis="y", visible=False)
    names = [c for c, _ in cats][::-1]
    values = [v for _, v in cats][::-1]
    ax1.barh(names, values, color=BLUE, height=0.6)
    top = max(values) if values else 1
    for i, v in enumerate(values):
        ax1.text(v + top * 0.02, i, f"{eur(v, decimals=False)}  ({v / total * 100:.0f}%)",
                 va="center", fontsize=9, color=INK)
    ax1.set_xlim(0, top * 1.55)
    ax1.set_xticks([])
    ax1.spines["bottom"].set_visible(False)
    ax1.tick_params(axis="y", labelsize=10, colors=INK_2)
    ax1.set_title("Per categoria", loc="left", fontsize=11, color=INK_2, pad=10)

    # ---- destra: andamento nel tempo ----
    _style(ax2)
    if mode == "month":
        days = calendar.monthrange(start.year, start.month)[1]
        now = datetime.now(TZ)
        is_current = (now.year, now.month) == (start.year, start.month)
        last_day = now.day if is_current else days
        daily = [0] * (days + 1)
        for r in rows:
            daily[datetime.fromtimestamp(r["spent_at"], TZ).day] += r["amount_cents"]
        xs, cum, running = [], [], 0
        for d in range(1, last_day + 1):
            running += daily[d]
            xs.append(d)
            cum.append(running)
        ax2.plot(xs, cum, color=BLUE, linewidth=2)
        ax2.plot(xs[-1], cum[-1], "o", color=BLUE, markersize=8, markeredgecolor=SURFACE, markeredgewidth=2)
        ax2.annotate(eur(cum[-1], decimals=False), (xs[-1], cum[-1]), textcoords="offset points", xytext=(0, 10),
                     ha="right" if xs[-1] > days * 0.8 else "center", fontsize=9, color=INK)
        ymax = max(cum[-1], budget_cents or 0)
        if budget_cents:
            ax2.axhline(budget_cents, color=ORANGE, linewidth=1.5, linestyle=DASHED)
            ax2.text(1, budget_cents + ymax * 0.02, f"Budget {eur(budget_cents, decimals=False)}",
                     color=INK_2, fontsize=9, va="bottom")
        ax2.set_xlim(1, days)
        ax2.set_ylim(0, ymax * 1.15 or 1)
        ax2.set_xticks(sorted({1, 5, 10, 15, 20, 25, days}))
        ax2.set_title("Spesa cumulata nel mese", loc="left", fontsize=11, color=INK_2, pad=10)
    else:
        monthly = [0] * 12
        for r in rows:
            monthly[datetime.fromtimestamp(r["spent_at"], TZ).month - 1] += r["amount_cents"]
        ax2.bar(range(12), monthly, color=BLUE, width=0.6)
        ymax = max(max(monthly), budget_cents or 0) or 1
        for i, v in enumerate(monthly):
            if v:
                ax2.text(i, v + ymax * 0.02, eur(v, decimals=False), ha="center", fontsize=8, color=INK)
        if budget_cents:
            ax2.axhline(budget_cents, color=ORANGE, linewidth=1.5, linestyle=DASHED)
            ax2.text(-0.4, budget_cents + ymax * 0.02, f"Budget mensile {eur(budget_cents, decimals=False)}",
                     color=INK_2, fontsize=9)
        ax2.set_xticks(range(12), MESI_BREVI)
        ax2.set_ylim(0, ymax * 1.2)
        ax2.set_title("Spesa per mese", loc="left", fontsize=11, color=INK_2, pad=10)
    ax2.set_yticks(ax2.get_yticks())
    ax2.set_yticklabels([eur(t, decimals=False) for t in ax2.get_yticks()])

    fig.subplots_adjust(left=0.13, right=0.97, top=0.78, bottom=0.1)
    return _png(fig, "resoconto.png")


@_one_at_a_time
def build_price_history(points: list[tuple[int, float]], title: str, target: float | None) -> io.BytesIO:
    """Storico del prezzo: linea a gradini (il prezzo resta valido fino al cambio successivo)."""
    def label(price: float) -> str:
        return eur(round(price * 100))

    real_x = [datetime.fromtimestamp(ts, TZ) for ts, _ in points]
    real_y = [p for _, p in points]
    xs, ys = [*real_x, datetime.now(TZ)], [*real_y, real_y[-1]]

    fig, ax = plt.subplots(figsize=(9, 5.2), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    _style(ax)
    fig.text(0.05, 0.945, title[:70], fontsize=15, fontweight="bold", color=INK, va="center")
    fig.text(
        0.05, 0.885,
        f"Attuale {label(ys[-1])}  ·  minimo {label(min(ys))}  ·  massimo {label(max(ys))}",
        fontsize=10.5, color=INK_2, va="center",
    )

    ax.step(xs, ys, where="post", color=BLUE, linewidth=2)
    ax.plot(real_x, real_y, "o", color=BLUE, markersize=7, markeredgecolor=SURFACE, markeredgewidth=2)

    low_i = real_y.index(min(real_y))
    if real_y[low_i] != ys[-1]:
        ax.annotate(label(real_y[low_i]), (real_x[low_i], real_y[low_i]), textcoords="offset points",
                    xytext=(0, -16), ha="center", fontsize=9, color=INK)
    ax.annotate(label(ys[-1]), (xs[-1], ys[-1]), textcoords="offset points",
                xytext=(0, 10), ha="right", fontsize=9, color=INK)

    lo, hi = min(ys), max(ys)
    if target:
        ax.axhline(target, color=ORANGE, linewidth=1.5, linestyle=DASHED)
        ax.text(xs[0], target, f" Obiettivo {label(target)}", color=INK_2, fontsize=9, va="bottom")
        lo, hi = min(lo, target), max(hi, target)
    pad = (hi - lo) * 0.2 or hi * 0.05
    ax.set_ylim(max(0, lo - pad), hi + pad)
    ax.set_yticks(ax.get_yticks())
    ax.set_yticklabels([label(t) for t in ax.get_yticks()])
    ax.grid(axis="x", visible=False)

    locator = mdates.AutoDateLocator(tz=TZ)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator, tz=TZ))

    fig.subplots_adjust(left=0.13, right=0.96, top=0.82, bottom=0.1)
    return _png(fig, "storico.png")
