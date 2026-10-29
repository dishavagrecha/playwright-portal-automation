"""Second-factor providers. Codes and secrets never enter logs or artifacts."""
import asyncio
import base64
import hashlib
import hmac
import os
import struct
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Challenge:
    attempt: int = 0


class TOTPProvider:
    def __init__(self, secret: str | None = None, period: int = 30):
        self.secret = (secret or os.environ["GITHUB_TOTP_SECRET"]).replace(" ", "").upper()
        self.period = period

    async def get_code(self, challenge: Challenge) -> str:
        # Do not submit the same code after a rejection. Avoid codes near expiry.
        remaining = self.period - (int(time.time()) % self.period)
        if challenge.attempt or remaining < 5:
            await asyncio.sleep(remaining + 1)
        key = base64.b32decode(self.secret, casefold=True)
        counter = int(time.time()) // self.period
        digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
        offset = digest[-1] & 15
        value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7fffffff
        return f"{value % 1000000:06d}"
