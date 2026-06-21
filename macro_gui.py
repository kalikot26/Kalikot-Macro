#!/usr/bin/env python3
"""
macro_gui.py - graphical front end for the action-replay macro tool.

    python macro_gui.py      (or double-click gui.bat)

Records keystrokes + mouse and replays them with loops/speed. Macros and their
play settings are stored in a portable SQLite database (macros.db) via
macro_store. Includes a step editor for inserting waits, trimming mouse jitter
and deleting events.

Global hotkeys (work even when the window is unfocused):
    F9  - start / stop recording
    F8  - start / stop playback
"""

import os
import queue
import threading
import time

import customtkinter as ctk
from pynput import keyboard
from pynput.keyboard import Key, KeyCode

import macro_engine as eng
import macro_store
import scheduler as sched
import winsend

# Diagnostic log written next to the program (helps debug a packaged .exe on
# another machine, where there's no console to see errors).
LOG_PATH = os.path.join(macro_store.BASE_DIR, "kalikot.log")


def _log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Look & feel
# ---------------------------------------------------------------------------
ACCENT = "#3a7afe"          # play / primary
ACCENT_HOVER = "#2f63cf"
REC = "#e0533d"             # record / stop (red)
REC_HOVER = "#c2412e"
OK = "#2fa56a"              # idle status dot
WARN = "#e0a23d"

DEFAULT_RECORD_HOTKEY = "f9"   # global record start/stop
DEFAULT_PLAY_HOTKEY = "f8"     # global play start/stop

NEW_LABEL = "+ New recording"


# ---------------------------------------------------------------------------
# Hotkey helpers — a hotkey is stored as a small token string so it survives in
# the settings DB: special keys keep their pynput name ("f8", "esc"), character
# keys keep the lowercased char ("a", "]"), anything else falls back to "vk###".
# ---------------------------------------------------------------------------
def key_token(key):
    """Normalize a pynput key/keycode (from a listener) to a comparable token."""
    if isinstance(key, Key):
        return key.name
    if isinstance(key, KeyCode):
        if key.char:
            return key.char.lower()
        if key.vk is not None:
            return f"vk{key.vk}"
    return None


def token_to_key(token):
    """Rebuild a pynput key object from a token (used as the recorder stop key)."""
    if not token:
        return None
    if hasattr(Key, token):
        return getattr(Key, token)
    if token.startswith("vk") and token[2:].isdigit():
        return KeyCode(vk=int(token[2:]))
    return KeyCode(char=token)


def token_label(token):
    """Human-friendly label, e.g. 'f8' -> 'F8', 'a' -> 'A', 'esc' -> 'Esc'."""
    if not token:
        return "—"
    if len(token) == 1:
        return token.upper()
    if token.startswith("vk"):
        return token.upper()
    return token.replace("_", " ").title()

ctk.set_appearance_mode(eng.get_setting("theme", "dark"))
ctk.set_default_color_theme("blue")


# ---------------------------------------------------------------------------
# Event rendering helpers (shared with the editor)
# ---------------------------------------------------------------------------
def _key_label(obj):
    if obj.get("k") == "key":
        return obj.get("v", "?")
    ch = obj.get("char") or obj.get("v")
    if ch and ch.isprintable() and ch not in ("\t", "\n", "\r"):
        return ch
    vk = obj.get("vk")
    return f"vk{vk}" if vk is not None else (ch or "?")


def describe_event(ev):
    typ = ev.get("type")
    if typ == "key_press":
        return f"⌨  press   {_key_label(ev['key'])}"
    if typ == "key_release":
        return f"⌨  release {_key_label(ev['key'])}"
    if typ == "click":
        act = "down" if ev.get("pressed") else "up"
        base = f"🖱  {ev.get('button')} {act}  @({ev.get('x')},{ev.get('y')})"
        win = ev.get("win")
        if win and win.get("title"):
            t = win["title"]
            base += f"  ▸ {t if len(t) <= 24 else t[:23] + '…'}"
        return base
    if typ == "scroll":
        return f"🖱  scroll {ev.get('dx')},{ev.get('dy')}  @({ev.get('x')},{ev.get('y')})"
    if typ == "move":
        return f"🖱  move  @({ev.get('x')},{ev.get('y')})"
    if typ == "wait":
        return f"⏸  WAIT {float(ev.get('d', 0.0)):.2f}s"
    if typ == "paste":
        text = ev.get("text", "")
        preview = text.replace("\n", " ⏎ ")
        if len(preview) > 40:
            preview = preview[:39] + "…"
        return f"📋  PASTE {len(text)} chars  “{preview}”"
    return str(typ)


class MacroApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("kalikot")
        self.geometry("460x710")
        self.minsize(430, 540)

        self.state_mode = "IDLE"          # IDLE | RECORDING  (playback is per-macro)
        self.recorder = None
        self.recorded_events = None       # current macro events held in memory
        self.current_name = None          # name of the loaded macro (or None)
        self.saved = True                 # is the in-memory recording on disk?
        self.editor = None
        # multi-macro playback: each running macro has its own MacroRunner.
        self.scheduler = sched.Scheduler()
        self.runners = {}                 # macro name -> MacroRunner (active only)
        self.run_rows = {}                # macro name -> state label in panel
        # runner threads talk to the UI through this queue (thread-safe); a
        # main-thread poller drains it. Never touch Tk from a runner thread.
        self._ui_queue = queue.Queue()
        self.hotkey_dialog = None
        self._windows = {}                 # display string -> hwnd (background mode)
        self._record_start = 0.0
        self._record_done = True

        # user-configurable global hotkeys (stored in the settings DB)
        self.record_token = eng.get_setting("hotkey_record", DEFAULT_RECORD_HOTKEY)
        self.play_token = eng.get_setting("hotkey_play", DEFAULT_PLAY_HOTKEY)
        self._capturing = False           # true while the dialog grabs a key

        self._build_ui()
        self._refresh_macro_list(select=NEW_LABEL)
        self._restore_ui_state()
        self.after(100, self._drain_ui_queue)   # main-thread poller for runners

        # one persistent global listener for the F8/F9 hotkeys.
        self._logged_first_key = False
        try:
            self._hotkey_listener = keyboard.Listener(on_press=self._on_global_key)
            self._hotkey_listener.start()
            _log(f"app start; hotkey listener started "
                 f"(record={self.record_token}, play={self.play_token})")
        except Exception as e:
            self._hotkey_listener = None
            _log(f"app start; FAILED to start hotkey listener: {e!r}")
            self.after(400, lambda: self._set_status(
                "⚠ Global hotkeys blocked — try Run as administrator", REC, REC))

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        pad = 12
        cpx = 14            # card inner x-padding

        # Header (fixed) -------------------------------------------------
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=pad, pady=(pad, 2))
        ctk.CTkLabel(header, text="🎬 kalikot",
                     font=ctk.CTkFont(size=20, weight="bold")).pack(side="left")
        self.theme_switch = ctk.CTkSwitch(header, text="Light",
                                          command=self._toggle_theme, width=44)
        self.theme_switch.pack(side="right")
        if ctk.get_appearance_mode().lower() == "light":
            self.theme_switch.select()
            self.theme_switch.configure(text="Dark")
        ctk.CTkButton(header, text="⌨ Hotkeys", width=84, height=26,
                      fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"),
                      hover_color=("gray80", "gray25"),
                      command=self._open_hotkeys).pack(side="right", padx=(0, 8))

        # Status bar (fixed, bottom) -------------------------------------
        status = ctk.CTkFrame(self, fg_color="transparent")
        status.pack(side="bottom", fill="x", padx=pad, pady=(2, 8))
        self.dot = ctk.CTkLabel(status, text="●", text_color=OK,
                                font=ctk.CTkFont(size=14))
        self.dot.pack(side="left")
        self.status = ctk.CTkLabel(status, text="Ready", text_color="gray70",
                                   font=ctk.CTkFont(size=12))
        self.status.pack(side="left", padx=6)

        # Scrollable body ------------------------------------------------
        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=pad - 4, pady=0)

        # Macro picker ---------------------------------------------------
        pick_row = ctk.CTkFrame(body, fg_color="transparent")
        pick_row.pack(fill="x", pady=(2, 4))
        ctk.CTkLabel(pick_row, text="Macro",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w")
        self.macro_menu = ctk.CTkOptionMenu(
            pick_row, values=[NEW_LABEL], command=self._on_pick_macro, height=32,
            fg_color=("gray85", "gray25"), button_color=ACCENT,
            button_hover_color=ACCENT_HOVER)
        self.macro_menu.pack(fill="x", pady=(3, 0))
        self.name_entry = ctk.CTkEntry(pick_row, placeholder_text="name", height=30)
        self.name_entry.insert(0, "macro")
        self.name_entry.pack(fill="x", pady=(6, 0))

        # Record card ----------------------------------------------------
        rec_card = ctk.CTkFrame(body, corner_radius=12)
        rec_card.pack(fill="x", pady=(10, 0))
        self.rec_header = ctk.CTkLabel(rec_card, text="RECORD", text_color="gray60",
                                       font=ctk.CTkFont(size=11, weight="bold"))
        self.rec_header.pack(anchor="w", padx=cpx, pady=(10, 0))
        self.kbd_only = ctk.CTkCheckBox(rec_card,
                                        text="Keyboard only (don't record mouse)")
        self.kbd_only.pack(anchor="w", padx=cpx, pady=(6, 0))
        self.track_window = ctk.CTkCheckBox(
            rec_card, text="Track windows — record where each click lands (multi-app)")
        self.track_window.pack(anchor="w", padx=cpx, pady=(6, 0))
        self.record_btn = ctk.CTkButton(
            rec_card, text="●  Record", height=40,
            font=ctk.CTkFont(size=15, weight="bold"),
            fg_color=REC, hover_color=REC_HOVER, command=self._toggle_record)
        self.record_btn.pack(fill="x", padx=cpx, pady=(8, 6))
        btn_row = ctk.CTkFrame(rec_card, fg_color="transparent")
        btn_row.pack(fill="x", padx=cpx, pady=(0, 4))
        btn_row.grid_columnconfigure((0, 1, 2), weight=1)
        self.save_btn = self._ghost_button(btn_row, "💾 Save", self._save_macro, 0)
        self.edit_btn = self._ghost_button(btn_row, "✎ Edit", self._open_editor, 1)
        self.delete_btn = self._ghost_button(btn_row, "🗑 Delete", self._delete_macro, 2)
        for b in (self.save_btn, self.edit_btn, self.delete_btn):
            b.configure(state="disabled")
        self.record_info = ctk.CTkLabel(
            rec_card, text="", text_color="gray60",
            font=ctk.CTkFont(size=11), wraplength=380, justify="left")
        self.record_info.pack(anchor="w", padx=cpx, pady=(2, 10))

        # Play card ------------------------------------------------------
        play_card = ctk.CTkFrame(body, corner_radius=12)
        play_card.pack(fill="x", pady=(10, 6))
        self.play_header = ctk.CTkLabel(play_card, text="PLAY", text_color="gray60",
                                        font=ctk.CTkFont(size=11, weight="bold"))
        self.play_header.pack(anchor="w", padx=cpx, pady=(10, 0))

        opts = ctk.CTkFrame(play_card, fg_color="transparent")
        opts.pack(fill="x", padx=cpx, pady=(6, 2))
        opts.grid_columnconfigure((0, 1, 2), weight=1)
        self.loops_entry = self._labeled_entry(opts, "Loops (0=∞)", "1", 0)
        self.speed_entry = self._labeled_entry(opts, "Speed ×", "1.0", 1)
        self.delay_entry = self._labeled_entry(opts, "Loop delay s", "0", 2)

        self.rec_delay = ctk.CTkLabel(play_card, text="", text_color=ACCENT,
                                      font=ctk.CTkFont(size=11), cursor="hand2")
        self.rec_delay.pack(anchor="w", padx=cpx, pady=(3, 0))
        self.rec_delay.bind("<Button-1>", lambda e: self._apply_recommended_delay())

        self.skip_move = ctk.CTkCheckBox(play_card,
                                         text="Ignore mouse movement (clicks only)")
        self.skip_move.pack(anchor="w", padx=cpx, pady=(8, 0))

        # Window targeting (multi-app aware) -----------------------------
        self.bg_mode = ctk.CTkCheckBox(
            play_card, text="Background mode — hands-free (mouse/keyboard stay free)",
            command=self._toggle_bg)
        self.bg_mode.pack(anchor="w", padx=cpx, pady=(8, 0))

        # background sub-options (shown only while background mode is on)
        self.bg_box = ctk.CTkFrame(play_card, fg_color=("gray90", "gray19"),
                                   corner_radius=8)
        self.bg_lock = ctk.CTkCheckBox(self.bg_box, font=ctk.CTkFont(size=12),
                                       text="Lock to one specific window",
                                       command=self._toggle_lock)
        self.bg_lock.pack(anchor="w", padx=10, pady=(8, 0))
        win_row = ctk.CTkFrame(self.bg_box, fg_color="transparent")
        win_row.pack(fill="x", padx=10, pady=(6, 0))
        self.win_menu = ctk.CTkOptionMenu(win_row, values=["(pick a window)"],
                                          height=28, dynamic_resizing=False)
        self.win_menu.pack(side="left", fill="x", expand=True)
        self.win_refresh = ctk.CTkButton(win_row, text="⟳", width=30, height=28,
                                         command=self._refresh_windows)
        self.win_refresh.pack(side="left", padx=(6, 0))
        self.win_menu.configure(state="disabled")
        self.win_refresh.configure(state="disabled")
        self.bg_hint = ctk.CTkLabel(self.bg_box, text="", text_color="gray55",
                                    font=ctk.CTkFont(size=11), wraplength=360,
                                    justify="left")
        self.bg_hint.pack(anchor="w", padx=10, pady=(6, 8))

        self.play_btn = ctk.CTkButton(
            play_card, text="▶  Play", height=40,
            font=ctk.CTkFont(size=15, weight="bold"),
            fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self._toggle_play)
        self.play_btn.pack(fill="x", padx=cpx, pady=(10, 6))
        self.play_info = ctk.CTkLabel(
            play_card, text="", text_color="gray60",
            font=ctk.CTkFont(size=11), wraplength=380, justify="left")
        self.play_info.pack(anchor="w", padx=cpx, pady=(0, 12))

        # Running panel --------------------------------------------------
        self.run_card = ctk.CTkFrame(body, corner_radius=12)
        ctk.CTkLabel(self.run_card, text="RUNNING", text_color="gray60",
                     font=ctk.CTkFont(size=11, weight="bold")).pack(
                         anchor="w", padx=cpx, pady=(10, 2))
        self.run_list = ctk.CTkFrame(self.run_card, fg_color="transparent")
        self.run_list.pack(fill="x", padx=cpx, pady=(0, 10))
        # shown only while at least one macro is running (see _refresh_run_panel)

        self._refresh_hotkey_labels()

    def _refresh_hotkey_labels(self):
        rec = token_label(self.record_token)
        play = token_label(self.play_token)
        self.rec_header.configure(text=f"RECORD  ·  {rec}")
        self.play_header.configure(text=f"PLAY  ·  {play}")
        if self.state_mode == "IDLE":
            self.record_info.configure(text=f"{rec} starts/stops recording.")
        self.play_info.configure(
            text=f"Keeps running through your input. {play} starts/stops playback.")

    def _ghost_button(self, parent, text, command, col):
        b = ctk.CTkButton(
            parent, text=text, height=36, font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="transparent", border_width=2,
            text_color=("gray10", "gray90"), hover_color=("gray80", "gray25"),
            command=command)
        b.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 6, 0))
        return b

    def _labeled_entry(self, parent, label, default, col):
        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        wrap.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 8, 0))
        ctk.CTkLabel(wrap, text=label, text_color="gray60",
                     font=ctk.CTkFont(size=11)).pack(anchor="w")
        e = ctk.CTkEntry(wrap, height=34, justify="center")
        e.insert(0, default)
        e.pack(fill="x", pady=(2, 0))
        return e

    # --------------------------------------------------------------- theme
    def _toggle_theme(self):
        mode = "light" if self.theme_switch.get() else "dark"
        ctk.set_appearance_mode(mode)
        self.theme_switch.configure(text="Dark" if mode == "light" else "Light")
        eng.set_setting("theme", mode)

    # ------------------------------------------------------------- status
    def _set_status(self, text, color="gray70", dot=OK):
        self.status.configure(text=text, text_color=color)
        self.dot.configure(text_color=dot)

    def _set_entry(self, entry, value):
        entry.delete(0, "end")
        entry.insert(0, str(value))

    # ------------------------------------------------------- macro picker
    def _refresh_macro_list(self, select=None):
        names = eng.macro_names()
        values = [NEW_LABEL] + names
        self.macro_menu.configure(values=values)
        if select is not None and select in values:
            self.macro_menu.set(select)
        elif self.macro_menu.get() not in values:
            self.macro_menu.set(NEW_LABEL)

    def _on_pick_macro(self, choice):
        if self.state_mode == "RECORDING":
            return
        if choice == NEW_LABEL:
            self.current_name = None
            self.recorded_events = None
            self.saved = True
            self._set_entry(self.name_entry, "macro")
            self._restore_ui_state()       # fall back to last-used defaults
            self._update_action_buttons()
            self._update_play_button()
            self._set_status("New recording — press Record", "gray70", OK)
            return
        try:
            data = eng.load_macro(choice)
        except KeyError:
            self._set_status(f"'{choice}' not found", REC, REC)
            self._refresh_macro_list(select=NEW_LABEL)
            return
        self.current_name = choice
        self.recorded_events = data["events"]
        self.saved = True
        self._set_entry(self.name_entry, choice)
        self._hydrate_config(eng.get_config(choice))
        self._update_action_buttons()
        self._update_play_button()
        n = len(self.recorded_events)
        run = " · RUNNING" if choice in self.runners else ""
        self._set_status(f"Loaded '{choice}' — {n} events{run}", OK, OK)
        self._refresh_recommended()

    def _hydrate_config(self, cfg):
        """Apply a per-macro config dict to the play widgets."""
        self._set_entry(self.loops_entry, cfg["loops"])
        self._set_entry(self.speed_entry, cfg["speed"])
        self._set_entry(self.delay_entry, cfg["loop_delay"])
        (self.skip_move.select if cfg["skip_move"] else self.skip_move.deselect)()
        (self.bg_mode.select if cfg["bg_mode"] else self.bg_mode.deselect)()
        (self.bg_lock.select if cfg["bg_lock"] else self.bg_lock.deselect)()
        self._update_window_target()
        if cfg.get("bg_lock") and cfg.get("win_title"):
            for disp, hwnd in self._windows.items():
                if disp.split("  ·#")[0].rstrip("…") in cfg["win_title"] \
                        or cfg["win_title"][:40] in disp:
                    self.win_menu.set(disp)
                    break

    def _update_action_buttons(self):
        has = bool(self.recorded_events)
        for b in (self.save_btn, self.edit_btn, self.play_btn):
            b.configure(state="normal" if has else "disabled")
        self.delete_btn.configure(
            state="normal" if (self.current_name and eng.macro_exists(self.current_name))
            else "disabled")

    def _selected_name(self):
        return (self.current_name or self.name_entry.get().strip() or "macro")

    def _update_play_button(self):
        """Reflect whether the *selected* macro is currently running."""
        running = self._selected_name() in self.runners
        if running:
            self.play_btn.configure(text="■  Stop", fg_color=REC, hover_color=REC_HOVER)
        else:
            self.play_btn.configure(text="▶  Play", fg_color=ACCENT,
                                    hover_color=ACCENT_HOVER)

    # ------------------------------------------------------------- record
    def _toggle_record(self):
        if self.state_mode == "RECORDING":
            self._stop_record()
        elif self.state_mode == "IDLE":
            self._start_record()

    def _start_record(self):
        if self.runners:
            self._set_status("Stop running macros before recording", REC, REC)
            return
        self.state_mode = "RECORDING"
        self._record_done = False
        kbd_only = bool(self.kbd_only.get())
        track = bool(self.track_window.get())
        # F9 stops recording via the recorder's own listener (and is swallowed,
        # so it never lands in the macro).
        self.recorder = eng.Recorder(stop_key=token_to_key(self.record_token),
                                     record_mouse=not kbd_only, track_window=track)
        self._record_start = time.perf_counter()
        self.recorder.start()
        self.record_btn.configure(text="■  Stop Recording")
        for b in (self.save_btn, self.edit_btn, self.delete_btn, self.play_btn):
            b.configure(state="disabled")
        self.name_entry.configure(state="disabled")
        self.macro_menu.configure(state="disabled")
        self.kbd_only.configure(state="disabled")
        self.track_window.configure(state="disabled")
        msg = ("Recording keyboard only… type freely" if kbd_only
               else "Recording… move/click/type freely")
        self._set_status(msg, WARN, REC)
        self._poll_record()

    def _poll_record(self):
        if self.state_mode != "RECORDING":
            return
        if self.recorder and not self.recorder.is_running:   # F9 / external stop
            self._finalize_record()
            return
        n = len(self.recorder.events) if self.recorder else 0
        elapsed = time.perf_counter() - self._record_start
        self.record_info.configure(
            text=f"● {n} events · {elapsed:.1f}s   ({token_label(self.record_token)} to stop)")
        self.after(100, self._poll_record)

    def _stop_record(self):
        if self.recorder:
            self.recorder.stop()
        self._finalize_record()

    def _finalize_record(self):
        if self._record_done:
            return
        self._record_done = True
        events = self.recorder.stop() if self.recorder else []
        self.recorded_events = events
        self.current_name = None
        self.saved = False
        dur = events[-1]["t"] if events else 0.0
        self.state_mode = "IDLE"
        self.record_btn.configure(text="●  Record")
        self.name_entry.configure(state="normal")
        self.macro_menu.configure(state="normal")
        self.kbd_only.configure(state="normal")
        self.track_window.configure(state="normal")
        self._update_action_buttons()
        self._update_play_button()
        if events:
            self.record_info.configure(
                text=f"Recorded {len(events)} events · {dur:.1f}s — not saved yet")
            self._set_status("Recorded — Save to keep it, or Edit", WARN, WARN)
        else:
            self.record_info.configure(text="Nothing recorded.")
            self._set_status("Empty recording", REC, REC)

    def _save_macro(self):
        if not self.recorded_events:
            self._set_status("Nothing to save", REC, REC)
            return
        name = self.name_entry.get().strip() or "macro"
        eng.save_macro(name, self.recorded_events)
        self._save_config(name)
        self.current_name = name
        self.saved = True
        n = len(self.recorded_events)
        self.record_info.configure(text=f"Saved {n} events → {name} (in macros.db)")
        self._refresh_macro_list(select=name)
        self._update_action_buttons()
        self._set_status(f"Saved '{name}'", OK, OK)

    def _save_config(self, name):
        opts = self._read_play_opts(quiet=True)
        if opts is None:
            return
        loops, speed, delay = opts
        win_title = ""
        if self.bg_mode.get() and self.bg_lock.get():
            disp = self.win_menu.get()
            win_title = disp.split("  ·#")[0] if disp else ""
        eng.set_config(name, {
            "loops": loops, "speed": speed, "loop_delay": delay,
            "skip_move": int(bool(self.skip_move.get())),
            "bg_mode": int(bool(self.bg_mode.get())),
            "bg_lock": int(bool(self.bg_lock.get())),
            "win_title": win_title,
        })

    def _delete_macro(self):
        name = self.current_name
        if not name or not eng.macro_exists(name):
            return
        if name in self.runners:
            self._set_status(f"Stop '{name}' before deleting it", REC, REC)
            return
        eng.delete_macro(name)
        self.current_name = None
        self.recorded_events = None
        self.saved = True
        self._refresh_macro_list(select=NEW_LABEL)
        self._on_pick_macro(NEW_LABEL)
        self._set_status(f"Deleted '{name}'", WARN, WARN)

    # ---------------------------------------------------- window targeting
    def _toggle_bg(self):
        self._update_window_target()

    def _toggle_lock(self):
        self._update_window_target()

    def _update_window_target(self):
        if self.bg_mode.get():
            self.bg_box.pack(fill="x", padx=14, pady=(6, 0), before=self.play_btn)
            locked = bool(self.bg_lock.get())
            state = "normal" if locked else "disabled"
            self.win_menu.configure(state=state)
            self.win_refresh.configure(state=state)
            if locked:
                self.bg_hint.configure(
                    text="Sends only to this one window. Anti-cheat online games "
                         "ignore posted input.")
                self._refresh_windows()
            else:
                self.bg_hint.configure(
                    text="Routes each action to whatever window is under it — works "
                         "across several apps. Tick 'Lock' to confine it to one.")
        else:
            self.bg_box.pack_forget()

    def _refresh_windows(self):
        wins = winsend.list_windows()
        self._windows = {}
        values = []
        for hwnd, title in wins:
            label = title if len(title) <= 48 else title[:47] + "…"
            disp = f"{label}  ·#{hwnd}"
            self._windows[disp] = hwnd
            values.append(disp)
        if not values:
            values = ["(no windows found)"]
        self.win_menu.configure(values=values)
        self.win_menu.set(values[0])

    def _read_play_opts(self, quiet=False):
        try:
            loops = int(float(self.loops_entry.get()))
            speed = float(self.speed_entry.get())
            delay = float(self.delay_entry.get())
            if loops < 0 or speed <= 0 or delay < 0:
                raise ValueError
            return loops, speed, delay
        except ValueError:
            if not quiet:
                self._set_status("Bad input: loops≥0, speed>0, delay≥0", REC, REC)
            return None

    # ------------------------------------------------ multi-macro playback
    def _toggle_play(self):
        name = self._selected_name()
        if name in self.runners:
            self._stop_runner(name)
        else:
            self._start_runner(name)

    def _start_runner(self, name):
        if name in self.runners:
            return
        events = self.recorded_events
        if not events:
            self._set_status("No macro loaded — record or pick one first", REC, REC)
            return
        opts = self._read_play_opts()
        if opts is None:
            return
        loops, speed, delay = opts

        target_hwnd = None
        bg_follow = False
        if self.bg_mode.get():
            if self.bg_lock.get():
                target_hwnd = self._windows.get(self.win_menu.get())
                if not target_hwnd:
                    self._set_status("Pick a window to lock to (⟳ to refresh)", REC, REC)
                    return
            else:
                bg_follow = True
        if self.current_name:
            self._save_config(self.current_name)

        runner = sched.MacroRunner(
            name, list(events),
            {"loops": loops, "speed": speed, "loop_delay": delay,
             "skip_move": bool(self.skip_move.get()),
             "target_hwnd": target_hwnd, "bg_follow": bg_follow},
            self.scheduler,
            on_state=lambda r: self._ui_queue.put(("state", r, None)),
            on_tick=lambda r, rem: self._ui_queue.put(("tick", r, rem)),
            on_done=lambda r: self._ui_queue.put(("done", r, None)))
        self.runners[name] = runner
        runner.start()
        self._update_play_button()
        self._refresh_run_panel()
        self._refresh_recommended()
        self._set_status(f"Started '{name}' — {len(self.runners)} running", ACCENT, ACCENT)

    def _stop_runner(self, name):
        r = self.runners.get(name)
        if r:
            r.stop()
            self._set_status(f"Stopping '{name}'…", WARN, WARN)

    # -- main-thread poller: drains runner events safely -----------------
    def _drain_ui_queue(self):
        try:
            while True:
                kind, runner, data = self._ui_queue.get_nowait()
                try:
                    if kind == "state":
                        self._on_runner_state(runner)
                    elif kind == "tick":
                        self._on_runner_tick(runner, data)
                    elif kind == "done":
                        self._on_runner_done(runner)
                except Exception:
                    pass
        except queue.Empty:
            pass
        self.after(80, self._drain_ui_queue)

    # -- runner callbacks (run on the UI thread via the poller) ----------
    def _on_runner_state(self, runner):
        self._update_run_row(runner)
        if runner.name == self._selected_name():
            self._update_play_button()

    def _on_runner_tick(self, runner, remaining):
        self._update_run_row(runner, remaining)

    def _on_runner_done(self, runner):
        self.runners.pop(runner.name, None)
        self._refresh_run_panel()
        self._update_play_button()
        self._refresh_recommended()
        if not self.runners:
            self._set_status("All macros stopped", OK, OK)

    # -- running panel ----------------------------------------------------
    def _state_text(self, runner, remaining=None):
        st = runner.state
        if st == "running":
            return "▶ running…", ACCENT
        if st == "delay":
            rem = remaining if remaining is not None else (runner.remaining_delay() or 0)
            return f"⏳ next in {int(rem + 0.999)}s", WARN
        if st == "waiting":
            return "⌛ waiting for slot", WARN
        return st, "gray60"

    def _refresh_run_panel(self):
        for w in self.run_list.winfo_children():
            w.destroy()
        self.run_rows = {}
        if not self.runners:
            self.run_card.pack_forget()
            return
        self.run_card.pack(fill="x", pady=(10, 6))
        for name, runner in self.runners.items():
            row = ctk.CTkFrame(self.run_list, fg_color=("gray90", "gray19"),
                               corner_radius=6)
            row.pack(fill="x", pady=2)
            nm = ctk.CTkLabel(row, text=name, anchor="w",
                              font=ctk.CTkFont(size=12, weight="bold"))
            nm.pack(side="left", padx=(10, 6), pady=5)
            txt, col = self._state_text(runner)
            lbl = ctk.CTkLabel(row, text=txt, text_color=col,
                               font=ctk.CTkFont(size=12))
            lbl.pack(side="left")
            ctk.CTkButton(row, text="■", width=30, height=26, fg_color=REC,
                          hover_color=REC_HOVER,
                          command=lambda n=name: self._stop_runner(n)).pack(
                              side="right", padx=(2, 8))
            self.run_rows[name] = lbl

    def _update_run_row(self, runner, remaining=None):
        lbl = self.run_rows.get(runner.name)
        if lbl is None:
            self._refresh_run_panel()
            return
        txt, col = self._state_text(runner, remaining)
        lbl.configure(text=txt, text_color=col)

    # -- recommended loop delay ------------------------------------------
    def _recommended_delay(self):
        """Delay (s) so this macro interleaves with the others now running."""
        others = [r for n, r in self.runners.items() if n != self._selected_name()]
        if not others:
            return None
        return sum(r.exec_time for r in others) + sched.SAFETY * (len(others) + 1) + 0.5

    def _refresh_recommended(self):
        rec = self._recommended_delay()
        if rec is None:
            self.rec_delay.configure(text="")
        else:
            self.rec_delay.configure(
                text=f"↳ recommended loop delay ≥ {int(rec + 0.999)}s "
                     f"(click to apply) — fits the {len(self.runners)} running")

    def _apply_recommended_delay(self):
        rec = self._recommended_delay()
        if rec is not None:
            self._set_entry(self.delay_entry, int(rec + 0.999))

    # -------------------------------------------------------------- editor
    def _open_editor(self):
        if not self.recorded_events:
            self._set_status("Nothing to edit", REC, REC)
            return
        if self._selected_name() in self.runners:
            self._set_status("Stop this macro before editing it", REC, REC)
            return
        if self.editor is not None and self.editor.winfo_exists():
            self.editor.focus()
            return
        self.editor = MacroEditor(self, list(self.recorded_events))

    def _apply_edits(self, events):
        """Called by the editor when the user applies changes."""
        self.recorded_events = events
        self.saved = False
        self._update_action_buttons()
        dur = (events[-1]["t"] if events else 0.0) + sum(
            e.get("d", 0.0) for e in events if e.get("type") == "wait")
        self.record_info.configure(
            text=f"Edited — {len(events)} events · ~{dur:.1f}s — not saved yet")
        self._set_status("Edits applied — Save to keep them", WARN, WARN)

    # ------------------------------------------------------- global hotkey
    def _on_global_key(self, key):
        if not self._logged_first_key:      # one-time proof that events arrive
            self._logged_first_key = True
            _log("first global key event received — hook is working")
        if self._capturing:                 # the Hotkeys dialog is grabbing a key
            return
        tok = key_token(key)
        if tok is None:
            return
        if tok == self.record_token:
            if self.state_mode == "IDLE":
                self.after(0, self._start_record)
            elif self.state_mode == "RECORDING":
                self.after(0, self._stop_record)
        elif tok == self.play_token:
            if self.state_mode == "IDLE":       # toggle the selected macro
                self.after(0, self._toggle_play)

    # ------------------------------------------------------------- hotkeys
    def _open_hotkeys(self):
        if self.state_mode != "IDLE":
            self._set_status("Finish recording/playback first", REC, REC)
            return
        if self.hotkey_dialog is not None and self.hotkey_dialog.winfo_exists():
            self.hotkey_dialog.focus()
            return
        self.hotkey_dialog = HotkeySettings(self)

    def _apply_hotkeys(self, record_token, play_token):
        """Called by the Hotkeys dialog when the user saves new bindings."""
        self.record_token = record_token
        self.play_token = play_token
        eng.set_setting("hotkey_record", record_token)
        eng.set_setting("hotkey_play", play_token)
        self._refresh_hotkey_labels()
        self._set_status(
            f"Hotkeys: {token_label(record_token)} record · "
            f"{token_label(play_token)} play", OK, OK)

    # ------------------------------------------------------ ui persistence
    _UI_CHECKS = (
        ("ui_skip", "skip_move"), ("ui_kbd", "kbd_only"),
        ("ui_track", "track_window"), ("ui_bg", "bg_mode"),
        ("ui_bglock", "bg_lock"),
    )

    def _restore_ui_state(self):
        self._set_entry(self.loops_entry, eng.get_setting("ui_loops", "1"))
        self._set_entry(self.speed_entry, eng.get_setting("ui_speed", "1.0"))
        self._set_entry(self.delay_entry, eng.get_setting("ui_delay", "0"))
        for key, attr in self._UI_CHECKS:
            cb = getattr(self, attr)
            (cb.select if eng.get_setting(key, "0") == "1" else cb.deselect)()
        self._update_window_target()      # reflect bg-box visibility + picker state

    def _save_ui_state(self):
        eng.set_setting("ui_loops", self.loops_entry.get())
        eng.set_setting("ui_speed", self.speed_entry.get())
        eng.set_setting("ui_delay", self.delay_entry.get())
        for key, attr in self._UI_CHECKS:
            eng.set_setting(key, int(bool(getattr(self, attr).get())))

    # -------------------------------------------------------------- close
    def _on_close(self):
        try:
            self._save_ui_state()
        except Exception:
            pass
        for r in list(self.runners.values()):    # stop all running macros
            try:
                r.stop()
            except Exception:
                pass
        if self.recorder:
            try:
                self.recorder.stop()
            except Exception:
                pass
        try:
            self._hotkey_listener.stop()
        except Exception:
            pass
        self.destroy()


# ---------------------------------------------------------------------------
# Step editor
# ---------------------------------------------------------------------------
class MacroEditor(ctk.CTkToplevel):
    """Edit a macro's event list: insert waits, delete events, trim moves."""

    def __init__(self, app, events):
        super().__init__(app)
        self.app = app
        self.events = events
        self.title("Edit macro")
        self.geometry("560x640")
        self.minsize(500, 520)
        self.transient(app)

        self._build_ui()
        self._rebuild_rows()

    def _build_ui(self):
        pad = 14
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=pad, pady=(pad, 6))
        ctk.CTkLabel(bar, text="Wait (s):",
                     font=ctk.CTkFont(size=12)).pack(side="left")
        self.wait_entry = ctk.CTkEntry(bar, width=64, height=30, justify="center")
        self.wait_entry.insert(0, "1.0")
        self.wait_entry.pack(side="left", padx=(6, 10))
        ctk.CTkButton(bar, text="＋ wait at end", height=30, width=104,
                      command=self._add_wait_end).pack(side="left")
        ctk.CTkButton(bar, text="📋 Paste script", height=30, width=116,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      command=self._add_paste_end).pack(side="left", padx=(8, 0))
        ctk.CTkButton(bar, text="Remove all moves", height=30, width=130,
                      fg_color="transparent", border_width=2,
                      text_color=("gray10", "gray90"),
                      hover_color=("gray80", "gray25"),
                      command=self._remove_moves).pack(side="right")

        ctk.CTkLabel(self,
                     text="＋wait / 📋 insert a step after this one · ✕ delete it",
                     text_color="gray60", font=ctk.CTkFont(size=11)).pack(
                         anchor="w", padx=pad)

        self.list_frame = ctk.CTkScrollableFrame(self, label_text="")
        self.list_frame.pack(fill="both", expand=True, padx=pad, pady=(6, 6))

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(fill="x", padx=pad, pady=(0, pad))
        self.count_lbl = ctk.CTkLabel(foot, text="", text_color="gray60",
                                      font=ctk.CTkFont(size=12))
        self.count_lbl.pack(side="left")
        ctk.CTkButton(foot, text="Apply & Save", height=36, fg_color=OK,
                      hover_color="#268554", command=self._apply_and_save).pack(
                          side="right")
        ctk.CTkButton(foot, text="Apply", height=36, command=self._apply).pack(
                          side="right", padx=(0, 8))
        ctk.CTkButton(foot, text="Close", height=36, fg_color="transparent",
                      border_width=2, text_color=("gray10", "gray90"),
                      hover_color=("gray80", "gray25"),
                      command=self.destroy).pack(side="right", padx=(0, 8))

    # ----------------------------------------------------- row management
    def _grouped_rows(self):
        """Collapse runs of consecutive mouse-move events into one row."""
        rows = []
        i = 0
        n = len(self.events)
        while i < n:
            if self.events[i].get("type") == "move":
                j = i
                while j < n and self.events[j].get("type") == "move":
                    j += 1
                t0 = self.events[i]["t"]
                t1 = self.events[j - 1]["t"]
                rows.append({
                    "indices": list(range(i, j)),
                    "label": f"{t0:7.2f}s   🖱  {j - i} mouse moves  ({t0:.2f}–{t1:.2f}s)",
                    "color": None,
                })
                i = j
            else:
                ev = self.events[i]
                typ = ev.get("type")
                rows.append({
                    "indices": [i],
                    "label": f"{ev['t']:7.2f}s   {describe_event(ev)}",
                    "color": WARN if typ == "wait" else ACCENT if typ == "paste" else None,
                })
                i += 1
        return rows

    def _rebuild_rows(self):
        for w in self.list_frame.winfo_children():
            w.destroy()
        rows = self._grouped_rows()
        for row in rows:
            line = ctk.CTkFrame(self.list_frame, fg_color=("gray92", "gray17"),
                                corner_radius=6)
            line.pack(fill="x", pady=2)
            ctk.CTkLabel(line, text=row["label"], anchor="w", text_color=row["color"],
                         font=ctk.CTkFont(size=12, family="Consolas")).pack(
                             side="left", padx=10, pady=5)
            last_idx = row["indices"][-1]
            ctk.CTkButton(line, text="✕", width=28, height=26, fg_color=REC,
                          hover_color=REC_HOVER,
                          command=lambda idx=row["indices"]: self._delete(idx)).pack(
                              side="right", padx=(2, 8))
            ctk.CTkButton(line, text="📋", width=34, height=26,
                          fg_color="transparent", border_width=1,
                          text_color=("gray10", "gray90"),
                          hover_color=("gray80", "gray30"),
                          command=lambda idx=last_idx: self._insert_paste_after(idx)).pack(
                              side="right", padx=2)
            ctk.CTkButton(line, text="＋wait", width=52, height=26,
                          fg_color="transparent", border_width=1,
                          text_color=("gray10", "gray90"),
                          hover_color=("gray80", "gray30"),
                          command=lambda idx=last_idx: self._insert_wait_after(idx)).pack(
                              side="right", padx=2)
        dur = (self.events[-1]["t"] if self.events else 0.0) + sum(
            e.get("d", 0.0) for e in self.events if e.get("type") == "wait")
        self.count_lbl.configure(text=f"{len(self.events)} events · ~{dur:.1f}s")

    # -------------------------------------------------------- operations
    def _wait_value(self):
        try:
            d = float(self.wait_entry.get())
            return d if d > 0 else None
        except ValueError:
            return None

    def _delete(self, indices):
        keep = set(range(len(self.events))) - set(indices)
        self.events = [e for i, e in enumerate(self.events) if i in keep]
        self._rebuild_rows()

    def _insert_wait_after(self, idx):
        d = self._wait_value()
        if d is None:
            self.count_lbl.configure(text="Enter a positive wait value first")
            return
        t = self.events[idx]["t"] if self.events else 0.0
        self.events.insert(idx + 1, {"t": t, "type": "wait", "d": d})
        self._rebuild_rows()

    def _add_wait_end(self):
        d = self._wait_value()
        if d is None:
            self.count_lbl.configure(text="Enter a positive wait value first")
            return
        t = self.events[-1]["t"] if self.events else 0.0
        self.events.append({"t": t, "type": "wait", "d": d})
        self._rebuild_rows()

    def _remove_moves(self):
        self.events = [e for e in self.events if e.get("type") != "move"]
        self._rebuild_rows()

    # ----------------------------------------------------- paste scripts
    def _insert_paste(self, idx, at_end=False):
        def on_text(text):
            if not text:
                return
            t = (self.events[-1]["t"] if at_end else self.events[idx]["t"]) \
                if self.events else 0.0
            pos = len(self.events) if at_end else idx + 1
            self.events.insert(pos, {"t": t, "type": "paste", "text": text})
            self._rebuild_rows()
        PasteScriptDialog(self, on_text)

    def _insert_paste_after(self, idx):
        self._insert_paste(idx, at_end=False)

    def _add_paste_end(self):
        self._insert_paste(0, at_end=True)

    def _apply(self):
        self.app._apply_edits(list(self.events))

    def _apply_and_save(self):
        self.app._apply_edits(list(self.events))
        self.app._save_macro()
        self.destroy()


# ---------------------------------------------------------------------------
# Paste-script dialog
# ---------------------------------------------------------------------------
class PasteScriptDialog(ctk.CTkToplevel):
    """Type or paste a block of text to be saved as a paste step."""

    def __init__(self, editor, on_text, initial=""):
        super().__init__(editor)
        self.on_text = on_text
        self.title("Paste script")
        self.geometry("520x420")
        self.minsize(420, 320)
        self.transient(editor)

        pad = 14
        ctk.CTkLabel(self, text="📋  Paste-script step",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(
                         anchor="w", padx=pad, pady=(pad, 0))
        ctk.CTkLabel(self,
                     text="This text is saved with the macro. On playback it's put on "
                          "the clipboard and pasted (Ctrl+V) wherever the cursor is.",
                     text_color="gray60", font=ctk.CTkFont(size=11),
                     wraplength=470, justify="left").pack(anchor="w", padx=pad, pady=(2, 8))

        self.box = ctk.CTkTextbox(self, wrap="word", font=ctk.CTkFont(size=13))
        self.box.pack(fill="both", expand=True, padx=pad, pady=(0, 8))
        self.box.insert("1.0", initial)
        self.box.focus()

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(fill="x", padx=pad, pady=(0, pad))
        ctk.CTkButton(foot, text="Save step", height=36, fg_color=OK,
                      hover_color="#268554", command=self._save).pack(side="right")
        ctk.CTkButton(foot, text="Cancel", height=36, fg_color="transparent",
                      border_width=2, text_color=("gray10", "gray90"),
                      hover_color=("gray80", "gray25"),
                      command=self.destroy).pack(side="right", padx=(0, 8))

    def _save(self):
        text = self.box.get("1.0", "end-1c")
        self.destroy()
        self.on_text(text)


# ---------------------------------------------------------------------------
# Hotkey settings dialog
# ---------------------------------------------------------------------------
class HotkeySettings(ctk.CTkToplevel):
    """Rebind the global record / play hotkeys. Click a field, press a key."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.record_token = app.record_token
        self.play_token = app.play_token
        self._capture_listener = None
        self._capture_target = None       # "record" | "play" while listening

        self.title("Hotkeys")
        self.geometry("380x300")
        self.resizable(False, False)
        self.transient(app)
        self.protocol("WM_DELETE_WINDOW", self._close)

        pad = 18
        ctk.CTkLabel(self, text="⌨  Global hotkeys",
                     font=ctk.CTkFont(size=18, weight="bold")).pack(
                         anchor="w", padx=pad, pady=(pad, 0))
        ctk.CTkLabel(self, text="Click a button, then press the key you want.",
                     text_color="gray60", font=ctk.CTkFont(size=12)).pack(
                         anchor="w", padx=pad, pady=(2, 10))

        self.record_btn = self._row("Record start / stop", "record")
        self.play_btn = self._row("Play start / stop", "play")

        ctk.CTkLabel(self, text="Tip: F1–F12 work best — other keys get typed "
                                "into the recording.",
                     text_color="gray55", font=ctk.CTkFont(size=11),
                     wraplength=330, justify="left").pack(
                         anchor="w", padx=pad, pady=(10, 0))

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(side="bottom", fill="x", padx=pad, pady=pad)
        ctk.CTkButton(foot, text="Save", height=36, fg_color=OK,
                      hover_color="#268554", command=self._save).pack(side="right")
        ctk.CTkButton(foot, text="Cancel", height=36, fg_color="transparent",
                      border_width=2, text_color=("gray10", "gray90"),
                      hover_color=("gray80", "gray25"),
                      command=self._close).pack(side="right", padx=(0, 8))
        self.msg = ctk.CTkLabel(foot, text="", text_color=WARN,
                                font=ctk.CTkFont(size=11))
        self.msg.pack(side="left")

        self._refresh()

    def _row(self, label, which):
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=4)
        ctk.CTkLabel(row, text=label, font=ctk.CTkFont(size=13)).pack(side="left")
        btn = ctk.CTkButton(row, text="", width=110, height=34,
                            command=lambda: self._begin_capture(which))
        btn.pack(side="right")
        return btn

    def _refresh(self):
        self.record_btn.configure(text=token_label(self.record_token))
        self.play_btn.configure(text=token_label(self.play_token))

    # ------------------------------------------------------- key capture
    def _begin_capture(self, which):
        if self._capture_listener is not None:
            return
        self._capture_target = which
        self.app._capturing = True        # silence the app's global hotkeys
        btn = self.record_btn if which == "record" else self.play_btn
        btn.configure(text="press a key…")
        self.msg.configure(text="Esc to cancel")
        self._capture_listener = keyboard.Listener(on_press=self._on_capture)
        self._capture_listener.start()

    def _on_capture(self, key):
        tok = key_token(key)
        self.after(0, lambda: self._finish_capture(tok))
        return False                       # stop this one-shot listener

    def _finish_capture(self, tok):
        if self._capture_listener is not None:
            try:
                self._capture_listener.stop()
            except Exception:
                pass
            self._capture_listener = None
        self.app._capturing = False
        which = self._capture_target
        self._capture_target = None
        self.msg.configure(text="")
        if tok is None or tok == "esc":    # cancelled
            self._refresh()
            return
        if which == "record":
            self.record_token = tok
        else:
            self.play_token = tok
        self._refresh()

    # -------------------------------------------------------------- save
    def _save(self):
        if self._capture_listener is not None:
            self.msg.configure(text="Finish setting the key first")
            return
        if self.record_token == self.play_token:
            self.msg.configure(text="Record and play must differ")
            return
        self.app._apply_hotkeys(self.record_token, self.play_token)
        self._close()

    def _close(self):
        if self._capture_listener is not None:
            try:
                self._capture_listener.stop()
            except Exception:
                pass
        self.app._capturing = False
        self.app.hotkey_dialog = None
        self.destroy()


if __name__ == "__main__":
    try:
        MacroApp().mainloop()
    except Exception as e:
        _log(f"FATAL: {e!r}")
        raise
