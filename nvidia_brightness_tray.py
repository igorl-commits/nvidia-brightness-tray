"""
NVIDIA Brightness Tray App
--------------------------
Sits in system tray, lets you adjust NVIDIA display brightness
via slider, hotkeys, or presets — no Control Panel needed.

Requirements:
    pip install pystray pillow keyboard pywin32

Run:
    pythonw nvidia_brightness_tray.py   (no console window)
    python  nvidia_brightness_tray.py   (with console for debugging)

Auto-start: Add a shortcut to shell:startup (Win+R → shell:startup)
"""

import sys
import threading
import ctypes
from ctypes import wintypes
import struct
import time

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
    print("'pywin32' not installed — using GDI fallback. pip install pywin32")


# ─── CONFIG ───────────────────────────────────────────────────────────────────

PRESETS = {
    "Night (30%)":  30,
    "Dim (50%)":    50,
    "Normal (75%)": 75,
    "Bright (91%)": 91,   # your current NVIDIA setting
    "Full (100%)":  100,
}

HOTKEY_UP   = "ctrl+alt+="   # Ctrl+Alt+Plus  → brightness +10
HOTKEY_DOWN = "ctrl+alt+-"   # Ctrl+Alt+Minus → brightness -10
STEP = 10

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

def _brightness_to_ramp(brightness_pct: int) -> bytes:
    """
    Convert 0-100 brightness % to a 256-entry gamma ramp.
    Each entry is a 16-bit value. At 100% → linear (identity).
    At lower values → compressed toward black.
    Matches roughly what NVIDIA CP does with its brightness slider.
    """
    scale = brightness_pct / 100.0
    ramp = []
    for i in range(256):
        val = int(i * scale * 257)   # 257 = 65535/255
        val = max(0, min(65535, val))
        ramp.append(val)
    # Ramp is 3 channels × 256 × 2 bytes (little-endian uint16)
    data = b""
    for channel in range(3):
        for v in ramp:
            data += struct.pack("<H", v)
    return data

def set_brightness(brightness_pct: int) -> bool:
    """Apply brightness via GDI gamma ramp. Returns True on success."""
    brightness_pct = max(10, min(100, brightness_pct))
    hdc = _get_primary_dc()
    if not hdc:
        return False
    ramp = _brightness_to_ramp(brightness_pct)
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


# ─── POWER RESUME HANDLING (RE-APPLY GAMMA RAMP) ──────────────────────────────

WM_POWERBROADCAST = 0x0218
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
WM_DESTROY = 0x0002


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


# ─── TRAY ICON ────────────────────────────────────────────────────────────────

class BrightnessApp:
    def __init__(self):
        self.brightness = 91  # start at your usual setting
        set_brightness(self.brightness)
        self.icon = self._create_icon()
        self._resume_reapply_lock = threading.Lock()

    def _reapply_after_resume(self):
        # Resume can happen before the display driver is fully ready.
        # Do a small retry loop to make re-apply reliable.
        if not self._resume_reapply_lock.acquire(blocking=False):
            return
        try:
            time.sleep(1.0)
            for _ in range(6):
                if set_brightness(self.brightness):
                    self.icon.title = f"NVIDIA Brightness: {self.brightness}% ✓"
                    return
                time.sleep(0.5)
            self.icon.title = f"NVIDIA Brightness: {self.brightness}% ✗ GDI failed"
        finally:
            self._resume_reapply_lock.release()

    def _make_icon_image(self, level: int) -> Image.Image:
        """Draw a dark rounded icon with green level number."""
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Dark rounded background
        draw.rounded_rectangle([2, 2, 62, 62], radius=12, fill=(30, 30, 30, 230))

        # Green number
        try:
            font = ImageFont.truetype("arialbd.ttf", 26)
        except Exception:
            try:
                font = ImageFont.truetype("arial.ttf", 26)
            except Exception:
                font = ImageFont.load_default()
        text = str(level)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        tx = (size - tw) // 2 - bbox[0]
        ty = (size - th) // 2 - bbox[1]
        draw.text((tx, ty), text, fill=(80, 220, 80, 255), font=font)

        return img

    def _create_icon(self):
        img = self._make_icon_image(self.brightness)

        def on_quit(icon, item):
            icon.stop()

        def make_preset_handler(val):
            def handler(icon, item):
                self._apply(val)
            return handler

        preset_items = [
            item(label, make_preset_handler(val))
            for label, val in PRESETS.items()
        ]

        menu = pystray.Menu(
            item("☀ Brightness", pystray.Menu(*preset_items)),
            pystray.Menu.SEPARATOR,
            item(lambda _: f"Current: {self.brightness}%", lambda i, it: None, enabled=False),
            pystray.Menu.SEPARATOR,
            item("Brighter  (Ctrl+Alt+=)", lambda i, it: self._step(+STEP)),
            item("Dimmer    (Ctrl+Alt+-)", lambda i, it: self._step(-STEP)),
            pystray.Menu.SEPARATOR,
            item("Quit", on_quit),
        )

        return pystray.Icon(
            "nvidia_brightness",
            img,
            f"NVIDIA Brightness: {self.brightness}%",
            menu,
        )

    def _apply(self, level: int):
        self.brightness = max(10, min(100, level))
        ok = set_brightness(self.brightness)
        status = "✓" if ok else "✗ GDI failed"
        self.icon.icon = self._make_icon_image(self.brightness)
        self.icon.title = f"NVIDIA Brightness: {self.brightness}% {status}"
        print(f"Brightness → {self.brightness}% ({status})")

    def _step(self, delta: int):
        self._apply(self.brightness + delta)

    def _setup_hotkeys(self):
        if not HAS_KEYBOARD:
            return
        keyboard.add_hotkey(HOTKEY_UP,   lambda: self._step(+STEP), suppress=True)
        keyboard.add_hotkey(HOTKEY_DOWN, lambda: self._step(-STEP), suppress=True)
        print(f"Hotkeys: {HOTKEY_UP} / {HOTKEY_DOWN}")

    def run(self):
        # Hotkeys in background thread
        if HAS_KEYBOARD:
            t = threading.Thread(target=self._setup_hotkeys, daemon=True)
            t.start()

        _start_power_event_listener(self._reapply_after_resume)

        print(f"Tray running. Brightness: {self.brightness}%")
        print(f"Hotkeys: Ctrl+Alt+= (up), Ctrl+Alt+- (down)")
        self.icon.run()


# ─── ENTRY ───────────────────────────────────────────────────────────────────

def _ensure_autostart():
    """Create a startup shortcut if one doesn't exist yet."""
    import os
    startup_dir = os.path.join(
        os.environ.get("APPDATA", ""),
        r"Microsoft\Windows\Start Menu\Programs\Startup",
    )
    lnk_path = os.path.join(startup_dir, "NvidiaBrightnessTray.lnk")
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
        print(f"Auto-start shortcut created: {lnk_path}")
    except Exception as e:
        print(f"Could not create auto-start shortcut: {e}")


if __name__ == "__main__":
    # Require admin for gamma ramp on some systems
    if ctypes.windll.shell32.IsUserAnAdmin() == 0:
        print("Note: Run as Administrator if brightness changes don't apply.")

    _ensure_autostart()

    app = BrightnessApp()
    app.run()
