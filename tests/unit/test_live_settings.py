"""Live configuration diagnostics with synthetic credentials and no live sources."""

import os
import traceback

import pytest
from pydantic import ValidationError

pytest.importorskip(
    "pydantic_settings",
    reason="Install optional live test dependencies: poetry install --with live",
)
from tests.config import Settings

PRIVATE_KEY = "SYNTHETIC-private-key-DO-NOT-PRINT"
PASSWORD = "SYNTHETIC-password-DO-NOT-PRINT"


@pytest.fixture(autouse=True)
def isolated_live_sources(monkeypatch):
    # Replace only Python's mapping temporarily; leave the real process
    # environment untouched and never load the repository's live .env file.
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(
        Settings, "model_config", {**Settings.model_config, "env_file": None}
    )


@pytest.mark.parametrize("render", [str, repr])
def test_settings_display_omits_private_credentials(render):
    settings = Settings(
        TEST_SELLER_KEY=PRIVATE_KEY,
        TEST_BUYER_PASSWORD=PASSWORD,
        TEST_SELLER_KEY_ID="public-test-key-id",
    )
    displayed = render(settings)
    assert PRIVATE_KEY not in displayed and PASSWORD not in displayed
    assert "public-test-key-id" in displayed


def test_explicit_credential_access_and_serialization_remain_available():
    settings = Settings(TEST_SELLER_KEY=PRIVATE_KEY, TEST_BUYER_PASSWORD=PASSWORD)
    assert settings.TEST_SELLER_KEY == PRIVATE_KEY
    assert settings.TEST_BUYER_PASSWORD == PASSWORD
    assert settings.model_dump()["TEST_SELLER_KEY"] == PRIVATE_KEY
    assert settings.model_dump()["TEST_BUYER_PASSWORD"] == PASSWORD


@pytest.mark.parametrize(
    "field, value",
    [("TEST_SELLER_KEY", PRIVATE_KEY), ("TEST_BUYER_PASSWORD", PASSWORD)],
)
def test_credential_validation_messages_hide_input_values(field, value):
    with pytest.raises(ValidationError) as caught:
        Settings(**{field: {"secret": value}})
    error = caught.value
    assert value not in str(error) + repr(error) + repr(error.args)
    assert value not in "".join(traceback.format_exception(error))
    assert error.errors(include_input=False)[0]["loc"] == (field,)


def test_synthetic_environment_credentials_load_without_being_displayed(monkeypatch):
    monkeypatch.setenv("TEST_SELLER_KEY", PRIVATE_KEY)
    monkeypatch.setenv("TEST_BUYER_PASSWORD", PASSWORD)
    settings = Settings()
    assert (
        settings.TEST_SELLER_KEY == PRIVATE_KEY
        and settings.TEST_BUYER_PASSWORD == PASSWORD
    )
    assert PRIVATE_KEY not in repr(settings) and PASSWORD not in repr(settings)


def test_explicit_synthetic_env_file_preserves_existing_configuration(tmp_path):
    env_file = tmp_path / "synthetic.env"
    env_file.write_text(
        f"TEST_SELLER_KEY={PRIVATE_KEY}\nTEST_BUYER_PASSWORD={PASSWORD}\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=env_file)
    assert (
        settings.TEST_SELLER_KEY == PRIVATE_KEY
        and settings.TEST_BUYER_PASSWORD == PASSWORD
    )
    assert PRIVATE_KEY not in str(settings) and PASSWORD not in str(settings)


def test_other_validation_errors_also_omit_raw_input():
    value = "SYNTHETIC-invalid-input-DO-NOT-PRINT"
    with pytest.raises(ValidationError) as caught:
        Settings(TEST_PRODUCT_VOLUME=value)
    assert value not in str(caught.value) + repr(caught.value)


@pytest.mark.parametrize("value", ["0", "1000", "0001000", "18446744073709551615"])
def test_payment_amount_setting_uses_the_shared_amount_string_contract(value):
    assert Settings(TEST_PAYMENT_AMOUNT=value).TEST_PAYMENT_AMOUNT == value


@pytest.mark.parametrize(
    "value", [True, 1, "-1", "1.5", "\u0661", "18446744073709551616", ""]
)
def test_payment_amount_setting_rejects_non_protocol_values(value):
    with pytest.raises(ValidationError):
        Settings(TEST_PAYMENT_AMOUNT=value)
