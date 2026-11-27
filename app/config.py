"""Global settings, read from environment variables (see .env.example)."""
import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Core
    database_url: str = "sqlite:///./speed_to_lead.db"
    public_base_url: str = "http://localhost:8000"  # used for Twilio signature checks and links
    admin_token: str = "change-me"  # protects the staff inbox

    # Live demo page at /demo (simulated texts and calendar, real LLM).
    # Each demo message spends LLM requests: on a public server set DEMO_TOKEN or turn it off.
    enable_demo: bool = True
    demo_token: str = ""  # if set, open the demo at /demo?token=...
    demo_max_actions_per_session: int = 30  # form + texts + calls per visitor conversation
    demo_daily_action_limit: int = 300  # across all visitors, protects the free LLM quota
    demo_max_sessions: int = 50  # visitors kept in memory at once
    demo_session_idle_minutes: int = 30

    # Which LLM to use: "gemini" (free tier for testing) or "anthropic"
    llm_provider: str = "gemini"

    # Gemini (Google AI Studio key)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    gemini_thinking_budget: int = 0  # gemini-2.5 models only: 0 = fastest
    gemini_thinking_level: str = "low"  # gemini-3+ models: minimal / low / medium / high

    # Anthropic
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"
    opener_deadline_seconds: float = 7.0  # fall back to a template opener after this
    reply_deadline_seconds: float = 25.0
    max_tool_rounds: int = 8
    max_agent_texts_per_day: int = 20  # per lead; beyond this the lead is escalated

    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    validate_twilio_signature: bool = True
    # Twilio's Advanced Opt-Out answers STOP/START/HELP itself. Leave True unless you turn it off.
    twilio_handles_opt_out: bool = True

    # Google Calendar: service-account JSON, base64-encoded
    google_service_account_json_b64: str = ""

    # Email alerts (optional; Slack is configured per client)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    alert_from_email: str = ""


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    # On Render, use the service's own public URL unless PUBLIC_BASE_URL was set explicitly.
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if render_url and "PUBLIC_BASE_URL" not in os.environ:
        settings.public_base_url = render_url
    return settings
