from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "Voice Agent SaaS"
    debug: bool = True

    database_url: str = "postgresql+asyncpg://user:pass@db:5432/voiceagent"
    redis_url: str = "redis://redis:6379"

    # Internal URL used by backend
    livekit_url: str = "http://livekit:7880"

    # Browser connects here
    livekit_public_url: str = "ws://localhost:7890"

    livekit_api_key: str = "devkey"
    livekit_api_secret: str = "secret"

    groq_api_key: str = ""
    groq_model: str = "llama-3.1-8b-instant"

    # Speech-to-text (faster-whisper). First run downloads the model from
    # HuggingFace. "tiny" is the fastest but garbles short names in live calls
    # (observed: caller names transcribed as "This is our fault"); "base" is
    # ~4x slower but reliably captures names/phone digits, which the booking
    # flow depends on. int8 compute on CPU is several times faster than float32
    # with negligible accuracy loss on Whisper.
    stt_model: str = "base"
    stt_device: str = "auto"
    stt_compute_type: str = "int8"

    # Text-to-speech (Kokoro onnx). First run downloads ~300MB from GitHub.
    tts_voice: str = "af_heart"
    # Optional explicit paths to pre-downloaded Kokoro model/voices files.
    kokoro_model_path: str = ""
    kokoro_voices_path: str = ""

    # Pre-download/load the STT and TTS models in a background task at startup
    # so the first live call is not stalled by the first-run download.
    auto_warmup: bool = True

    # Keep the LLM path lightweight so responses start sooner. The LLM is only
    # used for free-form turns (small talk/thanks), so a slow/failed call can
    # fall back to a polite line without breaking the booking flow.
    llm_max_tokens: int = 256
    llm_temperature: float = 0.35
    llm_history_turns: int = 6
    # Groq free tier returns 429s that the SDK retries with long backoff
    # (observed ~18s). Keep this comfortably above the retry window so the
    # retry can complete instead of being cut off by our own timeout.
    llm_timeout_s: float = 45.0
    llm_max_retries: int = 2
    # Max tools executed in a single turn so a token-cap artifact cannot spin.
    llm_max_tool_calls: int = 4

    # Repair gate: ask the caller to repeat ONLY when ASR confidence (avg
    # logprob from faster-whisper, typically -1..0) is below this threshold AND
    # the utterance looks like noise. -2.0 keeps it effectively disabled unless
    # transcription is clearly garbage.
    asr_confidence_repair_threshold: float = -2.0

    # Live backchannels while the caller is still speaking (ChatGPT-voice style).
    # A short "uh huh" / "mhm" plays after the caller has been talking briefly,
    # rate-limited so it never chatters. Disable with BACKCHANNEL_ENABLED=false.
    backchannel_enabled: bool = True
    backchannel_delay_s: float = 1.15
    backchannel_min_interval_s: float = 8.0

    tenant_id: str = "00000000-0000-0000-0000-000000000001"

    # Clinic settings for date/time resolution
    clinic_timezone: str = "Asia/Dhaka"
    business_hours_start: int = 9
    business_hours_end: int = 17
    slot_duration_minutes: int = 60

    # Telephony (outbound Programmable Voice + Media Streams via Twilio REST).
    # Dialing is done through Twilio's REST API, and call audio is bridged over
    # a WebSocket Media Stream back into the same ConversationManager pipeline.
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    # Caller ID shown to the person being called (Twilio number, E.164)
    twilio_from_number: str = ""
    # Number that outbound calls always dial. Trial accounts can only call
    # verified numbers, so the lead's entered phone is stored for the booking
    # but the actual dial target is fixed to this verified number.
    twilio_verified_number: str = ""
    # Public base URL (ngrok) Twilio reaches us on, e.g. https://abc.ngrok-free.app.
    # Used to build the Media Stream wss:// URL and the status-callback URL.
    twilio_public_base_url: str = ""

    # Legacy LiveKit SIP path (kept for future LiveKit Cloud use). LiveKit
    # outbound trunk id (set after provisioning). If empty, the trunk is
    # auto-created from SIP_TRUNK_HOSTNAME/_USERNAME/_PASSWORD (local dev).
    sip_trunk_id: str = ""
    # Twilio SIP trunk domain, e.g. "abcdef.pstn.twilio.com"
    sip_trunk_hostname: str = ""
    sip_trunk_auth_username: str = ""
    sip_trunk_auth_password: str = ""
    # Caller ID shown to the person being called (your Twilio number, E.164)
    sip_from_number: str = ""
    sip_ringing_timeout: int = 45
    sip_max_call_duration: int = 900
    # Max seconds to wait for the agent participant to join before dialing
    sip_agent_join_timeout: int = 45
    # Outbound scheduler poll interval (seconds)
    outbound_poll_interval: int = 5
    # No-answer retry delay + attempt cap
    outbound_retry_seconds: int = 600
    outbound_max_attempts: int = 3
    # Run the outbound lead-caller loop at startup (disable for pure web demos)
    outbound_caller: bool = True

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }


settings = Settings()
