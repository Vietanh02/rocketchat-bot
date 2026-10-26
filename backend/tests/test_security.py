"""Password hashing, token handling, and secret encryption."""

from __future__ import annotations

from app.security import (
    create_access_token,
    decode_access_token,
    decrypt_secret,
    encrypt_secret,
    hash_password,
    verify_password,
)


def test_passwords_are_hashed_not_stored():
    digest = hash_password("hunter2")
    assert "hunter2" not in digest
    assert verify_password("hunter2", digest)
    assert not verify_password("hunter3", digest)


def test_the_same_password_hashes_differently_each_time():
    assert hash_password("same") != hash_password("same")


def test_a_corrupt_hash_fails_closed():
    assert not verify_password("anything", "not-a-hash")


def test_tokens_round_trip_with_their_claims():
    token, ttl = create_access_token("a@example.com", "admin")
    payload = decode_access_token(token)
    assert payload["sub"] == "a@example.com" and payload["role"] == "admin"
    assert ttl > 0


def test_a_tampered_token_is_rejected():
    token, _ = create_access_token("a@example.com", "admin")
    assert decode_access_token(token[:-2] + "xy") is None
    assert decode_access_token("garbage") is None


def test_secrets_round_trip_and_are_not_stored_in_the_clear():
    ciphertext = encrypt_secret("super-secret-token")
    assert "super-secret-token" not in ciphertext
    assert decrypt_secret(ciphertext) == "super-secret-token"


def test_empty_secrets_stay_empty():
    assert encrypt_secret(None) is None
    assert encrypt_secret("") is None
    assert decrypt_secret(None) is None


def test_undecryptable_ciphertext_returns_none_rather_than_raising():
    """A rotated APP_SECRET_KEY must not crash the bot on startup."""
    assert decrypt_secret("not-valid-fernet") is None


def test_encryption_is_not_deterministic():
    assert encrypt_secret("x") != encrypt_secret("x")
