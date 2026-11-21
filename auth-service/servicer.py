import logging
from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from email_validator import EmailNotValidError, validate_email
import grpc
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from typing import NoReturn

from auth_database import SessionLocal
from auth_models import User
from generated import auth_pb2, auth_pb2_grpc
from token_issuer import JwtIssuer


logger = logging.getLogger(__name__)
_MAX_PASSWORD_LENGTH = 128


def _abort(context, code: grpc.StatusCode, details: str) -> NoReturn:
    context.abort(code, details)
    raise RuntimeError("gRPC context.abort unexpectedly returned.")


def _normalized_email(value: str) -> str:
    try:
        validated = validate_email(value.strip(), check_deliverability=False)
    except EmailNotValidError as error:
        raise ValueError("A valid email address is required.") from error
    return validated.normalized.casefold()


def _created_at(value: datetime) -> Timestamp:
    timestamp = Timestamp()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    timestamp.FromDatetime(value.astimezone(timezone.utc))
    return timestamp


class AuthServiceServicer(auth_pb2_grpc.AuthServiceServicer):
    def __init__(
        self,
        *,
        session_factory=SessionLocal,
        issuer: JwtIssuer | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._issuer = issuer if issuer is not None else JwtIssuer.from_environment()
        self._password_hasher = PasswordHasher()

    def Register(self, request, context):
        name = request.name.strip()
        password = request.password
        if not name or len(name) > 120:
            _abort(
                context,
                grpc.StatusCode.INVALID_ARGUMENT,
                "Name must contain between 1 and 120 characters.",
            )
        if not password or len(password) < 12 or len(password) > _MAX_PASSWORD_LENGTH:
            _abort(
                context,
                grpc.StatusCode.INVALID_ARGUMENT,
                "Password must contain between 12 and 128 characters.",
            )
        try:
            email = _normalized_email(request.email)
        except ValueError as error:
            _abort(context, grpc.StatusCode.INVALID_ARGUMENT, str(error))

        user = User(
            name=name,
            email=email,
            password_hash=self._password_hasher.hash(password),
        )
        try:
            with self._session_factory.begin() as session:
                session.add(user)
                session.flush()
                response = auth_pb2.RegisterResponse(
                    user_id=user.id,
                    name=user.name,
                    email=user.email,
                    created_at=_created_at(user.created_at or datetime.now(timezone.utc)),
                )
        except IntegrityError:
            _abort(
                context,
                grpc.StatusCode.ALREADY_EXISTS,
                "An account with that email address already exists.",
            )
        except SQLAlchemyError:
            logger.exception("Database failure while registering a user.")
            _abort(
                context,
                grpc.StatusCode.UNAVAILABLE,
                "The authentication database is temporarily unavailable.",
            )
        return response

    def Login(self, request, context):
        try:
            email = _normalized_email(request.email)
        except ValueError:
            email = ""

        try:
            with self._session_factory() as session:
                user = session.scalar(select(User).where(User.email == email))
                if user is not None:
                    try:
                        self._password_hasher.verify(
                            user.password_hash, request.password
                        )
                    except (VerificationError, InvalidHashError):
                        user = None
                if user is None:
                    _abort(
                        context,
                        grpc.StatusCode.UNAUTHENTICATED,
                        "Invalid email or password.",
                    )
                user_id = user.id
        except SQLAlchemyError:
            logger.exception("Database failure while authenticating a user.")
            _abort(
                context,
                grpc.StatusCode.UNAVAILABLE,
                "The authentication database is temporarily unavailable.",
            )

        return auth_pb2.LoginResponse(
            access_token=self._issuer.issue(user_id),
            token_type="Bearer",
            expires_in=self._issuer.expires_in,
        )
