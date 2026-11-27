from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from open_payments_sdk.models.common import AmountValue


class Settings(BaseSettings):
    TEST_SELLER_WALLET: Optional[str] = Field(default="")
    TEST_SELLER_KEY: Optional[str] = Field(default="", repr=False)
    TEST_SELLER_KEY_ID: Optional[str] = Field(default="")
    TEST_BUYER_WALLET: Optional[str] = Field(default="")
    TEST_BUYER_LOGIN: Optional[str] = Field(default="")
    TEST_BUYER_PASSWORD: Optional[str] = Field(default="", repr=False)
    TEST_PAYMENT_AMOUNT: Optional[AmountValue] = Field(default=None)
    TEST_PRODUCT_VALUE: str = Field(default="2500")
    TEST_PRODUCT_VOLUME: int = Field(default=2)
    TEST_REDIRECT_URI: str = Field(default="http://localhost/redirect/")
    model_config = SettingsConfigDict(
        case_sensitive=True,
        env_file=Path(__file__).with_name(".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )
