"""Second-factor providers. Codes and secrets never enter logs or artifacts."""
import asyncio
import base64
import hashlib
import hmac
import os
import struct
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Challenge:
    attempt: int = 0


class CodeProvider(Protocol):
    async def get_code(self, challenge: Challenge) -> str: ...


class TOTPProvider:
    def __init__(self, secret: str | None = None, period: int = 30,
                 last_window_file: Path | None = None, min_remaining: int = 5,
                 sleep=asyncio.sleep):
        self.secret = (secret or os.environ["GITHUB_TOTP_SECRET"]).replace(" ", "").upper()
        self.period = period
        self.last_window_file = last_window_file
        self.min_remaining = min_remaining
        self.sleep = sleep
        self.last_window = -1

    async def get_code(self, challenge: Challenge) -> str:
        # Never intentionally submit a code from a window used by this runner.
        if self.last_window_file is not None:
            try:
                saved_window = int(self.last_window_file.read_text())
                if saved_window <= int(time.time()) // self.period:
                    self.last_window = max(self.last_window, saved_window)
            except (OSError, ValueError):
                pass
        while True:
            now = int(time.time())
            counter = now // self.period
            remaining = self.period - now % self.period
            if counter > self.last_window and remaining >= self.min_remaining:
                break
            await self.sleep(remaining + 1)
        key = base64.b32decode(self.secret, casefold=True)
        digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
        offset = digest[-1] & 15
        value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7fffffff
        self.last_window = counter
        if self.last_window_file is not None:
            self.last_window_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(dir=self.last_window_file.parent,
                                             prefix=".totp-window-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(str(counter))
                os.replace(temporary, self.last_window_file)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return f"{value % 1000000:06d}"
