"""Proxy integration tests.

Three critical scenarios:
1. Request under cap → proxied cleanly, usage persisted
2. Request that crosses cap mid-stream → synthetic message_stop injected, connection closed
3. Request on a different key (multi-key isolation) → cap from the correct key used
"""

# Tests are stubs — implementations added during W1 build.

import pytest


@pytest.mark.asyncio
async def test_health(client):  # type: ignore[no-untyped-def]
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_proxy_under_cap():
    """Proxied request under cap returns Anthropic response unchanged."""
    pytest.skip("implement in W1 with respx mock")


@pytest.mark.asyncio
async def test_proxy_cap_hit_mid_stream():
    """Request that crosses cap mid-stream receives synthetic message_stop."""
    pytest.skip("implement in W1 with respx mock")


@pytest.mark.asyncio
async def test_multi_key_isolation():
    """Key A's spend does not affect Key B's cap."""
    pytest.skip("implement in W1")
