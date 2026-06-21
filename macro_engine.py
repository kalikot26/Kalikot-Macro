#!/usr/bin/env python3
"""
macro_engine.py - shared recording/playback engine for the macro tool.

Used by both the CLI (macro.py) and the GUI (macro_gui.py).

Key design point: during playback we do NOT listen for user input to abort.
Playback stops ONLY when the caller sets the provided stop_event (Stop button
or a global Stop hotkey). This is what lets a macro keep running even while the
user moves the mouse or types.
"""

import threading
import time

from pynput import keyboard, mouse
from pynput.keyboard import Key, KeyCode
from pynput.mouse import Button

# Persistence now lives in macro_store (portable SQLite). These names are
# re-exported so existing callers can keep using eng.save_macro(), etc.
from macro_store import (  # noqa: F401
    DB_PATH,
    delete_macro,
    get_config,
    get_setting,
    list_macros,
    load_macro,
    macro_exists,
    macro_names,
    rename_macro,
    save_macro,
    set_config,
    set_setting,
)

# ---------------------------------------------------------------------------
# Storage / config
# ---------------------------------------------------------------------------
MOVE_SAMPLE_S = 0.015  # min seconds between recorded mouse-move samples


# ---------------------------------------------------------------------------
# Key (de)serialization
# ---------------------------------------------------------------------------
def key_to_obj(key):
    """Serialize a pynput key/keycode to a JSON-friendly dict.

    For ordinary keys we store the virtual key code (vk) as well as the char.
    The vk is the physical key and is what we replay, so combinations like
    Ctrl+C work even though Windows reports the char as a control code ('\\x03')
    while a modifier is held.
    """
    if isinstance(key, KeyCode):
        return {"k": "code", "char": key.char, "vk": key.vk}
    return {"k": "key", "v": key.name}


def obj_to_key(obj):
    """Rebuild a pynput key/keycode from its serialized dict."""
    kind = obj["k"]
    if kind == "key":                       # special key (Key.ctrl, Key.cmd, ...)
        return getattr(Key, obj["v"])
    if kind == "code":                      # current format: prefer the vk
        vk = obj.get("vk")
        if vk is not None:
            return KeyCode(vk=vk)
        return KeyCode(char=obj.get("char"))
    if kind == "char":                      # legacy files
        return KeyCode(char=obj["v"])
    if kind == "vk":                        # legacy files
        return KeyCode(vk=obj["v"])
    raise ValueError(f"unknown key object: {obj}")


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------
class Recorder:
    """Records keyboard + mouse events with timings on background threads."""

    def __init__(self, on_update=None, stop_key=Key.f9, record_mouse=True,
                 track_window=False):
        """
        on_update(count, elapsed) - optional callback fired as events arrive.
        stop_key                  - key that ends recording (also via stop()).
        record_mouse              - if False, only keyboard is recorded.
        track_window              - if True, record the window under each click
                                    (title + class) for robust multi-app replay.
        """
        self.on_update = on_update
        self.stop_key = stop_key
        self.record_mouse = record_mouse
        self.track_window = track_window
        self.events = []
        self._start = None
        self._last_move = 0.0
        self._k_listener = None
        self._m_listener = None
        self._lock = threading.Lock()

    def _now(self):
        return time.perf_counter() - self._start

    def _add(self, ev):
        with self._lock:
            self.events.append(ev)
        if self.on_update:
            self.on_update(len(self.events), ev["t"])

    # --- keyboard handlers ---
    def _on_press(self, key):
        if self.stop_key is not None and key == self.stop_key:
            self.stop()
            return False
        self._add({"t": self._now(), "type": "key_press", "key": key_to_obj(key)})

    def _on_release(self, key):
        if self.stop_key is not None and key == self.stop_key:
            return
        self._add({"t": self._now(), "type": "key_release", "key": key_to_obj(key)})

    # --- mouse handlers (inert when record_mouse is False) ---
    def _on_move(self, x, y):
        if not self.record_mouse:
            return
        t = self._now()
        if t - self._last_move >= MOVE_SAMPLE_S:
            self._last_move = t
            self._add({"t": t, "type": "move", "x": x, "y": y})

    def _on_click(self, x, y, button, pressed):
        if not self.record_mouse:
            return
        ev = {"t": self._now(), "type": "click", "x": x, "y": y,
              "button": button.name, "pressed": pressed}
        if pressed and self.track_window:       # remember which window was clicked
            try:
                import winsend
                win = winsend.window_at(x, y)
                if win:
                    ev["win"] = win
            except Exception:
                pass
        self._add(ev)

    def _on_scroll(self, x, y, dx, dy):
        if not self.record_mouse:
            return
        self._add({"t": self._now(), "type": "scroll", "x": x, "y": y,
                   "dx": dx, "dy": dy})

    def start(self):
        self.events = []
        self._last_move = 0.0
        self._start = time.perf_counter()
        self._k_listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release)
        self._k_listener.start()
        if self.record_mouse:
            self._m_listener = mouse.Listener(
                on_move=self._on_move, on_click=self._on_click, on_scroll=self._on_scroll)
            self._m_listener.start()
        else:
            self._m_listener = None

    def stop(self):
        if self._k_listener:
            self._k_listener.stop()
        if self._m_listener:
            self._m_listener.stop()
        with self._lock:
            return list(self.events)

    @property
    def is_running(self):
        return bool(self._k_listener and self._k_listener.running)


# ---------------------------------------------------------------------------
# Player
# ---------------------------------------------------------------------------
class Player:
    """Replays recorded events. Stops only when stop_event is set."""

    def __init__(self):
        self._kb = keyboard.Controller()
        self._ms = mouse.Controller()

    @staticmethod
    def _key_vk_char(key_obj):
        """Resolve a serialized key into (vk, char) for window-message sending."""
        k = obj_to_key(key_obj)
        if isinstance(k, KeyCode):
            return k.vk, k.char
        vkc = getattr(k, "value", None)        # special keys: Key.x.value is a KeyCode
        return getattr(vkc, "vk", None), None

    def _bg_sender(self, hwnd):
        """Cache one WinSender per window handle (background routing)."""
        if not hwnd:
            return None
        s = self._bg_cache.get(hwnd)
        if s is None:
            import winsend
            s = winsend.WinSender(hwnd)
            self._bg_cache[hwnd] = s
        return s

    def _resolve_win(self, ev):
        """Find the current handle for an event's recorded window (or None)."""
        win = ev.get("win")
        if not win:
            return None
        import winsend
        key = (win.get("title"), win.get("cls"))
        h = self._win_cache.get(key)
        if h and winsend.is_window(h):
            return h
        h = winsend.find_window(win)
        if h:
            self._win_cache[key] = h
        return h

    def _do_event(self, ev, skip_move, sender=None, bg_follow=False):
        et = ev["type"]
        if et == "wait":
            return  # handled by the scheduler in play(), nothing to actuate

        background = sender is not None or bg_follow

        if et == "paste":
            import winsend
            winsend.set_clipboard_text(ev.get("text", ""))
            time.sleep(0.05)                    # let the clipboard settle
            if background:
                s = sender if sender is not None else self._bg_sender(self._last_hwnd)
                if s is not None:
                    s.paste()
            else:
                self._kb.press(Key.ctrl)
                self._kb.press("v")
                self._kb.release("v")
                self._kb.release(Key.ctrl)
            return

        if background:                          # post to a window (no real cursor)
            s = sender
            if bg_follow:                       # route to the action's window
                import winsend
                if et in ("move", "click", "scroll"):
                    hwnd = self._resolve_win(ev) or winsend.window_from_point(
                        ev["x"], ev["y"])
                    if hwnd:
                        self._last_hwnd = hwnd
                    s = self._bg_sender(hwnd)
                else:                           # keys go to the last targeted window
                    s = self._bg_sender(self._last_hwnd)
            if s is None:
                return
            if et == "key_press":
                vk, ch = self._key_vk_char(ev["key"])
                s.key(vk, ch, True)
            elif et == "key_release":
                vk, ch = self._key_vk_char(ev["key"])
                s.key(vk, ch, False)
            elif et == "move":
                if not skip_move:
                    s.move(ev["x"], ev["y"])
            elif et == "click":
                s.click(ev["x"], ev["y"], ev["button"], ev["pressed"])
            elif et == "scroll":
                s.scroll(ev["x"], ev["y"], ev["dx"], ev["dy"])
            return

        # foreground: real cursor/keyboard. If this click recorded its window,
        # bring that window to the front first so multi-app workflows land right.
        if et == "click" and ev.get("pressed"):
            hwnd = self._resolve_win(ev)
            if hwnd and hwnd != self._front_hwnd:
                import winsend
                winsend.activate(hwnd)
                self._front_hwnd = hwnd
                time.sleep(0.05)

        if et == "key_press":
            self._kb.press(obj_to_key(ev["key"]))
        elif et == "key_release":
            self._kb.release(obj_to_key(ev["key"]))
        elif et == "move":
            if not skip_move:
                self._ms.position = (ev["x"], ev["y"])
        elif et == "click":
            self._ms.position = (ev["x"], ev["y"])
            btn = getattr(Button, ev["button"])
            (self._ms.press if ev["pressed"] else self._ms.release)(btn)
        elif et == "scroll":
            self._ms.position = (ev["x"], ev["y"])
            self._ms.scroll(ev["dx"], ev["dy"])

    def play(self, events, loops=1, speed=1.0, loop_delay=0.0, skip_move=False,
             stop_event=None, on_loop=None, on_delay=None, target_hwnd=None,
             bg_follow=False):
        """
        loops        - number of repeats; 0 = forever (until stop_event).
        speed        - timing multiplier (2 = twice as fast).
        loop_delay   - seconds to wait between loops.
        skip_move    - ignore recorded mouse-move events.
        stop_event   - threading.Event; when set, playback stops cleanly.
        on_loop(i, total) - optional callback at the start of each loop.
        on_delay(remaining) - optional callback each second of the loop delay
                       countdown (remaining seconds, then 0 when it ends).
        target_hwnd  - background mode locked to ONE window: post all events there.
        bg_follow    - background mode, multi-app: route each event to its recorded
                       window (keys -> last targeted window).

        Foreground replay automatically brings a click's recorded window to the
        front first (when the macro was recorded with window tracking on).
        """
        if speed <= 0:
            speed = 1.0
        if stop_event is None:
            stop_event = threading.Event()

        self._bg_cache = {}
        self._win_cache = {}
        self._last_hwnd = None
        self._front_hwnd = None
        sender = None
        if target_hwnd:
            import winsend
            sender = winsend.WinSender(target_hwnd)

        forever = (loops == 0)
        i = 0
        while (forever or i < loops) and not stop_event.is_set():
            i += 1
            if on_loop:
                on_loop(i, 0 if forever else loops)
            base = time.perf_counter()
            # Explicit "wait" events add extra time on top of the recorded
            # timestamps; we accumulate that here so later events stay in sync.
            offset = 0.0
            for ev in events:
                if stop_event.is_set():
                    return
                target = base + ev["t"] / speed + offset
                delay = target - time.perf_counter()
                if delay > 0:
                    # interruptible sleep so Stop responds promptly
                    if stop_event.wait(delay):
                        return
                if ev.get("type") == "wait":
                    d = float(ev.get("d", 0.0)) / speed
                    if d > 0:
                        if stop_event.wait(d):
                            return
                        offset += d
                    continue
                self._do_event(ev, skip_move, sender, bg_follow)
            if (forever or i < loops) and loop_delay > 0:
                remaining = loop_delay
                while remaining > 0.001:
                    if on_delay:
                        on_delay(remaining)
                    step = 1.0 if remaining > 1.0 else remaining
                    if stop_event.wait(step):
                        return
                    remaining -= step
                if on_delay:
                    on_delay(0)
