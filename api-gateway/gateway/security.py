import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt


@dataclass(frozen=True)
class VerifiedBearer:
    user_id: int
    token: str


class JwtVerifier:
    def __init__(self, public_key: str, issuer: str, audience: str) -> None:
        self._public_key = public_key
        self._issuer = issuer
        self._audience = audience

    @classmethod
    def from_environment(cls) -> "JwtVerifier":
        path_value = os.getenv("JWT_PUBLIC_KEY_PATH")
        if not path_value:
            raise RuntimeError("JWT_PUBLIC_KEY_PATH must be configured.")
        try:
            public_key = Path(path_value).expanduser().read_text(encoding="utf-8")
        except OSError as error:
            raise RuntimeError("Unable to read configured JWT_PUBLIC_KEY_PATH.") from error
        return cls(
            public_key=public_key,
            issuer=os.getenv("JWT_ISSUER", "minicart-auth"),
            audience=os.getenv("JWT_AUDIENCE", "minicart-api"),
        )

    def verify(self, token: str) -> int:
        claims: dict[str, Any] = jwt.decode(
            token,
            self._public_key,
            algorithms=["RS256"],
            issuer=self._issuer,
            audience=self._audience,
            options={"require": ["sub", "iss", "aud", "iat", "exp"]},
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
        return user_id
