from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.deps import get_unscoped_session
from app.core.mailer import send_verification_email
from app.core.security import (
    create_access_token,
    generate_email_verification_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.db.models.auth import EmailVerificationToken, RefreshToken
from app.db.models.user import User
from app.schemas.auth import (
    AccessTokenOut,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    RegisterResponseOut,
    TokenPairOut,
    UserOut,
    VerifyEmailRequest,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


async def _issue_token_pair(db: AsyncSession, user: User) -> TokenPairOut:
    settings = get_settings()
    raw_refresh, refresh_hash = generate_refresh_token()
    now = datetime.now(timezone.utc)
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=refresh_hash,
            issued_at=now,
            expires_at=now + timedelta(days=settings.refresh_token_ttl_days),
        )
    )
    await db.flush()

    return TokenPairOut(
        access_token=create_access_token(user.id),
        refresh_token=raw_refresh,
        user=UserOut.model_validate(user),
    )


@router.post("/register", response_model=RegisterResponseOut, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest, request: Request, db: AsyncSession = Depends(get_unscoped_session)
) -> RegisterResponseOut:
    email = payload.email.lower()
    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name,
        # Explicit, not relying on the column's own default: the column
        # defaults to True purely to grandfather rows that existed before
        # this feature shipped (see migration 0002) -- every genuinely new
        # signup goes through here and starts out unverified regardless of
        # what the column default says.
        email_verified=False,
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:
        # This does confirm the email is registered -- but only to whoever
        # already knows it well enough to type it into a registration form,
        # which is a much narrower disclosure than the login endpoint's
        # "wrong password" vs "no such account" distinction would be at
        # scale. Telling someone who's already registered "try signing in
        # instead" is standard, expected UX; it's not the enumeration risk
        # login deliberately avoids below.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "An account with this email already exists. Try signing in instead.",
        )

    settings = get_settings()
    raw_token, token_hash = generate_email_verification_token()
    now = datetime.now(timezone.utc)
    db.add(
        EmailVerificationToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=now + timedelta(hours=settings.email_verification_ttl_hours),
        )
    )
    await db.flush()

    # Same-origin static UI (see app/main.py's StaticFiles mount) -- the
    # link points back at the app's own root with the raw token as a query
    # param; static/app.js checks for that param on load and calls
    # /auth/verify-email itself, which is what makes "click the link" and
    # "land in the chat view already signed in" the same action.
    verification_url = f"{str(request.base_url).rstrip('/')}/?verify_token={raw_token}"
    send_verification_email(user.email, user.display_name, verification_url)

    return RegisterResponseOut(
        user=UserOut.model_validate(user),
        message="Account created. Check your email for a confirmation link before signing in.",
        dev_verification_url=verification_url if settings.local_dev_email_stub else None,
    )


@router.post("/verify-email", response_model=TokenPairOut)
async def verify_email(
    payload: VerifyEmailRequest, db: AsyncSession = Depends(get_unscoped_session)
) -> TokenPairOut:
    token_hash = hash_refresh_token(payload.token)
    result = await db.execute(
        select(EmailVerificationToken).where(EmailVerificationToken.token_hash == token_hash)
    )
    token_row = result.scalar_one_or_none()

    invalid = HTTPException(
        status.HTTP_400_BAD_REQUEST,
        "This verification link is invalid, expired, or has already been used.",
    )
    now = datetime.now(timezone.utc)
    if token_row is None or token_row.expires_at < now or token_row.used_at is not None:
        raise invalid

    result = await db.execute(select(User).where(User.id == token_row.user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise invalid

    token_row.used_at = now
    user.email_verified = True

    # Clicking the link *is* the first sign-in -- issue real tokens here
    # rather than making the person turn around and log in again right
    # after proving they own the address.
    return await _issue_token_pair(db, user)


@router.post("/login", response_model=TokenPairOut)
async def login(
    payload: LoginRequest, db: AsyncSession = Depends(get_unscoped_session)
) -> TokenPairOut:
    email = payload.email.lower()
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    # Same error for "no such user" and "wrong password" -- an
    # attacker-distinguishable difference here is exactly how account
    # enumeration works. This is intentionally NOT split into "user not
    # registered" / "wrong password" even though the UI was asked for that
    # distinction -- doing so would let anyone probe which email addresses
    # have accounts, one login attempt at a time. The email-not-verified
    # check below is different in kind: it only ever fires *after* the
    # correct password was supplied, so reaching it already proves the
    # caller knows the account's credentials -- there's nothing left to
    # enumerate at that point.
    invalid_credentials = HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    if user is None or not user.is_active:
        raise invalid_credentials
    if not verify_password(payload.password, user.password_hash):
        raise invalid_credentials
    if not user.email_verified:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Please verify your email address before signing in. Check your inbox for the "
            "confirmation link we sent when you registered.",
        )

    return await _issue_token_pair(db, user)


@router.post("/refresh", response_model=AccessTokenOut)
async def refresh(
    payload: RefreshRequest, db: AsyncSession = Depends(get_unscoped_session)
) -> AccessTokenOut:
    incoming_hash = hash_refresh_token(payload.refresh_token)
    result = await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == incoming_hash)
    )
    token_row = result.scalar_one_or_none()

    invalid = HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired refresh token")
    now = datetime.now(timezone.utc)
    if token_row is None or token_row.expires_at < now:
        raise invalid
    if token_row.revoked_at is not None:
        # This token was already rotated away once before -- somebody is
        # replaying an old refresh token. Treat it as a compromise signal:
        # revoke every other still-live token for this user too, forcing a
        # fresh login everywhere, rather than silently accepting reuse.
        stale_tokens = (
            await db.execute(
                select(RefreshToken).where(
                    RefreshToken.user_id == token_row.user_id,
                    RefreshToken.revoked_at.is_(None),
                )
            )
        ).scalars().all()
        for stale in stale_tokens:
            stale.revoked_at = now
        # Commit explicitly before raising: get_unscoped_session wraps this
        # request in session.begin(), whose contract is "exception ->
        # rollback, no matter what was written first" -- without this
        # commit, the revocations above would vanish the instant `raise
        # invalid` unwinds the request, silently undoing the very thing
        # this branch exists to do. (Every other endpoint either writes and
        # then returns normally, or raises before writing anything, which
        # is why this is the one place that needed an explicit commit.)
        await db.commit()
        raise invalid

    result = await db.execute(select(User).where(User.id == token_row.user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise invalid

    # Rotate: mint a new refresh token, mark the old one used-and-replaced.
    raw_refresh, refresh_hash = generate_refresh_token()
    new_token = RefreshToken(
        user_id=user.id,
        token_hash=refresh_hash,
        issued_at=now,
        expires_at=now + timedelta(days=get_settings().refresh_token_ttl_days),
    )
    db.add(new_token)
    await db.flush()
    token_row.revoked_at = now
    token_row.replaced_by_id = new_token.id

    return AccessTokenOut(access_token=create_access_token(user.id), refresh_token=raw_refresh)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    payload: LogoutRequest, db: AsyncSession = Depends(get_unscoped_session)
) -> None:
    incoming_hash = hash_refresh_token(payload.refresh_token)
    result = await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == incoming_hash)
    )
    token_row = result.scalar_one_or_none()
    if token_row is not None and token_row.revoked_at is None:
        token_row.revoked_at = datetime.now(timezone.utc)
    # Logging out with an already-invalid/unknown token is still a 204 --
    # the end state the client wants (not logged in) is already true.
