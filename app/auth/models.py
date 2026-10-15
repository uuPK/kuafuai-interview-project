from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator

from app.storage.models import StoredModel


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, strict=True)
    email: EmailStr = Field(max_length=254, repr=False)
    password: SecretStr = Field(min_length=12, max_length=128, repr=False, exclude=True)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value):
        if not value.isascii():
            raise ValueError("email must use ASCII")
        return value.casefold()


class Registration(Credentials):
    nickname: str = Field(min_length=1, max_length=100)

    @field_validator("nickname")
    @classmethod
    def nickname_text(cls, value):
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("nickname must be printable")
        return value


class User(StoredModel):
    id: str
    email: str = Field(repr=False)
    nickname: str
    created_at: str


class AuthError(Exception):
    def __init__(self, code, status=401, *, retry_after=None):
        self.code, self.status, self.retry_after = code, status, retry_after
        super().__init__(code)
