"""Run the suite as if the host had just booted: ``-p tools.fakeboot``.

The mode-flip dwell runs on ``time.monotonic()`` (#32), whose origin on Linux
is the boot. A test that ages the dwell with a wall-clock habit (stamp ``0.0``
for "long ago") passes on a long-running host and fails on a fresh one, so
CI runs the whole suite a second time with this plugin, to fail it on any host.

It offsets ``time.monotonic`` so the clock reads ``FAKE_UPTIME`` seconds
(default 120, below the default hysteresis) when the plugin loads, then counts
up at the real rate. ``asyncio``'s ``loop.time()`` reads ``time.monotonic`` on
each call, so Home Assistant's timers stay consistent with it. ``-p`` loads
this before the installed plugins, so freezegun (``freezer``) records this
clock as its real one and puts it back when a frozen test ends. If anything
else leaves ``time.monotonic`` replaced, the next test errors instead of
quietly running on the real clock. The log carries ``FAKEBOOT_UPTIME=<s>``
once, as evidence the run was made on this clock.

Only ``time.monotonic`` moves; ``monotonic_ns`` and ``perf_counter`` do not.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

UPTIME = float(os.environ.get("FAKE_UPTIME", "120"))

_real = time.monotonic
_offset = _real() - UPTIME


def monotonic() -> float:
    """The real monotonic clock, as if the host booted ``UPTIME`` s before load."""
    return _real() - _offset


time.monotonic = monotonic


def pytest_sessionstart(session: pytest.Session) -> None:
    """One evidence line: the run was made on the fake clock, from this uptime."""
    sys.stdout.write(f"FAKEBOOT_UPTIME={UPTIME:g}\n")
    sys.stdout.flush()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Refuse to run a test on any clock but this one."""
    if time.monotonic is not monotonic:
        raise RuntimeError(f"fakeboot: time.monotonic was replaced by {time.monotonic!r}")
