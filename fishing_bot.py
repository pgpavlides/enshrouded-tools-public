"""Enshrouded fishing bot.

Cast: the loop starts with a cast (hold LMB 1 s, release): on arming (F8), after a catch, and
after a lost fish.
Mark: whenever the yellow mouse mark (white ring) shows in the middle of the screen, left click.
It shows on the bite and again after each drained stamina segment (some fish need 3 clicks).
Fight: starts when the bar at the top centre appears. The bar is two halves. Left half = your
line endurance (green, eaten by red from the left). Right half = fish stamina (red; turns
orange while you steer the right way, drained segments go dark). Hold A to start; whenever the
fish's red has not shrunk for SWITCH_AFTER seconds, switch to the other key.
Catch: once the bar is gone, the "You caught" popup shows on the right. Press E (Take; its LMB
option is Release, so the bot never clicks it), wait 0.5 s, then cast again: loop.
No popup within FIGHT_TIMEOUT seconds of the bar going: fish lost, cast again.
Fights are recorded to shots/fight_<time>/ (one frame every RECORD_EVERY seconds).

Run in a console: `python fishing_bot.py`. F8 arms/disarms (starts disarmed). Ctrl+C quits.
Only acts while Enshrouded is the active window. enshrouded_tools.pyw drives the same loop via run().
"""
import ctypes
import os
import time
from ctypes import wintypes

import cv2
import numpy as np
from PIL import ImageGrab

GAME_EXE = "enshrouded.exe"
SCAN_HZ = 20
CLICK_COOLDOWN = 1.5   # seconds after a click before the next one
MIN_YELLOW = 8         # yellow pixels an icon needs at 1080p (the mark has ~25+)
MAX_ICON_SIZE = 0.03   # an icon's yellow spans at most this fraction of the window height
POPUP_E_FILL = 0.35    # white fill above the icon that marks it as the popup's (E key block)
# Icon search area, as fractions of the game window: below the fight bar, above the HUD.
ICON_X = (0.30, 0.85)
ICON_Y = (0.22, 0.80)
# Marks are only clicked in the middle part (fish popups sit at ~69 % x; item popups further left).
ROI_X = (0.30, 0.66)
# Fight bar band (the bar sits at ~16 % of the height, centred on the window).
BAR_Y = (0.12, 0.20)
BAR_MIN_FILL = 0.08    # the bar row must be at least this much of the window width
FIGHT_TIMEOUT = 4.0    # seconds without the bar (and no catch popup) before giving the fight up
SWITCH_AFTER = 0.6     # seconds without the fish's red shrinking before switching A <-> D
PROGRESS_PX = 3        # red must shrink by more than this many pixels to count as progress
RECORD_EVERY = 0.25    # seconds between fight screenshots; 0 turns recording off
CAST_HOLD = 1.0        # seconds LMB is held to charge a cast
SHOTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shots")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)  # real pixels, not scaled ones

VK_F8 = 0x77
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x2, 0x4
KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x2, 0x8
SCAN = {"A": 0x1E, "D": 0x20, "E": 0x12}
OTHER = {"A": "D", "D": "A"}
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MAGIC = 0x454E5346  # tags our injected input, so enshrouded_tools' hooks do not take it for the player's
ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.GetClientRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
user32.ClientToScreen.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.POINT))
kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD))
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)


def game_window():
    """The Enshrouded window if it is the active one, else None."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return None
    buf = ctypes.create_unicode_buffer(260)
    size = wintypes.DWORD(len(buf))
    ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
    kernel32.CloseHandle(handle)
    if ok and os.path.basename(buf.value).lower() == GAME_EXE:
        return hwnd
    return None


def client_box(hwnd, xs, ys):
    """Screen bbox (left, top, right, bottom) of a fractional area of the game's client area."""
    rect = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    origin = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    w, h = rect.right, rect.bottom
    return (origin.x + int(w * xs[0]), origin.y + int(h * ys[0]),
            origin.x + int(w * xs[1]), origin.y + int(h * ys[1]))


def find_icons(img, full_h):
    """Yellow mouse icons in the image: a list of (kind, x, y, yellow_px).

    kind "mark":  the bite / pull mark (yellow inside a white ring) -> click it.
    kind "popup": the LMB icon of a "You caught" popup (= Release): a white E key block sits
                  ~2.7 % of the window height above it -> never click, press E.
    full_h is the game window height; icon sizes scale with it.
    """
    px = np.asarray(img.convert("RGB"), dtype=np.int16)
    r, g, b = px[..., 0], px[..., 1], px[..., 2]
    yellow = ((r > 220) & (g > 170) & (g < 235) & (b < 80)).astype(np.uint8)
    white = (r > 200) & (g > 200) & (b > 200)
    scale = full_h / 1080
    min_px = max(3, int(MIN_YELLOW * scale * scale))
    n, _, stats, cents = cv2.connectedComponentsWithStats(yellow, connectivity=8)
    icons = []
    for i in range(1, n):
        area = int(stats[i, cv2.CC_STAT_AREA])
        size = max(stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT])
        if area < min_px or size > MAX_ICON_SIZE * full_h:  # noise, or a yellow fish
            continue
        cx, cy = int(cents[i][0]), int(cents[i][1])
        rad = max(6, int(0.025 * full_h))
        if white[max(0, cy - rad):cy + rad, max(0, cx - rad):cx + rad].sum() < area:
            continue  # no white ring / mouse body around it
        above = white[max(0, cy - int(0.035 * full_h)):max(0, cy - int(0.018 * full_h)),
                      max(0, cx - int(0.012 * full_h)):cx + int(0.008 * full_h) + 1]
        kind = "popup" if above.size and above.mean() >= POPUP_E_FILL else "mark"
        icons.append((kind, cx, cy, area))
    return icons


def read_bar(img):
    """Pixel counts on the fight bar row, or None when the bar is not on screen.

    line   = green in the left half (your line endurance left)
    red    = red in the right half (fish stamina left)
    orange = orange in the right half (stamina being drained right now)
    """
    px = np.asarray(img.convert("RGB"), dtype=np.int16)
    r, g, b = px[..., 0], px[..., 1], px[..., 2]
    green = (g > 150) & (g - r > 50)
    red = (r > 120) & (r - g > 80) & (g < 110)
    orange = (r > 180) & (g >= 110) & (r - g > 30) & (b < 100)
    lit = green | red | orange
    row = int(lit.sum(axis=1).argmax())
    w = px.shape[1]
    if lit[row].sum() < w * BAR_MIN_FILL:
        return None
    mid = w // 2
    return {"line": int(green[row, :mid].sum()), "red": int(red[row, mid:].sum()),
            "orange": int(orange[row, mid:].sum())}


def click():
    down = INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, MAGIC))
    up = INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTUP, 0, MAGIC))
    user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
    time.sleep(0.05)
    user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))


def key(name, up):
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    inp = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(0, SCAN[name], flags, 0, MAGIC))
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def tap_key(name):
    key(name, up=False)
    time.sleep(0.05)
    key(name, up=True)


def hold_mouse(seconds):
    """Hold the left button (charge the cast) and release (throw)."""
    down = INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, MAGIC))
    up = INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTUP, 0, MAGIC))
    user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
    time.sleep(seconds)
    user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))


class Steer:
    """Holds at most one of A / D down."""

    def __init__(self):
        self.held = None

    def hold(self, name):
        if self.held == name:
            return
        self.release()
        key(name, up=False)
        self.held = name

    def release(self):
        if self.held:
            key(self.held, up=True)
            self.held = None


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def run(is_armed, should_stop=lambda: False):
    """The bot loop. is_armed() is polled every tick; the loop ends once should_stop() is true."""
    os.makedirs(SHOTS_DIR, exist_ok=True)
    was_armed = False
    last_click = 0.0
    fighting = False
    pending_cast = False
    caught = 0
    steer = Steer()
    try:
        while not should_stop():
            armed = is_armed()
            if armed != was_armed:
                log("ARMED" if armed else "disarmed")
                pending_cast = armed  # the loop starts with a cast
                was_armed = armed

            hwnd = game_window() if armed else None
            if not hwnd:
                steer.release()
                time.sleep(1 / SCAN_HZ)
                continue

            if pending_cast:
                time.sleep(0.3)  # let F8 / E go up first
                # Never cast while a catch popup is up: there LMB means Release.
                left, top, right, bottom = client_box(hwnd, (0.0, 1.0), (0.0, 1.0))
                check = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
                cw, ch = check.size
                still = [i for i in find_icons(check.crop((int(cw * ICON_X[0]), int(ch * ICON_Y[0]),
                                                           int(cw * ICON_X[1]), int(ch * ICON_Y[1]))), ch)
                         if i[0] == "popup"]
                if still:
                    log("catch popup still up: E again, not casting yet")
                    tap_key("E")
                    time.sleep(0.5)
                    continue
                log(f"cast: hold LMB {CAST_HOLD:g} s, release")
                hold_mouse(CAST_HOLD)
                pending_cast = False
                fighting = False
                last_click = time.monotonic()
                continue

            now = time.monotonic()
            # One grab of the whole game window per tick; mark area and bar band are crops of it.
            left, top, right, bottom = client_box(hwnd, (0.0, 1.0), (0.0, 1.0))
            frame = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
            w, h = frame.size

            area = frame.crop((int(w * ICON_X[0]), int(h * ICON_Y[0]), int(w * ICON_X[1]), int(h * ICON_Y[1])))
            icons = find_icons(area, h)

            # "You caught" popup (fish after a fight, or an item grabbed straight away):
            # E = Take (its LMB is Release, never clicked), then cast again.
            popup = next((i for i in icons if i[0] == "popup"), None)
            if popup:
                steer.release()
                caught += 1
                what = f"fish after {last_bar - fight_start:.1f}s fight" if fighting else "item, no fight"
                log(f"catch popup at ({popup[1]},{popup[2]}): {what} (total {caught}), E to take")
                tap_key("E")
                time.sleep(0.5)
                pending_cast = True
                continue

            # Mark: clicked whenever it shows in the middle (bite, and after each drained segment).
            mark_x_max = (ROI_X[1] - ICON_X[0]) * w
            mark = next((i for i in icons if i[0] == "mark" and i[1] < mark_x_max), None)
            if mark and now - last_click > CLICK_COOLDOWN:
                click()
                last_click = now
                log(f"mark at ({mark[1]},{mark[2]}) yellow={mark[3]}: clicked")
                if not fighting:
                    area.save(os.path.join(SHOTS_DIR, f"hit_{time.strftime('%H%M%S')}.png"))

            bar = read_bar(frame.crop((0, int(h * BAR_Y[0]), w, int(h * BAR_Y[1]))))
            if bar and not fighting:
                fighting = True
                fight_start = last_bar = last_progress = last_switch = now
                best_red = None
                want = "A"
                steer.hold(want)
                last_frame = 0.0
                frame_no = 0
                fight_dir = os.path.join(SHOTS_DIR, f"fight_{time.strftime('%H%M%S')}")
                os.makedirs(fight_dir, exist_ok=True)
                log("bar up: fight starts, holding A")

            if fighting:
                if bar:
                    last_bar = now
                    steer.hold(want)  # re-press after a moment without the bar
                    if best_red is None or bar["red"] > best_red + PROGRESS_PX:
                        best_red = bar["red"]  # first reading, or the fish got stamina back
                        last_progress = now
                    elif bar["red"] < best_red - PROGRESS_PX:
                        best_red = bar["red"]
                        last_progress = now
                    elif now - max(last_progress, last_switch) > SWITCH_AFTER:
                        want = OTHER[want]
                        steer.hold(want)
                        last_switch = now
                        log(f"no progress, switch to {steer.held}  fish red={bar['red']} "
                            f"orange={bar['orange']} line={bar['line']}")
                else:
                    steer.release()
                    if now - last_bar > FIGHT_TIMEOUT:
                        # No popup on screen, so LMB casts here (it cannot hit Release).
                        log(f"bar gone for {FIGHT_TIMEOUT}s and no catch popup: fish lost, recasting")
                        pending_cast = True
                        continue
                if RECORD_EVERY and now - last_frame >= RECORD_EVERY:
                    last_frame = now
                    small = frame.resize((w // 2, h // 2))
                    info = f"red{bar['red']}_or{bar['orange']}_line{bar['line']}" if bar else "nobar"
                    name = f"{frame_no:04d}_{now - fight_start:05.1f}s_{info}_hold-{steer.held}.jpg"
                    small.convert("RGB").save(os.path.join(fight_dir, name), quality=80)
                    frame_no += 1
            time.sleep(1 / SCAN_HZ)
    finally:
        steer.release()


def main():
    f8 = {"was_down": False, "armed": False}

    def is_armed():
        down = bool(user32.GetAsyncKeyState(VK_F8) & 0x8000)
        if down and not f8["was_down"]:
            f8["armed"] = not f8["armed"]
        f8["was_down"] = down
        return f8["armed"]

    log("Fishing bot ready. Press F8 in game to arm. Ctrl+C here to quit.")
    run(is_armed)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
