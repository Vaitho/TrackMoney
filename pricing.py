"""Monitor prezzi: lettura della pagina di un prodotto e logica degli avvisi."""
import asyncio
import html
import json
import logging
import random
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

import browser
import urlguard
from formatting import eur

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": browser.USER_AGENT,
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
}

_UNAVAILABLE = ("outofstock", "soldout", "discontinued", "nondisponibile", "esaurito", "currentlyunavailable")
_AVAILABLE = ("instock", "limitedavailability", "onlineonly", "instoreonly", "disponibile", "disponibilit")

# Chiavi JSON-LD che introducono prodotti diversi da quello della pagina (correlati, elenchi…)
_OTHER_PRODUCTS_KEYS = {"itemListElement", "isRelatedTo", "isSimilarTo", "isAccessoryOrSparePartFor"}

_AMAZON_PRICE_SELECTORS = (
    "#corePriceDisplay_desktop_feature_div span.a-offscreen",
    "#corePrice_feature_div span.a-offscreen",
    "#apex_desktop span.a-offscreen",
    "#priceblock_ourprice",
    "#priceblock_dealprice",
    "span.a-price span.a-offscreen",
    "span.a-offscreen",
)

EPS = 0.005  # tolleranza nei confronti tra prezzi


@dataclass
class PageInfo:
    price: float | None
    title: str
    available: bool | None  # None = non determinabile


@dataclass
class CheckResult:
    notifications: list[str] = field(default_factory=list)  # testi HTML pronti per Telegram
    state: dict = field(default_factory=dict)  # stato da salvare per il prossimo controllo
    new_price: float | None = None  # valorizzato quando il prezzo è cambiato (va nello storico)


def price_text(price: float) -> str:
    return eur(round(price * 100))


def parse_price(value) -> float | None:
    """Converte '1.234,56 €', '1,234.56', 1234.5 ecc. in float (formati che si trovano nelle pagine web)."""
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    s = re.sub(r"[^\d.,]", "", str(value))
    if not s:
        return None
    if "," in s and "." in s:
        decimal = "," if s.rfind(",") > s.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        s = s.replace(thousands, "").replace(decimal, ".")
    elif "," in s:
        parts = s.split(",")
        s = f"{parts[0]}.{parts[1]}" if len(parts) == 2 and len(parts[1]) <= 2 else s.replace(",", "")
    elif "." in s:
        parts = s.split(".")
        if not (len(parts) == 2 and len(parts[1]) <= 2):
            s = s.replace(".", "")
    try:
        price = float(s)
    except ValueError:
        return None
    return price if price > 0 else None


def parse_availability(value) -> bool | None:
    s = re.sub(r"[\s_\-]", "", str(value).lower())
    if any(w in s for w in _UNAVAILABLE):
        return False
    if any(w in s for w in _AVAILABLE):
        return True
    return None


# ---------- estrazione dalla pagina ----------

def _flatten_offers(offers) -> Iterator[dict]:
    if isinstance(offers, list):
        for o in offers:
            yield from _flatten_offers(o)
    elif isinstance(offers, dict):
        yield offers
        if "offers" in offers:
            yield from _flatten_offers(offers["offers"])


def _nodes_with_offers(node, secondary: bool = False) -> Iterator[tuple[dict, bool]]:
    """Tutti i nodi JSON-LD che hanno 'offers'; secondary=True se sono prodotti correlati/elencati."""
    if isinstance(node, list):
        for item in node:
            yield from _nodes_with_offers(item, secondary)
    elif isinstance(node, dict):
        if "offers" in node:
            yield node, secondary
        for key, value in node.items():
            if key != "offers" and isinstance(value, (dict, list)):
                yield from _nodes_with_offers(value, secondary or key in _OTHER_PRODUCTS_KEYS)


def _offer_price(offer: dict) -> float | None:
    for key in ("price", "lowPrice"):
        if key in offer:
            price = parse_price(offer[key])
            if price:
                return price
    spec = offer.get("priceSpecification")
    if isinstance(spec, list):
        spec = spec[0] if spec else None
    if isinstance(spec, dict):
        return _offer_price(spec)
    return None


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower()))


def _from_jsonld(soup: BeautifulSoup, title: str) -> tuple[float | None, bool | None]:
    found: list[tuple[dict, bool]] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        found.extend(_nodes_with_offers(data))

    # Prima i prodotti "principali"; i correlati solo se non c'è altro.
    nodes = [n for n, secondary in found if not secondary] or [n for n, _ in found]
    if len(nodes) > 1 and title:  # più candidati: vince quello il cui nome somiglia di più al titolo della pagina
        title_words = _words(title)
        nodes.sort(key=lambda n: -len(_words(str(n.get("name", ""))) & title_words))

    for node in nodes:
        offers = list(_flatten_offers(node["offers"]))
        price = next((p for p in map(_offer_price, offers) if p), None)
        states = [a for a in (parse_availability(o["availability"]) for o in offers if "availability" in o) if a is not None]
        available = True if True in states else (False if False in states else None)
        if price is not None or available is not None:
            return price, available
    return None, None


def extract_page(page: str) -> PageInfo:
    soup = BeautifulSoup(page, "html.parser")

    title = ""
    for finder in (
        lambda: soup.find("meta", property="og:title"),
        lambda: soup.find(id="productTitle"),
        lambda: soup.title,
    ):
        tag = finder()
        if tag:
            title = (tag.get("content") if tag.name == "meta" else tag.get_text() or "").strip()
            if title:
                break

    # 1) Dati strutturati JSON-LD (usati dalla maggior parte degli e-commerce)
    price, available = _from_jsonld(soup, title)

    # 2) Meta tag
    if price is None:
        for attrs in ({"property": "product:price:amount"}, {"property": "og:price:amount"}, {"itemprop": "price"}):
            tag = soup.find("meta", attrs=attrs) or soup.find(attrs=attrs)
            if tag:
                price = parse_price(tag.get("content") or tag.get_text())
                if price:
                    break
    if available is None:
        for attrs in ({"property": "product:availability"}, {"property": "og:availability"}, {"itemprop": "availability"}):
            tag = soup.find(attrs=attrs)
            if tag:
                available = parse_availability(tag.get("content") or tag.get("href") or tag.get_text())
                if available is not None:
                    break

    # 3) Amazon
    if price is None:
        for selector in _AMAZON_PRICE_SELECTORS:
            tag = soup.select_one(selector)
            price = parse_price(tag.get_text()) if tag else None
            if price:
                break
    if price is None:
        whole, fraction = soup.select_one("span.a-price-whole"), soup.select_one("span.a-price-fraction")
        if whole:
            digits = re.sub(r"\D", "", whole.get_text())
            cents = re.sub(r"\D", "", fraction.get_text()) if fraction else "00"
            price = parse_price(f"{digits},{cents}") if digits else None
    if available is None:
        tag = soup.select_one("#availability")
        if tag:
            available = parse_availability(tag.get_text())

    return PageInfo(price, title, available)


# Tutti i controlli partono dallo stesso indirizzo: per non farsi bloccare dai negozi, le richieste
# allo stesso sito vengono distanziate di qualche secondo.
HOST_GAP = 4.0  # secondi tra due richieste allo stesso sito
HOST_JITTER = 2.0  # più un tempo casuale, così i controlli non sembrano un orologio
_next_slot: dict[str, float] = {}
_slot_lock = asyncio.Lock()


async def _wait_turn(url: str) -> None:
    host = urlsplit(url).netloc.lower()
    loop = asyncio.get_running_loop()
    async with _slot_lock:
        now = loop.time()
        slot = max(now, _next_slot.get(host, 0.0))
        _next_slot[host] = slot + HOST_GAP + random.uniform(0, HOST_JITTER)
    if slot > now:
        await asyncio.sleep(slot - now)


async def _guard_request(request: httpx.Request) -> None:
    await urlguard.ensure_public(str(request.url))  # vale anche per ogni redirect


async def _http_html(url: str) -> str:
    async with httpx.AsyncClient(
        timeout=20, follow_redirects=True, headers=HEADERS, event_hooks={"request": [_guard_request]}
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    return resp.text


async def load_page(url: str, use_browser: bool = False, throttle: bool = False) -> tuple[PageInfo, bool]:
    """Ritorna (info, usato_browser). Prova prima HTTP e, solo se la pagina non contiene nulla
    di utile, ricade sul browser automatico (se installato). throttle=True per i controlli in
    background, che aspettano il loro turno; un utente che aggiunge un link non aspetta."""
    await urlguard.ensure_public(url)
    if not use_browser:
        try:
            if throttle:
                await _wait_turn(url)
            info = extract_page(await _http_html(url))
            if info.price is not None or info.available is not None:
                return info, False
        except urlguard.BlockedUrl:
            raise
        except Exception as exc:
            log.info("HTTP fallito per %s (%s)", url, exc)
            if not browser.is_installed():
                raise
    if not browser.is_installed():
        return PageInfo(None, "", None), False
    if throttle:
        await _wait_turn(url)
    return extract_page(await browser.get_html(url)), True


# ---------- logica del monitor ----------

async def validate(url: str) -> tuple[dict | None, str | None]:
    """Controlla un link nuovo. Ritorna (parametri da salvare, None) oppure (None, messaggio d'errore)."""
    if not url.lower().startswith(("http://", "https://")):
        return None, "Il link deve iniziare con http:// o https://"
    try:
        info, used_browser = await load_page(url)
    except urlguard.BlockedUrl:
        return None, "Questo indirizzo non è consentito: posso controllare solo negozi online pubblici."
    except Exception:
        return None, "Non riesco ad aprire quel link (il sito potrebbe bloccare i bot)."
    if info.price is None:
        if info.available is False:
            return None, "Il prodotto risulta esaurito e non mostra il prezzo: riprova quando è disponibile."
        return None, "Non riesco a leggere il prezzo da questa pagina. Prova con un altro negozio."
    return {
        "url": url,
        "title": info.title[:120] or url,
        "initial_price": info.price,
        "browser": used_browser,
    }, None


def current_price(params: dict, state: dict | None) -> float | None:
    return (state or {}).get("price") or params.get("initial_price")


def is_below_target(price: float | None, target: float | None) -> bool:
    return price is not None and target is not None and price <= target + EPS


async def check(params: dict, state: dict | None) -> CheckResult:
    info, _ = await load_page(params["url"], use_browser=params.get("browser", False), throttle=True)
    target = params.get("target")

    if info.price is None:
        if info.available is False and state:  # prodotto esaurito: il prezzo sparisce dalla pagina
            return CheckResult(state={**state, "available": False})
        raise RuntimeError("prezzo non trovato")

    price = info.price
    available = info.available is not False
    below = is_below_target(price, target)
    title = html.escape(params.get("title", "Prodotto"))
    link = html.escape(params["url"], quote=True)
    header = f"<a href=\"{link}\">{title}</a>"
    notes: list[str] = []

    if state is None:  # primo controllo: salvo il punto di partenza
        if below and available:  # obiettivo impostato sopra il prezzo attuale: avviso subito
            notes.append(
                f"🎯 <b>Il prezzo è già sotto il tuo obiettivo!</b>\n{header}\n"
                f"Ora <b>{price_text(price)}</b> (obiettivo {price_text(target)})"
            )
        return CheckResult(
            notes, {"price": price, "min": price, "available": available, "below_target": below}, new_price=price
        )

    old = state["price"]
    lowest = state.get("min", old)
    dropped = price < old - EPS

    if available and not state.get("available", True):
        notes.append(f"✅ <b>Di nuovo disponibile!</b>\n{header}\nPrezzo: <b>{price_text(price)}</b>")

    if available:
        if target is not None:
            if below and (not state.get("below_target", False) or dropped):
                text = f"🎯 <b>Prezzo obiettivo raggiunto!</b>\n{header}\n<b>{price_text(price)}</b> (obiettivo {price_text(target)})"
                if dropped:
                    text += f"\nprima: {price_text(old)}"
                notes.append(text)
        elif dropped:
            pct = (old - price) / old * 100
            text = f"📉 <b>Prezzo sceso!</b>\n{header}\n{price_text(old)} → <b>{price_text(price)}</b> (-{pct:.0f}%)"
            if price < lowest - EPS:
                text += "\n🏆 Minimo da quando lo monitoro"
            notes.append(text)

    new_state = {"price": price, "min": min(lowest, price), "available": available, "below_target": below}
    return CheckResult(notes, new_state, new_price=price if abs(price - old) > EPS else None)
