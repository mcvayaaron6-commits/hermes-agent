"""Tests for the file-lock coordinator."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.file_locks import FileLockManager, LockOutcome


@pytest.fixture
def mgr():
    return FileLockManager(default_ttl=10.0)


def test_first_acquire_grants(mgr):
    assert mgr.try_acquire("/tmp/x.py", owner="agent-A") is LockOutcome.GRANTED


def test_second_acquire_by_other_owner_busy(mgr):
    assert mgr.try_acquire("/tmp/x.py", owner="A") is LockOutcome.GRANTED
    assert mgr.try_acquire("/tmp/x.py", owner="B") is LockOutcome.BUSY


def test_reentrant_returns_reentrant(mgr):
    mgr.try_acquire("/tmp/x.py", owner="A")
    assert mgr.try_acquire("/tmp/x.py", owner="A") is LockOutcome.REENTRANT


def test_release_by_owner(mgr):
    mgr.try_acquire("/tmp/x.py", owner="A")
    assert mgr.release("/tmp/x.py", owner="A") is True
    # Now another owner can acquire.
    assert mgr.try_acquire("/tmp/x.py", owner="B") is LockOutcome.GRANTED


def test_release_by_non_owner_returns_false(mgr):
    mgr.try_acquire("/tmp/x.py", owner="A")
    assert mgr.release("/tmp/x.py", owner="B") is False
    # A still holds it.
    assert mgr.try_acquire("/tmp/x.py", owner="C") is LockOutcome.BUSY


def test_force_release(mgr):
    mgr.try_acquire("/tmp/x.py", owner="A")
    assert mgr.force_release("/tmp/x.py") is True
    assert mgr.try_acquire("/tmp/x.py", owner="B") is LockOutcome.GRANTED


def test_expired_lock_can_be_reclaimed():
    mgr = FileLockManager(default_ttl=0.01)
    mgr.try_acquire("/tmp/x.py", owner="A")
    time.sleep(0.05)
    outcome = mgr.try_acquire("/tmp/x.py", owner="B")
    assert outcome is LockOutcome.EXPIRED


def test_who_reports_holder(mgr):
    mgr.try_acquire("/tmp/x.py", owner="A")
    record = mgr.who("/tmp/x.py")
    assert record is not None
    assert record.owner == "A"


def test_who_returns_none_for_unheld(mgr):
    assert mgr.who("/tmp/x.py") is None


def test_who_returns_none_for_expired():
    mgr = FileLockManager(default_ttl=0.01)
    mgr.try_acquire("/tmp/x.py", owner="A")
    time.sleep(0.05)
    assert mgr.who("/tmp/x.py") is None


def test_all_held_prunes_expired():
    mgr = FileLockManager(default_ttl=0.01)
    mgr.try_acquire("/tmp/x.py", owner="A")
    mgr.try_acquire("/tmp/y.py", owner="B")
    time.sleep(0.05)
    assert mgr.all_held() == {}


def test_blocking_succeeds_when_lock_released(mgr):
    mgr.try_acquire("/tmp/x.py", owner="A")
    result_holder = {}

    def grabber():
        result_holder["outcome"] = mgr.acquire_blocking(
            "/tmp/x.py", owner="B", timeout=2.0,
        )

    t = threading.Thread(target=grabber)
    t.start()
    time.sleep(0.1)  # let it block
    mgr.release("/tmp/x.py", owner="A")
    t.join(timeout=2.0)
    assert result_holder["outcome"] is LockOutcome.GRANTED


def test_blocking_returns_busy_on_timeout(mgr):
    mgr.try_acquire("/tmp/x.py", owner="A")
    t0 = time.monotonic()
    outcome = mgr.acquire_blocking("/tmp/x.py", owner="B", timeout=0.2)
    assert outcome is LockOutcome.BUSY
    assert time.monotonic() - t0 >= 0.15


def test_context_manager_releases_on_exit(mgr):
    with mgr.acquire("/tmp/x.py", owner="A", timeout=0.1) as outcome:
        assert outcome is LockOutcome.GRANTED
        assert mgr.who("/tmp/x.py").owner == "A"
    # After exit, lock is released
    assert mgr.who("/tmp/x.py") is None


def test_context_manager_does_not_release_reentrant(mgr):
    mgr.try_acquire("/tmp/x.py", owner="A")
    with mgr.acquire("/tmp/x.py", owner="A", timeout=0.1) as outcome:
        assert outcome is LockOutcome.REENTRANT
    # Outer acquire still holds the lock.
    assert mgr.who("/tmp/x.py").owner == "A"


def test_path_normalisation(mgr):
    mgr.try_acquire("/tmp/x.py", owner="A")
    # Different string, same canonical path
    outcome = mgr.try_acquire("/tmp/./x.py", owner="B")
    assert outcome is LockOutcome.BUSY


def test_concurrent_grabs_only_one_wins():
    mgr = FileLockManager()
    winners = []
    barrier = threading.Barrier(8)

    def contender(i):
        barrier.wait()
        outcome = mgr.try_acquire("/tmp/contended.py", owner=f"agent-{i}")
        if outcome is LockOutcome.GRANTED:
            winners.append(i)

    threads = [threading.Thread(target=contender, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(winners) == 1
