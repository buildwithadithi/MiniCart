import os
import time
from pathlib import Path

import jwt

from auth_security import DEFAULT_AUDIENCE, DEFAULT_ISSUER


class JwtIssuer:
    def __init__(
        self,
        private_key: str,
        issuer: str = DEFAULT_ISSUER,
        audience: str = DEFAULT_AUDIENCE,
        expires_in: int = 900,
    ) -> None:
        if expires_in <= 0:
            raise ValueError("JWT_ACCESS_TOKEN_SECONDS must be positive.")
        self._private_key = private_key
        self.issuer = issuer
        self.audience = audience
        self.expires_in = expires_in

    @classmethod
    def from_environment(cls) -> "JwtIssuer":
        key_path_value = os.getenv("JWT_PRIVATE_KEY_PATH")
        if not key_path_value:
            raise RuntimeError("JWT_PRIVATE_KEY_PATH must be configured.")
        try:
            private_key = Path(key_path_value).expanduser().read_text(
                encoding="utf-8"
            )
        except OSError as error:
            raise RuntimeError("Unable to read configured JWT_PRIVATE_KEY_PATH.") from error
        return cls(
            private_key=private_key,
            issuer=os.getenv("JWT_ISSUER", DEFAULT_ISSUER),
            audience=os.getenv("JWT_AUDIENCE", DEFAULT_AUDIENCE),
            expires_in=int(os.getenv("JWT_ACCESS_TOKEN_SECONDS", "900")),
        )

    def issue(self, user_id: int) -> str:
        issued_at = int(time.time())
        return jwt.encode(
            {
                "sub": str(user_id),
                "iss": self.issuer,
                "aud": self.audience,
                "iat": issued_at,
                "exp": issued_at + self.expires_in,
            },
            self._private_key,
            algorithm="RS256",
        )
