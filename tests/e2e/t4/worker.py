"""Real platform factory solely for testing two pre-started uvicorn workers."""
from agentpal.api import create_app
from agentpal.config import get_settings
from agentpal.platform.wiring import build_platform


def factory():
    settings = get_settings()
    deps = build_platform(settings=settings)
    return create_app(service=deps.risk_service, settings=settings, platform=deps)
