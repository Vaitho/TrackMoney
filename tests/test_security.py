"""Protezione dei link, accesso riservato e conversazioni in parallelo."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from telegram.ext import ApplicationHandlerStop

import db
import pricing
import urlguard
from concurrency import PerChatUpdateProcessor
from config import parse_allowed, settings
from handlers import common
from urlguard import BlockedUrl, ensure_public

from helpers import Chat, FakeBot, with_db


def blocked(url: str) -> bool:
    try:
        asyncio.run(ensure_public(url))
    except BlockedUrl:
        return True
    return False


@pytest.fixture(autouse=True)
def fresh_cache():
    urlguard._verified.clear()


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://localhost:8000/admin", "http://192.168.1.10/", "http://10.0.0.5/",
    "http://172.16.0.1/", "http://169.254.169.254/latest/meta-data/",  # servizio metadati dei cloud
    "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/", "http://100.64.0.1/",
    "file:///etc/passwd", "ftp://example.com/", "javascript:alert(1)", "http:///nohost",
])
def test_internal_and_odd_addresses_are_blocked(url):
    assert blocked(url)


def test_public_literal_address_is_allowed():
    assert not blocked("http://8.8.8.8/")
    assert not blocked("https://[2001:4860:4860::8888]/")


def test_hostname_resolving_to_a_private_address_is_blocked():
    async def to_private(host):
        return ["93.184.216.34", "10.1.2.3"]  # basta un indirizzo interno tra quelli restituiti
    with patch.object(urlguard, "_resolve", to_private):
        assert blocked("https://sembra-un-negozio.example/")

    async def to_public(host):
        return ["93.184.216.34"]
    with patch.object(urlguard, "_resolve", to_public):
        assert not blocked("https://negozio.example/")


def test_unresolvable_host_is_rejected():
    async def nothing(host):
        raise OSError("no such host")
    with patch.object(urlguard, "_resolve", nothing):
        assert blocked("https://non-esiste.example/")


def test_every_http_request_is_checked_including_redirects():
    """Un negozio (o chi finge di esserlo) non deve poter reindirizzare il bot verso la rete interna."""
    async def go():
        seen = []

        async def fake_public(url):
            seen.append(url)
            if "interno" in url:
                raise BlockedUrl("interno")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "negozio.example":
                return httpx.Response(302, headers={"location": "http://interno.local/segreto"})
            return httpx.Response(200, text="<html>segreto</html>")

        transport = httpx.MockTransport(handler)
        real_client = httpx.AsyncClient

        def client_with_mock(**kwargs):
            return real_client(transport=transport, **kwargs)

        with patch.object(urlguard, "ensure_public", fake_public), patch.object(httpx, "AsyncClient", client_with_mock):
            with pytest.raises(BlockedUrl):
                await pricing._http_html("https://negozio.example/prodotto")
        return seen
    seen = asyncio.run(go())
    assert seen == ["https://negozio.example/prodotto", "http://interno.local/segreto"]


@with_db
async def test_adding_an_internal_link_is_refused_with_a_clear_message():
    chat = Chat()
    sent = await chat.say("http://192.168.1.1/admin")
    assert "non è consentito" in sent.text
    assert await db.count_subscriptions(chat.chat_id) == 0


# ---------- spaziatura delle richieste allo stesso sito ----------

def test_requests_to_the_same_host_are_spaced_and_others_are_not():
    async def go():
        pricing._next_slot.clear()
        with patch.object(pricing, "HOST_GAP", 0.3), patch.object(pricing, "HOST_JITTER", 0.0):
            loop = asyncio.get_running_loop()
            start = loop.time()
            await asyncio.gather(*(pricing._wait_turn("https://www.amazon.it/dp/1") for _ in range(3)))
            same_host = loop.time() - start

            start = loop.time()
            await asyncio.gather(*(pricing._wait_turn(f"https://shop{i}.example/x") for i in range(3)))
            other_hosts = loop.time() - start
        return same_host, other_hosts
    same_host, other_hosts = asyncio.run(go())
    assert same_host >= 0.55  # la terza richiesta aspetta due intervalli
    assert other_hosts < 0.1


# ---------- accesso riservato ----------

def test_parse_allowed():
    assert parse_allowed("") == set() and parse_allowed("  ") == set()
    assert parse_allowed("123, 456;789 -100") == {123, 456, 789, -100}
    with pytest.raises(ValueError, match="mario"):
        parse_allowed("123, mario")


def guard_update(user_id: int, callback: bool = False):
    log = []

    async def reply_text(text, **kw):
        log.append(text)

    async def answer(text=None, **kw):
        log.append(("answer", text))

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(reply_text=reply_text),
        callback_query=SimpleNamespace(answer=answer) if callback else None,
    )
    return update, log


def run_guard(update):
    async def go():
        await common.access_guard(update, SimpleNamespace())
    try:
        asyncio.run(go())
    except ApplicationHandlerStop:
        return True
    return False


def test_bot_is_open_when_no_allowed_users_are_set(monkeypatch):
    monkeypatch.setattr(settings, "allowed_users", "")
    update, log = guard_update(999)
    assert not run_guard(update) and log == []


def test_only_allowed_users_get_through(monkeypatch):
    monkeypatch.setattr(settings, "allowed_users", "111, 222")
    update, log = guard_update(222)
    assert not run_guard(update) and log == []

    update, log = guard_update(999)
    assert run_guard(update)  # fermato
    assert len(log) == 1 and "privato" in log[0] and "999" in log[0]  # e gli dice il suo ID, per farsi aggiungere

    update, log = guard_update(999, callback=True)
    assert run_guard(update) and log == [("answer", "Bot privato.")]


# ---------- elaborazione in parallelo ----------

def make_update(chat_id):
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id))


def test_different_chats_run_together_and_the_same_chat_in_order():
    async def go():
        processor = PerChatUpdateProcessor(8)
        events = []

        async def work(name, seconds):
            events.append(f"{name}:start")
            await asyncio.sleep(seconds)
            events.append(f"{name}:end")

        loop = asyncio.get_running_loop()
        start = loop.time()
        await asyncio.gather(
            processor.process_update(make_update(1), work("a1", 0.3)),
            processor.process_update(make_update(1), work("a2", 0.05)),  # stessa chat: aspetta a1
            processor.process_update(make_update(2), work("b", 0.05)),  # altra chat: non aspetta
        )
        return events, loop.time() - start, processor

    events, elapsed, processor = asyncio.run(go())
    assert events.index("b:end") < events.index("a1:end")  # un utente lento non blocca gli altri
    assert events.index("a1:end") < events.index("a2:start")  # ma lo stesso utente resta in ordine
    assert elapsed < 0.5
    assert processor._locks == {} and processor._waiting == {}  # niente memoria occupata a lavoro finito
