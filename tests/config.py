from pydantic import Field
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    TEST_SELLER_WALLET: Optional[str] = Field(default="")
    TEST_SELLER_KEY: Optional[str] = Field(default="")
    TEST_SELLER_KEY_ID: Optional[str] = Field(default="")
    TEST_BUYER_WALLET: Optional[str] = Field(default="")
    TEST_BUYER_LOGIN: Optional[str] = Field(default="")
    TEST_BUYER_PASSWORD: Optional[str] = Field(default="")
    TEST_PRODUCT_VALUE: str = Field(default="2500")
    TEST_PRODUCT_VOLUME: int = Field(default=2)
    TEST_REDIRECT_URI: str = Field(default="http://localhost/redirect/")
    model_config = SettingsConfigDict(case_sensitive=True, env_file="./tests/.env", env_file_encoding="utf-8")


settings = Settings()
