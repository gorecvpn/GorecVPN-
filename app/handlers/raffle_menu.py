"""User-facing handler for the main-menu «Розыгрыш» button.

Always an in-bot screen (callback ``menu_raffle``). The cabinet ``/raffle``
page remains available via the miniapp itself — the bot menu must not launch it.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import Any

import structlog
from aiogram import Dispatcher, F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud import raffle as raffle_crud
from app.database.models import RaffleCampaign, RafflePrizeType, User
from app.localization.texts import get_texts
from app.services.raffle.service import (
    _normalize_prize_slots,
    max_winners_for_campaign,
    tickets_by_tariff_for_api,
)
from app.utils.decorators import auth_required, error_handler


logger = structlog.get_logger(__name__)

_TICKET_LIST_LIMIT = 30


def _format_prize_line(prize_type: str | None, prize_value: Any, prize_text: str | None, texts) -> str:
    """Human-readable prize description (HTML-escaped where needed)."""
    ptype = (prize_type or RafflePrizeType.CUSTOM.value).lower()
    if ptype == RafflePrizeType.DAYS.value and prize_value:
        return texts.t('RAFFLE_PRIZE_DAYS', '{days} дн. подписки').format(days=int(prize_value))
    if ptype == RafflePrizeType.BALANCE.value and prize_value:
        return texts.t('RAFFLE_PRIZE_BALANCE', '{amount}').format(amount=settings.format_price(int(prize_value)))
    if prize_text:
        return html.escape(str(prize_text))
    return html.escape(ptype or texts.t('RAFFLE_PRIZE_UNKNOWN', 'приз'))


def _format_prizes_block(campaign: RaffleCampaign, texts) -> str:
    slots = _normalize_prize_slots(getattr(campaign, 'prize_slots', None))
    lines: list[str] = []
    if slots:
        for slot in slots:
            place = int(slot.get('place') or 0)
            desc = _format_prize_line(
                slot.get('prize_type'),
                slot.get('prize_value'),
                slot.get('prize_text'),
                texts,
            )
            place_label = texts.t('RAFFLE_PLACE', '{place}-е место').format(place=place)
            lines.append(f'• {place_label}: {desc}')
    else:
        desc = _format_prize_line(campaign.prize_type, campaign.prize_value, campaign.prize_text, texts)
        winners = max_winners_for_campaign(campaign)
        if winners > 1:
            lines.append(texts.t('RAFFLE_PRIZE_MULTI', '• {desc} (победителей: {n})').format(desc=desc, n=winners))
        else:
            lines.append(f'• {desc}')
    header = texts.t('RAFFLE_PRIZES_HEADER', '🎁 <b>Призы</b>')
    return header + '\n' + '\n'.join(lines)


def _format_how_to_earn(campaign: RaffleCampaign, texts) -> str:
    default_n = max(1, int(getattr(campaign, 'tickets_per_purchase', 1) or 1))
    lines = [
        texts.t('RAFFLE_HOW_HEADER', '🎟 <b>Как получить билеты</b>'),
        texts.t(
            'RAFFLE_HOW_PURCHASE',
            'За покупку подписки — <b>{n}</b> билет(ов).',
        ).format(n=default_n),
    ]
    by_tariff = tickets_by_tariff_for_api(getattr(campaign, 'tickets_by_tariff', None))
    if by_tariff:
        lines.append(texts.t('RAFFLE_HOW_TARIFF_NOTE', 'Для отдельных тарифов число билетов может отличаться.'))
    if getattr(campaign, 'skip_trial_purchases', True):
        lines.append(texts.t('RAFFLE_HOW_SKIP_TRIAL', 'Триальные / бесплатные покупки билеты не дают.'))
    return '\n'.join(lines)


def _format_progress_block(campaign: RaffleCampaign, tickets: list, texts) -> str:
    """Tickets count + days left (no pool size)."""
    count = len(tickets)
    lines = [
        texts.t('RAFFLE_PROGRESS_HEADER', '📊 <b>Ваш прогресс</b>'),
        texts.t('RAFFLE_PROGRESS_TICKETS', 'Билетов у вас: <b>{count}</b>').format(count=count),
    ]
    ends_at = getattr(campaign, 'ends_at', None)
    if ends_at is not None:
        now = datetime.now(UTC)
        ends = ends_at if ends_at.tzinfo else ends_at.replace(tzinfo=UTC)
        delta = ends - now
        if delta.total_seconds() <= 0:
            lines.append(texts.t('RAFFLE_PROGRESS_ENDED', 'Кампания завершается — ожидайте итоги.'))
        else:
            days = max(0, delta.days)
            hours = max(0, delta.seconds // 3600)
            if days > 0:
                lines.append(texts.t('RAFFLE_PROGRESS_DAYS_LEFT', 'До окончания: <b>{days}</b> дн.').format(days=days))
            else:
                lines.append(
                    texts.t('RAFFLE_PROGRESS_HOURS_LEFT', 'До окончания: <b>{hours}</b> ч.').format(hours=hours)
                )
            lines.append(
                texts.t('RAFFLE_PROGRESS_ENDS_AT', 'Дата окончания: {dt}').format(
                    dt=html.escape(ends.strftime('%d.%m.%Y %H:%M UTC'))
                )
            )
    return '\n'.join(lines)


def _format_tickets_block(tickets: list, texts) -> str:
    count = len(tickets)
    header = texts.t('RAFFLE_YOUR_TICKETS', 'Ваши билеты: <b>{count}</b>').format(count=count)
    if count == 0:
        return header + '\n' + texts.t('RAFFLE_NO_TICKETS', 'Пока нет — купите или продлите подписку.')
    shown = tickets[:_TICKET_LIST_LIMIT]
    codes = '\n'.join(f'• <code>{html.escape(t.ticket_code or "")}</code>' for t in shown)
    extra = ''
    if count > _TICKET_LIST_LIMIT:
        extra = '\n' + texts.t(
            'RAFFLE_TICKETS_TRUNCATED',
            '…и ещё {n}',
        ).format(n=count - _TICKET_LIST_LIMIT)
    return f'{header}\n{codes}{extra}'


async def _build_raffle_screen(db: AsyncSession, db_user: User) -> tuple[str, InlineKeyboardMarkup] | None:
    """Build campaign summary text + Back keyboard. None → no active campaign."""
    language = getattr(db_user, 'language', None) or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)

    campaign = await raffle_crud.get_current_active_campaign(db)
    if campaign is None:
        return None

    tickets = await raffle_crud.list_tickets_for_user(db, db_user.id, campaign_id=campaign.id)

    parts: list[str] = [
        texts.t('RAFFLE_TITLE', '🎫 <b>{name}</b>').format(name=html.escape(campaign.name or '')),
    ]
    if campaign.description:
        parts.append(html.escape(campaign.description))
    parts.append(_format_prizes_block(campaign, texts))
    parts.append(_format_progress_block(campaign, tickets, texts))
    parts.append(_format_how_to_earn(campaign, texts))
    parts.append(_format_tickets_block(tickets, texts))

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')]]
    )
    return '\n\n'.join(parts), keyboard


@auth_required
@error_handler
async def handle_menu_raffle(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
) -> None:
    """Show in-bot raffle summary (campaign, prizes, how to earn, user tickets)."""
    language = (
        getattr(db_user, 'language', None)
        or getattr(callback.from_user, 'language_code', None)
        or settings.DEFAULT_LANGUAGE
    )
    try:
        texts = get_texts(language)
    except Exception:
        texts = get_texts()

    if not settings.is_raffle_enabled():
        await callback.answer(
            texts.t('RAFFLE_DISABLED', '🎫 Розыгрыш сейчас недоступен.'),
            show_alert=True,
        )
        return

    view = await _build_raffle_screen(db, db_user)
    if view is None:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')]]
        )
        message_text = texts.t(
            'RAFFLE_NO_CAMPAIGN',
            '🎫 Сейчас нет активного розыгрыша.\n\nЗагляните позже.',
        )
        try:
            await callback.message.edit_text(message_text, reply_markup=keyboard)
        except Exception:
            await callback.message.answer(message_text, reply_markup=keyboard)
        await callback.answer()
        return

    message_text, keyboard = view
    try:
        await callback.message.edit_text(message_text, reply_markup=keyboard)
    except Exception:
        await callback.message.answer(message_text, reply_markup=keyboard)
    await callback.answer()


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(handle_menu_raffle, F.data == 'menu_raffle')
