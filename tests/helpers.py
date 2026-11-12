"""Strumenti per i test: database temporaneo e una chat simulata (senza Telegram)."""
import asyncio
import inspect
import re
from dataclasses import dataclass
from types import SimpleNamespace

import db
import handlers


def with_db(fn):
    """Esegue il test (una funzione async) su un database nuovo e vuoto.
    Se il test dichiara un parametro `tmp_path`, riceve la cartella temporanea."""
    wants_tmp = "tmp_path" in inspect.signature(fn).parameters

    def wrapper(tmp_path):
        async def go():
            await db.connect(str(tmp_path / "test.db"))
            try:
                await (fn(tmp_path) if wants_tmp else fn())
            finally:
                await db.close()
        asyncio.run(go())

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


@dataclass
class Sent:
    kind: str  # "text" (risposta), "edit" (messaggio sostituito), "photo", "document", "bot" (invio diretto)
    text: str
    markup: object = None

    @property
    def buttons(self) -> list[tuple[str, str]]:
        if self.markup is None:
            return []
        return [(b.text, b.callback_data) for row in self.markup.inline_keyboard for b in row]


class FakeMessage:
    def __init__(self, log: list, text: str | None = None):
        self.log, self.text = log, text

    async def reply_text(self, text, **kw):
        self.log.append(Sent("text", text, kw.get("reply_markup")))
        return FakeMessage(self.log)

    async def reply_photo(self, photo, **kw):
        self.log.append(Sent("photo", kw.get("caption") or "", kw.get("reply_markup")))

    async def reply_document(self, document, **kw):
        self.log.append(Sent("document", kw.get("caption") or "", None))


class FakeQuery:
    def __init__(self, log: list, data: str):
        self.log, self.data, self.message = log, data, FakeMessage(log)
        self.alert = None

    async def answer(self, text=None, **kw):
        self.alert = text

    async def edit_message_text(self, text, **kw):
        self.log.append(Sent("edit", text, kw.get("reply_markup")))


class FakeBot:
    """Bot finto: registra quello che verrebbe inviato. Con fail=<eccezione> ogni invio fallisce."""

    def __init__(self, log: list | None = None, fail: Exception | None = None):
        self.log = log if log is not None else []
        self.fail = fail

    async def send_message(self, chat_id, text, **kw):
        if self.fail:
            raise self.fail
        self.log.append(Sent("bot", text, kw.get("reply_markup")))

    async def send_photo(self, chat_id, photo, **kw):
        if self.fail:
            raise self.fail
        self.log.append(Sent("photo", kw.get("caption") or "", kw.get("reply_markup")))

    async def send_chat_action(self, chat_id, action, **kw):
        pass


class Chat:
    """Una conversazione simulata con il bot: scrivi, usa comandi, tocca i bottoni."""

    def __init__(self, chat_id: int = 1):
        self.chat_id = chat_id
        self.log: list[Sent] = []
        self.context = SimpleNamespace(user_data={}, args=[], bot=FakeBot(self.log), error=None)
        self.last_query: FakeQuery | None = None

    def _update(self, message=None, query=None):
        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=self.chat_id),
            effective_message=message or (query.message if query else None),
            callback_query=query,
        )

    async def say(self, text: str) -> Sent:
        await handlers.on_text(self._update(FakeMessage(self.log, text)), self.context)
        return self.last

    async def cmd(self, name: str, *args: str) -> Sent:
        callback = next(cb for names, cb in handlers.COMMANDS if name in names)
        self.context.args = list(args)
        await callback(self._update(FakeMessage(self.log, f"/{name}")), self.context)
        return self.last

    async def click(self, data: str) -> Sent | None:
        query = self.last_query = FakeQuery(self.log, data)
        callback = next((cb for pattern, cb in handlers.CALLBACKS if re.match(pattern, data)), None)
        await (callback or handlers.common.stale_button)(self._update(query=query), self.context)
        return self.last

    async def tap(self, label: str) -> Sent | None:
        """Tocca, nell'ultimo messaggio con bottoni, quello la cui etichetta contiene label."""
        source = next(s for s in reversed(self.log) if s.buttons)
        matches = [data for text, data in source.buttons if label in text]
        assert matches, f"nessun bottone «{label}» tra {[t for t, _ in source.buttons]}"
        return await self.click(matches[0])

    @property
    def last(self) -> Sent | None:
        return self.log[-1] if self.log else None

    def labels(self) -> list[str]:
        return [text for text, _ in self.last.buttons]
