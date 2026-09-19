"""Сервис розыгрыша: выдача билетов за оплату подписки и жеребьёвка."""

from __future__ import annotations

import hashlib
import html
import random
import secrets
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud import raffle as raffle_crud
from app.database.crud.subscription import get_subscription_by_user_id
from app.database.crud.user import add_user_balance, get_user_by_id
from app.database.models import (
    RaffleCampaign,
    RaffleCampaignStatus,
    RafflePrizeType,
    RaffleTicket,
    RaffleTicketSource,
    RaffleWinner,
    Transaction,
)


logger = structlog.get_logger(__name__)

DRAW_ALGORITHM = 'weighted_unique_v1'


def _make_ticket_code() -> str:
    return f'RAFFLE_{secrets.token_hex(4).upper()}'


def _normalize_image_url(raw: Any, *, strict: bool = False) -> str | None:
    """Optional prize image: absolute https URL or site-relative path."""
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    if value.startswith('https://'):
        return value
    if value.startswith('/') and not value.startswith('//'):
        return value
    if strict:
        raise ValueError('image_url must be an https:// URL or a path starting with /')
    return None


def _normalize_prize_slots(raw: Any, *, strict_images: bool = False) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    slots: list[dict[str, Any]] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        place = int(item.get('place') or (idx + 1))
        prize_type = str(item.get('prize_type') or RafflePrizeType.CUSTOM.value).lower()
        image_url = _normalize_image_url(item.get('image_url'), strict=strict_images)
        slot = {
            'place': place,
            'prize_type': prize_type,
            'prize_value': item.get('prize_value'),
            'prize_text': item.get('prize_text'),
        }
        if image_url is not None:
            slot['image_url'] = image_url
        slots.append(slot)
    slots.sort(key=lambda s: s['place'])
    return slots


def resolve_prize_for_place(campaign: RaffleCampaign, place: int) -> dict[str, Any]:
    slots = _normalize_prize_slots(getattr(campaign, 'prize_slots', None))
    for slot in slots:
        if int(slot['place']) == int(place):
            return {
                'prize_type': slot.get('prize_type') or campaign.prize_type,
                'prize_value': slot.get('prize_value') if slot.get('prize_value') is not None else campaign.prize_value,
                'prize_text': slot.get('prize_text') if slot.get('prize_text') is not None else campaign.prize_text,
            }
    return {
        'prize_type': campaign.prize_type,
        'prize_value': campaign.prize_value,
        'prize_text': campaign.prize_text,
    }


def max_winners_for_campaign(campaign: RaffleCampaign) -> int:
    slots = _normalize_prize_slots(getattr(campaign, 'prize_slots', None))
    if slots:
        return max(1, len(slots))
    return max(1, int(campaign.max_winners or 1))


def _normalize_tickets_by_tariff(raw) -> dict[int, int]:
    if not isinstance(raw, dict):
        return {}
    out: dict[int, int] = {}
    for key, value in raw.items():
        try:
            tid = int(key)
            count = int(value)
        except (TypeError, ValueError):
            continue
        if tid < 1 or count < 1 or count > 50:
            continue
        out[tid] = count
    return out


def tickets_by_tariff_for_api(raw) -> dict[str, int] | None:
    """Cabinet contract: tickets_by_tariff keys are decimal strings."""
    mapping = _normalize_tickets_by_tariff(raw)
    if not mapping:
        return None
    return {str(k): int(v) for k, v in mapping.items()}


def tickets_count_for_purchase(campaign: RaffleCampaign, tariff_id: int | None) -> int:
    """Resolve how many tickets one purchase grants (per-tariff map, else default)."""
    default = max(1, min(50, int(getattr(campaign, 'tickets_per_purchase', 1) or 1)))
    mapping = _normalize_tickets_by_tariff(getattr(campaign, 'tickets_by_tariff', None))
    if tariff_id is not None and int(tariff_id) in mapping:
        return mapping[int(tariff_id)]
    return default


def _looks_like_trial_purchase(tx: Transaction | None) -> bool:
    if tx is None:
        return False
    # SUBSCRIPTION_PAYMENT / GIFT_PAYMENT are stored as negative debits in
    # create_transaction. Treat only a zero amount as free/trial — otherwise
    # every paid purchase would be skipped when skip_trial_purchases=True.
    if abs(int(getattr(tx, 'amount_kopeks', 0) or 0)) == 0:
        return True
    desc = (getattr(tx, 'description', None) or '').lower()
    external = (getattr(tx, 'external_id', None) or '').lower()
    method = (getattr(tx, 'payment_method', None) or '').lower()
    haystack = f'{desc} {external} {method}'
    return 'trial' in haystack or 'триал' in haystack


_REFUND_MARKERS = (
    'refund',
    'chargeback',
    'charge_back',
    'возврат',
    'чарджбек',
    'чарджбэк',
    'рефанд',
)


def _looks_like_refund_or_chargeback(tx: Transaction | None) -> bool:
    if tx is None:
        return False
    desc = (getattr(tx, 'description', None) or '').lower()
    external = (getattr(tx, 'external_id', None) or '').lower()
    method = (getattr(tx, 'payment_method', None) or '').lower()
    tx_type = str(getattr(tx, 'type', None) or getattr(tx, 'transaction_type', None) or '').lower()
    haystack = f'{desc} {external} {method} {tx_type}'
    return any(marker in haystack for marker in _REFUND_MARKERS)


def _should_skip_transaction(tx: Transaction | None, *, skip_trial: bool) -> bool:
    """Antifraud / eligibility gate. Paid logic uses abs(amount_kopeks)."""
    if tx is None:
        return False
    if abs(int(getattr(tx, 'amount_kopeks', 0) or 0)) == 0:
        return True
    if _looks_like_refund_or_chargeback(tx):
        return True
    if skip_trial and _looks_like_trial_purchase(tx):
        return True
    return False


def _synthetic_source_transaction_id(namespace: str, ref: str) -> int:
    """Stable negative id for non-purchase ticket batches (unique constraint)."""
    digest = hashlib.sha256(f'{namespace}:{ref}'.encode()).hexdigest()
    return -1 - (int(digest[:8], 16) % 1_999_999_999)


def _apply_ticket_caps(desired: int, *, user_have: int, per_user_cap: int, per_payment_cap: int) -> int:
    count = max(0, int(desired or 0))
    if per_payment_cap > 0:
        count = min(count, per_payment_cap)
    if per_user_cap > 0:
        remaining = max(0, per_user_cap - max(0, user_have))
        count = min(count, remaining)
    return count


async def _issue_ticket_batch(
    db: AsyncSession,
    *,
    campaign: RaffleCampaign,
    user_id: int,
    count: int,
    source_transaction_id: int,
    source: str,
    source_ref: str | None = None,
    tariff_id: int | None = None,
) -> list[RaffleTicket]:
    """Create up to ``count`` tickets for one source batch; notify once."""
    if count < 1:
        return []
    issued: list[RaffleTicket] = []
    for index in range(count):
        ticket = None
        for _ in range(5):
            code = _make_ticket_code()
            try:
                async with db.begin_nested():
                    ticket = await raffle_crud.create_ticket(
                        db,
                        campaign_id=campaign.id,
                        user_id=user_id,
                        ticket_code=code,
                        source_transaction_id=source_transaction_id,
                        tariff_id=tariff_id,
                        ticket_index=index,
                        source=source,
                        source_ref=source_ref,
                        commit=False,
                    )
                break
            except IntegrityError:
                already = await raffle_crud.list_tickets_by_campaign_tx(db, campaign.id, source_transaction_id)
                if already:
                    return already
                logger.debug('Коллизия кода билета, повтор', campaign_id=campaign.id)
        if ticket is None:
            logger.warning(
                'Не удалось выдать билет розыгрыша',
                user_id=user_id,
                source=source,
                ticket_index=index,
            )
            break
        issued.append(ticket)

    if not issued:
        return []

    await db.commit()
    for ticket in issued:
        await db.refresh(ticket)

    logger.info(
        'Выданы билеты розыгрыша',
        count=len(issued),
        user_id=user_id,
        campaign_id=campaign.id,
        source=source,
        source_transaction_id=source_transaction_id,
    )
    await _notify_user_ticket(db, user_id, issued[0], campaign, tickets_count=len(issued))
    return issued


async def issue_for_purchase(
    db: AsyncSession,
    user_id: int,
    transaction_id: int,
    tariff_id: int | None = None,
) -> list[RaffleTicket]:
    """Выдать N билетов за оплаченную подписку.

    N = tickets_by_tariff[tariff_id] если задано, иначе tickets_per_purchase.
    Идемпотентно по (campaign_id, source_transaction_id) — retries return existing rows.
    Пустой список — если RAFFLE_ENABLED=false / нет кампании / trial/refund skip / caps.
    """
    if not settings.is_raffle_enabled():
        return []
    if not user_id or not transaction_id:
        return []

    campaign = await raffle_crud.get_current_active_campaign(db)
    if campaign is None:
        return []

    existing = await raffle_crud.list_tickets_by_campaign_tx(db, campaign.id, transaction_id)
    if existing:
        return existing

    tx = await db.get(Transaction, transaction_id)
    if _should_skip_transaction(tx, skip_trial=bool(getattr(campaign, 'skip_trial_purchases', True))):
        logger.info(
            'Пропуск билета розыгрыша (trial/refund/zero)',
            transaction_id=transaction_id,
            campaign_id=campaign.id,
        )
        return []

    resolved_tariff_id = tariff_id
    if resolved_tariff_id is None:
        try:
            subscription = await get_subscription_by_user_id(db, user_id)
            resolved_tariff_id = getattr(subscription, 'tariff_id', None) if subscription else None
        except Exception:
            resolved_tariff_id = None

    desired = tickets_count_for_purchase(campaign, resolved_tariff_id)
    user_have = await raffle_crud.count_tickets_for_user(db, campaign.id, user_id)
    count = _apply_ticket_caps(
        desired,
        user_have=user_have,
        per_user_cap=settings.get_raffle_max_tickets_per_user(),
        per_payment_cap=settings.get_raffle_max_tickets_per_payment(),
    )
    if count < 1:
        logger.info(
            'Лимит билетов розыгрыша исчерпан',
            user_id=user_id,
            campaign_id=campaign.id,
            user_have=user_have,
        )
        return []

    return await _issue_ticket_batch(
        db,
        campaign=campaign,
        user_id=user_id,
        count=count,
        source_transaction_id=transaction_id,
        source=RaffleTicketSource.PURCHASE,
        source_ref=str(transaction_id),
        tariff_id=resolved_tariff_id,
    )


async def grant_tickets(
    db: AsyncSession,
    user_id: int,
    count: int,
    *,
    source: str = RaffleTicketSource.ADMIN,
    source_ref: str | None = None,
    campaign_id: int | None = None,
    notify: bool = True,
) -> list[RaffleTicket]:
    """Админ / промо: выдать N билетов на активную (или указанную) кампанию."""
    if not settings.is_raffle_enabled():
        return []
    if not user_id or count < 1:
        return []

    if campaign_id is not None:
        campaign = await raffle_crud.get_campaign_by_id(db, campaign_id)
        if campaign is None or campaign.status != RaffleCampaignStatus.ACTIVE.value:
            raise ValueError('Campaign is not active')
    else:
        campaign = await raffle_crud.get_current_active_campaign(db)
        if campaign is None:
            raise ValueError('No active campaign')

    ref = (source_ref or f'{source}:{user_id}:{secrets.token_hex(4)}')[:128]
    if source_ref:
        existing = await raffle_crud.list_tickets_by_campaign_source_ref(db, campaign.id, source, ref)
        if existing:
            return existing

    user_have = await raffle_crud.count_tickets_for_user(db, campaign.id, user_id)
    capped = _apply_ticket_caps(
        min(50, int(count)),
        user_have=user_have,
        per_user_cap=settings.get_raffle_max_tickets_per_user(),
        per_payment_cap=0,
    )
    if capped < 1:
        return []

    synthetic_id = _synthetic_source_transaction_id(source, f'{campaign.id}:{ref}')
    issued = await _issue_ticket_batch(
        db,
        campaign=campaign,
        user_id=user_id,
        count=capped,
        source_transaction_id=synthetic_id,
        source=source,
        source_ref=ref,
    )
    if not notify:
        return issued
    return issued


async def issue_for_referral_topup(
    db: AsyncSession,
    referrer_id: int,
    referee_id: int,
    topup_amount_kopeks: int,
) -> list[RaffleTicket]:
    """Award ticket(s) to referrer when referred user successfully tops up."""
    if not settings.is_raffle_enabled():
        return []
    tickets_n = settings.get_raffle_referral_topup_tickets()
    if tickets_n < 1 or topup_amount_kopeks <= 0 or not referrer_id or not referee_id:
        return []

    campaign = await raffle_crud.get_current_active_campaign(db)
    if campaign is None:
        return []

    prefix = f'referral:{referee_id}:'
    recent = await raffle_crud.find_recent_source_tickets(
        db,
        campaign_id=campaign.id,
        user_id=referrer_id,
        source=RaffleTicketSource.REFERRAL,
        source_ref_prefix=prefix,
        within_seconds=120,
    )
    if recent:
        return recent

    already_referral = await raffle_crud.count_tickets_for_user_by_source(
        db, campaign.id, referrer_id, RaffleTicketSource.REFERRAL
    )
    max_ref = settings.get_raffle_referral_topup_max_per_campaign()
    if max_ref > 0 and already_referral >= max_ref:
        logger.info(
            'Реферальный лимит билетов розыгрыша исчерпан',
            referrer_id=referrer_id,
            campaign_id=campaign.id,
            already=already_referral,
        )
        return []

    desired = tickets_n
    if max_ref > 0:
        desired = min(desired, max(0, max_ref - already_referral))

    user_have = await raffle_crud.count_tickets_for_user(db, campaign.id, referrer_id)
    count = _apply_ticket_caps(
        desired,
        user_have=user_have,
        per_user_cap=settings.get_raffle_max_tickets_per_user(),
        per_payment_cap=settings.get_raffle_max_tickets_per_payment(),
    )
    if count < 1:
        return []

    ref = f'{prefix}{topup_amount_kopeks}:{secrets.token_hex(3)}'[:128]
    return await grant_tickets(
        db,
        referrer_id,
        count,
        source=RaffleTicketSource.REFERRAL,
        source_ref=ref,
        campaign_id=campaign.id,
        notify=True,
    )


async def auto_draw_due_campaigns(db: AsyncSession) -> list[int]:
    """Draw ACTIVE campaigns whose ends_at has passed. Returns drawn campaign ids."""
    if not settings.is_raffle_auto_draw_enabled():
        return []
    due = await raffle_crud.list_expired_active_campaigns(db)
    drawn_ids: list[int] = []
    for campaign in due:
        try:
            await draw_winners(db, campaign.id)
            drawn_ids.append(campaign.id)
            logger.info('Авто-жеребьёвка розыгрыша', campaign_id=campaign.id)
        except Exception as exc:
            logger.warning('Авто-жеребьёвка не удалась', campaign_id=campaign.id, error=exc)
    return drawn_ids


async def send_ending_reminders(db: AsyncSession) -> int:
    """Notify users with tickets ~N hours before ends_at (deduped)."""
    if not settings.is_raffle_reminder_enabled():
        return 0
    hours = settings.get_raffle_reminder_hours_before()
    reminder_type = f'ends_{hours}h'
    campaigns = await raffle_crud.list_campaigns_needing_reminder(db, hours_before=hours)
    sent = 0
    for campaign in campaigns:
        user_ids = await raffle_crud.list_distinct_ticket_user_ids(db, campaign.id)
        for user_id in user_ids:
            try:
                if await raffle_crud.has_reminder_been_sent(db, campaign.id, user_id, reminder_type):
                    continue
                user = await get_user_by_id(db, user_id)
                if not user or not getattr(user, 'telegram_id', None):
                    await raffle_crud.mark_reminder_sent(db, campaign.id, user_id, reminder_type)
                    continue
                ends = campaign.ends_at
                ends_label = ends.strftime('%d.%m.%Y %H:%M UTC') if ends else ''
                tickets = await raffle_crud.list_tickets_for_user(db, user_id, campaign_id=campaign.id)
                text = (
                    f'⏰ Напоминание о розыгрыше <b>{html.escape(campaign.name)}</b>\n\n'
                    f'До окончания ~{hours} ч. ({html.escape(ends_label)}).\n'
                    f'У вас билетов: <b>{len(tickets)}</b>.'
                )
                from app.bot_factory import create_bot
                from app.services.notification_delivery_service import notification_delivery_service
                from app.services.notification_types import NotificationType

                bot = create_bot()
                try:
                    notif_type = getattr(NotificationType, 'RAFFLE_REMINDER', NotificationType.RAFFLE_TICKET)
                    await notification_delivery_service.send_notification(
                        user=user,
                        notification_type=notif_type,
                        context={
                            'campaign_name': campaign.name,
                            'hours_before': hours,
                            'tickets_count': len(tickets),
                        },
                        bot=bot,
                        telegram_message=text,
                    )
                finally:
                    await bot.session.close()
                await raffle_crud.mark_reminder_sent(db, campaign.id, user_id, reminder_type)
                sent += 1
            except Exception as exc:
                logger.debug(
                    'Не удалось отправить напоминание о розыгрыше',
                    campaign_id=campaign.id,
                    user_id=user_id,
                    error=exc,
                )
    return sent


async def _notify_user_ticket(
    db: AsyncSession,
    user_id: int,
    ticket: RaffleTicket,
    campaign: RaffleCampaign,
    *,
    tickets_count: int = 1,
) -> None:
    try:
        user = await get_user_by_id(db, user_id)
        if not user or not getattr(user, 'telegram_id', None):
            return

        if tickets_count > 1:
            text = (
                f'🎟 Вам выдано билетов розыгрыша: <b>{tickets_count}</b>\n\n'
                f'Кампания: <b>{html.escape(campaign.name)}</b>\n'
                f'Пример кода: <code>{html.escape(ticket.ticket_code)}</code>'
            )
        else:
            text = (
                '🎟 Вам выдан билет розыгрыша!\n\n'
                f'Кампания: <b>{html.escape(campaign.name)}</b>\n'
                f'Код билета: <code>{html.escape(ticket.ticket_code)}</code>'
            )

        from app.bot_factory import create_bot
        from app.services.notification_delivery_service import notification_delivery_service
        from app.services.notification_types import NotificationType

        bot = create_bot()
        try:
            await notification_delivery_service.send_notification(
                user=user,
                notification_type=NotificationType.RAFFLE_TICKET,
                context={
                    'ticket_code': ticket.ticket_code,
                    'campaign_name': campaign.name,
                    'tickets_count': tickets_count,
                },
                bot=bot,
                telegram_message=text,
            )
        finally:
            await bot.session.close()
    except Exception as exc:
        logger.debug('Не удалось уведомить о билете розыгрыша', user_id=user_id, error=exc)


def _make_draw_seed(campaign_id: int) -> str:
    raw = f'{campaign_id}:{datetime.now(UTC).isoformat()}:{secrets.token_hex(8)}'
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


async def draw_winners(db: AsyncSession, campaign_id: int) -> list[RaffleWinner]:
    """Взвешенный выбор уникальных пользователей по числу билетов; статус → drawn."""
    campaign = await raffle_crud.get_campaign_by_id(db, campaign_id)
    if campaign is None:
        raise ValueError(f'Campaign {campaign_id} not found')

    if campaign.status == RaffleCampaignStatus.DRAWN.value:
        return await raffle_crud.list_winners(db, campaign_id)

    tickets = await raffle_crud.list_tickets_for_campaign(db, campaign_id)
    seed = _make_draw_seed(campaign_id)
    rng = random.Random(int(seed, 16) % (2**32 - 1) or 1)
    drawn_at = datetime.now(UTC)

    if not tickets:
        campaign.status = RaffleCampaignStatus.DRAWN.value
        campaign.updated_at = drawn_at
        campaign.drawn_at = drawn_at
        campaign.draw_seed = seed
        campaign.draw_algorithm = DRAW_ALGORITHM
        await db.commit()
        return []

    by_user: dict[int, list[RaffleTicket]] = defaultdict(list)
    for ticket in tickets:
        by_user[ticket.user_id].append(ticket)

    remaining = {uid: list(ts) for uid, ts in by_user.items()}
    winners: list[RaffleWinner] = []
    max_winners = max_winners_for_campaign(campaign)

    for place in range(1, max_winners + 1):
        if not remaining:
            break
        population: list[int] = []
        for uid, ts in remaining.items():
            population.extend([uid] * len(ts))
        if not population:
            break
        chosen_user = rng.choice(population)
        user_tickets = remaining.pop(chosen_user)
        winning_ticket = rng.choice(user_tickets)
        prize = resolve_prize_for_place(campaign, place)

        awarded = False
        awarded_at = None
        try:
            awarded = await _try_award_prize(
                db,
                campaign,
                chosen_user,
                prize_type=prize['prize_type'],
                prize_value=prize['prize_value'],
            )
            if awarded:
                awarded_at = datetime.now(UTC)
        except Exception as exc:
            logger.warning(
                'Автоначисление приза розыгрыша не удалось',
                campaign_id=campaign_id,
                user_id=chosen_user,
                error=exc,
            )

        winner = await raffle_crud.create_winner(
            db,
            campaign_id=campaign.id,
            user_id=chosen_user,
            ticket_id=winning_ticket.id,
            ticket_code=winning_ticket.ticket_code,
            place=place,
            prize_type=prize['prize_type'],
            prize_value=prize['prize_value'],
            prize_text=prize['prize_text'],
            awarded=awarded,
            awarded_at=awarded_at,
            commit=False,
        )
        winners.append(winner)

    campaign.status = RaffleCampaignStatus.DRAWN.value
    campaign.updated_at = drawn_at
    campaign.drawn_at = drawn_at
    campaign.draw_seed = seed
    campaign.draw_algorithm = DRAW_ALGORITHM
    await db.commit()

    for w in winners:
        await db.refresh(w)

    await _notify_admins_draw(db, campaign, winners)
    await _notify_winners(db, campaign, winners)
    return winners


async def retry_award_winner(db: AsyncSession, winner_id: int) -> RaffleWinner:
    winner = await raffle_crud.get_winner_by_id(db, winner_id)
    if winner is None:
        raise ValueError('Winner not found')
    if winner.awarded:
        return winner
    campaign = winner.campaign or await raffle_crud.get_campaign_by_id(db, winner.campaign_id)
    if campaign is None:
        raise ValueError('Campaign not found')

    awarded = await _try_award_prize(
        db,
        campaign,
        winner.user_id,
        prize_type=winner.prize_type or campaign.prize_type,
        prize_value=winner.prize_value if winner.prize_value is not None else campaign.prize_value,
    )
    if not awarded:
        raise ValueError('Prize could not be awarded automatically (custom prize or missing subscription)')
    winner.awarded = True
    winner.awarded_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(winner)
    return winner


async def _try_award_prize(
    db: AsyncSession,
    campaign: RaffleCampaign,
    user_id: int,
    *,
    prize_type: str | None = None,
    prize_value: int | None = None,
) -> bool:
    prize_type = (prize_type or campaign.prize_type or RafflePrizeType.CUSTOM.value).lower()
    if prize_value is None:
        prize_value = campaign.prize_value

    if prize_type == RafflePrizeType.CUSTOM.value or prize_value is None or int(prize_value) <= 0:
        return False

    user = await get_user_by_id(db, user_id)
    if user is None:
        return False

    if prize_type == RafflePrizeType.BALANCE.value:
        await add_user_balance(
            db,
            user,
            int(prize_value),
            description=f'Приз розыгрыша «{campaign.name}»',
            create_transaction=True,
            commit=False,
        )
        return True

    if prize_type == RafflePrizeType.DAYS.value:
        if settings.is_multi_tariff_enabled():
            return False
        from app.services.grace_access_echo import undo_grace_overlay_echo
        from app.services.subscription_service import SubscriptionService

        subscription = await get_subscription_by_user_id(db, user_id)
        if subscription is None:
            return False
        await undo_grace_overlay_echo(db, subscription)
        base = subscription.end_date or datetime.now(UTC)
        subscription.end_date = base + timedelta(days=int(prize_value))
        subscription.updated_at = datetime.now(UTC)
        # Условия тарифа на новый срок: база тарифа + активные докупки.
        from app.database.crud.subscription import reconcile_tariff_traffic_limit

        await reconcile_tariff_traffic_limit(db, subscription)
        try:
            await SubscriptionService().update_remnawave_user(db, subscription)
        except Exception as exc:
            logger.warning('RemnaWave sync после приза дней розыгрыша', user_id=user_id, error=exc)
        return True

    return False


async def _notify_winners(
    db: AsyncSession,
    campaign: RaffleCampaign,
    winners: list[RaffleWinner],
) -> None:
    try:
        from app.bot_factory import create_bot
        from app.services.notification_delivery_service import notification_delivery_service
        from app.services.notification_types import NotificationType

        bot = create_bot()
        try:
            for w in winners:
                user = await get_user_by_id(db, w.user_id)
                if not user or not getattr(user, 'telegram_id', None):
                    continue
                prize_bits = []
                if w.prize_text:
                    prize_bits.append(html.escape(w.prize_text))
                elif w.prize_type == RafflePrizeType.DAYS.value and w.prize_value:
                    prize_bits.append(f'{int(w.prize_value)} дн. подписки')
                elif w.prize_type == RafflePrizeType.BALANCE.value and w.prize_value:
                    prize_bits.append(f'{int(w.prize_value) / 100:.0f} ₽')
                else:
                    prize_bits.append(html.escape(w.prize_type or 'приз'))
                award_note = 'Приз начислен автоматически.' if w.awarded else 'Приз будет выдан администратором.'
                text = (
                    f'🏆 Поздравляем! Вы заняли <b>{w.place}</b> место в розыгрыше '
                    f'<b>{html.escape(campaign.name)}</b>!\n\n'
                    f'Билет: <code>{html.escape(w.ticket_code or "")}</code>\n'
                    f'Приз: {" ".join(prize_bits)}\n'
                    f'{award_note}'
                )
                try:
                    notif_type = getattr(NotificationType, 'RAFFLE_WINNER', NotificationType.RAFFLE_TICKET)
                    await notification_delivery_service.send_notification(
                        user=user,
                        notification_type=notif_type,
                        context={
                            'campaign_name': campaign.name,
                            'place': w.place,
                            'ticket_code': w.ticket_code,
                        },
                        bot=bot,
                        telegram_message=text,
                    )
                except Exception as exc:
                    logger.debug('Не удалось уведомить победителя розыгрыша', user_id=w.user_id, error=exc)
        finally:
            await bot.session.close()
    except Exception as exc:
        logger.debug('Не удалось отправить уведомления победителям', campaign_id=campaign.id, error=exc)


async def _notify_admins_draw(
    db: AsyncSession,
    campaign: RaffleCampaign,
    winners: list[RaffleWinner],
) -> None:
    try:
        lines = [
            '🎲 <b>Розыгрыш завершён</b>',
            f'Кампания: <b>{html.escape(campaign.name)}</b> (#{campaign.id})',
            f'Алгоритм: <code>{html.escape(campaign.draw_algorithm or DRAW_ALGORITHM)}</code>',
            f'Seed: <code>{html.escape(campaign.draw_seed or "")}</code>',
            f'Приз (кампания): {html.escape(campaign.prize_type or "")}'
            + (f' / {campaign.prize_value}' if campaign.prize_value else '')
            + (f' — {html.escape(campaign.prize_text)}' if campaign.prize_text else ''),
            '',
            'Победители:',
        ]
        if not winners:
            lines.append('Билетов не было.')
        for w in winners:
            user = await get_user_by_id(db, w.user_id)
            uname = f'@{user.username}' if user and user.username else f'user#{w.user_id}'
            award_mark = '✅' if w.awarded else '⏳ ручная выдача'
            place_prize = f'{w.prize_type or ""}'
            if w.prize_value is not None:
                place_prize += f'/{w.prize_value}'
            lines.append(
                f'{w.place}. {html.escape(uname)} — <code>{html.escape(w.ticket_code or "")}</code> '
                f'({html.escape(place_prize)}) {award_mark}'
            )

        from app.bot_factory import create_bot
        from app.services.admin_notification_service import AdminNotificationService, NotificationCategory

        bot = create_bot()
        try:
            service = AdminNotificationService(bot)
            await service.send_admin_notification(
                '\n'.join(lines),
                category=NotificationCategory.PROMO,
            )
        finally:
            await bot.session.close()
    except Exception as exc:
        logger.debug('Не удалось уведомить админов о розыгрыше', campaign_id=campaign.id, error=exc)


class RaffleService:
    async def issue_for_purchase(self, db, user_id, transaction_id, tariff_id=None):
        return await issue_for_purchase(db, user_id, transaction_id, tariff_id=tariff_id)

    async def grant_tickets(self, db, user_id, count, **kwargs):
        return await grant_tickets(db, user_id, count, **kwargs)

    async def issue_for_referral_topup(self, db, referrer_id, referee_id, topup_amount_kopeks):
        return await issue_for_referral_topup(db, referrer_id, referee_id, topup_amount_kopeks)

    async def draw_winners(self, db, campaign_id):
        return await draw_winners(db, campaign_id)

    async def retry_award_winner(self, db, winner_id):
        return await retry_award_winner(db, winner_id)

    async def auto_draw_due_campaigns(self, db):
        return await auto_draw_due_campaigns(db)

    async def send_ending_reminders(self, db):
        return await send_ending_reminders(db)


raffle_service = RaffleService()
