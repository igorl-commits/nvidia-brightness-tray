"""
NVIDIA Brightness Tray App
--------------------------
Sits in system tray, lets you adjust display brightness and color
temperature (warmth) via a popup slider window, menu presets, or
hotkeys — no Control Panel needed. Re-applies the gamma ramp after
sleep/resume and display changes (e.g. exiting fullscreen games).

Requirements:
    pip install pystray pillow keyboard pywin32

Run:
    pythonw nvidia_brightness_tray.py   (no console window)
    python  nvidia_brightness_tray.py   (with console for debugging)

Auto-start: toggle "Start with Windows" in the tray menu (creates/removes
a shortcut in shell:startup). Created automatically on first launch.
"""

import sys
import threading
import ctypes
from ctypes import wintypes
import struct
import time
import json
import os

try:
    import pystray
    from pystray import MenuItem as item
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("Missing deps. Run: pip install pystray pillow")
    sys.exit(1)

try:
    import keyboard
    HAS_KEYBOARD = True
except ImportError:
    HAS_KEYBOARD = False
    print("'keyboard' not installed — hotkeys disabled. pip install keyboard")

try:
    import win32api
    import win32con
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False
    print("'pywin32' not installed — some advanced features disabled. pip install pywin32")


# ─── CONFIG ───────────────────────────────────────────────────────────────────

# Register multiple aliases because keyboard layouts and numpad naming differ.
HOTKEY_UP_BINDINGS = [
    "ctrl+alt+=",
    "ctrl+alt++",
    "ctrl+alt+plus",
    "ctrl+alt+add",
]
HOTKEY_DOWN_BINDINGS = [
    "ctrl+alt+-",
    "ctrl+alt+minus",
    "ctrl+alt+subtract",
]
STEP = 10

SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", ""), "NvidiaBrightnessTray")
SETTINGS_PATH = os.path.join(SETTINGS_DIR, "settings.json")

# ─── GAMMA / BRIGHTNESS VIA GDI ──────────────────────────────────────────────
# Windows GDI SetDeviceGammaRamp works at the driver level — same effect as
# the NVIDIA Control Panel brightness slider (it sets the GPU gamma LUT).

gdi32 = ctypes.windll.gdi32
user32 = ctypes.windll.user32

def _get_primary_dc():
    """Get device context for the primary display."""
    return user32.GetDC(None)

def _release_dc(hdc):
    user32.ReleaseDC(None, hdc)

def _compute_ramp(brightness_pct: int, warmth_pct: int = 0) -> bytes:
    """
    Build a 3×256 uint16 gamma ramp from brightness (10-100) and warmth (0-100).
    Red is untouched; green and blue are scaled down as warmth rises (blue most),
    producing an amber, lower-blue-light tint. At warmth=0 all channels are equal
    (matches the original single-channel ramp). At brightness=100, warmth=0 the
    ramp is identity. Matches roughly what NVIDIA CP does with its brightness slider.
    """
    brightness_pct = max(10, min(100, int(brightness_pct)))
    warmth_pct = max(0, min(100, int(warmth_pct)))
    b_scale = brightness_pct / 100.0
    green_scale = 1.0 - 0.20 * (warmth_pct / 100.0)
    blue_scale = 1.0 - 0.55 * (warmth_pct / 100.0)
    channel_scales = (1.0, green_scale, blue_scale)  # R, G, B order
    # Ramp is 3 channels × 256 × 2 bytes (little-endian uint16)
    data = b""
    for cs in channel_scales:
        for i in range(256):
            val = int(i * b_scale * cs * 257)   # 257 = 65535/255
            val = max(0, min(65535, val))
            data += struct.pack("<H", val)
    return data

def set_brightness(brightness_pct: int, warmth_pct: int = 0) -> bool:
    """Apply brightness + warmth via GDI gamma ramp. Returns True on success."""
    brightness_pct = max(10, min(100, brightness_pct))
    warmth_pct = max(0, min(100, warmth_pct))
    hdc = _get_primary_dc()
    if not hdc:
        return False
    ramp = _compute_ramp(brightness_pct, warmth_pct)
    # SetDeviceGammaRamp expects a 3×256 array of WORD
    GammaArray = ctypes.c_uint16 * (3 * 256)
    gamma = GammaArray(*struct.unpack("<768H", ramp))
    result = gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(gamma))
    _release_dc(hdc)
    return bool(result)

def get_current_brightness() -> int:
    """Read current gamma ramp and estimate brightness %."""
    hdc = _get_primary_dc()
    if not hdc:
        return 100
    GammaArray = ctypes.c_uint16 * (3 * 256)
    gamma = GammaArray()
    ok = gdi32.GetDeviceGammaRamp(hdc, ctypes.byref(gamma))
    _release_dc(hdc)
    if not ok:
        return 100
    # Sample midpoint of red channel to estimate level
    mid = gamma[128]  # index 128 in red channel
    approx = int((mid / 65535) * 100 / (128/255))
    return max(10, min(100, approx))


def _load_settings(default_brightness: int = 91, default_warmth: int = 0):
    """Load (brightness, warmth) from disk. Missing warmth key -> 0 (old format)."""
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        b = int(data.get("brightness", default_brightness))
        w = int(data.get("warmth", default_warmth))
        return max(10, min(100, b)), max(0, min(100, w))
    except Exception:
        return default_brightness, default_warmth


def _save_settings(brightness: int, warmth: int) -> None:
    """Persist brightness + warmth for next app launch."""
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "brightness": int(max(10, min(100, brightness))),
                    "warmth": int(max(0, min(100, warmth))),
                },
                f,
            )
    except Exception:
        pass


def _make_tray_icon_image(brightness: int, warmth: int = 0) -> "Image.Image":
    """
    Generate the 64×64 tray icon.

    - Large brightness percentage as primary signal (legacy behavior preserved).
    - Thin amber bar at bottom appears only when warmth > 0 (secondary, calm indicator).
    - Number color warms slightly toward amber as warmth increases.
    Pure function — no side effects, fully unit-testable.
    """
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Dark rounded background with subtle border for polish
    draw.rounded_rectangle(
        [1, 1, 62, 62],
        radius=11,
        fill=(26, 26, 26, 245),
        outline=(45, 45, 45, 200),
        width=1,
    )

    # Brightness number (tuned size for good legibility at tray scale)
    try:
        font = ImageFont.truetype("arialbd.ttf", 28)
    except Exception:
        try:
            font = ImageFont.truetype("arial.ttf", 28)
        except Exception:
            font = ImageFont.load_default()

    text = str(brightness)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = (size - tw) // 2 - bbox[0]
    ty = (size - th) // 2 - bbox[1] - 2  # minor optical centering adjustment

    # Gentle color shift toward warm amber as warmth rises (secondary cue)
    r = int(90 + (warmth * 0.85))
    g = int(215 - (warmth * 0.65))
    number_color = (max(60, min(255, r)), max(110, min(255, g)), 70, 255)
    draw.text((tx, ty), text, fill=number_color, font=font)

    # Warmth indicator — thin bottom bar, only visible when active
    if warmth > 0:
        bar_y = 52
        bar_h = 5
        track_color = (18, 18, 18, 255)
        fill_color = (255, 165, 50, 255)  # warm amber

        draw.rounded_rectangle([6, bar_y, 57, bar_y + bar_h], radius=2, fill=track_color)
        fill_w = int((warmth / 100.0) * 51)
        if fill_w > 2:
            draw.rounded_rectangle([6, bar_y, 6 + fill_w, bar_y + bar_h], radius=2, fill=fill_color)

    return img


# ─── POWER RESUME HANDLING (RE-APPLY GAMMA RAMP) ──────────────────────────────

WM_POWERBROADCAST = 0x0218
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
WM_DESTROY = 0x0002
WM_DISPLAYCHANGE = 0x007E


def _start_power_event_listener(on_resume_callback):
    """
    Start a background thread that listens for Windows power events.
    When the system resumes, call on_resume_callback().
    """

    # Ensure correct 64-bit types (prevents LPARAM overflow on modern Windows).
    LRESULT_T = ctypes.c_int64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
    WNDPROCTYPE = ctypes.WINFUNCTYPE(LRESULT_T, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASS(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROCTYPE),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HCURSOR),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("message", wintypes.UINT),
            ("wParam", wintypes.WPARAM),
            ("lParam", wintypes.LPARAM),
            ("time", wintypes.DWORD),
            ("pt", wintypes.POINT),
        ]

    def _thread_main():
        hinstance = ctypes.windll.kernel32.GetModuleHandleW(None)
        class_name = "NvidiaBrightnessTrayPowerListener"

        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = LRESULT_T
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = ctypes.c_int
        user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
        user32.TranslateMessage.restype = wintypes.BOOL
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
        user32.DispatchMessageW.restype = LRESULT_T

        @WNDPROCTYPE
        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_POWERBROADCAST:
                if wparam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                    try:
                        on_resume_callback()
                    except Exception:
                        pass
                return 1
            if msg == WM_DISPLAYCHANGE:
                try:
                    on_resume_callback()
                except Exception:
                    pass
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        wc = WNDCLASS()
        wc.lpfnWndProc = wndproc
        wc.hInstance = hinstance
        wc.lpszClassName = class_name

        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            # If already registered in this process, RegisterClassW fails; proceed anyway.
            pass

        hwnd = user32.CreateWindowExW(
            0,
            class_name,
            class_name,
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            hinstance,
            None,
        )
        if not hwnd:
            return

        msg_obj = MSG()
        while user32.GetMessageW(ctypes.byref(msg_obj), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg_obj))
            user32.DispatchMessageW(ctypes.byref(msg_obj))

    t = threading.Thread(target=_thread_main, daemon=True)
    t.start()
    return t


def _is_windows_dark_theme() -> bool:
    """Detect if Windows is using dark theme for apps (0 = dark)."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        )
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return value == 0
    except Exception:
        return True  # default dark


# ─── SLIDER WINDOW (TKINTER) ──────────────────────────────────────────────────

class SliderWindow:
    """
    Hidden Tk root on its own daemon thread. Shown on demand via the
    thread-safe `event_generate` call. Sliders drive on_change(brightness, warmth).
    """

    def __init__(self, on_change, get_state):
        self.on_change = on_change      # callable(brightness:int, warmth:int)
        self.get_state = get_state      # callable() -> (brightness:int, warmth:int)
        self.root = None
        self._syncing = False
        self._tk = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            import tkinter as tk
            from tkinter import ttk
        except Exception as e:
            print(f"Tkinter unavailable — sliders disabled: {e}")
            return

        self._tk = tk
        self.root = tk.Tk()

        # Glass / borderless modern look (no borders, draggable)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)

        # Detect and apply system theme
        self._is_dark = _is_windows_dark_theme()

        bg = "#1f1f1f" if self._is_dark else "#f3f3f3"
        fg = "#e0e0e0" if self._is_dark else "#1f1f1f"
        accent_b = "#4a9eff" if self._is_dark else "#0078d4"
        accent_w = "#ff9f4a" if self._is_dark else "#e07a2f"

        self.root.configure(bg=bg)

        # Draggable (no title bar)
        self._drag_data = {"x": 0, "y": 0}
        self.root.bind("<ButtonPress-1>", self._start_drag)
        self.root.bind("<B1-Motion>", self._do_drag)

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        # Very thin modern sliders
        style.configure("ThinB.Horizontal.TScale", background=bg, troughcolor="#3a3a3a" if self._is_dark else "#d0d0d0",
                        sliderlength=10, sliderthickness=5)
        style.configure("ThinW.Horizontal.TScale", background=bg, troughcolor="#3a3a3a" if self._is_dark else "#d0d0d0",
                        sliderlength=10, sliderthickness=5)

        b0, w0 = self.get_state()
        self.b_var = tk.IntVar(value=b0)
        self.w_var = tk.IntVar(value=w0)

        # Only B and W labels (big, clear)
        self.b_label = tk.Label(self.root, text="B", bg=bg, fg=accent_b, font=("Segoe UI", 18, "bold"))
        self.w_label = tk.Label(self.root, text="W", bg=bg, fg=accent_w, font=("Segoe UI", 18, "bold"))

        self.b_value = tk.Label(self.root, text=f"{b0}%", bg=bg, fg=fg, font=("Segoe UI", 11))
        self.w_value = tk.Label(self.root, text=f"{w0}%", bg=bg, fg=fg, font=("Segoe UI", 11))

        self.b_scale = ttk.Scale(self.root, from_=10, to=100, orient=tk.HORIZONTAL,
                                 variable=self.b_var, command=self._on_slider,
                                 style="ThinB.Horizontal.TScale")
        self.w_scale = ttk.Scale(self.root, from_=0, to=100, orient=tk.HORIZONTAL,
                                 variable=self.w_var, command=self._on_slider,
                                 style="ThinW.Horizontal.TScale")

        # Tight glass-like layout
        self.b_label.grid(row=0, column=0, padx=(10, 3), pady=(8, 1), sticky="w")
        self.b_scale.grid(row=0, column=1, padx=2, pady=(8, 1), sticky="ew")
        self.b_value.grid(row=0, column=2, padx=(3, 10), pady=(8, 1))

        self.w_label.grid(row=1, column=0, padx=(10, 3), pady=(1, 6), sticky="w")
        self.w_scale.grid(row=1, column=1, padx=2, pady=(1, 6), sticky="ew")
        self.w_value.grid(row=1, column=2, padx=(3, 10), pady=(1, 6))

        # Subtle preview bar
        preview_bg = "#2a2a2a" if self._is_dark else "#e8e8e8"
        self.preview = tk.Canvas(self.root, width=260, height=14, bg=preview_bg,
                                 highlightthickness=0, bd=0)
        self.preview.grid(row=2, column=0, columnspan=3, padx=10, pady=(0, 8), sticky="ew")

        self.root.grid_columnconfigure(1, weight=1)

        self.root.protocol("WM_DELETE_WINDOW", self._hide)
        self.root.bind("<<ShowSliders>>", lambda e: self._show())
        self.root.bind("<Escape>", lambda e: self._hide())
        self.root.withdraw()
        self.root.mainloop()

    def _start_drag(self, event):
        self._drag_data = {"x": event.x, "y": event.y}

    def _do_drag(self, event):
        x = self.root.winfo_x() + event.x - self._drag_data["x"]
        y = self.root.winfo_y() + event.y - self._drag_data["y"]
        self.root.geometry(f"+{x}+{y}")

    def _on_slider(self, _value=None):
        if self._syncing:
            return
        b = self.b_var.get()
        w = self.w_var.get()
        self._update_preview_and_labels(b, w)
        self.on_change(b, w)

    def _update_preview_and_labels(self, b: int, w: int):
        if not self.root:
            return

        self.b_value.config(text=f"{b}%")
        self.w_value.config(text=f"{w}%")

        c = self.preview
        c.delete("all")
        width = c.winfo_width() or 260
        height = 14

        for x in range(width):
            warmth_influence = w / 100.0
            if self._is_dark:
                r = int(80 * (1 - warmth_influence * 0.3) + 255 * warmth_influence * 0.9)
                g = int(90 * (1 - warmth_influence * 0.2) + 170 * warmth_influence)
                b_col = int(110 * (1 - warmth_influence * 0.5) + 70 * warmth_influence)
            else:
                r = int(200 + 55 * warmth_influence * 0.8)
                g = int(210 - 40 * warmth_influence * 0.5)
                b_col = int(220 - 150 * warmth_influence * 0.7)
            color = f"#{max(0, min(255, r)):02x}{max(0, min(255, g)):02x}{max(0, min(255, b_col)):02x}"
            c.create_line(x, 0, x, height, fill=color)

    def _show(self):
        self._syncing = True
        b, w = self.get_state()
        self.b_var.set(b)
        self.w_var.set(w)
        self._syncing = False
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)

        try:
            self.root.grab_set()
        except Exception:
            pass

        self.root.bind("<FocusOut>", self._on_focus_out, add="+")
        self.root.after(50, lambda: self._update_preview_and_labels(b, w))

    def _on_focus_out(self, event):
        if self.root:
            self.root.after(60, self._hide)

    def _hide(self):
        if self.root:
            try:
                self.root.grab_release()
            except Exception:
                pass
            self.root.withdraw()

    def request_show(self):
        if self.root is None:
            return
        try:
            self.root.event_generate("<<ShowSliders>>", when="tail")
        except Exception as e:
            print(f"Could not open sliders: {e}")


# ─── TRAY ICON ────────────────────────────────────────────────────────────────

class BrightnessApp:
    def __init__(self):
        self.brightness, self.warmth = _load_settings()
        set_brightness(self.brightness, self.warmth)
        _save_settings(self.brightness, self.warmth)  # ensure settings.json exists after first launch
        self.icon = self._create_icon()
        self._resume_reapply_lock = threading.Lock()
        self.slider = SliderWindow(
            on_change=lambda b, w: self._apply(brightness=b, warmth=w),
            get_state=lambda: (self.brightness, self.warmth),
        )

    def _reapply_after_resume(self):
        # Resume can happen before the display driver is fully ready.
        # Do a small retry loop to make re-apply reliable.
        if not self._resume_reapply_lock.acquire(blocking=False):
            return
        try:
            time.sleep(1.0)
            for _ in range(6):
                if set_brightness(self.brightness, self.warmth):
                    self.icon.title = f"NVIDIA Brightness: {self.brightness}% ✓"
                    return
                time.sleep(0.5)
            self.icon.title = f"NVIDIA Brightness: {self.brightness}% ✗ GDI failed"
        finally:
            self._resume_reapply_lock.release()

    def _make_icon_image(self, brightness: int, warmth: int = 0) -> Image.Image:
        """Delegate to the pure module-level drawer (keeps class API small)."""
        return _make_tray_icon_image(brightness, warmth)

    def _create_icon(self):
        img = self._make_icon_image(self.brightness, self.warmth)

        def on_quit(icon, item):
            icon.stop()

        BRIGHTNESS_STEPS = list(range(100, 0, -10))  # 100,90,...,10
        WARMTH_PRESETS = [("Off", 0), ("Low", 25), ("Medium", 50),
                          ("High", 75), ("Max", 100)]

        def make_b_handler(val):
            return lambda i, it: self._apply(brightness=val)

        def make_w_handler(val):
            return lambda i, it: self._apply(warmth=val)

        brightness_items = [
            item(f"{v}%", make_b_handler(v),
                 checked=lambda it, v=v: round(self.brightness / 10) * 10 == v,
                 radio=True)
            for v in BRIGHTNESS_STEPS
        ]
        warmth_items = [
            item(f"{label} ({v}%)", make_w_handler(v),
                 checked=lambda it, v=v: self.warmth == v,
                 radio=True)
            for label, v in WARMTH_PRESETS
        ]

        menu = pystray.Menu(
            item("Open sliders…", lambda i, it: self.slider.request_show()),
            pystray.Menu.SEPARATOR,
            item("☀ Brightness", pystray.Menu(*brightness_items)),
            item("🌅 Warmth", pystray.Menu(*warmth_items)),
            pystray.Menu.SEPARATOR,
            item(lambda _: f"Current: {self.brightness}% · warm {self.warmth}%",
                 lambda i, it: None, enabled=False),
            pystray.Menu.SEPARATOR,
            item("Brighter  (Ctrl+Alt +)", lambda i, it: self._step(+STEP)),
            item("Dimmer    (Ctrl+Alt -)", lambda i, it: self._step(-STEP)),
            item("Reset display (100%, no warmth)",
                 lambda i, it: self._apply(brightness=100, warmth=0)),
            pystray.Menu.SEPARATOR,
            item("Start with Windows", self._toggle_autostart,
                 checked=lambda it: is_autostart_enabled()),
            item("Quit", on_quit),
        )

        return pystray.Icon(
            "nvidia_brightness",
            img,
            f"NVIDIA Brightness: {self.brightness}% · warm {self.warmth}%",
            menu,
        )

    def _apply(self, brightness=None, warmth=None):
        if brightness is not None:
            self.brightness = max(10, min(100, int(brightness)))
        if warmth is not None:
            self.warmth = max(0, min(100, int(warmth)))
        ok = set_brightness(self.brightness, self.warmth)
        _save_settings(self.brightness, self.warmth)
        status = "✓" if ok else "✗ GDI failed"
        self.icon.icon = self._make_icon_image(self.brightness, self.warmth)
        self.icon.title = f"NVIDIA Brightness: {self.brightness}% · warm {self.warmth}% {status}"
        try:
            self.icon.update_menu()
        except Exception:
            pass
        print(f"Brightness → {self.brightness}%  Warmth → {self.warmth}% ({status})")

    def _step(self, delta: int):
        self._apply(brightness=self.brightness + delta)

    def _toggle_autostart(self, icon, item):
        if is_autostart_enabled():
            disable_autostart()
        else:
            enable_autostart()

    def _setup_hotkeys(self):
        if not HAS_KEYBOARD:
            return
        registered = []
        for hk in HOTKEY_UP_BINDINGS:
            try:
                keyboard.add_hotkey(hk, lambda: self._step(+STEP), suppress=True)
                registered.append(hk)
            except Exception:
                pass
        for hk in HOTKEY_DOWN_BINDINGS:
            try:
                keyboard.add_hotkey(hk, lambda: self._step(-STEP), suppress=True)
                registered.append(hk)
            except Exception:
                pass
        if registered:
            print(f"Hotkeys registered: {', '.join(registered)}")
        else:
            print("No hotkeys were registered.")

    def run(self):
        # Hotkeys in background thread
        if HAS_KEYBOARD:
            t = threading.Thread(target=self._setup_hotkeys, daemon=True)
            t.start()

        _start_power_event_listener(self._reapply_after_resume)

        print(f"Tray running. Brightness: {self.brightness}%")
        print("Hotkeys: Ctrl+Alt plus (up), Ctrl+Alt minus (down)")
        self.icon.run()


# ─── ENTRY ───────────────────────────────────────────────────────────────────

def _startup_lnk_path() -> str:
    startup_dir = os.path.join(
        os.environ.get("APPDATA", ""),
        r"Microsoft\Windows\Start Menu\Programs\Startup",
    )
    return os.path.join(startup_dir, "NvidiaBrightnessTray.lnk")


def is_autostart_enabled() -> bool:
    return os.path.exists(_startup_lnk_path())


def enable_autostart() -> None:
    """Create the startup shortcut if missing."""
    lnk_path = _startup_lnk_path()
    if os.path.exists(lnk_path):
        return
    try:
        pythonw = sys.executable.replace("python.exe", "pythonw.exe")
        script = os.path.abspath(__file__)
        ps = (
            f'$ws = New-Object -ComObject WScript.Shell; '
            f'$s = $ws.CreateShortcut("{lnk_path}"); '
            f'$s.TargetPath = "{pythonw}"; '
            f'$s.Arguments = \'"{script}"\'; '
            f'$s.WorkingDirectory = "{os.path.dirname(script)}"; '
            f'$s.Description = "NVIDIA Brightness Tray"; '
            f'$s.Save()'
        )
        import subprocess
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True,
                       capture_output=True)
        print(f"Auto-start enabled: {lnk_path}")
    except Exception as e:
        print(f"Could not enable auto-start: {e}")


def disable_autostart() -> None:
    """Remove the startup shortcut if present."""
    lnk_path = _startup_lnk_path()
    try:
        if os.path.exists(lnk_path):
            os.remove(lnk_path)
            print(f"Auto-start disabled: {lnk_path}")
    except Exception as e:
        print(f"Could not disable auto-start: {e}")


if __name__ == "__main__":
    # Require admin for gamma ramp on some systems
    if ctypes.windll.shell32.IsUserAnAdmin() == 0:
        print("Note: Run as Administrator if brightness changes don't apply.")

    first_run = not os.path.exists(SETTINGS_PATH)

    app = BrightnessApp()  # writes settings.json, so after this first_run is consumed

    if first_run:
        enable_autostart()

    app.run()
