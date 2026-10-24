"""The role password must reach PostgreSQL only as a SCRAM verifier, never as plaintext."""

from __future__ import annotations

import base64
import hashlib
import hmac
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "infrastructure" / "scripts"))

import db_roles


def test_the_verifier_is_scram_sha_256_and_never_contains_the_password() -> None:
    password = "correct horse battery staple"
    verifier = db_roles.scram_verifier(password)
    assert password not in verifier
    scheme, rest = verifier.split("$", 1)
    assert scheme == "SCRAM-SHA-256"
    params, keys = rest.split("$", 1)
    iterations, salt_b64 = params.split(":")
    stored_b64, server_b64 = keys.split(":")
    salted = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), base64.b64decode(salt_b64), int(iterations)
    )
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    assert base64.b64decode(stored_b64) == hashlib.sha256(client_key).digest()
    assert base64.b64decode(server_b64) == hmac.new(salted, b"Server Key", hashlib.sha256).digest()


def test_two_verifiers_for_one_password_differ() -> None:
    assert db_roles.scram_verifier("pw") != db_roles.scram_verifier("pw")
