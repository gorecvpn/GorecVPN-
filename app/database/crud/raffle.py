"""CRUD для кампаний и билетов розыгрыша за покупку подписки."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import (
    RaffleCampaign,
    RaffleCampaignStatus,
    RaffleReminderLog,
    RaffleTicket,
    RaffleTicketSource,
    RaffleWinner,
)


logger = structlog.get_logger(__name__)


async def create_campaign(
    db: AsyncSession,
    *,
    name: str,
    description: str | None = None,
    status: str = RaffleCampaignStatus.DRAFT.value,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    max_winners: int = 1,
    prize_type: str = 'custom',
    prize_value: int | None = None,
    prize_text: str | None = None,
    prize_slots: list[dict[str, Any]] | None = None,
    tickets_per_purchase: int = 1,
    tickets_by_tariff: dict | None = None,
    skip_trial_purchases: bool = True,
) -> RaffleCampaign:
    slots = prize_slots or None
    winners = max(1, int(max_winners or 1))
    if slots:
        winners = max(winners, len(slots))

    campaign = RaffleCampaign(
        name=name,
        description=description,
        status=status,
        starts_at=starts_at or datetime.now(UTC),
        ends_at=ends_at,
        max_winners=winners,
        prize_type=prize_type,
        prize_value=prize_value,
        prize_text=prize_text,
        prize_slots=slots,
        tickets_per_purchase=max(1, min(50, int(tickets_per_purchase or 1))),
        tickets_by_tariff=tickets_by_tariff or None,
        skip_trial_purchases=bool(skip_trial_purchases),
    )
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)
    logger.info('Создана кампания розыгрыша', campaign_id=campaign.id, name=campaign.name)
    return campaign


async def get_campaign_by_id(db: AsyncSession, campaign_id: int) -> RaffleCampaign | None:
    result = await db.execute(select(RaffleCampaign).where(RaffleCampaign.id == campaign_id))
    return result.scalar_one_or_none()


async def list_campaigns(db: AsyncSession, *, limit: int = 50, offset: int = 0) -> list[RaffleCampaign]:
    result = await db.execute(select(RaffleCampaign).order_by(RaffleCampaign.id.desc()).offset(offset).limit(limit))
    return list(result.scalars().all())


async def set_campaign_status(db: AsyncSession, campaign: RaffleCampaign, status: str) -> RaffleCampaign:
    campaign.status = status
    campaign.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(campaign)
    return campaign


async def get_current_active_campaign(db: AsyncSession) -> RaffleCampaign | None:
    """Активная кампания в окне дат. Если несколько — с самым поздним starts_at."""
    now = datetime.now(UTC)
    result = await db.execute(
        select(RaffleCampaign)
        .where(
            and_(
                RaffleCampaign.status == RaffleCampaignStatus.ACTIVE.value,
                RaffleCampaign.starts_at <= now,
                (RaffleCampaign.ends_at.is_(None)) | (RaffleCampaign.ends_at >= now),
            )
        )
        .order_by(RaffleCampaign.starts_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_ticket_by_campaign_tx(
    db: AsyncSession, campaign_id: int, source_transaction_id: int
) -> RaffleTicket | None:
    """Any ticket for this purchase (idempotency check)."""
    result = await db.execute(
        select(RaffleTicket)
        .where(
            RaffleTicket.campaign_id == campaign_id,
            RaffleTicket.source_transaction_id == source_transaction_id,
        )
        .order_by(RaffleTicket.ticket_index.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_tickets_by_campaign_tx(
    db: AsyncSession, campaign_id: int, source_transaction_id: int
) -> list[RaffleTicket]:
    result = await db.execute(
        select(RaffleTicket)
        .where(
            RaffleTicket.campaign_id == campaign_id,
            RaffleTicket.source_transaction_id == source_transaction_id,
        )
        .order_by(RaffleTicket.ticket_index.asc())
    )
    return list(result.scalars().all())


async def create_ticket(
    db: AsyncSession,
    *,
    campaign_id: int,
    user_id: int,
    ticket_code: str,
    source_transaction_id: int,
    tariff_id: int | None = None,
    ticket_index: int = 0,
    source: str = RaffleTicketSource.PURCHASE,
    source_ref: str | None = None,
    commit: bool = True,
) -> RaffleTicket:
    ticket = RaffleTicket(
        campaign_id=campaign_id,
        user_id=user_id,
        ticket_code=ticket_code,
        source_transaction_id=source_transaction_id,
        ticket_index=int(ticket_index or 0),
        tariff_id=tariff_id,
        source=source or RaffleTicketSource.PURCHASE,
        source_ref=source_ref,
    )
    db.add(ticket)
    if commit:
        await db.commit()
    else:
        await db.flush()
    await db.refresh(ticket)
    return ticket


async def get_campaign_ticket_stats(db: AsyncSession, campaign_id: int) -> dict[str, int]:
    tickets_q = await db.execute(select(func.count(RaffleTicket.id)).where(RaffleTicket.campaign_id == campaign_id))
    users_q = await db.execute(
        select(func.count(func.distinct(RaffleTicket.user_id))).where(RaffleTicket.campaign_id == campaign_id)
    )
    winners_q = await db.execute(select(func.count(RaffleWinner.id)).where(RaffleWinner.campaign_id == campaign_id))
    return {
        'tickets': int(tickets_q.scalar() or 0),
        'unique_users': int(users_q.scalar() or 0),
        'winners': int(winners_q.scalar() or 0),
    }


async def list_tickets_for_campaign(db: AsyncSession, campaign_id: int) -> list[RaffleTicket]:
    result = await db.execute(select(RaffleTicket).where(RaffleTicket.campaign_id == campaign_id))
    return list(result.scalars().all())


async def list_winners(db: AsyncSession, campaign_id: int) -> list[RaffleWinner]:
    result = await db.execute(
        select(RaffleWinner)
        .options(selectinload(RaffleWinner.user))
        .where(RaffleWinner.campaign_id == campaign_id)
        .order_by(RaffleWinner.place.asc())
    )
    return list(result.scalars().all())


async def get_winner_by_id(db: AsyncSession, winner_id: int) -> RaffleWinner | None:
    result = await db.execute(
        select(RaffleWinner)
        .options(selectinload(RaffleWinner.user), selectinload(RaffleWinner.campaign))
        .where(RaffleWinner.id == winner_id)
    )
    return result.scalar_one_or_none()


async def create_winner(
    db: AsyncSession,
    *,
    campaign_id: int,
    user_id: int,
    ticket_id: int | None,
    ticket_code: str | None,
    place: int,
    prize_type: str | None,
    prize_value: int | None,
    prize_text: str | None,
    awarded: bool = False,
    awarded_at: datetime | None = None,
    commit: bool = False,
) -> RaffleWinner:
    winner = RaffleWinner(
        campaign_id=campaign_id,
        user_id=user_id,
        ticket_id=ticket_id,
        ticket_code=ticket_code,
        place=place,
        prize_type=prize_type,
        prize_value=prize_value,
        prize_text=prize_text,
        awarded=awarded,
        awarded_at=awarded_at,
    )
    db.add(winner)
    if commit:
        await db.commit()
    else:
        await db.flush()
    await db.refresh(winner)
    return winner


async def list_tickets_for_user(
    db: AsyncSession,
    user_id: int,
    *,
    campaign_id: int | None = None,
    limit: int = 100,
) -> list[RaffleTicket]:
    """Билеты пользователя, опционально в рамках одной кампании (новые сверху)."""
    stmt = select(RaffleTicket).where(RaffleTicket.user_id == user_id)
    if campaign_id is not None:
        stmt = stmt.where(RaffleTicket.campaign_id == campaign_id)
    stmt = stmt.order_by(RaffleTicket.created_at.desc()).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def update_campaign(
    db: AsyncSession,
    campaign: RaffleCampaign,
    *,
    name: str | None = None,
    description: Any = ...,
    starts_at: Any = ...,
    ends_at: Any = ...,
    max_winners: int | None = None,
    prize_type: str | None = None,
    prize_value: Any = ...,
    prize_text: Any = ...,
    prize_slots: Any = ...,
    tickets_per_purchase: int | None = None,
    tickets_by_tariff: Any = ...,
    skip_trial_purchases: bool | None = None,
) -> RaffleCampaign:
    """Patch campaign fields. Ellipsis (...) means leave unchanged for nullable fields."""
    if name is not None:
        campaign.name = name
    if description is not ...:
        campaign.description = description
    if starts_at is not ... and starts_at is not None:
        campaign.starts_at = starts_at
    if ends_at is not ...:
        campaign.ends_at = ends_at
    if max_winners is not None:
        campaign.max_winners = max(1, int(max_winners))
    if prize_type is not None:
        campaign.prize_type = prize_type
    if prize_value is not ...:
        campaign.prize_value = prize_value
    if prize_text is not ...:
        campaign.prize_text = prize_text
    if prize_slots is not ...:
        slots = prize_slots or None
        campaign.prize_slots = slots
        if slots:
            campaign.max_winners = max(1, len(slots))
    if tickets_per_purchase is not None:
        campaign.tickets_per_purchase = max(1, min(50, int(tickets_per_purchase)))
    if tickets_by_tariff is not ...:
        campaign.tickets_by_tariff = tickets_by_tariff or None
    if skip_trial_purchases is not None:
        campaign.skip_trial_purchases = bool(skip_trial_purchases)
    campaign.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(campaign)
    logger.info('Обновлена кампания розыгрыша', campaign_id=campaign.id)
    return campaign


async def delete_campaign(db: AsyncSession, campaign: RaffleCampaign) -> None:
    """Delete campaign; tickets/winners removed explicitly then campaign (FK CASCADE backup)."""
    campaign_id = campaign.id
    await db.execute(delete(RaffleWinner).where(RaffleWinner.campaign_id == campaign_id))
    await db.execute(delete(RaffleTicket).where(RaffleTicket.campaign_id == campaign_id))
    await db.delete(campaign)
    await db.commit()
    logger.info('Удалена кампания розыгрыша', campaign_id=campaign_id)


async def count_tickets_for_user(db: AsyncSession, campaign_id: int, user_id: int) -> int:
    result = await db.execute(
        select(func.count(RaffleTicket.id)).where(
            RaffleTicket.campaign_id == campaign_id,
            RaffleTicket.user_id == user_id,
        )
    )
    return int(result.scalar() or 0)


async def count_tickets_for_user_by_source(db: AsyncSession, campaign_id: int, user_id: int, source: str) -> int:
    result = await db.execute(
        select(func.count(RaffleTicket.id)).where(
            RaffleTicket.campaign_id == campaign_id,
            RaffleTicket.user_id == user_id,
            RaffleTicket.source == source,
        )
    )
    return int(result.scalar() or 0)


async def list_expired_active_campaigns(db: AsyncSession) -> list[RaffleCampaign]:
    """ACTIVE campaigns whose ends_at is in the past (due for auto-draw)."""
    now = datetime.now(UTC)
    result = await db.execute(
        select(RaffleCampaign).where(
            and_(
                RaffleCampaign.status == RaffleCampaignStatus.ACTIVE.value,
                RaffleCampaign.ends_at.is_not(None),
                RaffleCampaign.ends_at < now,
            )
        )
    )
    return list(result.scalars().all())


async def list_campaigns_needing_reminder(
    db: AsyncSession, *, hours_before: int, window_hours: float = 1.0
) -> list[RaffleCampaign]:
    """ACTIVE campaigns whose ends_at is within [now+hours_before-window, now+hours_before]."""
    from datetime import timedelta

    now = datetime.now(UTC)
    target = now + timedelta(hours=hours_before)
    window_start = target - timedelta(hours=window_hours)
    result = await db.execute(
        select(RaffleCampaign).where(
            and_(
                RaffleCampaign.status == RaffleCampaignStatus.ACTIVE.value,
                RaffleCampaign.ends_at.is_not(None),
                RaffleCampaign.ends_at >= window_start,
                RaffleCampaign.ends_at <= target,
            )
        )
    )
    return list(result.scalars().all())


async def list_distinct_ticket_user_ids(db: AsyncSession, campaign_id: int) -> list[int]:
    result = await db.execute(select(RaffleTicket.user_id).where(RaffleTicket.campaign_id == campaign_id).distinct())
    return [int(r) for r in result.scalars().all()]


async def has_reminder_been_sent(
    db: AsyncSession, campaign_id: int, user_id: int, reminder_type: str = 'ends_24h'
) -> bool:
    result = await db.execute(
        select(RaffleReminderLog.id)
        .where(
            RaffleReminderLog.campaign_id == campaign_id,
            RaffleReminderLog.user_id == user_id,
            RaffleReminderLog.reminder_type == reminder_type,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def mark_reminder_sent(
    db: AsyncSession,
    campaign_id: int,
    user_id: int,
    reminder_type: str = 'ends_24h',
    *,
    commit: bool = True,
) -> RaffleReminderLog:
    log = RaffleReminderLog(
        campaign_id=campaign_id,
        user_id=user_id,
        reminder_type=reminder_type,
        sent_at=datetime.now(UTC),
    )
    db.add(log)
    if commit:
        await db.commit()
    else:
        await db.flush()
    await db.refresh(log)
    return log


async def find_recent_source_tickets(
    db: AsyncSession,
    *,
    campaign_id: int,
    user_id: int,
    source: str,
    source_ref_prefix: str,
    within_seconds: int = 120,
) -> list[RaffleTicket]:
    from datetime import timedelta

    since = datetime.now(UTC) - timedelta(seconds=within_seconds)
    result = await db.execute(
        select(RaffleTicket).where(
            RaffleTicket.campaign_id == campaign_id,
            RaffleTicket.user_id == user_id,
            RaffleTicket.source == source,
            RaffleTicket.source_ref.is_not(None),
            RaffleTicket.source_ref.startswith(source_ref_prefix),
            RaffleTicket.created_at >= since,
        )
    )
    return list(result.scalars().all())


async def list_tickets_by_campaign_source_ref(
    db: AsyncSession, campaign_id: int, source: str, source_ref: str
) -> list[RaffleTicket]:
    result = await db.execute(
        select(RaffleTicket)
        .where(
            RaffleTicket.campaign_id == campaign_id,
            RaffleTicket.source == source,
            RaffleTicket.source_ref == source_ref,
        )
        .order_by(RaffleTicket.ticket_index.asc())
    )
    return list(result.scalars().all())
