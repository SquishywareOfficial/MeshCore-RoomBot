"""Conservative wall-clock validity for offline dates and expiry decisions."""
from datetime import datetime, timezone
import math
import time


class ClockUnavailable(ValueError):
    pass


class SessionClock:
    """Trust is asserted for this process only, never saved across a reboot.

    The operator must check the OS clock before passing --clock-trusted. A
    discontinuity relative to monotonic elapsed time permanently revokes trust
    for this session; monotonic time remains usable for paging and rate limits.
    """
    def __init__(self, trusted=False, max_jump_seconds=5, wall=time.time,
                 monotonic=time.monotonic):
        if type(trusted) is not bool:
            raise ValueError('trusted must be boolean')
        if (type(max_jump_seconds) not in (int, float)
                or not math.isfinite(max_jump_seconds) or max_jump_seconds <= 0):
            raise ValueError('Clock jump limit must be finite and positive')
        self.wall, self.monotonic, self.max_jump = wall, monotonic, max_jump_seconds
        self.trusted = trusted
        self.reason = 'operator confirmed this session' if trusted else 'clock not confirmed for this session'
        self.anchor_wall, self.anchor_mono = wall(), monotonic()

    def now(self):
        wall, mono = self.wall(), self.monotonic()
        if not math.isfinite(wall) or not math.isfinite(mono):
            self.trusted = False
            self.reason = 'clock is not finite'
        elif mono < self.anchor_mono or abs((wall - self.anchor_wall) - (mono - self.anchor_mono)) > self.max_jump:
            self.trusted = False
            self.reason = 'clock changed; restart and confirm the corrected clock'
        if not self.trusted:
            raise ClockUnavailable(self.reason)
        try:
            return datetime.fromtimestamp(wall, timezone.utc)
        except (ValueError, OverflowError, OSError) as exc:
            self.trusted = False
            self.reason = 'clock outside supported date range'
            raise ClockUnavailable(self.reason) from exc
