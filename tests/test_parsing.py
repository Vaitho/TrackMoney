from datetime import datetime

import pytest

from config import TZ
from parsing import (
    extract_date,
    guess_category,
    parse_amount,
    parse_expense,
    parse_recurring,
    resolve_category,
)

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=TZ)


@pytest.mark.parametrize("text, cents, note", [
    ("caffè 1,20", 120, "caffè"),
    ("45 benzina", 4500, "benzina"),
    ("12,50€ pizza", 1250, "pizza"),
    ("€5 gelato", 500, "gelato"),
    ("pizza 12 euro", 1200, "pizza"),
    ("bolletta luce 89,5", 8950, "bolletta luce"),
    ("iPhone 15 999", 99900, "iPhone 15"),
    # separatore delle migliaia
    ("affitto 1.200", 120000, "affitto"),
    ("1.200,50 affitto", 120050, "affitto"),
    ("auto 12.500", 1250000, "auto"),
    # due numeri: l'importo è quello in fondo, a meno che solo il primo abbia i decimali
    ("2 caffè 3", 300, "2 caffè"),
    ("12,50 pizza 2", 1250, "pizza 2"),
])
def test_amount_and_note(text, cents, note):
    expense = parse_expense(text, now=NOW)
    assert expense is not None
    assert (expense.cents, expense.note) == (cents, note)


@pytest.mark.parametrize("text", ["ciao", "ok grazie", "12", "12 €", "0 caffè", "31/02 12 pizza", ""])
def test_not_an_expense(text):
    assert parse_expense(text, now=NOW) is None


def test_dates():
    assert parse_expense("caffè 1,20", now=NOW).when is None
    assert parse_expense("pizza 12 ieri", now=NOW).when.date() == datetime(2026, 11, 11).date()
    assert parse_expense("pizza 12 altroieri", now=NOW).when.date() == datetime(2026, 11, 10).date()
    assert parse_expense("45 benzina 10/11", now=NOW).when.date() == datetime(2026, 11, 10).date()
    assert parse_expense("10/11/2025 45 benzina", now=NOW).when.date() == datetime(2025, 11, 10).date()
    # una data senza anno che cadrebbe nel futuro è dell'anno scorso
    assert parse_expense("45 benzina 15/11", now=NOW).when.date() == datetime(2025, 11, 15).date()
    # la data non finisce nella descrizione
    assert parse_expense("pizza 12 ieri", now=NOW).note == "pizza"


def test_invalid_or_future_dates_are_ignored():
    assert extract_date("31/02", NOW)[1] is None
    assert extract_date("01/01/2030", NOW)[1] is None


@pytest.mark.parametrize("note, category", [
    ("caffè", "Ristoranti e bar"),
    ("benzina", "Trasporti"),
    ("bolletta luce", "Casa"),
    ("barbiere", "Cura persona"),  # non deve finire in 'Ristoranti e bar' per via di 'bar'
    ("just eat", "Ristoranti e bar"),
    ("amazon prime", "Abbonamenti"),
    ("amazon", "Shopping"),
    ("veterinario", "Animali"),
    ("qualcosa di strano", "Altro"),
])
def test_guess_category(note, category):
    assert guess_category(note) == category


def test_tag_sets_category():
    assert parse_expense("80 cena #svago", now=NOW).category == "Svago"
    assert parse_expense("80 regalo #ristoranti", now=NOW).category == "Ristoranti e bar"
    assert parse_expense("80 regalo #famiglia", now=NOW).category == "Famiglia"


def test_resolve_category_reuses_existing_ones():
    assert resolve_category("ristoranti") == "Ristoranti e bar"
    assert resolve_category("SVAGO") == "Svago"
    # singolare/plurale di una categoria creata dall'utente
    assert resolve_category("regali", ["Regalo"]) == "Regalo"
    assert resolve_category("regalo", ["Regali"]) == "Regali"
    # un'abbreviazione troppo corta non viene "indovinata"
    assert resolve_category("s") == "S"
    assert resolve_category("") == "Altro"


@pytest.mark.parametrize("text, cents", [
    ("1500", 150000), ("1.500", 150000), ("12,5 €", 1250), ("€ 80", 8000), ("0,99", 99),
    ("abc", None), ("0", None), ("12 pizza", None), ("", None),
])
def test_parse_amount(text, cents):
    assert parse_amount(text) == cents


def test_parse_recurring():
    rec = parse_recurring("12,99 netflix ogni 15")
    assert (rec.cents, rec.note, rec.day, rec.category) == (1299, "netflix", 15, "Svago")
    assert parse_recurring("12,99 netflix 15").day == 15
    assert parse_recurring("800 affitto ogni mese il 1").day == 1
    assert parse_recurring("50 palestra 5 #salute").category == "Salute"
    assert parse_recurring("12,99 netflix ogni 40") is None
    assert parse_recurring("netflix") is None
