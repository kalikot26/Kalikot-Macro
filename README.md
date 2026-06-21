# Simple Macro (action replay)

Records keystrokes, mouse clicks, mouse movement and scrolling with real
timings, lets you edit the steps (insert waits, trim jitter), and replays them
with loops + speed control. Everything is stored in a **portable SQLite
database** (`local.db`) next to the scripts — copy the folder and your macros
and per-macro play settings come with it.

## Setup

**Easiest — no install needed:** just run the standalone **`dist\kalikot.exe`**.
It bundles Python and all dependencies. (On another PC, run *that* file — not
`gui.bat` or the `.py` files, which need Python installed.)

**To run from source** (Python 3 required), install the dependencies once:

```
install.bat          (double-click — installs everything via pip)
```

or manually:

```
pip install -r requirements.txt
```

That installs `pynput` + `customtkinter`. `sqlite3` ships with Python, so
there's nothing else to install. To build the `.exe` yourself, also
`pip install pyinstaller` and run `python -m PyInstaller kalikot.spec`.

## GUI (recommended)

Double-click **`gui.bat`**, or run:

```
python macro_gui.py
```

- Pick a saved macro from the **dropdown**, or leave it on **+ New recording**.
- Type a **name**, hit **Record** (or **F9**), do your thing, then **F9** again.
- **Edit** opens the step editor: insert **waits** anywhere, add **📋 paste-script**
  steps (a saved block of text pasted via Ctrl+V on playback), delete events,
  or **Remove all moves** to drop mouse jitter.
- **Save** writes the macro + its Loops/Speed/Delay settings into `local.db`.
- **Track windows** (Record card) — records *which window* each click lands in.
  Turn this on when your task spans **several apps**; on replay each click's window
  is brought to the front first, so it runs across all of them where you can watch.
  (Found by window title/class, so it still works if the window moved.)
- Set **Loops** (0 = forever), **Speed**, **Loop delay**, then **Play** (or **F8**).
  A loop delay shows a live **countdown** so you know when the next run fires.
- **Background mode (hands-free)** — replays without taking over your cursor/keyboard.
  By default it **routes each action to its recorded window** (multi-app); tick
  **Lock to one specific window** to confine it to a single app you pick.
  (Anti-cheat online games ignore posted input.)
- Your play settings and checkboxes are **remembered** between runs (stored in
  `local.db`), and **each saved macro keeps its own** loops/speed/delay/options.

## Running several macros at once

Each saved macro plays independently — start one, switch the selector to another,
and the first keeps going. A **RUNNING** panel lists every active macro with its
live state (▶ running / ⏳ next-loop countdown / ⌛ waiting) and a stop button each.

Because there's only one real mouse/keyboard, macros **time-share**: while one is
in its loop delay, another may run — but only if it fits inside that remaining
delay (plus a small buffer), so nothing gets knocked off schedule. A macro that
can't fit yet shows **waiting for slot** until a gap opens (no loops are skipped).

Set each macro's **loop delay** long enough to leave room for the others. Click
the **recommended loop delay** hint under the loop fields to auto-fill a value
that fits the macros currently running.
- Playback **keeps running while you move the mouse or type** — it stops only via
  the **Stop** button or the global **F8** hotkey.
- **Delete** removes the selected macro. Light/dark toggle top-right (remembered).

## Use it (command line)

```
python macro.py record mymacro      # record, press F9 to stop
python macro.py play   mymacro      # replay once
python macro.py list                # see all macros
python macro.py show   mymacro      # print the raw JSON
python macro.py delete mymacro      # remove a saved macro
```

### Play options
```
--loops N        repeat N times (0 = forever)
--speed X        2 = twice as fast, 0.5 = half speed
--loop-delay S   seconds to pause between loops
--no-move        ignore recorded mouse movement (clicks still land)
--countdown N    seconds before it starts (default 3)
```
Example: `python macro.py play mymacro --loops 10 --speed 1.5 --loop-delay 2`

## Or just double-click
- `record.bat mymacro`  -> records
- `play.bat mymacro --loops 5`  -> replays

## Hotkeys (GUI — global, work even when unfocused)
- **F9** — start / stop recording *(default)*
- **F8** — start / stop playback *(default)*

Click **⌨ Hotkeys** (top of the window) to rebind either one — press the key you
want and Save. Your choice is stored in `local.db`, so each person can set their
own. Tip: stick to **F1–F12**; other keys get typed into the recording.

CLI: **F9** stops recording; **F8**/**F10** stop playback.

Note: ordinary user mouse/keyboard activity does **not** stop playback — only the
Stop button or the Stop hotkey does.

## Editing macros
Use the **Edit** button in the GUI to insert waits, delete steps, and trim mouse
moves. The data lives in `local.db` (SQLite); each event has a `t` (seconds from
start) and a type.

Event types: `key_press`, `key_release`, `move`, `click`, `scroll`, and `wait`
(an explicit pause of `d` seconds, scaled by the Speed setting on playback).

The legacy `macros\*.json` files are imported into `local.db` automatically on
first run; after that the database is the source of truth.

## Notes
- For macros that target other apps (games, etc.), run from a terminal started
  **as Administrator** if keystrokes don't register — some apps block input from
  non-elevated processes.
- Mouse movement is sampled ~every 15ms to keep files small; tweak
  `MOVE_SAMPLE_S` in `macro.py` if you want finer/coarser capture.
