"""Shared test fixtures.

Tests use:
- A real Postgres database (DATABASE_URL from env, set in CI)
- respx for mocking Anthropic SSE responses
- FastAPI TestClient for HTTP-level tests
"""

import pytest
from fastapi.testclient import TestClient

from burnrate.main import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)
