#!/usr/bin/env python3
"""
scheduler.py - run several macros at once, sharing one keyboard/mouse.

Because there is only one real input device, two macros can't actuate at the
same instant. Instead they time-share: while macro A is sleeping in its loop
delay, macro B may run its action sequence -- but only if B fits inside A's
remaining delay (plus a safety buffer), so A never gets pushed off schedule.

- Scheduler   : the shared coordinator. Only one runner "executes" at a time,
                and a runner may only start when every *other* running macro is
                currently in a delay long enough to contain this execution.
- MacroRunner : one background thread per playing macro, with its own loop /
                speed / delay / window settings and its own stop control.
"""

import threading
import time

import macro_engine as eng

SAFETY = 0.2        # seconds of buffer required on top of a macro's run time


def estimate_exec_time(events, speed=1.0):
    """Rough wall-clock time for one pass of `events` at `speed` (seconds)."""
    if not events:
        return 0.0
    last_t = events[-1].get("t", 0.0)
    waits = sum(e.get("d", 0.0) for e in events if e.get("type") == "wait")
    pastes = sum(1 for e in events if e.get("type") == "paste")
    speed = speed if speed and speed > 0 else 1.0
    return (last_t + waits) / speed + pastes * 0.06 + 0.1


class Scheduler:
    """Coordinates exclusive device access across concurrent MacroRunners."""

    def __init__(self):
        self._cv = threading.Condition()
        self._runners = set()
        self._executing = None      # the runner currently actuating input

    def register(self, runner):
        with self._cv:
            self._runners.add(runner)
            self._cv.notify_all()

    def unregister(self, runner):
        with self._cv:
            self._runners.discard(runner)
            if self._executing is runner:
                self._executing = None
            self._cv.notify_all()

    def acquire_to_execute(self, runner, exec_time, stop_event):
        """Block until `runner` may run its sequence. False if stopped first."""
        with self._cv:
            while True:
                if stop_event.is_set():
                    return False
                ok = self._executing is None
                if ok:
                    # every other running macro must be idle in a delay that is
                    # long enough to contain this whole execution + buffer.
                    for other in self._runners:
                        if other is runner:
                            continue
                        rem = other.remaining_delay()
                        if rem is not None and rem <= exec_time + SAFETY:
                            ok = False
                            break
                if ok:
                    self._executing = runner
                    self._cv.notify_all()
                    return True
                self._cv.wait(0.1)      # re-check periodically / on notify

    def release(self, runner):
        with self._cv:
            if self._executing is runner:
                self._executing = None
            self._cv.notify_all()


class MacroRunner(threading.Thread):
    """Plays one macro on a loop, coordinating through the shared scheduler."""

    def __init__(self, name, events, opts, scheduler,
                 on_state=None, on_tick=None, on_done=None):
        super().__init__(daemon=True)
        self.name = name
        self.events = events
        self.loops = int(opts.get("loops", 1))
        self.speed = float(opts.get("speed", 1.0))
        self.loop_delay = float(opts.get("loop_delay", 0.0))
        self.skip_move = bool(opts.get("skip_move", False))
        self.target_hwnd = opts.get("target_hwnd")
        self.bg_follow = bool(opts.get("bg_follow", False))

        self.scheduler = scheduler
        self.on_state = on_state
        self.on_tick = on_tick
        self.on_done = on_done

        self.exec_time = estimate_exec_time(events, self.speed)
        self.stop_event = threading.Event()
        self.player = eng.Player()

        self.state = "starting"     # starting|waiting|running|delay|stopped
        self.loop_i = 0
        self._deadline = None
        self._lock = threading.Lock()

    # -- state shared with the scheduler / GUI -----------------------------
    def remaining_delay(self):
        with self._lock:
            if self.state == "delay" and self._deadline is not None:
                return max(0.0, self._deadline - time.perf_counter())
            return None

    def _set_state(self, state, deadline=None):
        with self._lock:
            self.state = state
            self._deadline = deadline
        if self.on_state:
            self.on_state(self)

    def stop(self):
        self.stop_event.set()

    # -- the loop ----------------------------------------------------------
    def run(self):
        self.scheduler.register(self)
        try:
            forever = self.loops == 0
            i = 0
            while (forever or i < self.loops) and not self.stop_event.is_set():
                i += 1
                self.loop_i = i
                self._set_state("waiting")
                if not self.scheduler.acquire_to_execute(
                        self, self.exec_time, self.stop_event):
                    break
                self._set_state("running")
                try:
                    self.player.play(
                        self.events, loops=1, speed=self.speed, loop_delay=0,
                        skip_move=self.skip_move, stop_event=self.stop_event,
                        target_hwnd=self.target_hwnd, bg_follow=self.bg_follow)
                finally:
                    self.scheduler.release(self)
                if self.stop_event.is_set():
                    break
                if (forever or i < self.loops) and self.loop_delay > 0:
                    deadline = time.perf_counter() + self.loop_delay
                    self._set_state("delay", deadline=deadline)
                    while True:
                        rem = deadline - time.perf_counter()
                        if rem <= 0.001 or self.stop_event.is_set():
                            break
                        if self.on_tick:
                            self.on_tick(self, rem)
                        if self.stop_event.wait(min(0.5, rem)):
                            break
        finally:
            self.scheduler.release(self)
            self.scheduler.unregister(self)
            self._set_state("stopped")
            if self.on_done:
                self.on_done(self)
