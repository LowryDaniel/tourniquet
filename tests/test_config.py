"""Settings validation — fernet_key and secret_key must be sane at startup."""

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from tourniquet.config import Settings


def _base_kwargs(**overrides) -> dict:
    """Minimum-viable kwargs to construct Settings; callers override fields under test."""
    kwargs = {
        "database_url": "postgresql://test/test",
        "fernet_key": Fernet.generate_key().decode(),
        "secret_key": "x" * 32,
    }
    kwargs.update(overrides)
    return kwargs


def test_settings_constructs_with_valid_keys():
    s = Settings(**_base_kwargs())  # type: ignore[arg-type]
    assert s.fernet_key
    assert s.secret_key


def test_invalid_fernet_key_raises_validation_error():
    with pytest.raises(ValidationError) as exc:
        Settings(**_base_kwargs(fernet_key="not-base64"))  # type: ignore[arg-type]
    assert "FERNET_KEY" in str(exc.value)


def test_too_short_secret_key_raises_validation_error():
    with pytest.raises(ValidationError) as exc:
        Settings(**_base_kwargs(secret_key="x" * 31))  # type: ignore[arg-type]
    assert "SECRET_KEY" in str(exc.value)


def test_secret_key_at_threshold_is_accepted():
    s = Settings(**_base_kwargs(secret_key="x" * 32))  # type: ignore[arg-type]
    assert len(s.secret_key.encode()) == 32
