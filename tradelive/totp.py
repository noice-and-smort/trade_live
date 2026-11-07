"""RFC 6238 TOTP (the 6-digit authenticator code), standard library only."""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time
from typing import Optional
from urllib.parse import parse_qs, urlparse


def normalize_secret(secret: str) -> str:
  """Accepts a base32 secret (spaces/lowercase ok) or a full otpauth:// URI."""
  s = secret.strip().strip("\"'")
  if s.lower().startswith("otpauth://"):
    s = parse_qs(urlparse(s).query).get("secret", [""])[0]
  return s.replace(" ", "").replace("-", "").upper()


_B32 = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567=")


def secret_problem(secret: str) -> Optional[str]:
  """Returns a user-friendly message if the secret can't be valid, else None."""
  s = normalize_secret(secret)
  if not s:
    return "The TOTP secret key is empty."
  bad = sorted(set(ch for ch in s if ch not in _B32))
  if bad:
    hint = ""
    if set(bad) & set("0189"):
      hint = (" A secret key never contains 0, 1, 8 or 9: if this is the "
              "6-digit code, put it in the code box instead; if you typed the "
              "key by hand, 0 is the letter O and 1 is the letter I.")
    return (f"The TOTP secret key contains invalid characters: "
            f"{' '.join(bad)}. It should only have letters A–Z and digits 2–7."
            + hint)
  if len(s.rstrip("=")) < 16:
    return ("The TOTP secret key looks too short (usually 16–32 characters). "
            "Copy the full key shown under the QR code.")
  return None


def totp(secret: str, at: Optional[float] = None, digits: int = 6,
         period: int = 30) -> str:
  key_b32 = normalize_secret(secret)
  key = base64.b32decode(key_b32 + "=" * (-len(key_b32) % 8))
  counter = int((time.time() if at is None else at) // period)
  digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
  off = digest[-1] & 0x0F
  code = (struct.unpack(">I", digest[off:off + 4])[0] & 0x7FFFFFFF) % 10**digits
  return str(code).zfill(digits)
