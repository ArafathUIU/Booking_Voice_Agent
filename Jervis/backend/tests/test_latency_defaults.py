import importlib


def test_latency_related_settings_are_leaner():
    config = importlib.import_module("app.config")
    settings = config.Settings(_env_file=None)

    # "base" is the minimum accuracy needed to reliably capture spoken names
    # for the booking flow; "tiny" garbles them in live calls.
    assert settings.stt_model == "base"
    # The LLM is now used only for free-form small talk, so a modest cap is fine.
    assert settings.llm_max_tokens <= 256
    assert settings.llm_temperature <= 0.35
    assert settings.llm_history_turns <= 6
