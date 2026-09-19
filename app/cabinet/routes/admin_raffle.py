"""Admin raffle campaign management for cabinet."""

import asyncio
import csv
import io
from datetime import datetime
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from PIL import Image as PILImage
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud import raffle as raffle_crud
from app.database.crud.user import get_user_by_id, get_user_by_telegram_id
from app.database.models import RaffleCampaignStatus, RafflePrizeType, RaffleTicketSource, RaffleWinner, User
from app.services.news_media_service import (
    SavedMedia,
    detect_file_type,
    ensure_upload_dirs,
    save_image,
)
from app.services.raffle.service import (
    DRAW_ALGORITHM,
    _normalize_image_url,
    _normalize_prize_slots,
    _normalize_tickets_by_tariff,
    draw_winners,
    grant_tickets,
    max_winners_for_campaign,
    retry_award_winner,
    tickets_by_tariff_for_api,
)

from ..dependencies import get_cabinet_db, get_client_ip, get_current_cabinet_user, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/raffle', tags=['Cabinet Admin Raffle'])

_BYTES_PER_MB = 1024 * 1024
# Prize photos from phone gallery — keep modest for Mini App admin UX.
_MAX_RAFFLE_IMAGE_BYTES = 5 * _BYTES_PER_MB


class RaffleImageUploadResponse(BaseModel):
    """Public path/URL for a prize image stored under /uploads."""

    url: str
    thumbnail_url: str | None = None
    media_type: Literal['image'] = 'image'
    filename: str
    size_bytes: int
    width: int | None = None
    height: int | None = None


def _build_upload_url(_request: Request, relative_path: str) -> str:
    """Return a site-relative /uploads/... path.

    Absolute cabinet-host URLs break in Gorec Caddy setups: only /api/* is
    proxied to the bot, so https://cabinet/.../uploads/... hits the SPA and
    prize cards render blank. Relative paths are resolved by the cabinet via
    VITE_API_URL (/api/uploads/...) or via a Caddy /uploads handle.
    """
    rel = relative_path.lstrip('/')
    return f'/uploads/{rel}'


def _upload_response(request: Request, saved: SavedMedia) -> RaffleImageUploadResponse:
    thumbnail_url = _build_upload_url(request, saved.thumbnail_path) if saved.thumbnail_path else None
    return RaffleImageUploadResponse(
        url=_build_upload_url(request, saved.relative_path),
        thumbnail_url=thumbnail_url,
        filename=saved.filename,
        size_bytes=saved.size_bytes,
        width=saved.width,
        height=saved.height,
    )


async def require_raffle_writer(
    request: Request,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> User:
    """Allow prize image upload for admins who can create or edit raffles."""
    from app.services.permission_service import PermissionService

    try:
        client_ip = get_client_ip(request)
    except HTTPException:
        client_ip = 'unknown'
    user_agent = request.headers.get('user-agent', '')

    last_reason = 'missing permission'
    for perm in ('raffle:create', 'raffle:edit'):
        allowed, reason = await PermissionService.check_permission(
            db,
            user,
            perm,
            ip_address=client_ip,
        )
        if allowed:
            await PermissionService.log_action(
                db,
                user_id=user.id,
                action='raffle:upload',
                resource_type='raffle',
                status='success',
                ip_address=client_ip,
                user_agent=user_agent,
                request_method=request.method,
                request_path=str(request.url.path),
                details={'via': perm},
            )
            await db.commit()
            return user
        last_reason = reason or last_reason

    await PermissionService.log_action(
        db,
        user_id=user.id,
        action='raffle:upload',
        resource_type='raffle',
        status='denied',
        ip_address=client_ip,
        user_agent=user_agent,
        request_method=request.method,
        request_path=str(request.url.path),
        details={'reason': last_reason},
    )
    await db.commit()
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f'Permission denied: {last_reason}',
    )


class PrizeSlotInput(BaseModel):
    place: int = Field(..., ge=1, le=1000)
    prize_type: str = Field(RafflePrizeType.CUSTOM.value)
    prize_value: int | None = None
    prize_text: str | None = None
    image_url: str | None = None


class AdminRaffleCampaignItem(BaseModel):
    id: int
    name: str
    description: str | None = None
    status: str
    starts_at: datetime
    ends_at: datetime | None = None
    max_winners: int
    prize_type: str
    prize_value: int | None = None
    prize_text: str | None = None
    prize_slots: list[dict[str, Any]] | None = None
    tickets_per_purchase: int = 1
    tickets_by_tariff: dict[str, int] | None = None
    skip_trial_purchases: bool = True
    tickets: int = 0
    unique_users: int = 0
    winners: int = 0
    draw_seed: str | None = None
    draw_algorithm: str | None = None
    drawn_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AdminRaffleCampaignListResponse(BaseModel):
    enabled: bool
    campaigns: list[AdminRaffleCampaignItem]


class CreateRaffleCampaignRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    max_winners: int = Field(1, ge=1, le=1000)
    prize_type: str = Field(RafflePrizeType.CUSTOM.value)
    prize_value: int | None = None
    prize_text: str | None = None
    prize_slots: list[PrizeSlotInput] | None = None
    tickets_per_purchase: int = Field(1, ge=1, le=50)
    tickets_by_tariff: dict[str, int] | None = None
    skip_trial_purchases: bool = True
    starts_at: datetime | None = None
    ends_at: datetime | None = None


class UpdateRaffleCampaignRequest(BaseModel):
    """Patch campaign. Active: ends_at + prize_slots (+ name/description). Drawn: forbidden."""

    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = None
    ends_at: datetime | None = None
    clear_ends_at: bool = False
    prize_type: str | None = None
    prize_value: int | None = None
    prize_text: str | None = None
    prize_slots: list[PrizeSlotInput] | None = None
    tickets_per_purchase: int | None = Field(None, ge=1, le=50)
    tickets_by_tariff: dict[str, int] | None = None
    skip_trial_purchases: bool | None = None
    starts_at: datetime | None = None


class AdminRaffleWinnerItem(BaseModel):
    id: int
    campaign_id: int
    user_id: int
    telegram_id: int | None = None
    username: str | None = None
    first_name: str | None = None
    display_name: str | None = None
    ticket_id: int | None = None
    ticket_code: str | None = None
    place: int
    prize_type: str | None = None
    prize_value: int | None = None
    prize_text: str | None = None
    awarded: bool
    awarded_at: datetime | None = None
    created_at: datetime | None = None


class AdminRaffleDrawResponse(BaseModel):
    campaign_id: int
    status: str
    drawn_at: datetime | None = None
    draw_seed: str | None = None
    draw_algorithm: str | None = None
    winners: list[AdminRaffleWinnerItem]


class AdminRaffleCampaignDetailResponse(BaseModel):
    enabled: bool
    campaign: AdminRaffleCampaignItem
    winners: list[AdminRaffleWinnerItem]


def _slots_from_request(slots: list[PrizeSlotInput] | None) -> list[dict[str, Any]] | None:
    if slots is None:
        return None
    slots_raw = []
    for slot in slots:
        ptype = _validate_prize(slot.prize_type, slot.prize_value, slot.prize_text)
        try:
            image_url = _normalize_image_url(slot.image_url, strict=True)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        item: dict[str, Any] = {
            'place': slot.place,
            'prize_type': ptype,
            'prize_value': slot.prize_value,
            'prize_text': slot.prize_text,
        }
        if image_url is not None:
            item['image_url'] = image_url
        slots_raw.append(item)
    normalized = _normalize_prize_slots(slots_raw, strict_images=True)
    if not normalized:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Invalid prize_slots')
    return normalized


def _validate_prize(prize_type: str, prize_value: int | None, prize_text: str | None) -> str:
    prize_type = (prize_type or RafflePrizeType.CUSTOM.value).lower()
    if prize_type not in {e.value for e in RafflePrizeType}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Invalid prize_type')
    if prize_type == RafflePrizeType.CUSTOM.value:
        if not (prize_text or '').strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='prize_text required for custom prize',
            )
    elif prize_value is None or int(prize_value) <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='prize_value must be > 0')
    return prize_type


def _user_display(user: User | None, user_id: int) -> tuple[int | None, str | None, str | None, str]:
    if user is None:
        return None, None, None, f'user#{user_id}'
    telegram_id = getattr(user, 'telegram_id', None)
    username = getattr(user, 'username', None) or None
    first_name = getattr(user, 'first_name', None) or None
    if username:
        display = f'@{username}'
    elif first_name:
        display = first_name
    elif telegram_id:
        display = f'tg:{telegram_id}'
    else:
        display = f'user#{user_id}'
    return telegram_id, username, first_name, display


def _winner_item(winner: RaffleWinner) -> AdminRaffleWinnerItem:
    user = getattr(winner, 'user', None)
    telegram_id, username, first_name, display_name = _user_display(user, winner.user_id)
    return AdminRaffleWinnerItem(
        id=winner.id,
        campaign_id=winner.campaign_id,
        user_id=winner.user_id,
        telegram_id=telegram_id,
        username=username,
        first_name=first_name,
        display_name=display_name,
        ticket_id=winner.ticket_id,
        ticket_code=winner.ticket_code,
        place=winner.place,
        prize_type=winner.prize_type,
        prize_value=winner.prize_value,
        prize_text=winner.prize_text,
        awarded=bool(winner.awarded),
        awarded_at=winner.awarded_at,
        created_at=winner.created_at,
    )


def _campaign_item(campaign, stats: dict[str, int]) -> AdminRaffleCampaignItem:
    drawn_at = getattr(campaign, 'drawn_at', None)
    if drawn_at is None and campaign.status == RaffleCampaignStatus.DRAWN.value:
        drawn_at = getattr(campaign, 'updated_at', None)
    return AdminRaffleCampaignItem(
        id=campaign.id,
        name=campaign.name,
        description=campaign.description,
        status=campaign.status,
        starts_at=campaign.starts_at,
        ends_at=campaign.ends_at,
        max_winners=max_winners_for_campaign(campaign),
        prize_type=campaign.prize_type,
        prize_value=campaign.prize_value,
        prize_text=campaign.prize_text,
        prize_slots=_normalize_prize_slots(getattr(campaign, 'prize_slots', None)) or None,
        tickets_per_purchase=int(getattr(campaign, 'tickets_per_purchase', 1) or 1),
        tickets_by_tariff=tickets_by_tariff_for_api(getattr(campaign, 'tickets_by_tariff', None)),
        skip_trial_purchases=bool(getattr(campaign, 'skip_trial_purchases', True)),
        tickets=stats.get('tickets', 0),
        unique_users=stats.get('unique_users', 0),
        winners=stats.get('winners', 0),
        draw_seed=getattr(campaign, 'draw_seed', None),
        draw_algorithm=getattr(campaign, 'draw_algorithm', None),
        drawn_at=drawn_at,
        created_at=campaign.created_at,
        updated_at=getattr(campaign, 'updated_at', None),
    )


@router.get('/campaigns', response_model=AdminRaffleCampaignListResponse)
async def list_raffle_campaigns(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    admin: User = Depends(require_permission('raffle:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    campaigns = await raffle_crud.list_campaigns(db, limit=limit, offset=offset)
    items: list[AdminRaffleCampaignItem] = []
    for campaign in campaigns:
        stats = await raffle_crud.get_campaign_ticket_stats(db, campaign.id)
        items.append(_campaign_item(campaign, stats))
    return AdminRaffleCampaignListResponse(enabled=settings.is_raffle_enabled(), campaigns=items)


@router.get('/campaigns/{campaign_id}', response_model=AdminRaffleCampaignDetailResponse)
async def get_raffle_campaign(
    campaign_id: int,
    admin: User = Depends(require_permission('raffle:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    campaign = await raffle_crud.get_campaign_by_id(db, campaign_id)
    if not campaign:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Campaign not found')
    stats = await raffle_crud.get_campaign_ticket_stats(db, campaign.id)
    winners = await raffle_crud.list_winners(db, campaign.id)
    return AdminRaffleCampaignDetailResponse(
        enabled=settings.is_raffle_enabled(),
        campaign=_campaign_item(campaign, stats),
        winners=[_winner_item(w) for w in winners],
    )


@router.post('/campaigns', response_model=AdminRaffleCampaignItem, status_code=status.HTTP_201_CREATED)
async def create_raffle_campaign(
    request: CreateRaffleCampaignRequest,
    admin: User = Depends(require_permission('raffle:create')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    slots_raw = None
    if request.prize_slots:
        slots_raw = _slots_from_request(request.prize_slots)
        # Default campaign prize = 1st place for backward-compatible fields
        first = slots_raw[0]
        prize_type = first['prize_type']
        prize_value = first.get('prize_value')
        prize_text = first.get('prize_text')
        max_winners = len(slots_raw)
    else:
        prize_type = _validate_prize(request.prize_type, request.prize_value, request.prize_text)
        prize_value = request.prize_value
        prize_text = request.prize_text
        max_winners = request.max_winners

    campaign = await raffle_crud.create_campaign(
        db,
        name=request.name.strip(),
        description=request.description,
        status=RaffleCampaignStatus.DRAFT.value,
        starts_at=request.starts_at,
        ends_at=request.ends_at,
        max_winners=max_winners,
        prize_type=prize_type,
        prize_value=prize_value,
        prize_text=prize_text,
        prize_slots=slots_raw,
        tickets_per_purchase=request.tickets_per_purchase,
        tickets_by_tariff=_normalize_tickets_by_tariff(request.tickets_by_tariff),
        skip_trial_purchases=request.skip_trial_purchases,
    )
    logger.info('Admin created raffle campaign', campaign_id=campaign.id, admin_id=admin.id)
    return _campaign_item(campaign, {'tickets': 0, 'unique_users': 0, 'winners': 0})


@router.post('/campaigns/{campaign_id}/activate', response_model=AdminRaffleCampaignItem)
async def activate_raffle_campaign(
    campaign_id: int,
    admin: User = Depends(require_permission('raffle:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    if not settings.is_raffle_enabled():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='RAFFLE_ENABLED is false')
    campaign = await raffle_crud.get_campaign_by_id(db, campaign_id)
    if not campaign:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Campaign not found')
    campaign = await raffle_crud.set_campaign_status(db, campaign, RaffleCampaignStatus.ACTIVE.value)
    stats = await raffle_crud.get_campaign_ticket_stats(db, campaign.id)
    logger.info('Admin activated raffle campaign', campaign_id=campaign.id, admin_id=admin.id)
    return _campaign_item(campaign, stats)


@router.post('/campaigns/{campaign_id}/close', response_model=AdminRaffleCampaignItem)
async def close_raffle_campaign(
    campaign_id: int,
    admin: User = Depends(require_permission('raffle:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    campaign = await raffle_crud.get_campaign_by_id(db, campaign_id)
    if not campaign:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Campaign not found')
    campaign = await raffle_crud.set_campaign_status(db, campaign, RaffleCampaignStatus.CLOSED.value)
    stats = await raffle_crud.get_campaign_ticket_stats(db, campaign.id)
    logger.info('Admin closed raffle campaign', campaign_id=campaign.id, admin_id=admin.id)
    return _campaign_item(campaign, stats)


@router.post('/campaigns/{campaign_id}/draw', response_model=AdminRaffleDrawResponse)
async def draw_raffle_campaign(
    campaign_id: int,
    admin: User = Depends(require_permission('raffle:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    campaign = await raffle_crud.get_campaign_by_id(db, campaign_id)
    if not campaign:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Campaign not found')
    try:
        await draw_winners(db, campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    campaign = await raffle_crud.get_campaign_by_id(db, campaign_id)
    winners = await raffle_crud.list_winners(db, campaign_id)
    logger.info(
        'Admin drew raffle campaign',
        campaign_id=campaign_id,
        winners=len(winners),
        admin_id=admin.id,
    )
    return AdminRaffleDrawResponse(
        campaign_id=campaign_id,
        status=campaign.status if campaign else RaffleCampaignStatus.DRAWN.value,
        drawn_at=getattr(campaign, 'drawn_at', None) if campaign else None,
        draw_seed=getattr(campaign, 'draw_seed', None) if campaign else None,
        draw_algorithm=(getattr(campaign, 'draw_algorithm', None) or DRAW_ALGORITHM) if campaign else DRAW_ALGORITHM,
        winners=[_winner_item(w) for w in winners],
    )


@router.post(
    '/campaigns/{campaign_id}/winners/{winner_id}/award',
    response_model=AdminRaffleWinnerItem,
)
async def award_raffle_winner(
    campaign_id: int,
    winner_id: int,
    admin: User = Depends(require_permission('raffle:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Retry automatic prize award for a winner with awarded=false."""
    winner = await raffle_crud.get_winner_by_id(db, winner_id)
    if not winner or winner.campaign_id != campaign_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Winner not found')
    try:
        winner = await retry_award_winner(db, winner_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    # Reload with user for display
    winner = await raffle_crud.get_winner_by_id(db, winner_id)
    logger.info(
        'Admin retried raffle award',
        campaign_id=campaign_id,
        winner_id=winner_id,
        admin_id=admin.id,
    )
    return _winner_item(winner)


@router.patch('/campaigns/{campaign_id}', response_model=AdminRaffleCampaignItem)
async def update_raffle_campaign(
    campaign_id: int,
    request: UpdateRaffleCampaignRequest,
    admin: User = Depends(require_permission('raffle:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Edit campaign. Active/draft: ends_at and prize places; drawn — forbidden; closed — limited."""
    campaign = await raffle_crud.get_campaign_by_id(db, campaign_id)
    if not campaign:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Campaign not found')

    status_value = (campaign.status or '').lower()
    if status_value == RaffleCampaignStatus.DRAWN.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Cannot edit a drawn campaign',
        )

    # Active: allow ends_at + prizes (+ cosmetic name/description). No starts_at / ticket rules.
    # Closed: allow cosmetic + ends_at + prizes (re-open via activate separately). No auto-draw.
    # Draft: full editable set.
    provided = request.model_fields_set

    slots_raw = ...
    if 'prize_slots' in provided:
        if request.prize_slots is None:
            slots_raw = None
        else:
            slots_raw = _slots_from_request(request.prize_slots)

    prize_type = None
    prize_value = ...
    prize_text = ...
    max_winners = None
    if slots_raw is not ... and slots_raw is not None:
        first = slots_raw[0]
        prize_type = first['prize_type']
        prize_value = first.get('prize_value')
        prize_text = first.get('prize_text')
        max_winners = len(slots_raw)
    elif status_value == RaffleCampaignStatus.DRAFT.value:
        if 'prize_type' in provided or 'prize_value' in provided or 'prize_text' in provided:
            ptype = request.prize_type if 'prize_type' in provided else campaign.prize_type
            pval = request.prize_value if 'prize_value' in provided else campaign.prize_value
            ptext = request.prize_text if 'prize_text' in provided else campaign.prize_text
            prize_type = _validate_prize(ptype or RafflePrizeType.CUSTOM.value, pval, ptext)
            prize_value = pval if 'prize_value' in provided else ...
            prize_text = ptext if 'prize_text' in provided else ...

    ends_at = ...
    if request.clear_ends_at:
        ends_at = None
    elif 'ends_at' in provided:
        ends_at = request.ends_at

    name = request.name.strip() if request.name is not None else None
    description = ...
    if 'description' in provided:
        description = request.description

    starts_at = ...
    tickets_per_purchase = None
    tickets_by_tariff = ...
    skip_trial = None

    if status_value == RaffleCampaignStatus.DRAFT.value:
        if 'starts_at' in provided and request.starts_at is not None:
            starts_at = request.starts_at
        if request.tickets_per_purchase is not None:
            tickets_per_purchase = request.tickets_per_purchase
        if 'tickets_by_tariff' in provided:
            tickets_by_tariff = _normalize_tickets_by_tariff(request.tickets_by_tariff)
        if request.skip_trial_purchases is not None:
            skip_trial = request.skip_trial_purchases
    elif status_value in {
        RaffleCampaignStatus.ACTIVE.value,
        RaffleCampaignStatus.CLOSED.value,
    }:
        # Disallow changing ticket issuance rules on live/closed campaigns
        if any(
            k in provided for k in ('tickets_per_purchase', 'tickets_by_tariff', 'skip_trial_purchases', 'starts_at')
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Cannot change ticket rules or starts_at on active/closed campaigns',
            )
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Cannot edit campaign in this status')

    campaign = await raffle_crud.update_campaign(
        db,
        campaign,
        name=name,
        description=description,
        starts_at=starts_at,
        ends_at=ends_at,
        max_winners=max_winners,
        prize_type=prize_type,
        prize_value=prize_value,
        prize_text=prize_text,
        prize_slots=slots_raw,
        tickets_per_purchase=tickets_per_purchase,
        tickets_by_tariff=tickets_by_tariff,
        skip_trial_purchases=skip_trial,
    )
    stats = await raffle_crud.get_campaign_ticket_stats(db, campaign.id)
    logger.info('Admin updated raffle campaign', campaign_id=campaign.id, admin_id=admin.id)
    return _campaign_item(campaign, stats)


@router.post(
    '/upload',
    response_model=RaffleImageUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_raffle_prize_image(
    request: Request,
    file: UploadFile = File(...),
    admin: User = Depends(require_raffle_writer),
) -> RaffleImageUploadResponse:
    """Upload a JPEG/PNG/WebP prize photo (max 5 MB) into the public /uploads tree."""
    absolute_max_bytes = _MAX_RAFFLE_IMAGE_BYTES + 1
    data = await file.read(absolute_max_bytes)
    await file.close()
    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Empty file',
        )
    if len(data) >= absolute_max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail='File too large. Maximum size: 5 MB',
        )

    try:
        media_type, _ext = detect_file_type(data)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail='Unsupported file type. Allowed: JPEG, PNG, WebP',
        ) from None

    if media_type != 'image':
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail='Only images are allowed for raffle prizes',
        )

    upload_path = settings.get_media_upload_path()
    await asyncio.to_thread(ensure_upload_dirs, upload_path)

    try:
        saved = await save_image(
            data,
            upload_path,
            max_dim=settings.MEDIA_IMAGE_MAX_DIMENSION,
            quality=settings.MEDIA_JPEG_QUALITY,
        )
    except (ValueError, OSError, PILImage.DecompressionBombError) as exc:
        logger.warning('Failed to save raffle prize image', error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Failed to process uploaded file',
        ) from None

    logger.info(
        'Raffle prize image uploaded',
        filename=saved.filename,
        size_bytes=saved.size_bytes,
        admin_id=admin.id,
    )
    return _upload_response(request, saved)


@router.delete('/campaigns/{campaign_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_raffle_campaign(
    campaign_id: int,
    force: bool = Query(False, description='Allow delete of active campaign'),
    admin: User = Depends(require_permission('raffle:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Delete finished/closed/drawn (or draft) campaigns. Active requires force=true."""
    campaign = await raffle_crud.get_campaign_by_id(db, campaign_id)
    if not campaign:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Campaign not found')

    status_value = (campaign.status or '').lower()
    if status_value == RaffleCampaignStatus.ACTIVE.value and not force:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Cannot delete an active campaign without force=true',
        )

    await raffle_crud.delete_campaign(db, campaign)
    logger.info(
        'Admin deleted raffle campaign',
        campaign_id=campaign_id,
        status=status_value,
        force=force,
        admin_id=admin.id,
    )


class GrantRaffleTicketsRequest(BaseModel):
    user_id: int | None = Field(None, ge=1)
    telegram_id: int | None = Field(None, ge=1)
    count: int = Field(..., ge=1, le=50)
    note: str | None = Field(None, max_length=100)


class GrantRaffleTicketsResponse(BaseModel):
    campaign_id: int
    user_id: int
    tickets_issued: int
    ticket_codes: list[str]


@router.post(
    '/campaigns/{campaign_id}/grant-tickets',
    response_model=GrantRaffleTicketsResponse,
    status_code=status.HTTP_201_CREATED,
)
async def grant_raffle_tickets(
    campaign_id: int,
    request: GrantRaffleTicketsRequest,
    admin: User = Depends(require_permission('raffle:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Grant N tickets to a user on an active campaign (admin promo)."""
    if not settings.is_raffle_enabled():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Raffle disabled')
    if not request.user_id and not request.telegram_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='user_id or telegram_id required')

    campaign = await raffle_crud.get_campaign_by_id(db, campaign_id)
    if not campaign:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Campaign not found')
    if campaign.status != RaffleCampaignStatus.ACTIVE.value:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Campaign is not active')

    user = None
    if request.user_id:
        user = await get_user_by_id(db, request.user_id)
    elif request.telegram_id:
        user = await get_user_by_telegram_id(db, request.telegram_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='User not found')

    note = (request.note or '').strip() or f'admin:{admin.id}'
    source_ref = f'admin:{admin.id}:{note}'[:128]
    try:
        tickets = await grant_tickets(
            db,
            user.id,
            request.count,
            source=RaffleTicketSource.ADMIN,
            source_ref=source_ref,
            campaign_id=campaign_id,
            notify=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    logger.info(
        'Admin granted raffle tickets',
        campaign_id=campaign_id,
        user_id=user.id,
        count=len(tickets),
        admin_id=admin.id,
    )
    return GrantRaffleTicketsResponse(
        campaign_id=campaign_id,
        user_id=user.id,
        tickets_issued=len(tickets),
        ticket_codes=[t.ticket_code for t in tickets],
    )


def _csv_stream(rows: list[list[Any]], filename: str) -> StreamingResponse:
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in rows:
        writer.writerow(row)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


@router.get('/campaigns/{campaign_id}/export/tickets.csv')
async def export_raffle_tickets_csv(
    campaign_id: int,
    admin: User = Depends(require_permission('raffle:view')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    campaign = await raffle_crud.get_campaign_by_id(db, campaign_id)
    if not campaign:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Campaign not found')
    tickets = await raffle_crud.list_tickets_for_campaign(db, campaign_id)
    rows: list[list[Any]] = [
        [
            'ticket_id',
            'ticket_code',
            'user_id',
            'telegram_id',
            'username',
            'source',
            'source_ref',
            'source_transaction_id',
            'tariff_id',
            'ticket_index',
            'created_at',
        ]
    ]
    for t in tickets:
        user = await get_user_by_id(db, t.user_id)
        rows.append(
            [
                t.id,
                t.ticket_code,
                t.user_id,
                getattr(user, 'telegram_id', None) if user else None,
                getattr(user, 'username', None) if user else None,
                getattr(t, 'source', None) or RaffleTicketSource.PURCHASE,
                getattr(t, 'source_ref', None),
                t.source_transaction_id,
                t.tariff_id,
                t.ticket_index,
                t.created_at.isoformat() if t.created_at else '',
            ]
        )
    return _csv_stream(rows, f'raffle_{campaign_id}_tickets.csv')


@router.get('/campaigns/{campaign_id}/export/winners.csv')
async def export_raffle_winners_csv(
    campaign_id: int,
    admin: User = Depends(require_permission('raffle:view')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    campaign = await raffle_crud.get_campaign_by_id(db, campaign_id)
    if not campaign:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Campaign not found')
    if campaign.status != RaffleCampaignStatus.DRAWN.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='CSV winners available after draw',
        )
    winners = await raffle_crud.list_winners(db, campaign_id)
    rows: list[list[Any]] = [
        [
            'place',
            'winner_id',
            'user_id',
            'telegram_id',
            'username',
            'ticket_code',
            'prize_type',
            'prize_value',
            'prize_text',
            'awarded',
            'awarded_at',
            'draw_seed',
            'draw_algorithm',
            'drawn_at',
        ]
    ]
    for w in winners:
        user = w.user or await get_user_by_id(db, w.user_id)
        rows.append(
            [
                w.place,
                w.id,
                w.user_id,
                getattr(user, 'telegram_id', None) if user else None,
                getattr(user, 'username', None) if user else None,
                w.ticket_code,
                w.prize_type,
                w.prize_value,
                w.prize_text,
                w.awarded,
                w.awarded_at.isoformat() if w.awarded_at else '',
                getattr(campaign, 'draw_seed', None),
                getattr(campaign, 'draw_algorithm', None),
                campaign.drawn_at.isoformat() if getattr(campaign, 'drawn_at', None) else '',
            ]
        )
    return _csv_stream(rows, f'raffle_{campaign_id}_winners.csv')
