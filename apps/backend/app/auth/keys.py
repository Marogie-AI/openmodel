"""API key material: generation, parsing, and peppered argon2id hashing of the secret."""

import asyncio
import re
import secrets
from dataclasses import dataclass

import argon2
from argon2.exceptions import InvalidHashError, VerificationError

KEY_RE = re.compile(r"^om_[a-z]+_([A-Za-z0-9_-]{12})_([A-Za-z0-9_-]{32})$")

_hasher = argon2.PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)


@dataclass(frozen=True)
class GeneratedKey:
    raw: str
    key_id: str
    secret: str
    prefix: str


ENV_RE = re.compile(r"^[a-z]+$")


def generate_key(env: str) -> GeneratedKey:
    if ENV_RE.match(env) is None:
        raise ValueError(f"env must match {ENV_RE.pattern}, got {env!r}")
    key_id = secrets.token_urlsafe(9)
    secret = secrets.token_urlsafe(24)
    prefix = f"om_{env}_{key_id}"
    return GeneratedKey(raw=f"{prefix}_{secret}", key_id=key_id, secret=secret, prefix=prefix)


def parse_key(raw: str) -> tuple[str, str] | None:
    """(key_id, secret), or None when `raw` is not a well-formed key."""
    match = KEY_RE.match(raw)
    return None if match is None else (match.group(1), match.group(2))


def hash_secret(secret: str, pepper: str) -> str:
    return _hasher.hash(secret + pepper)


async def hash_secret_async(secret: str, pepper: str) -> str:
    """argon2 burns ~20ms of CPU and 19MB per call: keep it off the event loop."""
    return await asyncio.to_thread(hash_secret, secret, pepper)


def verify_secret(secret_hash: str, secret: str, pepper: str) -> bool:
    """Constant-time-ish verify that never raises: a bad hash or secret is just False."""
    try:
        return _hasher.verify(secret_hash, secret + pepper)
    except (VerificationError, InvalidHashError):
        return False
