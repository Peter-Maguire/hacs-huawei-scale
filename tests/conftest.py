"""Shared fixtures."""
from __future__ import annotations

import pytest

from custom_components.huawei_scale3 import session


@pytest.fixture(autouse=True)
def fast_session_timers(monkeypatch):
    """Shrink every session timeout so simulated weigh-ins take milliseconds."""
    monkeypatch.setattr(session, "STATUS_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(session, "AUTH_TIMEOUT", 0.2)
    monkeypatch.setattr(session, "MANAGER_INFO_TIMEOUT", 0.2)
    monkeypatch.setattr(session, "USER_INFO_ACK_TIMEOUT", 0.2)
    monkeypatch.setattr(session, "HISTORY_RECORD_TIMEOUT", 0.1)
    monkeypatch.setattr(session, "FINAL_REQUEST_GRACE", 0.1)
    monkeypatch.setattr(session, "MEASUREMENT_TIMEOUT", 1.0)
    monkeypatch.setattr(session, "MAX_STATUS_POLLS", 5)


@pytest.fixture
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load ``custom_components`` in Home Assistant tests."""
    yield
