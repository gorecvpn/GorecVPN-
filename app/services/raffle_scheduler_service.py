"""Background loop: auto-draw expired raffles + ending reminders."""

from __future__ import annotations

import asyncio

import structlog

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.services.raffle.service import auto_draw_due_campaigns, send_ending_reminders


logger = structlog.get_logger(__name__)


class RaffleSchedulerService:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._interval_seconds = 60

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        await self.stop()
        if not settings.is_raffle_enabled():
            logger.info('Сервис розыгрыша отключён (RAFFLE_ENABLED=false)')
            return
        if not (settings.is_raffle_auto_draw_enabled() or settings.is_raffle_reminder_enabled()):
            logger.info('Авто-draw и напоминания розыгрыша выключены')
            return

        interval = int(getattr(settings, 'RAFFLE_AUTO_DRAW_INTERVAL_SECONDS', 60) or 60)
        self._interval_seconds = max(15, interval)
        self._task = asyncio.create_task(self._loop())
        logger.info(
            'Сервис розыгрыша запущен',
            auto_draw=settings.is_raffle_auto_draw_enabled(),
            reminders=settings.is_raffle_reminder_enabled(),
            interval=self._interval_seconds,
        )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _loop(self) -> None:
        try:
            while True:
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error('Ошибка планировщика розыгрыша', error=exc)
                await asyncio.sleep(self._interval_seconds)
        except asyncio.CancelledError:
            logger.info('Сервис розыгрыша остановлен')
            raise

    async def _tick(self) -> None:
        async with AsyncSessionLocal() as db:
            if settings.is_raffle_auto_draw_enabled():
                await auto_draw_due_campaigns(db)
            if settings.is_raffle_reminder_enabled():
                await send_ending_reminders(db)


raffle_scheduler_service = RaffleSchedulerService()
