"""Route overlay: save spots where you stand (chests, ore, ...) and get pointed to them in order.

The player and camera positions are read (read only) from the game's memory. The struct holding
them was found with route/memscan.py and route/near.py, and the pointer chains below with
route/ptrscan.py (2026-10-31); they survived a game restart. After a game patch they may break:
then run route/chaincheck.py, and if nothing is valid redo the scan (route/README.md).

Struct: +0x0 camera position (x, y, z), +0xc player position (x, y, z). Y is up. The camera sits
behind the player, so camera -> player is the view direction on the ground plane.

A route is routes/<name>.json: {"points": [{"x": .., "y": .., "z": .., "note": ""}, ...]}.
"""
import ctypes
import json
import math
import os
import struct
import tkinter as tk
from ctypes import wintypes
from tkinter import messagebox, ttk

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = None

import fishing_bot as fb

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTES_DIR = os.path.join(HERE, "routes")

# "enshrouded.exe" + offset, then read + add each offset but the last, which is only added.
CHAINS = [
    (0x1fb8138, [0x260, 0x180, 0x870, 0xca0, 0x100]),
    (0x1fe5dd8, [0xa8, 0x180, 0x870, 0xca0, 0x100]),
    (0x1fe5f08, [0x5b8, 0x180, 0x870, 0xca0, 0x100]),
    (0x1fe7010, [0xa8, 0x180, 0x870, 0xca0, 0x100]),
]
FIXED_ADDRESS = 0x3001451cb80  # the struct was here in two game runs; tried last

# The world map: x east, z north, both 0..WORLD_SIZE m; the image has north at the top. Convention
# and image come from the MIT-licensed Enshrouded-minimap mod (github.com/elxokker/Enshrouded-minimap).
MAP_FILE = os.path.join(HERE, "maps", "embervale_realmap_1280.rgba")
WORLD_SIZE = 10240.0

PROCESS_VM_READ, PROCESS_QUERY_LIMITED_INFORMATION = 0x0010, 0x1000
TH32CS_SNAPMODULE, TH32CS_SNAPMODULE32 = 0x8, 0x10
STILL_ACTIVE = 259

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("GlblcntUsage", wintypes.DWORD), ("ProccntUsage", wintypes.DWORD),
                ("modBaseAddr", ctypes.c_void_p), ("modBaseSize", wintypes.DWORD), ("hModule", wintypes.HMODULE),
                ("szModule", ctypes.c_wchar * 256), ("szExePath", ctypes.c_wchar * 260)]


kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.ReadProcessMemory.argtypes = (wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                       ctypes.POINTER(ctypes.c_size_t))
kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Module32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W))
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)


class Game:
    """Read-only access to the running game's player and camera positions."""

    def __init__(self):
        self.handle = None
        self.exe = 0

    def attach(self):
        """Open the game process, found through its window. Only works while the game is active."""
        hwnd = fb.game_window()
        if hwnd is None:
            return False
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = kernel32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return False
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid.value)
        entry = MODULEENTRY32W(dwSize=ctypes.sizeof(MODULEENTRY32W))
        ok = snap not in (None, wintypes.HANDLE(-1).value) and kernel32.Module32FirstW(snap, ctypes.byref(entry))
        if snap:
            kernel32.CloseHandle(snap)
        if not ok:
            kernel32.CloseHandle(handle)
            return False
        self.handle, self.exe = handle, entry.modBaseAddr  # the first module is the exe
        return True

    def alive(self):
        code = wintypes.DWORD()
        return bool(self.handle and kernel32.GetExitCodeProcess(self.handle, ctypes.byref(code))
                    and code.value == STILL_ACTIVE)

    def close(self):
        if self.handle:
            kernel32.CloseHandle(self.handle)
        self.handle = None

    def read(self, addr, size):
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t()
        if not kernel32.ReadProcessMemory(self.handle, ctypes.c_void_p(addr), buf, size, ctypes.byref(got)) \
                or got.value != size:
            return None
        return buf.raw

    def follow(self, offset, chain):
        raw = self.read(self.exe + offset, 8)
        for step in chain[:-1]:
            if raw is None:
                return None
            raw = self.read(struct.unpack("<Q", raw)[0] + step, 8)
        return None if raw is None else struct.unpack("<Q", raw)[0] + chain[-1]

    def positions(self):
        """(camera, player) as (x, y, z) tuples, or None when they cannot be read."""
        if not self.alive():
            self.close()
            if not self.attach():
                return None
        candidates = [self.follow(offset, chain) for offset, chain in CHAINS] + [FIXED_ADDRESS]
        for addr in candidates:
            raw = addr and self.read(addr, 24)
            if raw is None:
                continue
            values = struct.unpack("<6f", raw)
            if not all(math.isfinite(v) and abs(v) < 50000 for v in values):
                continue
            cam, player = values[:3], values[3:]
            if sum(abs(v) for v in player) > 10 and 0.3 < math.dist(cam, player) < 40:
                return cam, player
        return None


class Route:
    def __init__(self, name):
        self.name = name
        self.points = []
        self.index = 0  # the point being pointed to
        self.load()

    @property
    def path(self):
        return os.path.join(ROUTES_DIR, f"{self.name}.json")

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                self.points = json.load(f)["points"]
        except (OSError, ValueError, KeyError):
            self.points = []
        self.index = min(self.index, max(0, len(self.points) - 1))

    def save(self):
        os.makedirs(ROUTES_DIR, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"points": self.points}, f, indent=1)

    def add(self, pos):
        self.points.append({"x": round(pos[0], 2), "y": round(pos[1], 2), "z": round(pos[2], 2), "note": ""})
        self.save()
        return len(self.points)

    def nearest(self, pos):
        if not self.points:
            return None, None
        dists = [flat_dist(pos, p) for p in self.points]
        i = min(range(len(dists)), key=dists.__getitem__)
        return i, dists[i]

    def remove(self, i):
        del self.points[i]
        if self.index > i or self.index >= len(self.points):
            self.index = max(0, self.index - 1)
        self.save()


def flat_dist(pos, point):
    return math.hypot(point["x"] - pos[0], point["z"] - pos[2])


def turn_angle(cam, player, point):
    """Degrees from the view direction to the point on the ground plane, positive = to the right.
    With x east and z north, facing north (0, 1) a point east (1, 0) is to the right."""
    fx, fz = player[0] - cam[0], player[2] - cam[2]
    tx, tz = point["x"] - player[0], point["z"] - player[2]
    return math.degrees(math.atan2(fz * tx - fx * tz, fx * tx + fz * tz))


KEY = "#010101"  # transparent colour of the overlays
KEY_RGB = (1, 1, 1)


def click_through(root, styles, width, height):
    """A hidden, borderless, always-on-top window the mouse clicks through, and its canvas."""
    win = tk.Toplevel(root)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.config(bg=KEY)
    win.attributes("-transparentcolor", KEY)
    canvas = tk.Canvas(win, width=width, height=height, bg=KEY, highlightthickness=0)
    canvas.pack()
    win.update_idletasks()
    hwnd = int(win.wm_frame(), 16)
    user32.SetWindowLongW(hwnd, -20, user32.GetWindowLongW(hwnd, -20) | styles)
    win.withdraw()
    return win, canvas


_map_image = []


def map_image():
    """The world map as a PIL RGB image (loaded once), or None."""
    if not _map_image:
        _map_image.append(MapWindow.load())
    return _map_image[0]


def to_map(x, z, side):
    """World x, z (m) to map pixel."""
    return x / WORLD_SIZE * side, (1 - z / WORLD_SIZE) * side


def clip_to_circle(a, b, centre, r):
    """The part of segment a-b inside the circle, or None."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    fx, fy = a[0] - centre[0], a[1] - centre[1]
    qa, qb, qc = dx * dx + dy * dy, 2 * (fx * dx + fy * dy), fx * fx + fy * fy - r * r
    if qa == 0:
        return None
    disc = qb * qb - 4 * qa * qc
    if disc <= 0:
        return None
    root = math.sqrt(disc)
    t0, t1 = max(0.0, (-qb - root) / (2 * qa)), min(1.0, (-qb + root) / (2 * qa))
    if t0 >= t1:
        return None
    return (a[0] + dx * t0, a[1] + dy * t0), (a[0] + dx * t1, a[1] + dy * t1)


class Overlay:
    """Compass bar at the top centre of the game window, click-through."""

    WIDTH, HEIGHT, FIELD = 640, 70, 90  # FIELD: degrees from the centre to either edge of the bar

    def __init__(self, root, styles):
        self.win, self.canvas = click_through(root, styles, self.WIDTH, self.HEIGHT)
        self.shown = False

    def hide(self):
        if self.shown:
            self.win.withdraw()
            self.shown = False

    def show_at(self, game_hwnd):
        left, top, right, _ = fb.client_box(game_hwnd, (0, 1), (0, 1))
        self.win.geometry(f"+{(left + right - self.WIDTH) // 2}+{top + 10}")
        if not self.shown:
            self.win.deiconify()
            self.shown = True

    def draw(self, items, text, colour="#ffd75e"):
        """items: [(angle, size, colour)], drawn as markers on the bar; text below it."""
        c, w = self.canvas, self.WIDTH
        c.delete("all")
        mid, y = w // 2, 14
        c.create_line(20, y, w - 20, y, fill="#c8c8c8", width=2)
        c.create_line(mid, y - 7, mid, y + 7, fill="#c8c8c8", width=2)
        for angle, size, col in items:
            off = max(-1.0, min(1.0, angle / self.FIELD))
            x = mid + off * (mid - 30)
            if abs(angle) > self.FIELD:  # off the bar: an arrow at the edge
                d = 1 if angle > 0 else -1
                c.create_polygon(x - d * size, y - size, x + d * size, y, x - d * size, y + size, fill=col)
            else:
                c.create_oval(x - size, y - size, x + size, y + size, fill=col, outline="black")
        for dx, dy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):  # dark outline so the text reads on any background
            c.create_text(mid + dx, y + 30 + dy, text=text, fill="black", font=("Segoe UI", 14, "bold"))
        c.create_text(mid, y + 30, text=text, fill=colour, font=("Segoe UI", 14, "bold"))


class Minimap:
    """Round world map at the bottom right of the game window, click-through. North is up, you are
    in the middle (blue arrow = where the camera looks), the route and its spots are drawn on it;
    the next spot sits on the rim when it is off the map."""

    SIZE, MARGIN = 240, 16
    ZOOMS = (2.0, 4.0, 8.0)  # screen pixels per map pixel; one map pixel is 8 m

    def __init__(self, root, styles):
        self.win, self.canvas = click_through(root, styles, self.SIZE, self.SIZE)
        self.shown = False
        self.photo = None
        self.mask = None
        if Image is not None:
            from PIL import ImageDraw
            self.mask = Image.new("L", (self.SIZE, self.SIZE), 0)
            ImageDraw.Draw(self.mask).ellipse((3, 3, self.SIZE - 4, self.SIZE - 4), fill=255)

    def hide(self):
        if self.shown:
            self.win.withdraw()
            self.shown = False

    def show_at(self, game_hwnd):
        _, _, right, bottom = fb.client_box(game_hwnd, (0, 1), (0, 1))
        self.win.geometry(f"+{right - self.SIZE - self.MARGIN}+{bottom - self.SIZE - self.MARGIN}")
        if not self.shown:
            self.win.deiconify()
            self.shown = True

    def draw(self, pos, route, zoom):
        c, s = self.canvas, self.SIZE
        mid, r = s / 2, s / 2 - 4
        c.delete("all")
        img = map_image()
        side = img.width if img else 1280
        if pos is None or img is None:
            c.create_oval(3, 3, s - 4, s - 4, fill="#20262b", outline="#c8b27a", width=3)
            c.create_text(mid, mid, fill="white", width=s - 60, justify="center", font=("Segoe UI", 10, "bold"),
                          text="position not found" if pos is None else "no map image")
            return
        cam, player = pos
        cx, cy = to_map(player[0], player[2], side)
        half = s / 2 / zoom
        # map pixel = (screen pixel) / zoom + top left of the view; sub-pixel exact, so it does not jitter
        view = img.transform((s, s), Image.Transform.AFFINE, (1 / zoom, 0, cx - half, 0, 1 / zoom, cy - half),
                             Image.BILINEAR)
        frame = Image.new("RGB", (s, s), KEY_RGB)
        frame.paste(view, (0, 0), self.mask)
        self.photo = ImageTk.PhotoImage(frame)
        c.create_image(0, 0, image=self.photo, anchor="nw")

        def screen(p):
            mx, my = to_map(p["x"], p["z"], side)
            return (mx - cx) * zoom + mid, (my - cy) * zoom + mid

        pts = [screen(p) for p in route.points]
        for a, b in zip(pts, pts[1:]):
            seg = clip_to_circle(a, b, (mid, mid), r - 2)
            if seg:
                c.create_line(*seg[0], *seg[1], fill="#ffd75e", width=2, dash=(5, 3))
        for i, (x, y) in enumerate(pts):
            current = i == route.index
            d = math.hypot(x - mid, y - mid)
            if d > r - 8:
                if not current:
                    continue
                x, y = mid + (x - mid) / d * (r - 8), mid + (y - mid) / d * (r - 8)  # on the rim
            size = 6 if current else 4
            c.create_oval(x - size, y - size, x + size, y + size, fill="#ffd75e" if current else "#e0e0e0",
                          outline="black")
            if zoom >= 4 or current:
                c.create_text(x + size + 6, y, text=str(i + 1), fill="white", font=("Segoe UI", 8, "bold"))
        fx, fz = player[0] - cam[0], player[2] - cam[2]
        n = math.hypot(fx, fz) or 1
        dx, dy = fx / n, -fz / n  # screen y points south
        c.create_polygon(mid + dx * 11, mid + dy * 11, mid - dx * 6 - dy * 6, mid - dy * 6 + dx * 6,
                         mid - dx * 6 + dy * 6, mid - dy * 6 - dx * 6, fill="#3399ff", outline="white", width=2)
        c.create_oval(3, 3, s - 4, s - 4, outline="#c8b27a", width=3)
        for ox, oy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
            c.create_text(mid + ox, 14 + oy, text="N", fill="black", font=("Segoe UI", 10, "bold"))
        c.create_text(mid, 14, text="N", fill="#ffd75e", font=("Segoe UI", 10, "bold"))
        c.create_text(mid, s - 18, text=f"{s / zoom * 8:.0f} m", fill="white", font=("Segoe UI", 8))


class SpotsWindow:
    """The saved spots of the route in a list; select one or more and delete them."""

    def __init__(self, root, route, positions, open_map, on_close):
        self.route, self.positions = route, positions
        self.win = tk.Toplevel(root)
        self.win.title("Route spots")
        self.win.protocol("WM_DELETE_WINDOW", on_close)
        body = ttk.Frame(self.win, padding=10)
        body.pack(fill="both", expand=True)
        self.title = ttk.Label(body, text="")
        self.title.pack(anchor="w")
        cols = (("n", "#", 40), ("note", "Note", 120), ("x", "x", 70), ("y", "height", 60), ("z", "z", 70),
                ("dist", "From you", 80))
        self.tree = ttk.Treeview(body, columns=[c[0] for c in cols], show="headings", height=16)
        for key, label, width in cols:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="w" if key == "note" else "e")
        self.tree.pack(fill="both", expand=True, pady=(4, 0))
        self.tree.bind("<Delete>", lambda e: self.delete_selected())
        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Delete selected", command=self.delete_selected).pack(side="left")
        ttk.Button(buttons, text="Delete all", command=self.delete_all).pack(side="left", padx=6)
        ttk.Button(buttons, text="Show on map", command=open_map).pack(side="right")
        self.shown = None  # the points as last put in the list

    def delete_selected(self):
        chosen = sorted((int(i) for i in self.tree.selection()), reverse=True)
        for i in chosen:
            self.route.remove(i)

    def delete_all(self):
        if self.route.points and messagebox.askyesno(
                "Route spots", f"Delete all {len(self.route.points)} spots of {self.route.name}?", parent=self.win):
            self.route.points.clear()
            self.route.index = 0
            self.route.save()

    def refresh(self):
        pts = self.route.points
        pos = self.positions()
        if pts != self.shown:  # spots added or deleted: rebuild the list
            self.shown = [dict(p) for p in pts]
            self.tree.delete(*self.tree.get_children())
            for i, p in enumerate(pts):
                self.tree.insert("", "end", iid=str(i), values=(i + 1, p.get("note", ""), f"{p['x']:.1f}",
                                                                f"{p['y']:.1f}", f"{p['z']:.1f}", ""))
        self.title.config(text=f"{self.route.name}: {len(pts)} spots, next is {self.route.index + 1}"
                          if pts else f"{self.route.name}: no spots yet (F6 in the game saves one)")
        for i, p in enumerate(pts):
            self.tree.set(str(i), "dist", f"{flat_dist(pos[1], p):.0f} m" if pos else "")


class MapWindow:
    """The world map with the route, the spots and you. Wheel zooms, dragging pans."""

    SIZE = 680

    def __init__(self, root, route, positions, on_close):
        self.route, self.positions = route, positions
        self.win = tk.Toplevel(root)
        self.win.title(f"Map: {route.name}")
        self.win.protocol("WM_DELETE_WINDOW", on_close)
        self.canvas = tk.Canvas(self.win, width=self.SIZE, height=self.SIZE, bg="#20262b", highlightthickness=0)
        self.canvas.pack()
        bar = ttk.Frame(self.win, padding=(8, 4))
        bar.pack(fill="x")
        self.follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Keep me in the middle", variable=self.follow).pack(side="left")
        ttk.Button(bar, text="Fit route", command=self.fit).pack(side="left", padx=6)
        self.info = ttk.Label(bar, text="")
        self.info.pack(side="right")
        self.img = map_image()
        self.side = self.img.width if self.img else 1280
        self.zoom = 3.0                       # screen pixels per map pixel
        self.centre = (self.side / 2, self.side / 2)  # map pixel at the middle of the window
        self.photo = None
        self.drag = None
        self.canvas.bind("<MouseWheel>", self.wheel)
        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.move)
        if route.points and not positions():
            self.fit()

    @staticmethod
    def load():
        if Image is None:
            return None
        try:
            with open(MAP_FILE, "rb") as f:
                data = f.read()
        except OSError:
            return None
        side = math.isqrt(len(data) // 4)
        if side * side * 4 != len(data):
            return None
        return Image.frombytes("RGBA", (side, side), data).convert("RGB")

    def to_map(self, x, z):
        return to_map(x, z, self.side)

    def to_screen(self, mx, my):
        return (mx - self.centre[0]) * self.zoom + self.SIZE / 2, (my - self.centre[1]) * self.zoom + self.SIZE / 2

    def fit(self):
        pts = [self.to_map(p["x"], p["z"]) for p in self.route.points]
        if not pts:
            return
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        self.centre = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
        span = max(max(xs) - min(xs), max(ys) - min(ys), 8)
        self.zoom = max(0.5, min(16.0, self.SIZE * 0.8 / span))
        self.follow.set(False)

    def wheel(self, event):
        factor = 1.25 if event.delta > 0 else 0.8
        new = max(0.5, min(16.0, self.zoom * factor))
        if not self.follow.get():  # keep the map point under the mouse where it is
            mx = self.centre[0] + (event.x - self.SIZE / 2) / self.zoom
            my = self.centre[1] + (event.y - self.SIZE / 2) / self.zoom
            self.centre = (mx - (event.x - self.SIZE / 2) / new, my - (event.y - self.SIZE / 2) / new)
        self.zoom = new

    def press(self, event):
        self.drag = (event.x, event.y, self.centre)

    def move(self, event):
        x0, y0, (cx, cy) = self.drag
        self.follow.set(False)
        self.centre = (cx - (event.x - x0) / self.zoom, cy - (event.y - y0) / self.zoom)

    def refresh(self):
        c, s = self.canvas, self.SIZE
        pos = self.positions()
        if pos and self.follow.get():
            self.centre = self.to_map(pos[1][0], pos[1][2])
        c.delete("all")
        if self.img is None:
            c.create_text(s / 2, s / 2, fill="white", width=s - 40, font=("Segoe UI", 11),
                          text=f"No map image: put embervale_realmap_1280.rgba in\n{os.path.dirname(MAP_FILE)}")
        else:
            half = s / 2 / self.zoom
            box = (self.centre[0] - half, self.centre[1] - half, self.centre[0] + half, self.centre[1] + half)
            view = self.img.crop(tuple(round(v) for v in box)).resize(
                (s, s), Image.NEAREST if self.zoom >= 3 else Image.BILINEAR)
            self.photo = ImageTk.PhotoImage(view)
            c.create_image(0, 0, image=self.photo, anchor="nw")
        pts = [self.to_screen(*self.to_map(p["x"], p["z"])) for p in self.route.points]
        if len(pts) > 1:
            c.create_line(*[v for p in pts for v in p], fill="#ffd75e", width=2, dash=(6, 4))
        for i, (x, y) in enumerate(pts):
            current = i == self.route.index
            r = 9 if current else 6
            c.create_oval(x - r, y - r, x + r, y + r, fill="#ffd75e" if current else "#e0e0e0", outline="black")
            c.create_text(x + r + 8, y, text=str(i + 1), fill="white", font=("Segoe UI", 10, "bold"))
        if pos:
            cam, player = pos
            x, y = self.to_screen(*self.to_map(player[0], player[2]))
            fx, fz = player[0] - cam[0], player[2] - cam[2]
            n = math.hypot(fx, fz) or 1
            dx, dy = fx / n, -fz / n  # screen y points south
            c.create_polygon(x + dx * 14, y + dy * 14, x - dx * 8 - dy * 8, y - dy * 8 + dx * 8,
                             x - dx * 8 + dy * 8, y - dy * 8 - dx * 8, fill="#3399ff", outline="white", width=2)
        self.info.config(text=f"{len(pts)} spots" + (f" | you {pos[1][0]:.0f}, {pos[1][2]:.0f}" if pos else ""))
