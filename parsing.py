"""Interpretazione del testo scritto dall'utente: importi, date, categorie, spese."""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from config import TZ

# parola chiave -> categoria (si può sempre forzare la categoria con #tag)
# L'ordine conta: vince la prima categoria che trova un match (nessuna parola è ripetuta).
CATEGORY_KEYWORDS = {
    "Alimentari": [
        "spesa", "supermercato", "coop", "conad", "esselunga", "lidl", "carrefour", "pane", "frutta",
        "verdura", "eurospin", "md", "aldi", "penny", "pam", "despar", "iper", "macelleria", "pescheria",
        "panificio", "latte", "uova", "mercato", "salumeria", "forno", "alimentari",
    ],
    "Ristoranti e bar": [
        "caffè", "caffe", "cena", "pranzo", "pizza", "ristorante", "bar", "colazione", "aperitivo", "gelato",
        "cornetto", "brioche", "cappuccino", "trattoria", "osteria", "pub", "birra", "sushi", "kebab",
        "panino", "mcdonald", "burger", "just eat", "glovo", "deliveroo", "delivery", "asporto",
        "pasticceria", "spritz", "cocktail", "vino",
    ],
    "Trasporti": [
        "benzina", "diesel", "treno", "bus", "metro", "taxi", "autostrada", "parcheggio", "pedaggio",
        "gpl", "carburante", "rifornimento", "uber", "italo", "trenitalia", "biglietto", "tram", "aereo",
        "volo", "ryanair", "easyjet", "traghetto", "bollo", "assicurazione auto", "meccanico", "gommista",
        "revisione", "tagliando", "lavaggio", "monopattino", "telepass",
    ],
    "Casa": [
        "affitto", "bolletta", "luce", "gas", "internet", "condominio", "mutuo", "acqua", "tari", "rifiuti",
        "enel", "eni", "a2a", "hera", "wifi", "fibra", "riscaldamento", "idraulico", "elettricista", "ikea",
        "mobili", "arredamento", "lavatrice", "detersivo", "pulizie", "manutenzione", "leroy merlin",
        "bricolage", "imu",
    ],
    "Salute": [
        "farmacia", "medico", "dentista", "visita", "ospedale", "ticket", "analisi", "medicina", "medicinali",
        "farmaco", "oculista", "occhiali", "lenti", "fisioterapia", "fisioterapista", "psicologo", "esame",
        "radiografia", "ecografia", "parafarmacia", "integratori",
    ],
    "Animali": [
        "veterinario", "crocchette", "cibo cani", "cibo gatti", "toelettatura", "pet", "arcaplanet", "lettiera",
    ],
    "Cura persona": [
        "parrucchiere", "barbiere", "estetista", "manicure", "unghie", "taglio", "capelli", "massaggio", "spa",
        "trucco", "sephora", "profumeria", "shampoo", "crema",
    ],
    "Istruzione": [
        "università", "universita", "tasse universitarie", "corso", "scuola", "libri", "lezione",
        "ripetizioni", "master", "cancelleria", "iscrizione", "udemy", "mensa",
    ],
    "Abbonamenti": [
        "abbonamento", "ricarica", "tim", "vodafone", "wind", "iliad", "fastweb", "icloud", "google one",
        "youtube premium", "chatgpt", "dropbox", "amazon prime", "canone", "rai",
    ],
    "Svago": [
        "cinema", "netflix", "spotify", "concerto", "libro", "videogioco", "viaggio", "teatro", "museo",
        "mostra", "disco", "discoteca", "steam", "playstation", "xbox", "nintendo", "disney", "prime video",
        "dazn", "hotel", "airbnb", "booking", "vacanza", "weekend", "palestra", "piscina", "sport", "calcio",
        "stadio", "parco", "escursione", "hobby", "giochi",
    ],
    "Shopping": [
        "amazon", "vestiti", "scarpe", "zalando", "abbigliamento", "maglia", "pantaloni", "giacca", "borsa",
        "zara", "h&m", "decathlon", "shein", "ebay", "subito", "mediaworld", "unieuro", "elettronica",
        "smartphone", "telefono", "computer", "cuffie", "regalo", "profumo", "gioielli", "orologio", "negozio",
    ],
}
KNOWN_CATEGORIES = [*CATEGORY_KEYWORDS, "Altro"]


def _compile_keywords() -> list[tuple[str, re.Pattern]]:
    """Un solo regex per categoria: parole intere (così 'bar' non becca 'barbiere'), con plurale semplice."""
    compiled = []
    for cat, words in CATEGORY_KEYWORDS.items():
        alts = "|".join(re.escape(w) + (r"[ei]?" if len(w) >= 5 else "") for w in words)
        compiled.append((cat, re.compile(rf"(?<!\w)(?:{alts})(?!\w)", re.I)))
    return compiled


_KEYWORD_PATTERNS = _compile_keywords()


def guess_category(note: str) -> str:
    return next((cat for cat, pat in _KEYWORD_PATTERNS if pat.search(note)), "Altro")

# 12 · 12,5 · 12.50 · 1.200 · 1.200,50
AMOUNT = r"(?:\d{1,3}(?:\.\d{3})+|\d+)(?:[.,]\d{1,2})?"
CUR = r"(?:euro|eur|€)?"
_RE_PARTS = re.compile(r"^(\d{1,3}(?:\.\d{3})+|\d+)(?:[.,](\d{1,2}))?$")
_RE_AMOUNT_ONLY = re.compile(rf"^\s*(?:€\s*)?(?P<amt>{AMOUNT})\s*{CUR}\s*$", re.I)
_RE_FIRST = re.compile(rf"^\s*(?:€\s*)?(?P<amt>{AMOUNT})\s*{CUR}\s+(?P<desc>.+?)\s*$", re.I)
_RE_LAST = re.compile(rf"^\s*(?P<desc>.+?)\s+(?:€\s*)?(?P<amt>{AMOUNT})\s*{CUR}\s*$", re.I)
_RE_TAG = re.compile(r"#(\w+)")
_RE_WORD_DATE = re.compile(r"\b(oggi|ieri|altroieri)\b", re.I)
_RE_DATE = re.compile(r"(?<![\d/,.])(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?![\d/])")
_RE_RECURRING = re.compile(
    rf"^\s*(?:€\s*)?(?P<amt>{AMOUNT})\s*{CUR}\s+(?P<desc>.+?)\s+"
    rf"(?:ogni\s+)?(?:mese\s+)?(?:il\s+)?(?P<day>\d{{1,2}})\s*$",
    re.I,
)


@dataclass
class Expense:
    cents: int
    note: str
    category: str
    when: datetime | None  # None = adesso


@dataclass
class Income:
    cents: int
    note: str
    when: datetime | None  # None = adesso


@dataclass
class Recurring:
    cents: int
    note: str
    category: str
    day: int
    income: bool = False


# Parole che da sole indicano un'entrata, anche senza il "+" davanti.
_INCOME_WORDS = re.compile(r"\b(stipendio|stipendi|tredicesima|quattordicesima)\b", re.I)
INCOME_CATEGORY = "Entrata"


def _to_cents(amount: str) -> int:
    m = _RE_PARTS.match(amount)
    whole = int(m.group(1).replace(".", ""))
    return whole * 100 + int((m.group(2) or "").ljust(2, "0"))


def parse_amount(text: str) -> int | None:
    """'1.500', '12,50 €', '80' -> centesimi. None se non è un importo valido."""
    m = _RE_AMOUNT_ONLY.match(text)
    if not m:
        return None
    return _to_cents(m["amt"]) or None


def _stem(word: str) -> str:
    """Toglie la vocale finale, così singolare e plurale coincidono (regalo/regali)."""
    word = word.strip().lower()
    return word[:-1] if len(word) > 3 and word[-1] in "aeio" else word


def resolve_category(name: str, existing: tuple[str, ...] | list[str] = ()) -> str:
    """Riporta un nome scritto dall'utente a una categoria già esistente, se ce n'è una che corrisponde."""
    low = name.strip().lower()
    if not low:
        return "Altro"
    candidates = [*KNOWN_CATEGORIES, *(c for c in existing if c not in KNOWN_CATEGORIES)]
    for c in candidates:
        if c.lower() == low:
            return c
    for c in candidates:
        if _stem(c) == _stem(low):
            return c
    if len(low) >= 3:
        for c in candidates:
            if c.lower().startswith(low):
                return c
    return low[:1].upper() + low[1:]


def extract_date(text: str, now: datetime | None = None) -> tuple[str, datetime | None]:
    """Estrae 'ieri', 'oggi', '15/11' o '15/11/2026'. Ritorna (testo senza la data, data trovata)."""
    now = now or datetime.now(TZ)

    def without(m: re.Match) -> str:
        return re.sub(r"\s+", " ", text[: m.start()] + text[m.end():]).strip()

    m = _RE_WORD_DATE.search(text)
    if m:
        delta = {"oggi": 0, "ieri": 1, "altroieri": 2}[m.group(1).lower()]
        day = now - timedelta(days=delta)
        return without(m), now if delta == 0 else day.replace(hour=12, minute=0, second=0, microsecond=0)

    m = _RE_DATE.search(text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else now.year
        if year < 100:
            year += 2000
        try:
            when = datetime(year, month, day, 12, tzinfo=TZ)
            if not m.group(3) and when > now:  # '15/12' scritto a novembre = dicembre dell'anno scorso
                when = when.replace(year=year - 1)
        except ValueError:
            return text, None
        if when > now + timedelta(days=1):
            return text, None
        return without(m), when
    return text, None


def _has_decimals(m: re.Match) -> bool:
    return "," in m["amt"] or "." in m["amt"]


def parse_expense(
    text: str, existing: tuple[str, ...] | list[str] = (), now: datetime | None = None
) -> Expense | None:
    """'caffè 1,20' · '45 benzina' · 'pizza 12 ieri #svago' -> Expense. None se non è una spesa."""
    text, when = extract_date(text, now)
    tag = _RE_TAG.search(text)
    clean = _RE_TAG.sub("", text).strip()

    first, last = _RE_FIRST.match(clean), _RE_LAST.match(clean)
    if first and last:
        # Due numeri ('2 caffè 3'): l'importo è quello in fondo, a meno che solo il primo abbia i decimali.
        m = first if _has_decimals(first) and not _has_decimals(last) else last
    else:
        m = first or last
    if not m:
        return None

    cents = _to_cents(m["amt"])
    note = m["desc"].strip()
    if cents <= 0 or not re.search(r"\w", note):
        return None
    category = resolve_category(tag.group(1), existing) if tag else guess_category(note)
    return Expense(cents, note, category, when)


def parse_income(text: str, now: datetime | None = None) -> Income | None:
    """'+1500 stipendio' · '+200 rimborso 15/11' · 'stipendio 1500' -> Income. None se è una spesa."""
    text = text.strip()
    explicit = text.startswith("+")
    parsed = parse_expense(text.lstrip("+").strip(), now=now)
    if parsed is None or not (explicit or _INCOME_WORDS.search(parsed.note)):
        return None
    return Income(parsed.cents, parsed.note, parsed.when)


def parse_recurring(text: str, existing: tuple[str, ...] | list[str] = ()) -> Recurring | None:
    """'12,99 netflix ogni 15' · '12,99 netflix 15' · '+1500 stipendio 27' (entrata) -> Recurring."""
    text = text.strip()
    income = text.startswith("+")
    text = text.lstrip("+").strip()
    tag = _RE_TAG.search(text)
    m = _RE_RECURRING.match(_RE_TAG.sub("", text).strip())
    if not m:
        return None
    cents, day, note = _to_cents(m["amt"]), int(m["day"]), m["desc"].strip()
    if cents <= 0 or not 1 <= day <= 31:
        return None
    income = income or bool(_INCOME_WORDS.search(note))
    if income:
        category = INCOME_CATEGORY
    else:
        category = resolve_category(tag.group(1), existing) if tag else guess_category(note)
    return Recurring(cents, note, category, day, income)
