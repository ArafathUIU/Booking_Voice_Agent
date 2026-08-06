import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import get_db
from app.services.lead_service import LeadService

router = APIRouter(prefix="/api/v1/leads", tags=["leads"])

_PHONE_RE = re.compile(r"^\+?\d{7,15}$")


class LeadCreateRequest(BaseModel):
    customer_name: str | None = Field(None, max_length=120)
    phone: str = Field(..., description="E.164-ish number, e.g. +8801712345678")
    purpose: str = Field("", max_length=1000)


class LeadResponse(BaseModel):
    id: str
    status: str
    customer_name: str | None = None
    phone: str
    purpose: str | None = None
    service: str | None = None


@router.post("", response_model=LeadResponse, status_code=201)
async def create_lead(
    body: LeadCreateRequest,
    db: AsyncSession = Depends(get_db),
):
    phone = body.phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if not _PHONE_RE.match(phone):
        raise HTTPException(status_code=422, detail="Invalid phone number")

    svc = LeadService(db)
    lead = await svc.create_lead(
        tenant_id=settings.tenant_id,
        customer_name=(body.customer_name or "").strip(),
        phone=phone,
        purpose=body.purpose.strip(),
    )
    await db.commit()
    return LeadResponse(
        id=str(lead.id),
        status=lead.status,
        customer_name=lead.customer_name,
        phone=lead.phone,
        purpose=lead.purpose,
        service=lead.service,
    )


@router.get("")
async def list_leads(
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    svc = LeadService(db)
    return await svc.list_leads(settings.tenant_id, limit=min(limit, 500))