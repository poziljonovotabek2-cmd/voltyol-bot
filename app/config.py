from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = ""
    telegram_mode: Literal["webhook", "polling"] = "webhook"
    telegram_webhook_secret: str = ""
    database_url: str = "postgresql://postgres:postgres@localhost:5432/evchargers"
    redis_url: str = "redis://localhost:6379/0"
    overpass_url: str = "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
    overpass_fallback_url: str = "https://overpass.private.coffee/api/interpreter"
    overpass_budget_seconds: float = Field(default=4.0, gt=0, le=30)
    overpass_cooldown_seconds: float = Field(default=60.0, ge=0)
    nominatim_url: str = "https://nominatim.openstreetmap.org/search"
    nominatim_fallback_enabled: bool = True
    map_tile_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    map_tile_cache_dir: str = "work/map-tiles"
    search_radius_meters: int = 5000
    cache_ttl_seconds: int = 900


settings = Settings()
