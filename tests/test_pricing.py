import asyncio
import json
from unittest.mock import patch

import pytest

import browser
import pricing
from pricing import PageInfo, extract_page, parse_price


def product_page(price=None, availability=None, title="Cuffie X", extra_scripts=""):
    offer = {"@type": "Offer"}
    if price is not None:
        offer["price"] = price
    if availability:
        offer["availability"] = f"https://schema.org/{availability}"
    data = {"@type": "Product", "name": title, "offers": offer}
    return (
        f'<html><head><meta property="og:title" content="{title}">{extra_scripts}'
        f'<script type="application/ld+json">{json.dumps(data)}</script></head></html>'
    )


@pytest.mark.parametrize("raw, value", [
    ("1.234,56 €", 1234.56), ("1,234.56", 1234.56), ("€ 19,99", 19.99), ("1.234", 1234.0),
    ("59", 59.0), (129.9, 129.9), ("gratis", None), ("0", None),
])
def test_parse_price(raw, value):
    assert parse_price(raw) == value


def test_jsonld_price_and_availability():
    assert extract_page(product_page("129.90", "InStock")) == PageInfo(129.9, "Cuffie X", True)
    assert extract_page(product_page(None, "OutOfStock")) == PageInfo(None, "Cuffie X", False)


def test_related_products_do_not_win_over_the_main_one():
    related = json.dumps({"@type": "ItemList", "itemListElement": [
        {"@type": "Product", "name": "Custodia", "offers": {"price": "9.99"}}]})
    page = product_page("499.00", extra_scripts=f'<script type="application/ld+json">{related}</script>')
    assert extract_page(page).price == 499.0


def test_among_several_products_the_one_matching_the_title_wins():
    other = json.dumps({"@type": "Product", "name": "Cavo USB", "offers": {"price": "5.00"}})
    page = product_page("89.90", title="Cuffie X", extra_scripts=f'<script type="application/ld+json">{other}</script>')
    assert extract_page(page).price == 89.9


def test_meta_tags():
    page = ('<html><head><title>Lampada</title><meta property="product:price:amount" content="34.50">'
            '<meta property="product:availability" content="out of stock"></head></html>')
    assert extract_page(page) == PageInfo(34.5, "Lampada", False)


def test_amazon_layout():
    page = ('<html><body><span id="productTitle"> Tastiera </span>'
            '<span class="a-offscreen">199,00 €</span>'  # prezzo di un altro prodotto, più in alto nella pagina
            '<div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">49,99 €</span></span></div>'
            '<div id="availability"><span>Disponibilità immediata</span></div></body></html>')
    assert extract_page(page) == PageInfo(49.99, "Tastiera", True)

    split = ('<html><body><span class="a-price"><span class="a-offscreen"></span>'
             '<span class="a-price-whole">1.299,</span><span class="a-price-fraction">50</span></span>'
             '<div id="availability">Attualmente non disponibile</div></body></html>')
    info = extract_page(split)
    assert (info.price, info.available) == (1299.5, False)


# ---------- logica degli avvisi ----------

def run_checks(params, pages):
    """Esegue un controllo per ogni pagina, riportando lo stato da uno al successivo."""
    async def go():
        results, state = [], None
        for page in pages:
            async def fake_load(url, use_browser=False, throttle=False, _page=page):
                assert throttle  # i controlli in background aspettano il loro turno
                return extract_page(_page), False
            with patch.object(pricing, "load_page", fake_load):
                result = await pricing.check(params, state)
            results.append(result)
            state = result.state
        return results
    return asyncio.run(go())


BASE = {"url": "http://shop/x", "title": "Cuffie X"}


def test_without_target_every_drop_is_notified():
    first, drop, same, rise = run_checks(BASE, [
        product_page("100"), product_page("95"), product_page("95"), product_page("98")])
    assert first.notifications == [] and first.new_price == 100
    assert len(drop.notifications) == 1 and "Prezzo sceso" in drop.notifications[0]
    assert "Minimo" in drop.notifications[0]
    assert same.notifications == [] and same.new_price is None  # niente punto nello storico se non cambia
    assert rise.notifications == [] and rise.new_price == 98


def test_with_target_only_the_target_is_notified():
    first, above, reached, same, lower = run_checks({**BASE, "target": 80.0}, [
        product_page("100"), product_page("90"), product_page("79"), product_page("79"), product_page("75")])
    assert first.notifications == []
    assert above.notifications == []  # è sceso, ma resta sopra l'obiettivo
    assert len(reached.notifications) == 1 and "obiettivo raggiunto" in reached.notifications[0]
    assert same.notifications == []  # nessuna ripetizione
    assert len(lower.notifications) == 1  # sotto l'obiettivo scende ancora: nuovo avviso


def test_target_above_current_price_is_notified_at_once():
    (first,) = run_checks({**BASE, "target": 150.0}, [product_page("100")])
    assert len(first.notifications) == 1 and "già sotto il tuo obiettivo" in first.notifications[0]


def test_back_in_stock():
    first, sold_out, back = run_checks(BASE, [
        product_page("100", "InStock"), product_page(None, "OutOfStock"), product_page("100", "InStock")])
    assert sold_out.notifications == [] and sold_out.state["available"] is False
    assert sold_out.state["price"] == 100  # l'ultimo prezzo noto resta
    assert len(back.notifications) == 1 and "disponibile" in back.notifications[0]


def test_missing_price_is_an_error():
    with pytest.raises(RuntimeError):
        run_checks(BASE, ["<html><head><title>Captcha</title></head></html>"])


def test_validate():
    async def go():
        async def found(url, use_browser=False):
            return PageInfo(59.9, "Lampada", True), True
        async def nothing(url, use_browser=False):
            return PageInfo(None, "", None), False
        with patch.object(pricing, "load_page", found):
            params, error = await pricing.validate("https://shop/lampada")
        assert error is None
        assert params == {"url": "https://shop/lampada", "title": "Lampada", "initial_price": 59.9, "browser": True}
        with patch.object(pricing, "load_page", nothing):
            assert (await pricing.validate("https://shop/lampada"))[0] is None
        assert (await pricing.validate("ftp://shop"))[0] is None
    asyncio.run(go())


def test_browser_reads_a_price_added_by_javascript(tmp_path):
    """Usa il vero Chromium: verifica che una pagina che inserisce il prezzo via JavaScript venga letta."""
    pytest.importorskip("playwright")
    page = """<html><head><title>JS shop</title></head><body><script>setTimeout(() => {
      const s = document.createElement('script'); s.type = 'application/ld+json';
      s.text = JSON.stringify({'@type': 'Product', 'offers': {'price': '59.90', 'availability': 'https://schema.org/InStock'}});
      document.head.appendChild(s); }, 300);</script></body></html>"""
    path = tmp_path / "shop.html"
    path.write_text(page, encoding="utf-8")
    assert extract_page(page).price is None  # senza eseguire il JavaScript il prezzo non c'è

    async def go():
        try:
            return await browser.get_html(path.as_uri(), check_urls=False)  # file locale: solo per il test
        finally:
            await browser.shutdown()
    try:
        rendered = asyncio.run(go())
    except Exception as exc:  # Chromium non installato su questa macchina
        pytest.skip(f"browser non disponibile: {exc}")
    assert extract_page(rendered).price == 59.9
