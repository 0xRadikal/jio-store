"""JioFarm — GrizzlySMS Refund Worker.

Background thread that cancels failed activations to reclaim balance.

Two modes:
- **normal** — wait the full delay, then cancel (for OTP timeout cases)
- **aggressive** — retry every 30s starting immediately, cancel as soon as the
  GrizzlySMS lock window opens (for fast-fail cases like "not a subscriber")
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Protocol


class Cancellable(Protocol):
    """Anything that can cancel an activation."""
    def cancel(self, act_id: str) -> None: ...


# How often to retry in aggressive mode (seconds)
AGGRESSIVE_INTERVAL = 30


class RefundWorker:
    """Cancels failed activations after their lock window to reclaim balance."""

    def __init__(self, sms: Cancellable, log: Callable[[str], None] | None = None):
        self.sms = sms
        self.log = log or (lambda _: None)
        self.q: "queue.Queue[tuple[str, float, bool]]" = queue.Queue()
        self.pending = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def schedule(self, act_id: str, delay: float, aggressive: bool = False) -> None:
        """Schedule a refund.

        Args:
            act_id: The activation ID to cancel.
            delay: Seconds to wait before first cancel attempt.
            aggressive: If True, try every 30s; if False, wait full delay once.
        """
        with self._lock:
            self.pending += 1
        self.q.put((act_id, time.monotonic() + delay, aggressive))
        mode = "aggressive" if aggressive else "normal"
        self.log(f"refund #{act_id[-8:]} dijadwalkan ({mode}, {int(delay)}s)")

    def _loop(self) -> None:
        while not self._stop.wait(2):
            due_now = time.monotonic()
            batch: list[tuple[str, float, bool]] = []
            while True:
                try:
                    batch.append(self.q.get_nowait())
                except queue.Empty:
                    break

            remaining: list[tuple[str, float, bool]] = []
            for act_id, fire_at, aggressive in batch:
                if due_now >= fire_at:
                    try:
                        self.sms.cancel(act_id)
                        self.log(f"refund #{act_id[-8:]} berhasil ✓")
                        with self._lock:
                            self.pending -= 1
                    except Exception as e:
                        err = type(e).__name__
                        # retry: aggressive = every 30s, normal = every 10s
                        retry_delay = AGGRESSIVE_INTERVAL if aggressive else 10
                        self.log(f"refund #{act_id[-8:]} gagal: {err} — retry {int(retry_delay)}s")
                        remaining.append((act_id, time.monotonic() + retry_delay, aggressive))
                else:
                    remaining.append((act_id, fire_at, aggressive))

            for item in remaining:
                self.q.put(item)

    def wait_all(self, timeout: float | None = None) -> bool:
        """Wait for confirmed cancellations; False never means refunded.

        A bounded caller must report unresolved activations for reconciliation.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                if self.pending <= 0:
                    self._stop.set()
                    self._thread.join()
                    return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(1)
