"""Formattazione condivisa: importi in euro, nomi dei mesi, barra di avanzamento."""

MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
        "agosto", "settembre", "ottobre", "novembre", "dicembre"]
MESI_BREVI = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]


def eur(cents: float, decimals: bool = True) -> str:
    """12345 -> '123,45 €' (formato italiano). Gli importi sono sempre in centesimi."""
    text = f"{cents / 100:,.2f}" if decimals else f"{cents / 100:,.0f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".") + " €"


def bar(pct: float, width: int = 10) -> str:
    filled = max(0, min(width, int(pct / 100 * width)))
    return "▓" * filled + "░" * (width - filled)


def n_spese(n: int) -> str:
    return "1 spesa" if n == 1 else f"{n} spese"
