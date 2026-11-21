import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt


DEFAULT_ISSUER = "minicart-auth"
DEFAULT_AUDIENCE = "minicart-api"


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: int


def _read_key(path_value: str | None, key_name: str) -> str:
    if not path_value:
        raise RuntimeError(f"{key_name} must be configured.")
    path = Path(path_value).expanduser()
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise RuntimeError(f"Unable to read configured {key_name}.") from error


class JwtVerifier:
    def __init__(
        self,
        public_key: str,
        issuer: str = DEFAULT_ISSUER,
        audience: str = DEFAULT_AUDIENCE,
    ) -> None:
        self._public_key = public_key
        self._issuer = issuer
        self._audience = audience

    @classmethod
    def from_environment(cls) -> "JwtVerifier":
        return cls(
            public_key=_read_key(
                os.getenv("JWT_PUBLIC_KEY_PATH"), "JWT_PUBLIC_KEY_PATH"
            ),
            issuer=os.getenv("JWT_ISSUER", DEFAULT_ISSUER),
            audience=os.getenv("JWT_AUDIENCE", DEFAULT_AUDIENCE),
        )

    def verify(self, token: str) -> AuthenticatedUser:
        claims: dict[str, Any] = jwt.decode(
            token,
            self._public_key,
            algorithms=["RS256"],
            issuer=self._issuer,
            audience=self._audience,
            options={
                "require": ["sub", "iss", "aud", "iat", "exp"],
            },
        )
        subject = claims.get("sub")
        if (
            not isinstance(subject, str)
            or not subject.isascii()
            or not subject.isdecimal()
            or len(subject) > 10
        ):
            raise jwt.InvalidTokenError("Invalid subject claim.")
        user_id = int(subject)
        if user_id <= 0 or user_id > 2147483647 or str(user_id) != subject:
            raise jwt.InvalidTokenError("Invalid subject claim.")
        return AuthenticatedUser(user_id=user_id)
