"""Минимальная Telegram-админка розыгрыша билетов за покупку подписки."""

from __future__ import annotations

import html

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud import raffle as raffle_crud
from app.database.crud.user import get_user_by_telegram_id
from app.database.models import RaffleCampaignStatus, RafflePrizeType, RaffleTicketSource, User
from app.services.raffle.service import draw_winners, grant_tickets
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)

_CANCEL = types.InlineKeyboardMarkup(
    inline_keyboard=[[types.InlineKeyboardButton(text='❌ Отмена', callback_data='admin_raffles')]]
)


def _status_label(status: str) -> str:
    return {
        RaffleCampaignStatus.DRAFT.value: '📝 draft',
        RaffleCampaignStatus.ACTIVE.value: '🟢 active',
        RaffleCampaignStatus.CLOSED.value: '⚪️ closed',
        RaffleCampaignStatus.DRAWN.value: '🎲 drawn',
    }.get(status, status)


async def _render_menu(db: AsyncSession) -> tuple[str, types.InlineKeyboardMarkup]:
    enabled = settings.is_raffle_enabled()
    campaigns = await raffle_crud.list_campaigns(db, limit=15)
    lines = [
        '🎟 <b>Розыгрыш билетов</b>',
        f'RAFFLE_ENABLED: <b>{"on" if enabled else "off"}</b>',
        '',
    ]
    keyboard: list[list[types.InlineKeyboardButton]] = [
        [types.InlineKeyboardButton(text='➕ Создать кампанию', callback_data='admin_raffle_create')],
    ]
    if not campaigns:
        lines.append('Кампаний пока нет.')
    for campaign in campaigns:
        stats = await raffle_crud.get_campaign_ticket_stats(db, campaign.id)
        lines.append(
            f'#{campaign.id} <b>{html.escape(campaign.name)}</b> — {_status_label(campaign.status)}'
            f' · билетов {stats["tickets"]} / юзеров {stats["unique_users"]}'
        )
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=f'#{campaign.id} {campaign.name[:20]}',
                    callback_data=f'admin_raffle_view_{campaign.id}',
                )
            ]
        )
    keyboard.append([types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_promo_submenu')])
    return '\n'.join(lines), types.InlineKeyboardMarkup(inline_keyboard=keyboard)


@admin_required
@error_handler
async def show_raffles_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.clear()
    text, markup = await _render_menu(db)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@admin_required
@error_handler
async def start_create_campaign(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    await state.set_state(AdminStates.creating_raffle_campaign_name)
    await callback.message.edit_text('Введите название кампании розыгрыша:', reply_markup=_CANCEL)
    await callback.answer()


@admin_required
@error_handler
async def process_campaign_name(message: types.Message, state: FSMContext, db_user: User):
    name = (message.text or '').strip()
    if not name or len(name) > 200:
        await message.answer('Название 1–200 символов.', reply_markup=_CANCEL)
        return
    await state.update_data(raffle_name=name)
    await state.set_state(AdminStates.creating_raffle_campaign_winners)
    await message.answer('Сколько победителей? (число, по умолчанию 1)', reply_markup=_CANCEL)


@admin_required
@error_handler
async def process_campaign_winners(message: types.Message, state: FSMContext, db_user: User):
    raw = (message.text or '').strip()
    try:
        max_winners = int(raw)
    except ValueError:
        await message.answer('Введите целое число ≥ 1', reply_markup=_CANCEL)
        return
    if max_winners < 1 or max_winners > 1000:
        await message.answer('Диапазон 1–1000', reply_markup=_CANCEL)
        return
    await state.update_data(raffle_max_winners=max_winners)
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(text='📅 Дни', callback_data='admin_raffle_prize_days'),
                types.InlineKeyboardButton(text='💰 Баланс', callback_data='admin_raffle_prize_balance'),
            ],
            [types.InlineKeyboardButton(text='📝 Custom (текст)', callback_data='admin_raffle_prize_custom')],
            [types.InlineKeyboardButton(text='❌ Отмена', callback_data='admin_raffles')],
        ]
    )
    await message.answer('Тип приза:', reply_markup=keyboard)


@admin_required
@error_handler
async def select_prize_type(callback: types.CallbackQuery, state: FSMContext, db_user: User):
    mapping = {
        'admin_raffle_prize_days': RafflePrizeType.DAYS.value,
        'admin_raffle_prize_balance': RafflePrizeType.BALANCE.value,
        'admin_raffle_prize_custom': RafflePrizeType.CUSTOM.value,
    }
    prize_type = mapping.get(callback.data or '')
    if not prize_type:
        await callback.answer('Неизвестный тип', show_alert=True)
        return
    await state.update_data(raffle_prize_type=prize_type)
    await state.set_state(AdminStates.creating_raffle_campaign_prize_value)
    if prize_type == RafflePrizeType.CUSTOM.value:
        prompt = 'Введите текст приза (описание для победителя):'
    elif prize_type == RafflePrizeType.DAYS.value:
        prompt = 'Введите число дней подписки:'
    else:
        prompt = 'Введите сумму приза в копейках (например 10000 = 100₽):'
    await callback.message.edit_text(prompt, reply_markup=_CANCEL)
    await callback.answer()


@admin_required
@error_handler
async def process_prize_value(message: types.Message, state: FSMContext, db: AsyncSession, db_user: User):
    data = await state.get_data()
    prize_type = data.get('raffle_prize_type', RafflePrizeType.CUSTOM.value)
    raw = (message.text or '').strip()
    prize_value = None
    prize_text = None
    if prize_type == RafflePrizeType.CUSTOM.value:
        prize_text = raw
        if not prize_text:
            await message.answer('Текст не должен быть пустым', reply_markup=_CANCEL)
            return
    else:
        try:
            prize_value = int(raw)
        except ValueError:
            await message.answer('Введите целое число', reply_markup=_CANCEL)
            return
        if prize_value <= 0:
            await message.answer('Значение должно быть > 0', reply_markup=_CANCEL)
            return

    campaign = await raffle_crud.create_campaign(
        db,
        name=data['raffle_name'],
        max_winners=int(data.get('raffle_max_winners') or 1),
        prize_type=prize_type,
        prize_value=prize_value,
        prize_text=prize_text,
        status=RaffleCampaignStatus.DRAFT.value,
    )
    await state.clear()
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text='🟢 Активировать',
                    callback_data=f'admin_raffle_activate_{campaign.id}',
                )
            ],
            [types.InlineKeyboardButton(text='⬅️ К списку', callback_data='admin_raffles')],
        ]
    )
    await message.answer(
        f'✅ Кампания #{campaign.id} «{html.escape(campaign.name)}» создана (draft).',
        reply_markup=keyboard,
    )


@admin_required
@error_handler
async def view_campaign(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        campaign_id = int((callback.data or '').rsplit('_', 1)[-1])
    except ValueError:
        await callback.answer('Ошибка ID', show_alert=True)
        return
    campaign = await raffle_crud.get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer('Не найдена', show_alert=True)
        return
    stats = await raffle_crud.get_campaign_ticket_stats(db, campaign.id)
    winners = await raffle_crud.list_winners(db, campaign.id)
    lines = [
        f'🎟 <b>Кампания #{campaign.id}</b>',
        f'Название: <b>{html.escape(campaign.name)}</b>',
        f'Статус: {_status_label(campaign.status)}',
        f'Победителей макс: {campaign.max_winners}',
        f'Приз: {html.escape(campaign.prize_type or "")}'
        + (f' / {campaign.prize_value}' if campaign.prize_value else '')
        + (f' — {html.escape(campaign.prize_text)}' if campaign.prize_text else ''),
        f'Билетов: {stats["tickets"]}',
        f'Уникальных юзеров: {stats["unique_users"]}',
        f'Победителей записано: {stats["winners"]}',
    ]
    if winners:
        lines.append('')
        lines.append('Победители:')
        for winner in winners:
            mark = '✅' if winner.awarded else '⏳'
            lines.append(
                f'{winner.place}. user#{winner.user_id} <code>{html.escape(winner.ticket_code or "")}</code> {mark}'
            )

    rows: list[list[types.InlineKeyboardButton]] = []
    if campaign.status == RaffleCampaignStatus.DRAFT.value:
        rows.append(
            [
                types.InlineKeyboardButton(
                    text='🟢 Активировать',
                    callback_data=f'admin_raffle_activate_{campaign.id}',
                )
            ]
        )
    if campaign.status == RaffleCampaignStatus.ACTIVE.value:
        rows.append(
            [
                types.InlineKeyboardButton(
                    text='⏹ Закрыть',
                    callback_data=f'admin_raffle_close_{campaign.id}',
                )
            ]
        )
        rows.append(
            [
                types.InlineKeyboardButton(
                    text='🎟 Выдать билеты',
                    callback_data=f'admin_raffle_grant_{campaign.id}',
                )
            ]
        )
        rows.append(
            [
                types.InlineKeyboardButton(
                    text='🎲 Провести розыгрыш',
                    callback_data=f'admin_raffle_draw_{campaign.id}',
                )
            ]
        )
    if campaign.status == RaffleCampaignStatus.CLOSED.value:
        rows.append(
            [
                types.InlineKeyboardButton(
                    text='🎲 Провести розыгрыш',
                    callback_data=f'admin_raffle_draw_{campaign.id}',
                )
            ]
        )
    rows.append([types.InlineKeyboardButton(text='⬅️ К списку', callback_data='admin_raffles')])
    await callback.message.edit_text(
        '\n'.join(lines),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@admin_required
@error_handler
async def activate_campaign(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    campaign_id = int((callback.data or '').rsplit('_', 1)[-1])
    campaign = await raffle_crud.get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer('Не найдена', show_alert=True)
        return
    if not settings.is_raffle_enabled():
        await callback.answer('Сначала включите RAFFLE_ENABLED', show_alert=True)
        return
    await raffle_crud.set_campaign_status(db, campaign, RaffleCampaignStatus.ACTIVE.value)
    await callback.answer('Активирована')
    callback.data = f'admin_raffle_view_{campaign_id}'
    await view_campaign(callback, db_user, db)


@admin_required
@error_handler
async def close_campaign(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    campaign_id = int((callback.data or '').rsplit('_', 1)[-1])
    campaign = await raffle_crud.get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer('Не найдена', show_alert=True)
        return
    await raffle_crud.set_campaign_status(db, campaign, RaffleCampaignStatus.CLOSED.value)
    await callback.answer('Закрыта')
    callback.data = f'admin_raffle_view_{campaign_id}'
    await view_campaign(callback, db_user, db)


@admin_required
@error_handler
async def run_draw(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    campaign_id = int((callback.data or '').rsplit('_', 1)[-1])
    try:
        winners = await draw_winners(db, campaign_id)
    except Exception as exc:
        logger.exception('Ошибка розыгрыша', campaign_id=campaign_id, error=exc)
        await callback.answer(f'Ошибка: {exc}', show_alert=True)
        return
    await callback.answer(f'Готово, победителей: {len(winners)}', show_alert=True)
    callback.data = f'admin_raffle_view_{campaign_id}'
    await view_campaign(callback, db_user, db)


@admin_required
@error_handler
async def start_grant_tickets(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    try:
        campaign_id = int((callback.data or '').rsplit('_', 1)[-1])
    except ValueError:
        await callback.answer('Ошибка ID', show_alert=True)
        return
    await state.set_state(AdminStates.raffle_grant_telegram_id)
    await state.update_data(raffle_grant_campaign_id=campaign_id)
    await callback.message.edit_text(
        f'Выдача билетов для кампании #{campaign_id}.\n\nВведите Telegram ID пользователя:',
        reply_markup=_CANCEL,
    )
    await callback.answer()


@admin_required
@error_handler
async def process_grant_telegram_id(message: types.Message, state: FSMContext, db_user: User):
    raw = (message.text or '').strip()
    try:
        telegram_id = int(raw)
    except ValueError:
        await message.answer('Введите числовой Telegram ID', reply_markup=_CANCEL)
        return
    await state.update_data(raffle_grant_telegram_id=telegram_id)
    await state.set_state(AdminStates.raffle_grant_count)
    await message.answer('Сколько билетов выдать? (1–50)', reply_markup=_CANCEL)


@admin_required
@error_handler
async def process_grant_count(message: types.Message, state: FSMContext, db: AsyncSession, db_user: User):
    raw = (message.text or '').strip()
    try:
        count = int(raw)
    except ValueError:
        await message.answer('Введите целое число 1–50', reply_markup=_CANCEL)
        return
    if count < 1 or count > 50:
        await message.answer('Диапазон 1–50', reply_markup=_CANCEL)
        return
    data = await state.get_data()
    campaign_id = int(data['raffle_grant_campaign_id'])
    telegram_id = int(data['raffle_grant_telegram_id'])
    user = await get_user_by_telegram_id(db, telegram_id)
    if user is None:
        await message.answer('Пользователь не найден', reply_markup=_CANCEL)
        return
    try:
        tickets = await grant_tickets(
            db,
            user.id,
            count,
            source=RaffleTicketSource.ADMIN,
            source_ref=f'admin_tg:{db_user.id}:{telegram_id}',
            campaign_id=campaign_id,
            notify=True,
        )
    except ValueError as exc:
        await message.answer(f'Ошибка: {html.escape(str(exc))}', reply_markup=_CANCEL)
        return
    await state.clear()
    codes = ', '.join(f'<code>{html.escape(t.ticket_code)}</code>' for t in tickets[:10])
    extra = f' …и ещё {len(tickets) - 10}' if len(tickets) > 10 else ''
    await message.answer(
        f'✅ Выдано билетов: <b>{len(tickets)}</b> пользователю <code>{telegram_id}</code>\n{codes}{extra}',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text='⬅️ К кампании',
                        callback_data=f'admin_raffle_view_{campaign_id}',
                    )
                ]
            ]
        ),
    )


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(show_raffles_menu, F.data == 'admin_raffles')
    dp.callback_query.register(start_create_campaign, F.data == 'admin_raffle_create')
    dp.callback_query.register(
        select_prize_type,
        F.data.in_(
            {
                'admin_raffle_prize_days',
                'admin_raffle_prize_balance',
                'admin_raffle_prize_custom',
            }
        ),
    )
    dp.callback_query.register(view_campaign, F.data.startswith('admin_raffle_view_'))
    dp.callback_query.register(activate_campaign, F.data.startswith('admin_raffle_activate_'))
    dp.callback_query.register(close_campaign, F.data.startswith('admin_raffle_close_'))
    dp.callback_query.register(run_draw, F.data.startswith('admin_raffle_draw_'))
    dp.callback_query.register(start_grant_tickets, F.data.startswith('admin_raffle_grant_'))
    dp.message.register(process_campaign_name, AdminStates.creating_raffle_campaign_name)
    dp.message.register(process_campaign_winners, AdminStates.creating_raffle_campaign_winners)
    dp.message.register(process_prize_value, AdminStates.creating_raffle_campaign_prize_value)
    dp.message.register(process_grant_telegram_id, AdminStates.raffle_grant_telegram_id)
    dp.message.register(process_grant_count, AdminStates.raffle_grant_count)
