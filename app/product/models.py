from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, field_validator

from app.auth.models import Credentials

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class ProfileInput(Input):
    email: EmailStr = Field(max_length=254, repr=False)
    nickname: str = Field(min_length=1, max_length=100)

    @field_validator("email")
    @classmethod
    def email_text(cls, value):
        return Credentials.normalize_email(value)

    @field_validator("nickname")
    @classmethod
    def nickname_text(cls, value):
        value = value.strip()
        if not value or not value.isprintable():
            raise ValueError("nickname must be printable")
        return value


class SubscriptionInput(Input):
    topics: list[Text] = Field(min_length=1, max_length=20)
    keywords: list[Text] = Field(default_factory=list, max_length=100)
    exclude_keywords: list[Text] = Field(default_factory=list, max_length=100)
    max_items: int = Field(default=5, ge=1, le=5)
    enabled: bool = True
    push_time: str = Field(default="09:00", pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")

    @field_validator("topics", "keywords", "exclude_keywords")
    @classmethod
    def unique_printable(cls, values):
        if any(not value.isprintable() for value in values):
            raise ValueError("preferences must be printable")
        if len({value.casefold() for value in values}) != len(values):
            raise ValueError("preferences must be unique")
        return values


class EmptyInput(Input):
    pass


class ProductError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)
