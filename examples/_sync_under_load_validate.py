#!/usr/bin/env python3
"""Measure what `sync()` is worth on a *busy* machine, not an idle one.

`Capability.INPUT_SYNC` was validated at 2.2ms against an idle GNOME
desktop, which showed the pleasant half of the story: the 0.3s sleep it
replaced was roughly 130x longer than the answer. That number argues
sync() is cheaper. It does not argue the thing that actually matters.

A fixed sleep fails in two directions. Idle, it is merely wasteful --
already measured. Under load it can be too *short*, and then it is not
waste but a flaky test: the events have not been consumed when the script
carries on regardless. Nothing has ever measured that direction, and it
is the one that justifies the feature.

So this compares sync() latency on an idle desktop against the same
desktop with every core saturated, and reports the numbers that decide
the question: how often the round trip exceeded the 0.3s sleep it
replaced, and whether it ever exceeded sync()'s own timeout, which is
where it stops returning True at all.

**What this measures, stated honestly**: a bare ping/pong round trip, with
no events queued ahead of it. Real use is inject-then-sync, where the PONG
cannot arrive until EIS has read past the injected events -- so every
number here is a *lower* bound on what a post-injection sync costs under
the same load. Bare sync is measured because the alternative is injecting
pointer and keyboard events into whatever the user has in front of them.

`connect(backend="eiinput")` raises a real consent dialog; click Allow.
The load phase saturates every core for a few seconds.

    python3 _sync_under_load_validate.py [--samples N]
"""

import os
import subprocess
import sys
import time

import pyguitest
from pyguitest import BackendUnavailable, Capability, PermissionRequired

SLEEP_REPLACED = 0.3
"""The fixed sleep sync() was introduced to remove, in seconds. A round
trip longer than this is one a sleep would have got wrong."""


def measure(gui, samples):
    """Time `samples` bare sync() round trips. Returns seconds, and failures."""
    timings = []
    unconfirmed = 0
    for _ in range(samples):
        started = time.monotonic()
        confirmed = gui.sync()
        timings.append(time.monotonic() - started)
        if not confirmed:
            unconfirmed += 1
    return timings, unconfirmed


def summarise(label, timings, unconfirmed):
    """Print the distribution, and the two numbers that decide the question."""
    ordered = sorted(timings)
    pick = lambda q: ordered[min(len(ordered) - 1, int(len(ordered) * q))]  # noqa: E731
    over = sum(1 for t in timings if t > SLEEP_REPLACED)
    print(
        f"  {label:<12} min {ordered[0] * 1000:7.1f}ms  "
        f"median {pick(0.5) * 1000:7.1f}ms  p95 {pick(0.95) * 1000:7.1f}ms  "
        f"max {ordered[-1] * 1000:7.1f}ms"
    )
    print(
        f"  {'':<12} over {SLEEP_REPLACED}s: {over}/{len(timings)}   "
        f"returned False: {unconfirmed}/{len(timings)}"
    )
    return over, unconfirmed


def start_load():
    """Saturate every core. Returns the processes, for the caller to kill."""
    workers = []
    for _ in range(os.cpu_count() or 4):
        workers.append(
            subprocess.Popen(
                [sys.executable, "-c", "while True: pass"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
    return workers


samples = 50
if "--samples" in sys.argv:
    samples = int(sys.argv[sys.argv.index("--samples") + 1])

print("connecting to eiinput -- a consent dialog will appear; click Allow")
try:
    gui = pyguitest.connect(backend="eiinput")
except (BackendUnavailable, PermissionRequired) as exc:
    sys.exit(f"could not open an eiinput session: {exc}")

if not gui.supports(Capability.INPUT_SYNC):
    gui.close()
    sys.exit(
        "INPUT_SYNC is not offered here -- it needs libei 1.4 for "
        "ei_new_ping. Nothing to measure."
    )

try:
    print(f"\nmeasuring {samples} round trips, idle:")
    idle_timings, idle_unconfirmed = measure(gui, samples)
    summarise("idle", idle_timings, idle_unconfirmed)

    cores = os.cpu_count() or 4
    print(f"\nsaturating {cores} core(s), then measuring the same again:")
    workers = start_load()
    try:
        time.sleep(1.0)  # let the scheduler actually load up
        load_timings, load_unconfirmed = measure(gui, samples)
    finally:
        for worker in workers:
            worker.kill()
        for worker in workers:
            worker.wait()
    over, unconfirmed = summarise("under load", load_timings, load_unconfirmed)

    print("\nverdict")
    ratio = sorted(load_timings)[len(load_timings) // 2] / max(
        sorted(idle_timings)[len(idle_timings) // 2], 1e-9
    )
    print(f"  median round trip grew {ratio:.1f}x under load")
    if unconfirmed:
        print(
            f"  {unconfirmed} sync() call(s) returned False -- the round trip "
            "exceeded sync()'s own timeout, which is worth raising"
        )
    elif over:
        print(
            f"  {over} round trip(s) took longer than the {SLEEP_REPLACED}s "
            "sleep sync() replaced: a sleep would have continued too early "
            "there, which is the flakiness this feature exists to remove"
        )
    else:
        print(
            f"  nothing exceeded the {SLEEP_REPLACED}s sleep even loaded, so "
            "this load did not reproduce the too-short case. Remember these "
            "are bare round trips: a post-injection sync waits for the queue "
            "as well, and is not measured here"
        )
finally:
    gui.close()
