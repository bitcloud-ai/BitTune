import hashlib
import hmac

import pytest
from pydantic import SecretStr, TypeAdapter, ValidationError

from autopilot.domain.enums import UserRole
from autopilot.domain.identifiers import UserId
from autopilot.domain.identities import (
    BearerTokenBinding,
    BearerTokenHash,
    HumanSubject,
    ServiceSubject,
    Subject,
    SubjectKind,
)
from autopilot.gateway import authentication
from autopilot.gateway.authentication import (
    AUTHENTICATION_FAILED,
    DUPLICATE_CREDENTIAL_DIGEST,
    AuthenticationError,
    BearerTokenAuthenticator,
    hash_bearer_token,
)

VALID_TOKEN = "A" * 43
SECOND_TOKEN = "B" * 43
UNKNOWN_TOKEN = "C" * 43


def make_human(role: UserRole = UserRole.OPERATOR) -> HumanSubject:
    return HumanSubject(user_id=UserId.new(), role=role)


def make_binding(token: str, subject: HumanSubject | None = None) -> BearerTokenBinding:
    return BearerTokenBinding(
        token_hash=hash_bearer_token(SecretStr(token)),
        subject=subject or make_human(),
    )


def test_subject_union_keeps_human_and_service_identities_separate() -> None:
    adapter = TypeAdapter(Subject)

    human = adapter.validate_python(
        {"kind": "human", "user_id": str(UserId.new()), "role": "admin"}
    )
    service = adapter.validate_python({"kind": "service", "service_name": "autopilot-worker"})

    assert isinstance(human, HumanSubject)
    assert human.role is UserRole.ADMIN
    assert isinstance(service, ServiceSubject)
    assert service.kind is SubjectKind.SERVICE


def test_service_subject_cannot_carry_a_user_role() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ServiceSubject(service_name="autopilot-worker", role=UserRole.ADMIN)


def test_bearer_token_binding_accepts_only_human_subjects() -> None:
    with pytest.raises(ValidationError):
        BearerTokenBinding(
            token_hash=hash_bearer_token(SecretStr(VALID_TOKEN)),
            subject=ServiceSubject(service_name="autopilot-worker"),
        )


@pytest.mark.parametrize(
    "token",
    [
        "A" * 42,
        "A" * 129,
        f"{'A' * 42}=",
        f"{'A' * 42}+",
        f"{'A' * 42}/",
        f"{'A' * 42} ",
    ],
)
def test_bearer_token_hash_rejects_noncanonical_or_out_of_range_tokens(token: str) -> None:
    with pytest.raises(AuthenticationError, match=AUTHENTICATION_FAILED):
        hash_bearer_token(SecretStr(token))


def test_bearer_token_hash_is_a_dedicated_sha256_digest() -> None:
    expected = hashlib.sha256(VALID_TOKEN.encode()).hexdigest()

    token_hash = hash_bearer_token(SecretStr(VALID_TOKEN))

    assert isinstance(token_hash, BearerTokenHash)
    assert str(token_hash) == f"sha256:{expected}"


def test_authenticator_maps_a_token_hash_to_its_stable_human_subject() -> None:
    subject = make_human(UserRole.ADMIN)
    authenticator = BearerTokenAuthenticator((make_binding(VALID_TOKEN, subject),))

    authenticated = authenticator.authenticate(SecretStr(VALID_TOKEN))

    assert authenticated == subject
    assert authenticated.user_id == subject.user_id
    assert authenticated.role is UserRole.ADMIN


def test_authenticator_returns_one_failure_for_unknown_and_malformed_tokens() -> None:
    authenticator = BearerTokenAuthenticator((make_binding(VALID_TOKEN),))

    failures: list[AuthenticationError] = []
    for token in (UNKNOWN_TOKEN, "malformed"):
        with pytest.raises(AuthenticationError) as captured:
            authenticator.authenticate(SecretStr(token))
        failures.append(captured.value)

    assert {failure.code for failure in failures} == {"AUTHENTICATION_FAILED"}
    assert {str(failure) for failure in failures} == {AUTHENTICATION_FAILED}
    assert all(token not in str(failure) for failure in failures for token in (UNKNOWN_TOKEN,))


def test_empty_registry_uses_the_same_authentication_failure() -> None:
    authenticator = BearerTokenAuthenticator(())

    with pytest.raises(AuthenticationError) as captured:
        authenticator.authenticate(SecretStr(VALID_TOKEN))

    assert captured.value.code == "AUTHENTICATION_FAILED"
    assert str(captured.value) == AUTHENTICATION_FAILED


def test_authenticator_compares_every_registered_hash_without_early_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bindings = (make_binding(VALID_TOKEN), make_binding(SECOND_TOKEN))
    authenticator = BearerTokenAuthenticator(bindings)
    comparisons: list[tuple[str, str]] = []
    real_compare_digest = hmac.compare_digest

    def recording_compare_digest(left: str, right: str) -> bool:
        comparisons.append((left, right))
        return real_compare_digest(left, right)

    monkeypatch.setattr(authentication.hmac, "compare_digest", recording_compare_digest)

    authenticator.authenticate(SecretStr(VALID_TOKEN))

    assert len(comparisons) == len(bindings)


def test_authenticator_rejects_duplicate_hash_bindings() -> None:
    binding = make_binding(VALID_TOKEN)

    with pytest.raises(ValueError, match=DUPLICATE_CREDENTIAL_DIGEST):
        BearerTokenAuthenticator((binding, binding))


def test_authenticator_does_not_retain_token_plaintext() -> None:
    credential = SecretStr(VALID_TOKEN)
    authenticator = BearerTokenAuthenticator((make_binding(VALID_TOKEN),))

    authenticator.authenticate(credential)

    assert VALID_TOKEN not in repr(credential)
    assert VALID_TOKEN not in repr(authenticator)
