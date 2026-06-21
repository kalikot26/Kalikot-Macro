#!/usr/bin/env python3
"""
winsend.py - send input to a specific window via Windows messages.

This is the "background" playback path: instead of moving the real cursor and
typing into the focused window (which fights with the user), we PostMessage the
events straight to a target window's handle. The physical mouse/keyboard stay
free for the user.

Caveat: many games (especially anti-cheat protected ones) read raw/DirectInput
and ignore posted window messages, so this won't drive those. It works well for
ordinary desktop apps, browsers and many offline games.
"""

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# --- message + flag constants ---------------------------------------------
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_MOUSEWHEEL = 0x020A
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_CHAR = 0x0102

MK_LBUTTON, MK_RBUTTON, MK_MBUTTON = 0x0001, 0x0002, 0x0010

VK_CONTROL, VK_V = 0x11, 0x56
SW_RESTORE = 9
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

_BTN = {
    "left":   (WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON),
    "right":  (WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON),
    "middle": (WM_MBUTTONDOWN, WM_MBUTTONUP, MK_MBUTTON),
}


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                            ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
kernel32.GetCurrentThreadId.restype = wintypes.DWORD

user32.WindowFromPoint.argtypes = [POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
kernel32.GlobalFree.restype = ctypes.c_void_p

_EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


GA_ROOT = 2


def is_window(hwnd):
    return bool(hwnd) and bool(user32.IsWindow(hwnd))


def get_title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def get_class(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_from_point(x, y):
    """Top-level window handle at a screen coordinate (None if none)."""
    hwnd = user32.WindowFromPoint(POINT(int(x), int(y)))
    if not hwnd:
        return None
    root = user32.GetAncestor(hwnd, GA_ROOT)
    return root or hwnd


def window_at(x, y):
    """Identity (title + class) of the window at a screen coordinate."""
    hwnd = window_from_point(x, y)
    if not hwnd:
        return None
    return {"title": get_title(hwnd), "cls": get_class(hwnd)}


def find_window(info):
    """Find a current window matching a recorded {title, cls} identity."""
    if not info:
        return None
    title = (info.get("title") or "").strip()
    cls = info.get("cls") or ""
    if not title and not cls:
        return None
    wins = [(h, get_title(h), get_class(h)) for h, _t in list_windows()]
    for h, t, c in wins:                         # exact title + class
        if t == title and (not cls or c == cls):
            return h
    for h, t, c in wins:                         # exact title
        if t == title:
            return h
    if title:                                    # title substring (dynamic titles)
        for h, t, c in wins:
            if t and (title in t or t in title):
                return h
    if cls:                                      # last resort: same window class
        for h, t, c in wins:
            if c == cls:
                return h
    return None


def activate(hwnd):
    """Bring a window to the foreground so the user can watch the replay.

    Uses the AttachThreadInput trick to get around Windows' restriction that
    only the foreground process may call SetForegroundWindow.
    """
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        cur = kernel32.GetCurrentThreadId()
        fg = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(fg, None)
        tgt_thread = user32.GetWindowThreadProcessId(hwnd, None)
        attached_fg = attached_tgt = False
        if fg_thread and fg_thread != cur:
            attached_fg = bool(user32.AttachThreadInput(cur, fg_thread, True))
        if tgt_thread and tgt_thread not in (cur, fg_thread):
            attached_tgt = bool(user32.AttachThreadInput(cur, tgt_thread, True))
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        if attached_fg:
            user32.AttachThreadInput(cur, fg_thread, False)
        if attached_tgt:
            user32.AttachThreadInput(cur, tgt_thread, False)
        return True
    except Exception:
        return False


def set_clipboard_text(text):
    """Put unicode text on the Windows clipboard (for paste-script steps)."""
    if text is None:
        text = ""
    data = text.encode("utf-16-le") + b"\x00\x00"
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        return False
    ptr = kernel32.GlobalLock(h)
    if not ptr:
        kernel32.GlobalFree(h)
        return False
    ctypes.memmove(ptr, data, len(data))
    kernel32.GlobalUnlock(h)
    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(h)
        return False
    user32.EmptyClipboard()
    if not user32.SetClipboardData(CF_UNICODETEXT, h):
        kernel32.GlobalFree(h)          # ownership not taken; free it
        user32.CloseClipboard()
        return False
    user32.CloseClipboard()             # on success the system owns the memory
    return True


def list_windows():
    """Return [(hwnd, title), ...] for visible, titled top-level windows."""
    out = []

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n > 0:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value:
                out.append((hwnd, buf.value))
        return True

    user32.EnumWindows(_EnumProc(cb), 0)
    return out


def _lparam_xy(x, y):
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


class WinSender:
    """Posts mouse/keyboard messages to one target window handle."""

    def __init__(self, hwnd):
        self.hwnd = hwnd

    def alive(self):
        return bool(user32.IsWindow(self.hwnd))

    def _client_xy(self, x, y):
        pt = POINT(int(x), int(y))
        user32.ScreenToClient(self.hwnd, ctypes.byref(pt))
        return pt.x, pt.y

    def move(self, x, y):
        cx, cy = self._client_xy(x, y)
        user32.PostMessageW(self.hwnd, WM_MOUSEMOVE, 0, _lparam_xy(cx, cy))

    def click(self, x, y, button, pressed):
        down, up, mk = _BTN.get(button, _BTN["left"])
        cx, cy = self._client_xy(x, y)
        lp = _lparam_xy(cx, cy)
        user32.PostMessageW(self.hwnd, WM_MOUSEMOVE, 0, lp)
        if pressed:
            user32.PostMessageW(self.hwnd, down, mk, lp)
        else:
            user32.PostMessageW(self.hwnd, up, 0, lp)

    def scroll(self, x, y, dx, dy):
        # wheel uses SCREEN coordinates in lParam, delta in the high word
        delta = int(dy) * 120
        user32.PostMessageW(self.hwnd, WM_MOUSEWHEEL,
                            (delta & 0xFFFF) << 16, _lparam_xy(x, y))

    def paste(self):
        """Send Ctrl+V to the window (no WM_CHAR, so 'v' isn't typed)."""
        self.key(VK_CONTROL, None, True)
        self.key(VK_V, None, True)
        self.key(VK_V, None, False)
        self.key(VK_CONTROL, None, False)

    def key(self, vk, char, pressed):
        if vk is not None:
            if pressed:
                user32.PostMessageW(self.hwnd, WM_KEYDOWN, int(vk), 0x00000001)
            else:
                user32.PostMessageW(self.hwnd, WM_KEYUP, int(vk), 0xC0000001)
        # send the character on key-down so text fields receive it
        if pressed and char and char.isprintable():
            user32.PostMessageW(self.hwnd, WM_CHAR, ord(char), 0x00000001)
