"""Idempotency of raffle issue_for_purchase on (campaign_id, source_transaction_id)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.models import RaffleCampaignStatus
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


def _patch_settings(monkeypatch, *, enabled: bool):
    monkeypatch.setattr(
        raffle_service,
        'settings',
        SimpleNamespace(
            is_raffle_enabled=lambda: enabled,
            get_raffle_max_tickets_per_user=lambda: 0,
            get_raffle_max_tickets_per_payment=lambda: 0,
        ),
    )


@pytest.fixture
def raffle_enabled(monkeypatch):
    _patch_settings(monkeypatch, enabled=True)


@pytest.fixture
def active_campaign():
    return SimpleNamespace(
        id=7,
        name='Test raffle',
        status=RaffleCampaignStatus.ACTIVE.value,
        max_winners=1,
        prize_type='custom',
        prize_value=None,
        prize_text='Prize',
        prize_slots=None,
        tickets_per_purchase=1,
        skip_trial_purchases=True,
    )


async def test_issue_for_purchase_disabled_returns_empty(monkeypatch):
    _patch_settings(monkeypatch, enabled=False)
    result = await raffle_service.issue_for_purchase(_stub_db(), user_id=1, transaction_id=100)
    assert result == []


async def test_issue_for_purchase_no_campaign_returns_empty(raffle_enabled, monkeypatch):
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'get_current_active_campaign',
        AsyncMock(return_value=None),
    )
    result = await raffle_service.issue_for_purchase(_stub_db(), user_id=1, transaction_id=100)
    assert result == []


async def test_issue_for_purchase_idempotent(raffle_enabled, active_campaign, monkeypatch):
    existing = [
        SimpleNamespace(
            id=1,
            campaign_id=7,
            user_id=42,
            ticket_code='RAFFLE_AAAA',
            source_transaction_id=555,
            ticket_index=0,
        )
    ]
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'get_current_active_campaign',
        AsyncMock(return_value=active_campaign),
    )
    list_tickets = AsyncMock(return_value=existing)
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'list_tickets_by_campaign_tx',
        list_tickets,
    )
    create_ticket = AsyncMock()
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'create_ticket',
        create_ticket,
    )

    first = await raffle_service.issue_for_purchase(_stub_db(), user_id=42, transaction_id=555)
    second = await raffle_service.issue_for_purchase(_stub_db(), user_id=42, transaction_id=555)

    assert first is existing
    assert second is existing
    create_ticket.assert_not_called()
    assert list_tickets.await_count == 2


async def test_issue_for_purchase_creates_once(raffle_enabled, active_campaign, monkeypatch):
    created = SimpleNamespace(
        id=9,
        campaign_id=7,
        user_id=42,
        ticket_code='RAFFLE_BBBB',
        source_transaction_id=777,
        ticket_index=0,
    )
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'get_current_active_campaign',
        AsyncMock(return_value=active_campaign),
    )
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'list_tickets_by_campaign_tx',
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'count_tickets_for_user',
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'create_ticket',
        AsyncMock(return_value=created),
    )
    monkeypatch.setattr(
        raffle_service,
        '_notify_user_ticket',
        AsyncMock(return_value=None),
    )

    result = await raffle_service.issue_for_purchase(_stub_db(), user_id=42, transaction_id=777, tariff_id=3)
    assert result == [created]


async def test_issue_skips_trial_when_flag_set(raffle_enabled, active_campaign, monkeypatch):
    active_campaign.skip_trial_purchases = True
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'get_current_active_campaign',
        AsyncMock(return_value=active_campaign),
    )
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'list_tickets_by_campaign_tx',
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'count_tickets_for_user',
        AsyncMock(return_value=0),
    )
    create_ticket = AsyncMock()
    monkeypatch.setattr(raffle_service.raffle_crud, 'create_ticket', create_ticket)

    db = _stub_db()
    db.get = AsyncMock(
        return_value=SimpleNamespace(
            amount_kopeks=0,
            description='trial activation',
            external_id='trial_1',
            payment_method='trial',
        )
    )
    result = await raffle_service.issue_for_purchase(db, user_id=42, transaction_id=1)
    assert result == []
    create_ticket.assert_not_called()


async def test_issue_uses_tickets_by_tariff(raffle_enabled, active_campaign, monkeypatch):
    active_campaign.tickets_per_purchase = 1
    active_campaign.tickets_by_tariff = {'3': 3}
    created_calls = []

    async def _create(db, **kwargs):
        ticket = SimpleNamespace(id=len(created_calls) + 1, **kwargs)
        if not getattr(ticket, 'ticket_code', None):
            ticket.ticket_code = f'T{len(created_calls)}'
        created_calls.append(ticket)
        return ticket

    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'get_current_active_campaign',
        AsyncMock(return_value=active_campaign),
    )
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'list_tickets_by_campaign_tx',
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'count_tickets_for_user',
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(raffle_service.raffle_crud, 'create_ticket', AsyncMock(side_effect=_create))
    monkeypatch.setattr(raffle_service, '_notify_user_ticket', AsyncMock(return_value=None))

    result = await raffle_service.issue_for_purchase(_stub_db(), user_id=42, transaction_id=900, tariff_id=3)
    assert len(result) == 3
    assert [t.ticket_index for t in result] == [0, 1, 2]


async def test_issue_allows_paid_negative_amount_subscription(raffle_enabled, active_campaign, monkeypatch):
    """Paid SUBSCRIPTION_PAYMENT rows store negative amount_kopeks — must still issue."""
    active_campaign.skip_trial_purchases = True
    created = SimpleNamespace(
        id=11,
        campaign_id=7,
        user_id=42,
        ticket_code='RAFFLE_PAID',
        source_transaction_id=1001,
        ticket_index=0,
    )
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'get_current_active_campaign',
        AsyncMock(return_value=active_campaign),
    )
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'list_tickets_by_campaign_tx',
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'count_tickets_for_user',
        AsyncMock(return_value=0),
    )
    create_ticket = AsyncMock(return_value=created)
    monkeypatch.setattr(raffle_service.raffle_crud, 'create_ticket', create_ticket)
    monkeypatch.setattr(raffle_service, '_notify_user_ticket', AsyncMock(return_value=None))

    db = _stub_db()
    db.get = AsyncMock(
        return_value=SimpleNamespace(
            amount_kopeks=-19900,  # 199 ₽ debit as stored by create_transaction
            description="Покупка тарифа 'Premium' на 30 дней",
            external_id=None,
            payment_method='balance',
        )
    )
    result = await raffle_service.issue_for_purchase(db, user_id=42, transaction_id=1001, tariff_id=3)
    assert result == [created]
    create_ticket.assert_awaited()


async def test_issue_skips_zero_amount_as_trial(raffle_enabled, active_campaign, monkeypatch):
    active_campaign.skip_trial_purchases = True
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'get_current_active_campaign',
        AsyncMock(return_value=active_campaign),
    )
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'list_tickets_by_campaign_tx',
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        raffle_service.raffle_crud,
        'count_tickets_for_user',
        AsyncMock(return_value=0),
    )
    create_ticket = AsyncMock()
    monkeypatch.setattr(raffle_service.raffle_crud, 'create_ticket', create_ticket)

    db = _stub_db()
    db.get = AsyncMock(
        return_value=SimpleNamespace(
            amount_kopeks=0,
            description='activation',
            external_id=None,
            payment_method='balance',
        )
    )
    result = await raffle_service.issue_for_purchase(db, user_id=42, transaction_id=2)
    assert result == []
    create_ticket.assert_not_called()
