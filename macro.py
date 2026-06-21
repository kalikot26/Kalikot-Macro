#!/usr/bin/env python3
"""
macro.py - command-line front end for the action-replay macro tool.

For the graphical version, run:  python macro_gui.py

Usage:
    python macro.py record [name]            Record a new macro (stop with F9)
    python macro.py play   [name] [opts]     Replay a macro (stop with F8/F10)
    python macro.py list                     List saved macros
    python macro.py show   [name]            Print a macro's JSON

Play options:
    --loops N        Repeat N times (0 = forever). Default 1.
    --speed X        Speed multiplier, e.g. 2 = twice as fast. Default 1.
    --loop-delay S   Seconds to wait between loops. Default 0.
    --no-move        Skip recorded mouse movement on replay (clicks only).
    --countdown N    Seconds to count down before starting. Default 3.

Hotkeys:
    F9             stop recording
    F8 or F10      stop playback   (user mouse/keyboard activity does NOT stop it)
"""

import argparse
import sys
import threading
import time

from pynput import keyboard
from pynput.keyboard import Key

import macro_engine as eng


def countdown(seconds, label):
    if seconds <= 0:
        return
    print(f"{label} in...", end=" ", flush=True)
    for i in range(seconds, 0, -1):
        print(i, end=" ", flush=True)
        time.sleep(1)
    print("GO!", flush=True)


def record(name, countdown_s=3):
    rec = eng.Recorder(stop_key=Key.f9)
    print(f"Recording macro '{name}'.  Press F9 to stop.")
    countdown(countdown_s, "Recording starts")
    rec.start()
    while rec.is_running:
        time.sleep(0.05)
    events = rec.stop()
    path = eng.save_macro(name, events)
    dur = events[-1]["t"] if events else 0.0
    print(f"\nSaved {len(events)} events ({dur:.1f}s) -> {path}")


def play(name, loops=1, speed=1.0, loop_delay=0.0, skip_move=False, countdown_s=3):
    if not eng.macro_exists(name):
        sys.exit(f"No macro named '{name}' found in {eng.DB_PATH}")
    data = eng.load_macro(name)
    events = data["events"]

    stop_event = threading.Event()
    stop_keys = {Key.f8, Key.f10}

    def on_press(key):
        if key in stop_keys:
            stop_event.set()
            return False

    listener = keyboard.Listener(on_press=on_press)
    listener.start()

    forever = (loops == 0)
    label = "forever" if forever else f"{loops} time(s)"
    print(f"Playing '{name}' {label} at {speed}x.  Press F8 or F10 to stop.")
    countdown(countdown_s, "Playback starts")

    def on_loop(i, total):
        print(f"  loop {i}{'' if total == 0 else f'/{total}'}")

    eng.Player().play(events, loops=loops, speed=speed, loop_delay=loop_delay,
                      skip_move=skip_move, stop_event=stop_event, on_loop=on_loop)
    listener.stop()
    print("Stopped." if stop_event.is_set() else "Done.")


def list_macros():
    rows = eng.list_macros()
    if not rows:
        print("No macros recorded yet.")
        return
    print(f"Macros in {eng.DB_PATH}:")
    for name, count, dur in rows:
        c = "?" if count < 0 else count
        print(f"  {name:<24} {c} events, {dur:.1f}s")


def show(name):
    if not eng.macro_exists(name):
        sys.exit(f"No macro named '{name}' found.")
    import json
    print(json.dumps(eng.load_macro(name), indent=2))


def delete(name):
    if not eng.macro_exists(name):
        sys.exit(f"No macro named '{name}' found.")
    eng.delete_macro(name)
    print(f"Deleted '{name}'.")


def main():
    p = argparse.ArgumentParser(description="Simple action-replay macro tool (CLI).")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("record", help="record a new macro")
    pr.add_argument("name", nargs="?", default="macro")
    pr.add_argument("--countdown", type=int, default=3)

    pl = sub.add_parser("play", help="replay a macro")
    pl.add_argument("name", nargs="?", default="macro")
    pl.add_argument("--loops", type=int, default=1, help="0 = forever")
    pl.add_argument("--speed", type=float, default=1.0)
    pl.add_argument("--loop-delay", type=float, default=0.0)
    pl.add_argument("--no-move", action="store_true")
    pl.add_argument("--countdown", type=int, default=3)

    sub.add_parser("list", help="list saved macros")

    ps = sub.add_parser("show", help="print a macro's JSON")
    ps.add_argument("name", nargs="?", default="macro")

    pd = sub.add_parser("delete", help="delete a saved macro")
    pd.add_argument("name")

    args = p.parse_args()
    if args.cmd == "record":
        record(args.name, countdown_s=args.countdown)
    elif args.cmd == "play":
        play(args.name, loops=args.loops, speed=args.speed,
             loop_delay=args.loop_delay, skip_move=args.no_move,
             countdown_s=args.countdown)
    elif args.cmd == "list":
        list_macros()
    elif args.cmd == "show":
        show(args.name)
    elif args.cmd == "delete":
        delete(args.name)


if __name__ == "__main__":
    main()
