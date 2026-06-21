# 🎬 kalikot — Macro Recorder & Multi-Macro Player

**Record your keyboard & mouse, edit the steps, then replay — loop it, speed it,
run it in the background, or run several macros at once.**

*kalikot* (Tagalog for *tinkering / fiddling with something*) is a Windows
action-replay tool. It records keystrokes, clicks, mouse movement and scrolling
with real timings, lets you fine-tune the steps (insert waits, paste blocks of
text, trim jitter), and replays them with loops, speed control and a live
loop-delay countdown. Everything is stored in a portable SQLite file, and a
standalone `.exe` means there's nothing to install on the machines you share it
with.

## 🛠️ Built With

- Python 3
- [pynput](https://pypi.org/project/pynput/) — global keyboard/mouse capture & playback
- [customtkinter](https://pypi.org/project/customtkinter/) — the GUI
- SQLite (`sqlite3`, bundled with Python) — portable storage
- PyInstaller — standalone `.exe` packaging
- Win32 API (`ctypes`) — window targeting, activation & clipboard

## ✨ Features

- 🎥 **Record** keystrokes, clicks, mouse moves & scroll with real timings — or
  **keyboard-only** to leave the mouse out.
- ✏️ **Step editor** — insert **waits**, add **📋 paste-script** steps (a saved
  block of text pasted via Ctrl+V), delete events, or strip mouse jitter.
- 🔁 **Loops** (count or forever), **speed** control, and a **loop-delay
  countdown** so you know when the next run fires.
- 🪟 **Multi-app playback**
  - *Watch it run* — foreground replay that follows whatever window each action
    lands on, across several apps.
  - *Background mode* — hands-free; routes each action to the window under it, or
    **lock to one specific window**.
- 🧩 **Run several macros at once** — each saved macro has its own settings and
  Play/Stop state. They cooperatively time-share the one keyboard/mouse, and a
  **RUNNING** panel shows each one's live state (running / next-loop countdown /
  waiting), with a recommended loop delay to help them interleave.
- ⌨️ **Configurable global hotkeys** (default **F9** record, **F8** play) that
  work even when the window is unfocused — each person can rebind their own.
- 💾 **Portable** — macros and per-macro settings live in a single `local.db`;
  the standalone `dist\kalikot.exe` needs no Python.

## 🔧 Setup

**Easiest — no install needed:** run the standalone **`dist\kalikot.exe`**.
It bundles Python and every dependency. On another PC, run *that* file (not
`gui.bat` or the `.py` files, which need Python installed).

**To run from source** (Python 3 required), install the dependencies once:

```bash
install.bat          # double-click — installs everything via pip
# or:
pip install -r requirements.txt
```

Then launch:

```bash
python macro_gui.py   # or double-click gui.bat
```

## 🚀 Usage

1. Type a **name**, tick **Keyboard only** if you don't want the mouse recorded,
   then **Record** (or press **F9**). Press **F9** again to stop.
2. Hit **Edit** to insert waits, paste-script steps, or trim mouse moves.
3. Set **Loops** (0 = forever), **Speed**, **Loop delay**, then **Play**
   (or press **F8**). Stop with the button or **F8**.
4. To run more than one macro, save each, then Play them — switch the selector to
   manage each independently while the others keep running.

### Command line

```bash
python macro.py record mymacro      # record, F9 to stop
python macro.py play   mymacro      # replay once
python macro.py list                # list saved macros
python macro.py show   mymacro      # print a macro's JSON
python macro.py delete mymacro      # delete a saved macro
```

## ⌨️ Hotkeys

| Key | Action |
|-----|--------|
| **F9** | Start / stop recording *(rebindable)* |
| **F8** | Start / stop playback of the selected macro *(rebindable)* |

Click **⌨ Hotkeys** in the app to rebind them — stick to **F1–F12** so the key
isn't typed into the recording.

## 📝 Notes

- For games/apps that block synthetic input (**anti-cheat**), playback and global
  hotkeys may not register — try **Run as administrator**, and note that
  anti-cheat online games will ignore posted input by design.
- Building the `.exe` yourself: `pip install pyinstaller` then
  `python -m PyInstaller kalikot.spec`.

## 👨‍💻 Author

**John Venice Almazan** — [@kalikot26](https://github.com/kalikot26)
