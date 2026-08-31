import pytest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from app.main import app
from app.config import settings

client = TestClient(app)

@pytest.mark.anyio
@patch("app.api.v1.twilio_ws.LeadService")
@patch("app.api.v1.twilio_ws.CallService")
@patch("app.api.v1.twilio_ws.async_session_factory")
async def test_twiml_inbound_dynamic_session_creation(mock_session_factory, mock_call_service_cls, mock_lead_service_cls):
    """Verify that an inbound call (missing leadId/sessionId query parameters)

    dynamically looks up/creates a lead, registers a new CallSession, and builds
    the TwiML response with the generated IDs.
    """
    # 1. Setup mock session and service instances
    mock_db = AsyncMock()
    mock_session_factory.return_value.__aenter__.return_value = mock_db

    mock_lead_service = MagicMock()
    mock_lead_service_cls.return_value = mock_lead_service

    mock_call_service = MagicMock()
    mock_call_service_cls.return_value = mock_call_service

    # Setup database mocks
    # For lead lookup query: return None (lead doesn't exist yet)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=None)
    mock_db.execute = AsyncMock(return_value=mock_result)

    # Mock dynamic lead creation
    mock_lead_id = uuid.uuid4()
    mock_lead = MagicMock()
    mock_lead.id = mock_lead_id
    mock_lead.phone = "+8801712345678"
    mock_lead.purpose = "Inbound call"
    mock_lead_service.create_lead = AsyncMock(return_value=mock_lead)

    # Mock call session creation
    mock_session_id = uuid.uuid4()
    mock_session = MagicMock()
    mock_session.id = mock_session_id
    mock_call_service.create_session = AsyncMock(return_value=mock_session)
    mock_call_service.start_session = AsyncMock()

    # 2. Trigger the /twiml endpoint as a POST request (simulating Twilio's webhook)
    # with empty leadId and sessionId, passing 'From' in form body
    response = client.post("/twiml", data={"From": "+8801712345678"})

    assert response.status_code == 200
    assert "text/xml" in response.headers["content-type"]
    
    # 3. Verify services were called to generate the session
    mock_lead_service.create_lead.assert_called_once_with(
        tenant_id=settings.tenant_id,
        customer_name="",
        phone="+8801712345678",
        purpose="Inbound call"
    )
    mock_call_service.create_session.assert_called_once()
    mock_call_service.start_session.assert_called_once_with(str(mock_session_id))

    # 4. Verify that generated sessionId and leadId are embedded in the TwiML output
    xml_content = response.text
    assert f"leadId={mock_lead_id}" in xml_content
    assert f"sessionId={mock_session_id}" in xml_content
    assert "<Gather" in xml_content
    assert "<Say" in xml_content
