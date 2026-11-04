"""Application configuration, shared by direct and Uvicorn startup."""

import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Existing process settings take precedence; backend-local settings override root defaults.
load_dotenv(PROJECT_ROOT / "backend" / ".env")
load_dotenv(PROJECT_ROOT / ".env")

DEVELOPMENT_PROFILE = (
    "https://shopify.dev/ucp/agent-profiles/examples/"
    "2026-08-25/valid-with-capabilities.json"
)


def agent_profile_url():
    configured = os.getenv("UCP_AGENT_PROFILE_URL", "").strip()
    if configured:
        return configured
    if os.getenv("APP_ENV", "development").lower() == "development":
        return DEVELOPMENT_PROFILE
    return None
