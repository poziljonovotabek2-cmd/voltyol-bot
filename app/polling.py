import asyncio
import logging
from time import perf_counter, time

import httpx

from app.config import settings
from app.telegram import handle_update, telegram
from app.metrics import current_update, record

logger = logging.getLogger(__name__)
# Bot API URLs contain the token. Keep HTTP request URLs out of the logs.
logging.getLogger("httpx").setLevel(logging.WARNING)


async def prepare_polling() -> None:
    bot = await telegram("getMe", {})
    if not bot.get("ok"):
        raise RuntimeError("Telegram rejected the bot token")
    webhook = await telegram("getWebhookInfo", {})
    if webhook.get("result", {}).get("url"):
        result = await telegram("deleteWebhook", {"drop_pending_updates": False})
        if not result.get("ok"):
            raise RuntimeError("Could not switch Telegram to polling")
    logger.info("Telegram bot @%s is ready", bot["result"]["username"])


async def poll_updates() -> None:
    offset = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(40, connect=5, write=10, pool=5)) as client:
        while True:
            try:
                response = await client.post(
                    f"https://api.telegram.org/bot{settings.telegram_bot_token}/getUpdates",
                    json={"offset": offset, "timeout": 25, "allowed_updates": ["message", "callback_query"]},
                )
                response.raise_for_status()
                data = response.json()
                if not data.get("ok"):
                    raise RuntimeError("Telegram polling request failed")
                batch_received = perf_counter()
                batch_wall_time = time()
                for update in data.get("result", []):
                    started = perf_counter()
                    token = current_update.set(update["update_id"])
                    message_date = update.get("message", {}).get("date")
                    record("update_received", queue_s=round(started - batch_received, 4),
                           telegram_age_s=round(max(0, batch_wall_time - message_date), 3) if message_date else None)
                    success = False
                    try:
                        await handle_update(update)
                        success = True
                    except Exception as exc:
                        logger.error("Update %s failed (%s)", update["update_id"], type(exc).__name__)
                    finally:
                        record("update_completed", seconds=round(perf_counter() - started, 4), success=success)
                        current_update.reset(token)
                    offset = update["update_id"] + 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Polling unavailable (%s); retrying", type(exc).__name__)
                await asyncio.sleep(3)
