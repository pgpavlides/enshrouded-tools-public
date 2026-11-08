"""Enshrouded Tools: the key spammer and the fishing bot in one tray app.

Key spam (only while Enshrouded is the active window):
  - hold E for more than 1 s: E is spammed until you let go
  - hold the left mouse button for more than 5 s: left clicks are spammed until you let go
  - Shift+E toggles turbo E (E spammed very fast, nothing held); green dot at the bottom left
Fishing bot: F8 arms / disarms it (blue dot at the bottom left while armed). See fishing_bot.py.
Spin: F9 toggles it: the left mouse button is held and the view turns slowly to one side, without
end (orange dot at the bottom left while on).
Route (see route.py): F6 saves the spot you stand on to the route, Shift+F6 deletes the saved spot
nearest to you. F7 shows / hides the compass bar at the top of the game that points to the next
spot; reaching a spot moves on to the next one, Shift+F7 skips one. F5 shows / hides the minimap
at the bottom right of the game, Shift+F5 changes its zoom. The tray menu and the settings window
open the list of saved spots (select and delete) and the world map with the route on it.

Tray icon: double-click or "Settings" opens the settings window; the menu also toggles turbo and
the fishing bot, and quits. Ctrl+F12 quits too. Settings are saved to enshrouded_tools.json, the
log goes to enshrouded_tools.log, both next to this file.

Run: double-click this file, or `pythonw enshrouded_tools.pyw`. The tray needs `pip install pystray`;
without it the settings window opens instead and closing it quits.
"""
import collections
import ctypes
import json
import os
import queue
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import messagebox, ttk

import fishing_bot as fb  # also makes the process per-monitor DPI aware
import route as rt

try:
    import pystray
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    pystray = None

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, "enshrouded_tools.json")
LOG_FILE = os.path.join(HERE, "enshrouded_tools.log")
GAME_EXE = "enshrouded.exe"
DOT_SIZE = 14    # indicator dot diameter in pixels
DOT_GAP = 6      # space between the turbo dot and the fishing dot
DOT_MARGIN = 12  # indicator distance from the bottom left corner of the screen
TURBO_COLOUR, FISHING_COLOUR, SPIN_COLOUR, OFF_COLOUR = "#22dd44", "#3399ff", "#ff9933", "black"

DEFAULTS = {
    "hold_e": True, "hold_e_delay": 1.0,
    "hold_click": True, "hold_click_delay": 5.0,
    "press_interval": 0.05,
    "turbo_hotkey": True, "turbo_down": 0.015, "turbo_interval": 0.015,
    "fishing_hotkey": True,
    "cast_hold": 1.0, "click_cooldown": 1.5, "switch_after": 0.6, "fight_timeout": 4.0, "record_every": 0.25,
    "spin_hotkey": True, "spin_left": False, "spin_px": 2.0, "spin_step": 0.02,
    "spin_circle_px": 3600.0, "spin_circles": 1.0, "spin_up_deg": 15.0,
    "route_hotkeys": True, "route_name": "chests", "arrive_radius": 4.0, "route_loop": True,
    "minimap_zoom": 4.0,
}
# Settings that are fishing_bot module constants (it reads them on every tick).
FISHING_CONSTANTS = {"cast_hold": "CAST_HOLD", "click_cooldown": "CLICK_COOLDOWN", "switch_after": "SWITCH_AFTER",
                     "fight_timeout": "FIGHT_TIMEOUT", "record_every": "RECORD_EVERY"}

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x201, 0x202
VK_E, VK_F5, VK_F6, VK_F7, VK_F8, VK_F9, VK_F12 = 0x45, 0x74, 0x75, 0x76, 0x77, 0x78, 0x7B
VK_CONTROL, VK_SHIFT = 0x11, 0x10
SCAN_E = 0x12
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x2, 0x8
MOUSEEVENTF_MOVE, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x1, 0x2, 0x4
MAGIC = fb.MAGIC  # tags our own injected events (and the fishing bot's) so the hooks ignore them
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ALREADY_EXISTS = 183
GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80000, 0x20, 0x80, 0x8000000
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10

ULONG_PTR = ctypes.c_size_t


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


INPUT, KEYBDINPUT, MOUSEINPUT = fb.INPUT, fb.KEYBDINPUT, fb.MOUSEINPUT

HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
user32.SetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.LONG)
user32.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wintypes.UINT)
kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
kernel32.CreateMutexW.restype = wintypes.HANDLE

stop = threading.Event()
turbo = threading.Event()    # set while turbo E is on
fishing = threading.Event()  # set while the fishing bot is armed
spin = threading.Event()     # set while spin (LMB held, view turning) is on
following = threading.Event()  # set while the route compass is shown
minimap_on = threading.Event()  # set while the minimap is shown
ui_queue = queue.Queue()     # "settings" / "quit" / "route_*" requests to the Tk thread
log_lines = collections.deque(maxlen=300)
settings = dict(DEFAULTS)
route_status = {"text": ""}  # shown in the settings window
DELETE_RADIUS = 10.0  # Shift+F6 only deletes a saved spot this close (m)
FLASH_TIME = 2.0      # how long a "spot saved" message stays on screen (s)


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    log_lines.append(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


fb.log = log  # the fishing bot logs here instead of to a console


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        settings.update({k: type(DEFAULTS[k])(v) for k, v in saved.items() if k in DEFAULTS})
    except (OSError, ValueError, TypeError):
        pass  # first run or a broken file: defaults
    apply_settings()


def save_settings():
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    apply_settings()


def apply_settings():
    for name, constant in FISHING_CONSTANTS.items():
        setattr(fb, constant, settings[name])


def game_focused():
    return fb.game_window() is not None


def send_e(up):
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    inp = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(0, SCAN_E, flags, 0, MAGIC))
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def send_click(up):
    flags = MOUSEEVENTF_LEFTUP if up else MOUSEEVENTF_LEFTDOWN
    inp = INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(0, 0, 0, flags, 0, MAGIC))
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def send_move(dx, dy=0):
    inp = INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx, dy, 0, MOUSEEVENTF_MOVE, 0, MAGIC))
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


class Repeater:
    """Tracks one physical button and spams it while it is held longer than its delay setting."""

    def __init__(self, enabled_key, delay_key, send):
        self.enabled_key = enabled_key
        self.delay_key = delay_key
        self.send = send
        self.held_since = None  # time.monotonic() when the physical button went down, None while it is up

    def down(self):
        if self.held_since is None:  # ignore Windows auto-repeat
            self.held_since = time.monotonic()

    def up(self):
        self.held_since = None


def spammer(rep):
    spamming = False
    while not stop.is_set():
        since = rep.held_since
        if (since is None or not settings[rep.enabled_key]
                or time.monotonic() - since < settings[rep.delay_key] or not game_focused()):
            if spamming and since is None:
                rep.send(up=True)  # make sure the button is not left stuck down after release
                spamming = False
            time.sleep(0.02)
            continue
        spamming = True
        rep.send(up=True)
        time.sleep(0.02)
        rep.send(up=False)
        time.sleep(settings["press_interval"])
    if spamming:
        rep.send(up=True)


def turbo_spammer():
    spamming = False
    while not stop.is_set():
        if not turbo.is_set() or not game_focused():
            if spamming:
                send_e(up=True)
                spamming = False
            time.sleep(0.05)
            continue
        spamming = True
        send_e(up=False)
        time.sleep(settings["turbo_down"])
        send_e(up=True)
        time.sleep(settings["turbo_interval"])
    if spamming:
        send_e(up=True)


def spinner():
    holding = False
    last_down = 0.0
    turned = 0  # px turned since the last upward tilt
    while not stop.is_set():
        if not spin.is_set() or not game_focused():
            if holding:
                send_click(up=True)
                holding = False
            if not spin.is_set():
                turned = 0
            time.sleep(0.05)
            continue
        now = time.monotonic()
        if not holding or now - last_down > 1.0:  # re-press now and then in case the player let go
            send_click(up=False)
            holding = True
            last_down = now
        step = max(1, round(settings["spin_px"]))
        send_move(-step if settings["spin_left"] else step)
        turned += step
        if settings["spin_circles"] > 0 and turned >= settings["spin_circles"] * settings["spin_circle_px"]:
            up = round(settings["spin_circle_px"] * settings["spin_up_deg"] / 360)
            send_move(0, -up)  # tilt the view up a little after each set of circles
            log(f"spin: tilted up {settings['spin_up_deg']:g} deg ({up} px)")
            turned = 0
        time.sleep(settings["spin_step"])
    if holding:
        send_click(up=True)


def toggle(event, name):
    if event.is_set():
        event.clear()
    else:
        event.set()
    log(f"{name} {'on' if event.is_set() else 'off'}")


def make_tray():
    """The tray icon, or None when pystray is not installed."""
    if pystray is None:
        return None
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((2, 2, 62, 62), radius=14, fill=(29, 43, 31, 255), outline=TURBO_COLOUR, width=3)
    try:
        font = ImageFont.truetype("segoeuib.ttf", 40)
    except OSError:
        font = ImageFont.load_default()
    draw.text((32, 33), "E", font=font, fill=TURBO_COLOUR, anchor="mm")
    menu = pystray.Menu(
        pystray.MenuItem("Settings", lambda: ui_queue.put("settings"), default=True),
        pystray.MenuItem("Turbo E (Shift+E)", lambda: toggle(turbo, "turbo E"),
                         checked=lambda item: turbo.is_set()),
        pystray.MenuItem("Fishing bot (F8)", lambda: toggle(fishing, "fishing bot"),
                         checked=lambda item: fishing.is_set()),
        pystray.MenuItem("Spin + hold click (F9)", lambda: toggle(spin, "spin"),
                         checked=lambda item: spin.is_set()),
        pystray.MenuItem("Route compass (F7)", lambda: toggle(following, "route compass"),
                         checked=lambda item: following.is_set()),
        pystray.MenuItem("Minimap (F5)", lambda: toggle(minimap_on, "minimap"),
                         checked=lambda item: minimap_on.is_set()),
        pystray.MenuItem("Route spots", lambda: ui_queue.put("spots")),
        pystray.MenuItem("Map", lambda: ui_queue.put("map")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", lambda: ui_queue.put("quit")),
    )
    return pystray.Icon("enshrouded_tools", img, "Enshrouded Tools", menu)


class SettingsWindow:
    """Settings and live state. Values apply on Save."""

    def __init__(self, root, on_close):
        self.win = tk.Toplevel(root)
        self.win.title("Enshrouded Tools")
        self.win.resizable(False, False)
        self.win.protocol("WM_DELETE_WINDOW", on_close)
        self.vars = {}
        body = ttk.Frame(self.win, padding=10)
        body.pack(fill="both", expand=True)

        spam = ttk.LabelFrame(body, text="Key spam (only in Enshrouded)", padding=8)
        spam.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self.check(spam, 0, "Hold E to spam it, after", "hold_e")
        self.number(spam, 0, "hold_e_delay")
        self.check(spam, 1, "Hold left click to spam it, after", "hold_click")
        self.number(spam, 1, "hold_click_delay")
        ttk.Label(spam, text="Spam interval").grid(row=2, column=0, sticky="w")
        self.number(spam, 2, "press_interval")
        self.check(spam, 3, "Shift+E toggles turbo E", "turbo_hotkey")
        ttk.Label(spam, text="Turbo: key down").grid(row=4, column=0, sticky="w")
        self.number(spam, 4, "turbo_down")
        ttk.Label(spam, text="Turbo: gap between presses").grid(row=5, column=0, sticky="w")
        self.number(spam, 5, "turbo_interval")
        self.turbo_now = tk.BooleanVar(value=turbo.is_set())
        ttk.Checkbutton(spam, text="Turbo E on now", variable=self.turbo_now,
                        command=lambda: toggle(turbo, "turbo E")).grid(row=6, column=0, sticky="w", pady=(4, 0))

        fish = ttk.LabelFrame(body, text="Fishing bot", padding=8)
        fish.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        self.check(fish, 0, "F8 arms / disarms the bot", "fishing_hotkey")
        for row, (label, name) in enumerate([("Cast: hold LMB for", "cast_hold"),
                                             ("Wait between mark clicks", "click_cooldown"),
                                             ("Switch A / D after no progress for", "switch_after"),
                                             ("Give a fight up after the bar is gone for", "fight_timeout"),
                                             ("Record fight frames every (0 = off)", "record_every")], start=1):
            ttk.Label(fish, text=label).grid(row=row, column=0, sticky="w")
            self.number(fish, row, name)
        self.fishing_now = tk.BooleanVar(value=fishing.is_set())
        ttk.Checkbutton(fish, text="Bot armed now", variable=self.fishing_now,
                        command=lambda: toggle(fishing, "fishing bot")).grid(row=6, column=0, sticky="w",
                                                                              pady=(4, 0))

        turn = ttk.LabelFrame(body, text="Spin: hold left click and turn (only in Enshrouded)", padding=8)
        turn.grid(row=1, column=1, sticky="nsew", padx=(8, 0), pady=(8, 0))
        self.check(turn, 0, "F9 toggles spin", "spin_hotkey")
        self.check(turn, 1, "Turn left (off = right)", "spin_left")
        ttk.Label(turn, text="Mouse move per step").grid(row=2, column=0, sticky="w")
        self.number(turn, 2, "spin_px", unit="px")
        ttk.Label(turn, text="Time between steps").grid(row=3, column=0, sticky="w")
        self.number(turn, 3, "spin_step")
        ttk.Label(turn, text="Mouse move for one full circle").grid(row=4, column=0, sticky="w")
        self.number(turn, 4, "spin_circle_px", unit="px")
        ttk.Label(turn, text="Tilt up after this many circles (0 = never)").grid(row=5, column=0, sticky="w")
        self.number(turn, 5, "spin_circles", unit="")
        ttk.Label(turn, text="Tilt up by").grid(row=6, column=0, sticky="w")
        self.number(turn, 6, "spin_up_deg", unit="deg")
        self.spin_now = tk.BooleanVar(value=spin.is_set())
        ttk.Checkbutton(turn, text="Spin on now", variable=self.spin_now,
                        command=lambda: toggle(spin, "spin")).grid(row=7, column=0, sticky="w", pady=(4, 0))

        way = ttk.LabelFrame(body, text="Route (F6 save spot, Shift+F6 delete nearest, F7 compass, "
                                        "Shift+F7 skip, F5 minimap)", padding=8)
        way.grid(row=2, column=1, sticky="nsew", padx=(8, 0), pady=(8, 0))
        self.check(way, 0, "Route hotkeys on", "route_hotkeys")
        ttk.Label(way, text="Route name (routes/<name>.json)").grid(row=1, column=0, sticky="w")
        self.text(way, 1, "route_name")
        ttk.Label(way, text="Next spot when this close").grid(row=2, column=0, sticky="w")
        self.number(way, 2, "arrive_radius", unit="m")
        self.check(way, 3, "Start over after the last spot", "route_loop")
        ttk.Label(way, text="Minimap zoom (Shift+F5: 2, 4, 8)").grid(row=4, column=0, sticky="w")
        self.number(way, 4, "minimap_zoom", unit="px per 8 m")
        self.route_label = ttk.Label(way, text="")
        self.route_label.grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 0))
        way_buttons = ttk.Frame(way)
        way_buttons.grid(row=6, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Button(way_buttons, text="Spots list", command=lambda: ui_queue.put("spots")).pack(side="left")
        ttk.Button(way_buttons, text="Map", command=lambda: ui_queue.put("map")).pack(side="left", padx=6)

        buttons = ttk.Frame(body)
        buttons.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Button(buttons, text="Save", command=self.save).pack(side="left")
        ttk.Button(buttons, text="Defaults", command=self.defaults).pack(side="left", padx=6)
        self.status = ttk.Label(buttons, text="")
        self.status.pack(side="left", padx=6)

        self.log = tk.Text(body, width=70, height=5, state="disabled", wrap="none")
        self.log.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.shown_lines = None

    def check(self, parent, row, label, name):
        var = tk.BooleanVar(value=settings[name])
        self.vars[name] = var
        ttk.Checkbutton(parent, text=label, variable=var).grid(row=row, column=0, sticky="w")

    def number(self, parent, row, name, unit="s"):
        var = tk.StringVar(value=f"{settings[name]:g}")
        self.vars[name] = var
        ttk.Entry(parent, textvariable=var, width=7, justify="right").grid(row=row, column=1, padx=4)
        ttk.Label(parent, text=unit).grid(row=row, column=2, sticky="w")

    def text(self, parent, row, name):
        var = tk.StringVar(value=settings[name])
        self.vars[name] = var
        ttk.Entry(parent, textvariable=var, width=14).grid(row=row, column=1, columnspan=2, padx=4, sticky="w")

    def save(self):
        new = {}
        for name, var in self.vars.items():
            if isinstance(DEFAULTS[name], bool):
                new[name] = bool(var.get())
                continue
            if isinstance(DEFAULTS[name], str):
                value = "".join(ch for ch in var.get().strip() if ch.isalnum() or ch in "-_ ")
                if not value:
                    self.status.config(text=f"{name}: empty", foreground="red")
                    return
                new[name] = value
                continue
            try:
                value = float(var.get().replace(",", "."))
            except ValueError:
                value = -1.0
            if value < 0:
                self.status.config(text=f"{name}: not a valid number", foreground="red")
                return
            new[name] = value
        settings.update(new)
        try:
            save_settings()
        except OSError as e:
            self.status.config(text=f"not saved: {e}", foreground="red")
            return
        self.status.config(text="Saved", foreground="green")
        log("settings saved")

    def defaults(self):
        for name, var in self.vars.items():
            var.set(DEFAULTS[name] if isinstance(DEFAULTS[name], (bool, str)) else f"{DEFAULTS[name]:g}")
        self.status.config(text="Defaults loaded, press Save to keep them", foreground="")

    def refresh(self):
        self.turbo_now.set(turbo.is_set())
        self.fishing_now.set(fishing.is_set())
        self.spin_now.set(spin.is_set())
        self.route_label.config(text=route_status["text"])
        lines = list(log_lines)
        if lines != self.shown_lines:
            self.shown_lines = lines
            self.log.config(state="normal")
            self.log.delete("1.0", "end")
            self.log.insert("end", "\n".join(lines))
            self.log.see("end")
            self.log.config(state="disabled")


def main():
    kernel32.CreateMutexW(None, False, "ENSCROUDED_FARMER")
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        messagebox.showinfo("Enshrouded Tools", "Already running (or the old ENSCROUDED_FARMER.pyw is).\n"
                                                "Quit it with Ctrl+F12 first.")
        return
    open(LOG_FILE, "w").close()  # a fresh log each run
    load_settings()

    e_key = Repeater("hold_e", "hold_e_delay", send_e)
    left_click = Repeater("hold_click", "hold_click_delay", send_click)
    # hotkeys that are down, so auto-repeat does not toggle again
    held = {"toggle": False, "f5": False, "f6": False, "f7": False, "f8": False, "f9": False}

    @HOOKPROC
    def key_hook(n_code, w_param, l_param):
        if n_code == 0:
            kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            down = w_param in (WM_KEYDOWN, WM_SYSKEYDOWN)
            up = w_param in (WM_KEYUP, WM_SYSKEYUP)
            if kb.dwExtraInfo != MAGIC:
                if kb.vkCode == VK_E:
                    if down:
                        if (settings["turbo_hotkey"] and user32.GetAsyncKeyState(VK_SHIFT) & 0x8000
                                and game_focused()):
                            if not held["toggle"]:
                                held["toggle"] = True
                                toggle(turbo, "turbo E")
                            return 1  # swallow Shift+E so the game does not see it
                        if not turbo.is_set():
                            e_key.down()
                    elif up:
                        held["toggle"] = False
                        e_key.up()
                elif kb.vkCode == VK_F8:
                    if down and not held["f8"] and settings["fishing_hotkey"]:
                        toggle(fishing, "fishing bot")
                    held["f8"] = down
                elif kb.vkCode == VK_F9:
                    if down and not held["f9"] and settings["spin_hotkey"]:
                        toggle(spin, "spin")
                    held["f9"] = down
                elif kb.vkCode in (VK_F5, VK_F6, VK_F7):
                    name = {VK_F5: "f5", VK_F6: "f6", VK_F7: "f7"}[kb.vkCode]
                    if down and not held[name] and settings["route_hotkeys"] and game_focused():
                        shift = bool(user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)
                        ui_queue.put({("f6", False): "route_add", ("f6", True): "route_delete",
                                      ("f7", False): "route_toggle", ("f7", True): "route_skip",
                                      ("f5", False): "route_minimap", ("f5", True): "route_zoom"}[name, shift])
                    held[name] = down
                elif kb.vkCode == VK_F12 and down and user32.GetAsyncKeyState(VK_CONTROL) & 0x8000:
                    stop.set()
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    @HOOKPROC
    def mouse_hook(n_code, w_param, l_param):
        if n_code == 0 and w_param in (WM_LBUTTONDOWN, WM_LBUTTONUP):
            ms = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            if ms.dwExtraInfo != MAGIC:
                if w_param == WM_LBUTTONDOWN:
                    left_click.down()
                else:
                    left_click.up()
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    # Indicator: small click-through, always-on-top window at the bottom left of the main screen.
    # It is also the Tk root, so its event loop pumps the messages the low-level hooks need.
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.config(bg=OFF_COLOUR)
    root.attributes("-transparentcolor", OFF_COLOUR)
    width, height = 3 * DOT_SIZE + 2 * DOT_GAP + 2, DOT_SIZE + 2
    root.geometry(f"{width}x{height}+{DOT_MARGIN}+{root.winfo_screenheight() - DOT_MARGIN - height}")
    canvas = tk.Canvas(root, width=width, height=height, bg=OFF_COLOUR, highlightthickness=0)
    canvas.pack()
    turbo_dot = canvas.create_oval(1, 1, DOT_SIZE, DOT_SIZE, fill=OFF_COLOUR, outline=OFF_COLOUR)
    x = DOT_SIZE + DOT_GAP
    fishing_dot = canvas.create_oval(x + 1, 1, x + DOT_SIZE, DOT_SIZE, fill=OFF_COLOUR, outline=OFF_COLOUR)
    x += DOT_SIZE + DOT_GAP
    spin_dot = canvas.create_oval(x + 1, 1, x + DOT_SIZE, DOT_SIZE, fill=OFF_COLOUR, outline=OFF_COLOUR)
    root.update_idletasks()
    hwnd = int(root.wm_frame(), 16)
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                          style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)

    tray = make_tray()
    window = {"settings": None, "spots": None, "map": None}
    game = rt.Game()
    route = rt.Route(settings["route_name"])
    overlay = rt.Overlay(root, WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
    minimap = rt.Minimap(root, WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
    flash = {"text": "", "until": 0.0}

    def say(text):
        flash.update(text=text, until=time.monotonic() + FLASH_TIME)
        log(text)

    def route_request(request):
        if route.name != settings["route_name"]:  # renamed in the settings window
            route.name = settings["route_name"]
            route.load()
        if request == "route_minimap":
            toggle(minimap_on, "minimap")
            return
        if request == "route_zoom":
            zooms = rt.Minimap.ZOOMS
            settings["minimap_zoom"] = next((z for z in zooms if z > settings["minimap_zoom"]), zooms[0])
            save_settings()
            say(f"minimap zoom {settings['minimap_zoom']:g}")
            return
        pos = game.positions()
        if request == "route_toggle":
            toggle(following, "route compass")
            if following.is_set() and pos is not None:
                i, _ = route.nearest(pos[1])
                route.index = i or 0  # start at the spot nearest to you
            return
        if request == "route_skip":
            if route.points:
                route.index = (route.index + 1) % len(route.points)
                say(f"skipped to spot {route.index + 1}/{len(route.points)}")
            return
        if pos is None:
            say("position not found: see route/README.md")
            return
        player = pos[1]
        if request == "route_add":
            n = route.add(player)
            say(f"spot {n} saved to {route.name} ({player[0]:.0f}, {player[1]:.0f}, {player[2]:.0f})")
        elif request == "route_delete":
            i, d = route.nearest(player)
            if i is None or d > DELETE_RADIUS:
                say(f"no saved spot within {DELETE_RADIUS:g} m")
            else:
                route.remove(i)
                say(f"spot {i + 1} deleted, {len(route.points)} left")

    def route_tick():
        if stop.is_set():
            return
        root.after(50, route_tick)
        game_hwnd = fb.game_window()
        flashing = time.monotonic() < flash["until"]
        show_compass = game_hwnd is not None and (following.is_set() or flashing)
        show_map = game_hwnd is not None and minimap_on.is_set()
        pos = game.positions() if show_compass or show_map else None
        if show_map:
            minimap.show_at(game_hwnd)
            minimap.draw(pos, route, max(0.5, min(16.0, settings["minimap_zoom"])))
        else:
            minimap.hide()
        if not show_compass:
            overlay.hide()
            return
        n = len(route.points)
        route_status["text"] = f"{route.name}: {n} spots" + (
            f", you are at {pos[1][0]:.1f}, {pos[1][1]:.1f}, {pos[1][2]:.1f}" if pos else ", position not readable")
        items, text = [], flash["text"] if flashing else ""
        if following.is_set() and not flashing:
            if pos is None:
                text = "position not found"
            elif n == 0:
                text = f"route {route.name} is empty: F6 saves the spot you stand on"
            else:
                cam, player = pos
                if rt.flat_dist(player, route.points[route.index]) < settings["arrive_radius"]:
                    if route.index + 1 < n or settings["route_loop"]:
                        route.index = (route.index + 1) % n
                    else:
                        following.clear()
                        say(f"route {route.name} done")
                        return
                point = route.points[route.index]
                # the next two spots small and dim, the current one last so it is drawn on top
                for ahead, size, colour in ((2, 5, "#7a6a3a"), (1, 6, "#b39a4f"), (0, 10, "#ffd75e")):
                    if ahead < n and (route.index + ahead < n or settings["route_loop"]):
                        items.append((rt.turn_angle(cam, player, route.points[(route.index + ahead) % n]),
                                      size, colour))
                dy = point["y"] - player[1]
                height = f"  {'▲' if dy > 0 else '▼'} {abs(dy):.0f} m" if abs(dy) > 3 else ""
                text = (f"{point.get('note') or 'spot'} {route.index + 1}/{n}  ·  "
                        f"{rt.flat_dist(player, point):.0f} m{height}")
        overlay.show_at(game_hwnd)
        overlay.draw(items, text)

    def close_settings():
        if tray is None:
            stop.set()  # no tray icon to come back to: closing the window quits
        window["settings"].win.destroy()
        window["settings"] = None

    def open_settings():
        if window["settings"] is None:
            window["settings"] = SettingsWindow(root, close_settings)
        win = window["settings"].win
        win.deiconify()
        win.lift()
        win.focus_force()

    def close_window(name):
        window[name].win.destroy()
        window[name] = None

    def open_window(name):
        if route.name != settings["route_name"]:  # renamed in the settings window
            route.name = settings["route_name"]
            route.load()
        if window[name] is None:
            if name == "spots":
                window[name] = rt.SpotsWindow(root, route, game.positions, lambda: open_window("map"),
                                              lambda: close_window("spots"))
            else:
                window[name] = rt.MapWindow(root, route, game.positions, lambda: close_window("map"))
        win = window[name].win
        win.deiconify()
        win.lift()
        win.focus_force()

    last_state = [None]
    ticks = [0]

    def refresh():
        while not ui_queue.empty():
            request = ui_queue.get_nowait()
            if request == "settings":
                open_settings()
            elif request == "quit":
                stop.set()
            elif request.startswith("route_"):
                route_request(request)
            elif request in ("spots", "map"):
                open_window(request)
        if stop.is_set():
            root.quit()
            return
        state = (turbo.is_set(), fishing.is_set(), spin.is_set())
        if state != last_state[0]:
            last_state[0] = state
            for dot, on, on_colour in ((turbo_dot, state[0], TURBO_COLOUR), (fishing_dot, state[1], FISHING_COLOUR),
                                       (spin_dot, state[2], SPIN_COLOUR)):
                colour = on_colour if on else OFF_COLOUR
                canvas.itemconfig(dot, fill=colour, outline=colour)
            if tray is not None:
                tray.update_menu()
        if window["settings"] is not None:
            window["settings"].refresh()
        if window["spots"] is not None:
            window["spots"].refresh()
        if window["map"] is not None and ticks[0] % 2 == 0:
            window["map"].refresh()
        ticks[0] += 1
        if ticks[0] % 10 == 0:  # the game can push itself above us, so re-assert topmost once a second
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
        root.after(100, refresh)

    hooks = [user32.SetWindowsHookExW(WH_KEYBOARD_LL, key_hook, None, 0),
             user32.SetWindowsHookExW(WH_MOUSE_LL, mouse_hook, None, 0)]
    workers = [threading.Thread(target=spammer, args=(rep,), daemon=True) for rep in (e_key, left_click)]
    workers.append(threading.Thread(target=turbo_spammer, daemon=True))
    workers.append(threading.Thread(target=spinner, daemon=True))
    workers.append(threading.Thread(target=fb.run, args=(fishing.is_set, stop.is_set), daemon=True))
    for worker in workers:
        worker.start()
    if tray is not None:
        threading.Thread(target=tray.run, daemon=True).start()
        log("ready: tray icon is up")
    else:
        log("ready, no tray icon: run `pip install pystray` for one")
        open_settings()

    refresh()
    route_tick()
    root.mainloop()

    stop.set()
    if tray is not None:
        tray.stop()
    for worker in workers:
        worker.join(timeout=2)
    for hook in hooks:
        user32.UnhookWindowsHookEx(hook)


if __name__ == "__main__":
    main()
