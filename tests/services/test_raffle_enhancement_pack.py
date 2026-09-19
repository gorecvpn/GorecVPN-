"""Raffle enhancement pack: auto-draw gate, referral top-up tickets, antifraud refund skip."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.models import RaffleCampaignStatus, RaffleTicketSource
from app.services.raffle import service as raffle_service


def _stub_db() -> SimpleNamespace:
    db = SimpleNamespace()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    db.rollback = AsyncMock()
    db.get = AsyncMock(return_value=None)

    @asynccontextmanager
    async def _begin_nested():
        yield

    db.begin_nested = _begin_nested
    return db


def _settings(**overrides):
    base = dict(
        is_raffle_enabled=lambda: True,
        is_raffle_auto_draw_enabled=lambda: False,
        get_raffle_max_tickets_per_user=lambda: 0,
        get_raffle_max_tickets_per_payment=lambda: 0,
        get_raffle_referral_topup_tickets=lambda: 1,
        get_raffle_referral_topup_max_per_campaign=lambda: 50,
        is_raffle_reminder_enabled=lambda: False,
        get_raffle_reminder_hours_before=lambda: 24,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _active_campaign(**extra):
    data = dict(
        id=7,
        name='Test raffle',
        status=RaffleCampaignStatus.ACTIVE.value,
        max_winners=1,
        prize_type='custom',
        prize_value=None,
        prize_text='Prize',
        prize_slots=None,
        tickets_per_purchase=2,
        tickets_by_tariff=None,
        skip_trial_purchases=True,
        ends_at=datetime.now(UTC) - timedelta(hours=1),
    )
    data.update(extra)
    return SimpleNamespace(**data)


# ---- auto-draw gate ----


@pytest.mark.asyncio
async def test_auto_draw_gate_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(
        raffle_service,
        'settings',
        _settings(is_raffle_auto_draw_enabled=lambda: False),
    )
    list_due = AsyncMock(return_value=[_active_campaign()])
    monkeypatch.setattr(raffle_service.raffle_crud, 'list_expired_active_campaigns', list_due)
    draw = AsyncMock()
    monkeypatch.setattr(raffle_service, 'draw_winners', draw)

    result = await raffle_service.auto_draw_due_campaigns(_stub_db())
    assert result == []
    list_due.assert_not_called()
    draw.assert_not_called()


@pytest.mark.asyncio
async def test_auto_draw_gate_enabled_draws_due(monkeypatch):
    monkeypatch.setattr(
        raffle_service,
        'settings',
        _settings(is_raffle_auto_draw_enabled=lambda: True),
    )
    campaign = _active_campaign()
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'list_expired_active_campaigns',
        AsyncMock(return_value=[campaign]),
    )
    monkeypatch.setattr(raffle_service, 'draw_winners', AsyncMock(return_value=[]))

    result = await raffle_service.auto_draw_due_campaigns(_stub_db())
    assert result == [7]
    raffle_service.draw_winners.assert_awaited_once()


# ---- antifraud: refund / chargeback skip ----


@pytest.mark.asyncio
async def test_issue_skips_refund_transaction(monkeypatch):
    monkeypatch.setattr(raffle_service, 'settings', _settings())
    campaign = _active_campaign()
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'get_current_active_campaign',
        AsyncMock(return_value=campaign),
    )
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'list_tickets_by_campaign_tx',
        AsyncMock(return_value=[]),
    )
    create_ticket = AsyncMock()
    monkeypatch.setattr(raffle_service.raffle_crud, 'create_ticket', create_ticket)

    db = _stub_db()
    db.get = AsyncMock(
        return_value=SimpleNamespace(
            amount_kopeks=-19900,
            description='Refund for subscription payment',
            external_id='refund_abc',
            payment_method='yookassa',
            type='refund',
        )
    )
    result = await raffle_service.issue_for_purchase(db, user_id=42, transaction_id=1001)
    assert result == []
    create_ticket.assert_not_called()


@pytest.mark.asyncio
async def test_issue_skips_chargeback_transaction(monkeypatch):
    monkeypatch.setattr(raffle_service, 'settings', _settings())
    campaign = _active_campaign()
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'get_current_active_campaign',
        AsyncMock(return_value=campaign),
    )
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'list_tickets_by_campaign_tx',
        AsyncMock(return_value=[]),
    )
    create_ticket = AsyncMock()
    monkeypatch.setattr(raffle_service.raffle_crud, 'create_ticket', create_ticket)

    db = _stub_db()
    db.get = AsyncMock(
        return_value=SimpleNamespace(
            amount_kopeks=5000,
            description='chargeback dispute',
            external_id=None,
            payment_method='card',
            type=None,
        )
    )
    result = await raffle_service.issue_for_purchase(db, user_id=42, transaction_id=1002)
    assert result == []
    create_ticket.assert_not_called()


@pytest.mark.asyncio
async def test_looks_like_refund_helpers():
    assert raffle_service._looks_like_refund_or_chargeback(
        SimpleNamespace(description='возврат средств', external_id='', payment_method='', type='')
    )
    assert not raffle_service._looks_like_refund_or_chargeback(
        SimpleNamespace(
            description='Покупка тарифа',
            external_id='',
            payment_method='balance',
            type='subscription_payment',
        )
    )


# ---- referral top-up tickets ----


@pytest.mark.asyncio
async def test_referral_topup_disabled_when_tickets_zero(monkeypatch):
    monkeypatch.setattr(
        raffle_service,
        'settings',
        _settings(get_raffle_referral_topup_tickets=lambda: 0),
    )
    result = await raffle_service.issue_for_referral_topup(_stub_db(), 1, 2, 10000)
    assert result == []


@pytest.mark.asyncio
async def test_referral_topup_awards_referrer(monkeypatch):
    monkeypatch.setattr(raffle_service, 'settings', _settings())
    campaign = _active_campaign()
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'get_current_active_campaign',
        AsyncMock(return_value=campaign),
    )
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'find_recent_source_tickets',
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'count_tickets_for_user_by_source',
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'count_tickets_for_user',
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'list_tickets_by_campaign_source_ref',
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'get_campaign_by_id',
        AsyncMock(return_value=campaign),
    )

    created = []

    async def _create(db, **kwargs):
        kwargs = dict(kwargs)
        kwargs.setdefault('ticket_code', f'T{len(created)}')
        t = SimpleNamespace(id=len(created) + 1, **kwargs)
        created.append(t)
        return t

    monkeypatch.setattr(raffle_service.raffle_crud, 'create_ticket', AsyncMock(side_effect=_create))
    monkeypatch.setattr(raffle_service, '_notify_user_ticket', AsyncMock())

    result = await raffle_service.issue_for_referral_topup(
        _stub_db(), referrer_id=10, referee_id=20, topup_amount_kopeks=15000
    )
    assert len(result) == 1
    assert created[0].user_id == 10
    assert created[0].source == RaffleTicketSource.REFERRAL


@pytest.mark.asyncio
async def test_referral_topup_respects_campaign_cap(monkeypatch):
    monkeypatch.setattr(
        raffle_service,
        'settings',
        _settings(
            get_raffle_referral_topup_tickets=lambda: 2,
            get_raffle_referral_topup_max_per_campaign=lambda: 3,
        ),
    )
    campaign = _active_campaign()
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'get_current_active_campaign',
        AsyncMock(return_value=campaign),
    )
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'find_recent_source_tickets',
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'count_tickets_for_user_by_source',
        AsyncMock(return_value=3),
    )
    grant = AsyncMock()
    monkeypatch.setattr(raffle_service, 'grant_tickets', grant)

    result = await raffle_service.issue_for_referral_topup(_stub_db(), 10, 20, 5000)
    assert result == []
    grant.assert_not_called()
