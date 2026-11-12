import re
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_CHECK_INTERVAL = 60  # minuti: controllare i prezzi più spesso fa bloccare l'indirizzo dai negozi


def parse_allowed(text: str) -> set[int]:
    """'123, 456' -> {123, 456}. Un elemento non numerico è un errore: meglio fermarsi che lasciare il bot aperto."""
    ids = set()
    for part in filter(None, re.split(r"[,\s;]+", text.strip())):
        try:
            ids.add(int(part))
        except ValueError:
            raise ValueError(f"ALLOWED_USERS: «{part}» non è un ID Telegram valido (serve un numero)") from None
    return ids


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str
    db_path: str = "bot.db"
    max_subs_per_user: int = 10
    timezone: str = "Europe/Rome"
    allowed_users: str = ""  # ID Telegram autorizzati, separati da virgola; vuoto = il bot è aperto a tutti
    backup_dir: str = ""  # vuoto = cartella "backups" accanto al database
    backup_keep: int = 7  # quante copie giornaliere tenere

    @property
    def allowed_ids(self) -> set[int]:
        return parse_allowed(self.allowed_users)

    @property
    def backups_path(self) -> Path:
        return Path(self.backup_dir) if self.backup_dir else Path(self.db_path).resolve().parent / "backups"


settings = Settings()
TZ = ZoneInfo(settings.timezone)
