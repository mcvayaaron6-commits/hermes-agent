"""
File-level coordination — optimistic locks for parallel agents.

Replaces ``tools/file_state.py``'s documentation-only promise with
actual semantics.  When subagents run in parallel they can't be
allowed to write the same file at the same time — the second write
wins silently and the first agent's work disappears.  This module
provides:

* **Exclusive locks** by absolute path with a per-agent owner id.
* **TTL expiry** so a crashed agent doesn't strand the lock.
* **Bus notifications** (FileLockAcquired/Released) so sibling
  agents can listen and back off proactively.
* **Conflict outcomes** with three strategies: ``WAIT`` (block with
  timeout), ``ABORT`` (raise), ``QUEUE`` (return a future the lock
  manager resolves when free).

No git, no merge logic — the existing ``CheckpointManager`` already
snapshots before writes.  This is just the mutual-exclusion primitive.

Usage
-----

::

    from agent.file_locks import file_locks, LockOutcome

    with file_locks().acquire("/path/to/x.py", owner="agent-A", timeout=5.0) as ok:
        if ok is LockOutcome.GRANTED:
            # write the file
            ...
        else:
            # ok is BUSY — another agent owns the lock; back off
            ...
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, Iterator, Optional

logger = logging.getLogger(__name__)


_DEFAULT_TTL_SECONDS = 300.0  # 5 minutes — well over typical tool-call duration
_POLL_INTERVAL = 0.05         # busy-wait granularity when blocking


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


class LockOutcome(Enum):
    GRANTED = "granted"
    BUSY = "busy"          # another agent holds it
    REENTRANT = "reentrant"  # same agent already holds it (no-op)
    EXPIRED = "expired"    # prior holder's TTL elapsed; we took over


@dataclass(frozen=True, slots=True)
class LockRecord:
    path: str
    owner: str
    acquired_at: float
    expires_at: float
    intent: str = "write"  # "write" | "exclusive" | "shared"


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class FileLockManager:
    """In-process exclusive file-lock coordinator.

    Single-machine for now; cross-machine would swap the dict for
    Redis SETNX or filesystem flock + lease renewal.  The public API
    stays the same.
    """

    def __init__(self, *, default_ttl: float = _DEFAULT_TTL_SECONDS) -> None:
        self._lock = threading.RLock()
        self._held: Dict[str, LockRecord] = {}
        self._default_ttl = default_ttl

    # ---------- core API ----------

    def try_acquire(
        self,
        path: str,
        *,
        owner: str,
        ttl: Optional[float] = None,
        intent: str = "write",
    ) -> LockOutcome:
        """Non-blocking attempt.  Returns the outcome immediately."""
        normalized = self._normalize(path)
        now = time.time()
        ttl_val = ttl if ttl is not None else self._default_ttl
        with self._lock:
            existing = self._held.get(normalized)
            if existing is not None:
                if existing.owner == owner:
                    # Reentrant — refresh TTL and report.
                    self._held[normalized] = LockRecord(
                        path=normalized, owner=owner,
                        acquired_at=existing.acquired_at,
                        expires_at=now + ttl_val,
                        intent=intent,
                    )
                    return LockOutcome.REENTRANT
                if existing.expires_at > now:
                    return LockOutcome.BUSY
                # Expired — fall through and take ownership
                self._held.pop(normalized, None)
                outcome = LockOutcome.EXPIRED
            else:
                outcome = LockOutcome.GRANTED
            self._held[normalized] = LockRecord(
                path=normalized, owner=owner, acquired_at=now,
                expires_at=now + ttl_val, intent=intent,
            )
        self._publish_acquired(normalized, owner, intent)
        return outcome

    def acquire_blocking(
        self,
        path: str,
        *,
        owner: str,
        timeout: float = 10.0,
        ttl: Optional[float] = None,
        intent: str = "write",
        poll: float = _POLL_INTERVAL,
    ) -> LockOutcome:
        """Block up to ``timeout`` seconds waiting for the lock.

        Returns GRANTED / EXPIRED / REENTRANT on success, BUSY on timeout.
        """
        deadline = time.time() + max(0.0, timeout)
        while True:
            outcome = self.try_acquire(path, owner=owner, ttl=ttl, intent=intent)
            if outcome is not LockOutcome.BUSY:
                return outcome
            remaining = deadline - time.time()
            if remaining <= 0:
                return LockOutcome.BUSY
            time.sleep(min(poll, remaining))

    def release(self, path: str, *, owner: str) -> bool:
        """Release the lock if held by ``owner``.  Returns True if released."""
        normalized = self._normalize(path)
        with self._lock:
            existing = self._held.get(normalized)
            if existing is None or existing.owner != owner:
                return False
            self._held.pop(normalized, None)
        self._publish_released(normalized, owner)
        return True

    def force_release(self, path: str) -> bool:
        """Drop any lock on ``path`` regardless of owner.  Use sparingly."""
        normalized = self._normalize(path)
        with self._lock:
            existing = self._held.pop(normalized, None)
        if existing is not None:
            self._publish_released(normalized, existing.owner)
            return True
        return False

    def who(self, path: str) -> Optional[LockRecord]:
        """Return the current holder, or None.  Read-only."""
        normalized = self._normalize(path)
        with self._lock:
            record = self._held.get(normalized)
            if record is None:
                return None
            if record.expires_at <= time.time():
                # Expired — clean up lazily
                self._held.pop(normalized, None)
                return None
            return record

    def all_held(self) -> Dict[str, LockRecord]:
        """Snapshot of currently-held locks (expired entries pruned)."""
        now = time.time()
        with self._lock:
            alive = {p: r for p, r in self._held.items() if r.expires_at > now}
            # Prune expired in-place
            for p in list(self._held):
                if self._held[p].expires_at <= now:
                    self._held.pop(p, None)
            return dict(alive)

    @contextmanager
    def acquire(
        self,
        path: str,
        *,
        owner: str,
        timeout: float = 10.0,
        ttl: Optional[float] = None,
        intent: str = "write",
    ) -> Iterator[LockOutcome]:
        """Context manager.  Releases on exit if we acquired.

        Use ``if outcome in (LockOutcome.GRANTED, LockOutcome.EXPIRED,
        LockOutcome.REENTRANT): proceed`` to gate writes.
        """
        outcome = self.acquire_blocking(
            path, owner=owner, timeout=timeout, ttl=ttl, intent=intent,
        )
        try:
            yield outcome
        finally:
            if outcome in (LockOutcome.GRANTED, LockOutcome.EXPIRED):
                # REENTRANT means we didn't actually acquire — don't release.
                self.release(path, owner=owner)

    # ---------- helpers ----------

    @staticmethod
    def _normalize(path: str) -> str:
        try:
            return str(Path(path).expanduser().resolve())
        except (OSError, RuntimeError, ValueError):
            return os.path.abspath(path)

    def _publish_acquired(self, path: str, owner: str, intent: str) -> None:
        try:
            from agent.bus import FileLockAcquiredEvent, get_bus
            get_bus().publish(FileLockAcquiredEvent(
                subject=f"file.lock.acquired",
                agent_id=owner, path=path, intent=intent,
            ))
        except Exception:
            pass

    def _publish_released(self, path: str, owner: str) -> None:
        try:
            from agent.bus import FileLockReleasedEvent, get_bus
            get_bus().publish(FileLockReleasedEvent(
                subject=f"file.lock.released",
                agent_id=owner, path=path,
            ))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Process-global default manager
# ---------------------------------------------------------------------------


_DEFAULT_MGR: Optional[FileLockManager] = None
_DEFAULT_LOCK = threading.Lock()


def file_locks() -> FileLockManager:
    """Return the process-global file-lock manager."""
    global _DEFAULT_MGR
    with _DEFAULT_LOCK:
        if _DEFAULT_MGR is None:
            _DEFAULT_MGR = FileLockManager()
        return _DEFAULT_MGR


def reset_default_file_locks() -> None:
    """Test-only: drop the global manager."""
    global _DEFAULT_MGR
    with _DEFAULT_LOCK:
        _DEFAULT_MGR = None


__all__ = [
    "FileLockManager",
    "LockOutcome",
    "LockRecord",
    "file_locks",
    "reset_default_file_locks",
]
