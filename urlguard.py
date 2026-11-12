"""Protezione dei link: il bot non deve scaricare indirizzi interni (rete aziendale, localhost, servizi cloud)."""
import asyncio
import ipaddress
import socket
import time
from urllib.parse import urlsplit


class BlockedUrl(Exception):
    """Il link non è consentito."""


_TTL = 300  # secondi: un host già verificato non viene risolto di nuovo
_MAX_CACHE = 1000
_verified: dict[str, float] = {}


async def _resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global  # False per reti private, localhost, link-local (169.254…), CGNAT, ecc.


def _is_literal_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


async def ensure_public(url: str) -> None:
    """Solleva BlockedUrl se il link non è http(s) o punta (anche dopo la risoluzione DNS) a un indirizzo non pubblico."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedUrl("solo link http o https")
    host = (parts.hostname or "").lower()
    if not host:
        raise BlockedUrl("indirizzo mancante")

    now = time.monotonic()
    if _verified.get(host, 0) > now:
        return
    try:
        addresses = [host] if _is_literal_ip(host) else await _resolve(host)
    except OSError:
        raise BlockedUrl("indirizzo non raggiungibile") from None
    if not addresses or not all(_is_public(a) for a in addresses):
        raise BlockedUrl("indirizzo interno non consentito")

    if len(_verified) >= _MAX_CACHE:
        _verified.clear()
    _verified[host] = now + _TTL
