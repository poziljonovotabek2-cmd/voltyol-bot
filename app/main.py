import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.chargers import find_chargers
from app.config import settings
from app.storage import lifespan_resources
from app.telegram import handle_update, telegram_session
from app.polling import poll_updates, prepare_polling


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with lifespan_resources(), telegram_session():
        task = None
        if settings.telegram_mode == "polling":
            await prepare_polling()
            task = asyncio.create_task(poll_updates())
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


app = FastAPI(title="Где зарядить API", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "telegram_mode": settings.telegram_mode}


@app.get("/api/stations")
async def stations(lat: float, lon: float, radius: int = Query(default=settings.search_radius_meters, ge=100, le=50000)):
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise HTTPException(status_code=422, detail="Invalid coordinates")
    try:
        return {"stations": await find_chargers(lat, lon, radius)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Station data provider unavailable") from exc


class Report(BaseModel):
    telegram_id: int
    message: str = Field(min_length=3, max_length=2000)
    lat: float | None = None
    lon: float | None = None


@app.post("/api/reports", status_code=201)
async def create_report(report: Report):
    from app.storage import save_report
    await save_report(report.telegram_id, report.message, report.lat, report.lon)
    return {"saved": True}


@app.post("/telegram/webhook")
async def webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    if settings.telegram_mode != "webhook":
        raise HTTPException(status_code=409, detail="Bot is running in polling mode")
    if settings.telegram_webhook_secret and x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    await handle_update(await request.json())
    return {"ok": True}
