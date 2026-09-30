import uuid

from pydantic import BaseModel, EmailStr, Field, field_validator


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    display_name: str = Field(min_length=1, max_length=120)

    @field_validator("password")
    @classmethod
    def password_has_variety(cls, v: str) -> str:
        # Deliberately simple, not a full policy engine: length is the
        # dominant factor in practical password strength, so the floor above
        # (10 chars) does most of the work. This just rules out the
        # single-character-class extremes (all-digits, all-lowercase).
        classes = sum(
            [any(c.islower() for c in v), any(c.isupper() for c in v),
             any(c.isdigit() for c in v), any(not c.isalnum() for c in v)]
        )
        if classes < 2:
            raise ValueError("password must mix at least two of: lowercase, uppercase, digits, symbols")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str

    model_config = {"from_attributes": True}


class TokenPairOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserOut


class RegisterResponseOut(BaseModel):
    """What POST /auth/register returns -- deliberately NOT a TokenPairOut.
    A brand-new account can't sign in yet (email_verified starts False), so
    handing back working access/refresh tokens here would let the UI walk
    straight into the chat view for an address nobody has proven ownership
    of. Only POST /auth/verify-email issues real tokens now; that endpoint
    is what "clicking the confirmation link logs you straight in" means in
    this API."""

    user: UserOut
    message: str
    # Only populated while settings.local_dev_email_stub is True -- see
    # app/core/mailer.py's docstring. A real deployment leaves this null and
    # relies entirely on the email actually landing in the user's inbox.
    dev_verification_url: str | None = None


class VerifyEmailRequest(BaseModel):
    token: str


class RefreshRequest(BaseModel):
    refresh_token: str


class AccessTokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class LogoutRequest(BaseModel):
    refresh_token: str
