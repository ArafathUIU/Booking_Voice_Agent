import json
from datetime import datetime, timedelta

from app.config import settings


def _business_hours(date: datetime) -> list[tuple[datetime, datetime]]:
    """Generate business hour slots for a given date."""
    start_hour = settings.business_hours_start
    end_hour = settings.business_hours_end
    slot_duration = timedelta(minutes=settings.slot_duration_minutes)
    slots = []
    current = datetime(
        date.year, date.month, date.day, start_hour, 0, tzinfo=date.tzinfo
    )
    while current + slot_duration <= datetime(
        date.year, date.month, date.day, end_hour, 0, tzinfo=date.tzinfo
    ):
        slots.append((current, current + slot_duration))
        current += slot_duration
    return slots


def _spoken_time(dt: datetime) -> str:
    """Convert a datetime into speech-friendly phrase."""
    hour = dt.hour
    minute = dt.minute
    suffix = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    if minute == 0:
        return f"{h12} {suffix}"
    return f"{h12}:{minute:02d} {suffix}"


def _is_slot_available(start_dt: datetime, end_dt: datetime) -> bool:
    """Check if a slot is not already booked (mock for now, will use Google Calendar)."""
    for held_start, held_end, _ in HELD_SLOTS:
        if start_dt < held_end and end_dt > held_start:
            return False
    for booked_start, booked_end, _ in BOOKED_APPOINTMENTS:
        if start_dt < booked_end and end_dt > booked_start:
            return False
    return True


HELD_SLOTS: list[tuple[datetime, datetime, str]] = []
BOOKED_APPOINTMENTS: list[tuple[datetime, datetime, str]] = []


async def check_availability(
    date: str, service_name: str = None, resolver=None
) -> list[dict]:
    """Return available slots for a given date."""
    if resolver is None:
        from app.agents.conversation_state import ClinicDateTimeResolver

        resolver = ClinicDateTimeResolver()

    date_dt = resolver.resolve_date(date)
    if date_dt is None:
        date_dt = resolver._today_start()

    business_slots = _business_hours(date_dt)
    available = []
    for start_dt, end_dt in business_slots:
        if _is_slot_available(start_dt, end_dt):
            available.append(
                {
                    "time": start_dt.strftime("%H:%M"),
                    "start_dt": start_dt,
                    "end_dt": end_dt,
                    "available": True,
                    "spoken_time": _spoken_time(start_dt),
                }
            )
    return available


async def hold_slot(
    slot_time: str, service_name: str = None, resolver=None
) -> dict:
    """Tentatively hold a time slot for 5 minutes."""
    if resolver is None:
        from app.agents.conversation_state import ClinicDateTimeResolver

        resolver = ClinicDateTimeResolver()

    # Try to parse slot_time as a datetime string first
    start_dt = resolver.resolve_date(slot_time) if resolver.resolve_date(slot_time) else None
    if start_dt is None:
        # Try as a time-only string on today
        time_obj = resolver.resolve_time(slot_time)
        if time_obj:
            today = resolver._today_start()
            start_dt = resolver.combine(today, time_obj)
            end_dt = start_dt + timedelta(minutes=settings.slot_duration_minutes)
        else:
            return {"held": False, "message": "Sorry, I could not parse that time."}
    else:
        end_dt = start_dt + timedelta(minutes=settings.slot_duration_minutes)

    if not _is_slot_available(start_dt, end_dt):
        return {
            "held": False,
            "message": "Sorry, that slot is no longer available. Let me check others.",
        }

    HELD_SLOTS.append((start_dt, end_dt, service_name or "appointment"))
    return {
        "held": True,
        "slot_time": start_dt.strftime("%H:%M"),
        "spoken_time": _spoken_time(start_dt),
        "start_dt": start_dt,
        "end_dt": end_dt,
        "expires_in_seconds": 300,
        "message": f"The {_spoken_time(start_dt)} slot is held for five minutes.",
    }


async def book_appointment(
    customer_name: str,
    customer_phone: str,
    notes: str = None,
    start_dt: datetime = None,
    end_dt: datetime = None,
) -> dict:
    """Confirm and book the appointment."""
    if start_dt is None:
        now = datetime.now(settings.clinic_timezone)
        start_dt = now + timedelta(hours=1)
        end_dt = start_dt + timedelta(minutes=settings.slot_duration_minutes)

    if isinstance(start_dt, str):
        start_dt = datetime.fromisoformat(start_dt)
    if isinstance(end_dt, str):
        end_dt = datetime.fromisoformat(end_dt)
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=settings.clinic_timezone)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=settings.clinic_timezone)

    booking_id = f"booking-{len(BOOKED_APPOINTMENTS) + 1}"
    BOOKED_APPOINTMENTS.append((start_dt, end_dt, booking_id))

    # Remove from held slots if present
    global HELD_SLOTS
    HELD_SLOTS = [
        (s, e, n) for s, e, n in HELD_SLOTS if not (start_dt >= s and start_dt < e)
    ]

    return {
        "booking_id": booking_id,
        "status": "confirmed",
        "customer_name": customer_name,
        "start_dt": start_dt,
        "end_dt": end_dt,
        "spoken_time": _spoken_time(start_dt),
        "spoken_date": start_dt.strftime("%A, %B %d"),
        "message": f"Appointment confirmed for {customer_name}.",
    }


TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "check_availability",
            "description": "Check available appointment slots for a date. Use when the caller asks about times or wants to book.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "ISO date, e.g. 2026-07-30"},
                    "service_name": {"type": "string"},
                },
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hold_slot",
            "description": "Tentatively hold a chosen time slot for the caller before final confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "slot_time": {"type": "string", "description": "Time or ISO datetime the caller chose"},
                    "service_name": {"type": "string"},
                },
                "required": ["slot_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": "Confirm and book the held appointment after the caller agrees.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_name": {"type": "string"},
                    "customer_phone": {"type": "string"},
                    "notes": {"type": "string"},
                    "start_dt": {"type": "string", "description": "ISO datetime of the appointment"},
                    "end_dt": {"type": "string", "description": "ISO datetime of appointment end"},
                },
                "required": ["customer_name", "customer_phone"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_intent_context",
            "description": "Update conversation memory (name, service, times, confirmation flag).",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "enum": [
                            "current_intent",
                            "mentioned_slots",
                            "preferred_service",
                            "customer_name",
                            "pending_confirmation",
                        ],
                    },
                    "value": {"type": "string"},
                },
                "required": ["key", "value"],
            },
        },
    },
]


async def execute_tool(name: str, args: dict) -> str:
    """Return JSON for the model, with speech-friendly fields where useful."""
    if name == "check_availability":
        result = await check_availability(**args)
        spoken = [_spoken_time(s["start_dt"]) if isinstance(s["start_dt"], datetime) else _spoken_time(s["time"]) for s in result]
        top = spoken[:3]
        return json.dumps(
            {
                "available_count": len(spoken),
                "spoken_times": top,
                "more_available": len(spoken) > len(top),
                "hint": "Offer at most two or three times, then ask if they want more.",
            }
        )
    elif name == "hold_slot":
        result = await hold_slot(**args)
        return json.dumps(result, default=str)
    elif name == "book_appointment":
        result = await book_appointment(**args)
        return json.dumps(result, default=str)
    elif name == "update_intent_context":
        return json.dumps({"updated": True, "key": args.get("key"), "value": args.get("value")})
    return json.dumps({"error": f"Unknown tool: {name}"})
