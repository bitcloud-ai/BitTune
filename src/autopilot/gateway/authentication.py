"""Opaque Bearer Token authentication without retaining credential plaintext."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable
from typing import Final

from pydantic import SecretStr

from autopilot.domain.identities import BearerTokenBinding, BearerTokenHash, HumanSubject

AUTHENTICATION_FAILED: Final = "authentication failed"
DUPLICATE_CREDENTIAL_DIGEST: Final = "Bearer Token registry contains a duplicate hash"
BEARER_TOKEN_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{43,128}")


class AuthenticationError(RuntimeError):
    """Uniform public failure that never reveals credential validation details."""

    code: Final = "AUTHENTICATION_FAILED"

    def __init__(self) -> None:
        super().__init__(AUTHENTICATION_FAILED)


def _hash_token_value(token_value: str) -> BearerTokenHash:
    digest = hashlib.sha256(token_value.encode("utf-8", errors="surrogatepass")).hexdigest()
    return BearerTokenHash(root=f"sha256:{digest}")


def hash_bearer_token(token: SecretStr) -> BearerTokenHash:
    """Validate and hash a deployment-injected Token without returning its plaintext."""
    token_value = token.get_secret_value()
    if BEARER_TOKEN_PATTERN.fullmatch(token_value) is None:
        raise AuthenticationError
    return _hash_token_value(token_value)


class BearerTokenAuthenticator:
    """Authenticate against immutable hash bindings using constant-time comparisons."""

    __slots__ = ("_bindings",)

    def __init__(self, bindings: Iterable[BearerTokenBinding]) -> None:
        registered = tuple(bindings)
        hashes = tuple(str(binding.token_hash) for binding in registered)
        if len(set(hashes)) != len(hashes):
            raise ValueError(DUPLICATE_CREDENTIAL_DIGEST)
        self._bindings = registered

    def authenticate(self, token: SecretStr) -> HumanSubject:
        """Return the bound human subject or raise the same error for every failure mode."""
        token_value = token.get_secret_value()
        format_is_valid = BEARER_TOKEN_PATTERN.fullmatch(token_value) is not None
        candidate_hash = _hash_token_value(token_value)
        matched_subject: HumanSubject | None = None

        for binding in self._bindings:
            if hmac.compare_digest(str(candidate_hash), str(binding.token_hash)):
                matched_subject = binding.subject

        if not format_is_valid or matched_subject is None:
            raise AuthenticationError
        return matched_subject
