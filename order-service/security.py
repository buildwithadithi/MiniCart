from typing import NoReturn

import grpc
import jwt

from security_config import JwtVerifier


def _abort(context: grpc.ServicerContext, details: str) -> NoReturn:
    context.abort(grpc.StatusCode.UNAUTHENTICATED, details)


def authenticated_user_id(context: grpc.ServicerContext, verifier: JwtVerifier) -> int:
    authorization_values = [
        value
        for key, value in context.invocation_metadata()
        if key.lower() == "authorization"
    ]
    if len(authorization_values) != 1:
        _abort(context, "A valid Bearer token is required.")
    candidate = authorization_values[0]
    if not isinstance(candidate, str):
        _abort(context, "A valid Bearer token is required.")
    authorization = candidate
    scheme, separator, token = authorization.partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not token
        or token.strip() != token
        or any(character.isspace() for character in token)
    ):
        _abort(context, "A valid Bearer token is required.")
    try:
        return verifier.verify(token).user_id
    except (jwt.InvalidTokenError, ValueError):
        _abort(context, "The Bearer token is invalid or expired.")
