import json
import logging
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from time import perf_counter
from typing import Awaitable, TypeVar

current_update: ContextVar[int | None] = ContextVar("current_update", default=None)
_logger = logging.getLogger("voltyol.metrics")
_logger.setLevel(logging.INFO)
_logger.propagate = False
T = TypeVar("T")


def record(event: str, **values) -> None:
    try:
        if not _logger.handlers:
            path = Path("work/latency.jsonl")
            path.parent.mkdir(parents=True, exist_ok=True)
            _logger.addHandler(RotatingFileHandler(path, maxBytes=2_000_000, backupCount=2))
        _logger.info(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "event": event,
                                 "update_id": current_update.get(), **values}))
    except OSError:
        pass


async def measured(stage: str, operation: Awaitable[T]) -> T:
    started = perf_counter()
    success = False
    try:
        result = await operation
        success = True
        return result
    finally:
        record("stage", stage=stage, seconds=round(perf_counter() - started, 4), success=success)
