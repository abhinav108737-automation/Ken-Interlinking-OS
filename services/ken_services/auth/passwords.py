"""Password hashing with the standard library's ``hashlib.scrypt`` (ADR-0013 sections 1, 7).

Stored format (``users.password_hash``, CHECK ``LIKE 'scrypt$%'``)::

    scrypt$<n>$<r>$<p>$<salt, urlsafe base64>$<derived key, urlsafe base64>

* Current parameters: n = 2**17, r = 8, p = 1, a 16-byte random salt, a 32-byte key,
  ``maxmem`` 192 MiB (scrypt needs 128 * n * r bytes = 128 MiB). This meets the OWASP minimum.
* Verification is constant-time (:func:`hmac.compare_digest`). A malformed or out-of-bounds
  stored hash never verifies -- and is never hashed with absurd parameters (bounded n, r, p).
* :func:`needs_rehash` is true for a stored hash with parameters BELOW the current ones; the
  provider re-hashes it at the next successful login.
* :data:`DUMMY_HASH` is a fixed hash of a random throwaway password, verified for unknown
  usernames so that a login costs the same whether or not the user exists.

The hashing functions are synchronous and CPU/memory heavy: async callers run them through
:func:`hash_password_async` / :func:`verify_password_async`, which use ``asyncio.to_thread``
behind a semaphore of 2 per event loop (memory and event-loop denial-of-service bound).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import secrets
import weakref
from dataclasses import dataclass
from typing import Final

__all__ = [
    "CURRENT_PARAMS",
    "DUMMY_HASH",
    "MAX_CONCURRENT_HASHES",
    "ScryptParams",
    "hash_password",
    "hash_password_async",
    "needs_rehash",
    "parse_hash",
    "verify_password",
    "verify_password_async",
]

SCHEME: Final = "scrypt"
SALT_BYTES: Final = 16
KEY_BYTES: Final = 32
MAXMEM: Final = 192 * 1024 * 1024
MAX_CONCURRENT_HASHES: Final = 2
_MIN_LOG2_N: Final = 10
_MAX_LOG2_N: Final = 20
_MAX_R: Final = 16
_MAX_P: Final = 4


@dataclass(frozen=True, slots=True, order=True)
class ScryptParams:
    n: int
    r: int
    p: int

    def valid(self) -> bool:
        return (
            self.n >= 2**_MIN_LOG2_N
            and self.n <= 2**_MAX_LOG2_N
            and self.n & (self.n - 1) == 0
            and 1 <= self.r <= _MAX_R
            and 1 <= self.p <= _MAX_P
            and 128 * self.n * self.r * self.p <= MAXMEM
        )

    def weaker_than(self, other: ScryptParams) -> bool:
        return self.n < other.n or self.r < other.r or self.p < other.p


CURRENT_PARAMS: Final = ScryptParams(n=2**17, r=8, p=1)

DUMMY_HASH: Final = (
    "scrypt$131072$8$1$hcwBNZj7M1NV8PXNOXnmKA==$otkTqwsc_otMsxH4QqJBgjOKT4FML7O9S3fsZWahZwc="
)
"""A well-formed hash (current parameters) of a random password nobody knows. Verifying
against it costs one full scrypt, like verifying a real user."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text.encode("ascii"))


def _derive(password: str, salt: bytes, params: ScryptParams) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=params.n,
        r=params.r,
        p=params.p,
        maxmem=MAXMEM,
        dklen=KEY_BYTES,
    )


def hash_password(password: str, *, params: ScryptParams = CURRENT_PARAMS) -> str:
    """A new salted hash in the stored format.

    Raises:
        ValueError: the parameters are out of bounds.
    """
    if not params.valid():
        raise ValueError("scrypt parameters out of bounds")
    salt = secrets.token_bytes(SALT_BYTES)
    key = _derive(password, salt, params)
    return f"{SCHEME}${params.n}${params.r}${params.p}${_b64(salt)}${_b64(key)}"


def parse_hash(stored: str) -> tuple[ScryptParams, bytes, bytes] | None:
    """``(params, salt, key)`` of a well-formed, in-bounds stored hash, else ``None``."""
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != SCHEME:
        return None
    try:
        params = ScryptParams(n=int(parts[1]), r=int(parts[2]), p=int(parts[3]))
        salt, key = _unb64(parts[4]), _unb64(parts[5])
    except (ValueError, binascii.Error):
        return None
    if not params.valid() or not salt or not key:
        return None
    return params, salt, key


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of ``password`` against a stored hash (malformed -> False)."""
    parsed = parse_hash(stored)
    if parsed is None:
        # Pay the cost anyway: a malformed row must not be distinguishable by timing.
        _derive(password, b"\x00" * SALT_BYTES, CURRENT_PARAMS)
        return False
    params, salt, key = parsed
    candidate = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=params.n,
        r=params.r,
        p=params.p,
        maxmem=MAXMEM,
        dklen=len(key),
    )
    return hmac.compare_digest(candidate, key)


def needs_rehash(stored: str, *, params: ScryptParams = CURRENT_PARAMS) -> bool:
    parsed = parse_hash(stored)
    return parsed is None or parsed[0].weaker_than(params)


_SEMAPHORES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _SEMAPHORES.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_HASHES)
        _SEMAPHORES[loop] = semaphore
    return semaphore


async def hash_password_async(password: str, *, params: ScryptParams = CURRENT_PARAMS) -> str:
    async with _semaphore():
        return await asyncio.to_thread(hash_password, password, params=params)


async def verify_password_async(password: str, stored: str) -> bool:
    async with _semaphore():
        return await asyncio.to_thread(verify_password, password, stored)
