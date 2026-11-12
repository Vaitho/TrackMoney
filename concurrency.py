"""Elaborazione dei messaggi: utenti diversi in parallelo, ma un solo messaggio alla volta per ogni utente."""
import asyncio

from telegram.ext import SimpleUpdateProcessor


class PerChatUpdateProcessor(SimpleUpdateProcessor):
    """Senza parallelismo, un controllo lento (un link che impiega 20 secondi) bloccherebbe tutti gli utenti.
    In parallelo "puro" però due tocchi rapidi dello stesso utente potrebbero sovrapporsi e confondere le sue
    domande in sospeso: per questo ogni chat ha una sua coda."""

    def __init__(self, max_concurrent_updates: int = 16):
        super().__init__(max_concurrent_updates)
        self._locks: dict[int, asyncio.Lock] = {}
        self._waiting: dict[int, int] = {}

    async def do_process_update(self, update, coroutine) -> None:
        chat = getattr(update, "effective_chat", None)
        key = chat.id if chat is not None else 0
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._waiting[key] = self._waiting.get(key, 0) + 1
        try:
            async with lock:
                await coroutine
        finally:
            self._waiting[key] -= 1
            if not self._waiting[key]:  # nessuno in coda: la chat non occupa più memoria
                del self._waiting[key]
                del self._locks[key]
