"""
NVIDIA Brightness Tray App
--------------------------
Sits in system tray, lets you adjust display brightness and color
temperature (warmth) via menu presets and hotkeys — no Control Panel
needed. Re-applies the gamma ramp after sleep/resume and display changes
(e.g. exiting fullscreen games).

Hotkeys are registered with the Win32 RegisterHotKey API (Ctrl+Alt +/-),
which is owned by the OS and survives sleep/resume — unlike a low-level
keyboard hook, which Windows silently tears down across suspend.

Requirements:
    pip install pystray pillow

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


# ─── CONFIG ───────────────────────────────────────────────────────────────────

STEP = 10

# ─── GLOBAL HOTKEYS (Win32 RegisterHotKey) ───────────────────────────────────
# RegisterHotKey is OS-owned system state: it survives sleep/resume and is not
# subject to the low-level-hook timeout teardown that breaks `keyboard`-style
# hooks after the machine wakes. The combo is consumed by the OS (no separate
# "suppress" needed). Each combo posts WM_HOTKEY (with our id in wParam) to the
# thread that registered it — here, the power-event message loop.

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
# Virtual-key codes for the +/- keys, main row and numpad (layouts/numpad differ).
VK_OEM_PLUS = 0xBB   # '=' / '+' on the main row
VK_OEM_MINUS = 0xBD  # '-' / '_' on the main row
VK_ADD = 0x6B        # numpad '+'
VK_SUBTRACT = 0x6D   # numpad '-'


def _build_hotkey_table(on_brighter, on_dimmer):
    """Map a stable hotkey id -> (modifiers, virtual_key, handler).

    Pure function (no Win32 calls) so the id->handler dispatch is unit-testable.
    Ctrl+Alt with several +/- variants so it works regardless of which +/- key
    the user presses (main row vs numpad).
    """
    mods = MOD_CONTROL | MOD_ALT
    return {
        1: (mods, VK_OEM_PLUS, on_brighter),
        2: (mods, VK_ADD, on_brighter),
        3: (mods, VK_OEM_MINUS, on_dimmer),
        4: (mods, VK_SUBTRACT, on_dimmer),
    }

SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", ""), "NvidiaBrightnessTray")
SETTINGS_PATH = os.path.join(SETTINGS_DIR, "settings.json")
_settings_lock = threading.RLock()
_instance_mutex = None

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

def _load_settings(default_brightness: int = 91, default_warmth: int = 0):
    """Load (brightness, warmth) from disk. Missing warmth key -> 0 (old format)."""
    with _settings_lock:
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            b = int(data.get("brightness", default_brightness))
            w = int(data.get("warmth", default_warmth))
            return max(10, min(100, b)), max(0, min(100, w))
        except Exception:
            return default_brightness, default_warmth


def _write_settings_raw_unlocked(data: dict) -> None:
    temp_path = f"{SETTINGS_PATH}.tmp"
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(temp_path, SETTINGS_PATH)
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass


def _save_settings(brightness: int, warmth: int) -> None:
    """Persist brightness + warmth atomically."""
    with _settings_lock:
        _write_settings_raw_unlocked({
            "brightness": int(max(10, min(100, brightness))),
            "warmth": int(max(0, min(100, warmth))),
        })


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
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
WM_DESTROY = 0x0002
WM_DISPLAYCHANGE = 0x007E


def _start_power_event_listener(on_resume_callback, hotkey_table=None):
    """
    Start a background thread that owns a hidden window + message loop.

    It listens for Windows power/display events (calling on_resume_callback when
    the system resumes or the display changes) and, if hotkey_table is given,
    registers those global hotkeys via RegisterHotKey and dispatches their
    handlers on WM_HOTKEY. Hotkeys live on this thread because WM_HOTKEY is
    delivered to the thread that registered them.
    """
    hotkey_table = hotkey_table or {}

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
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = wintypes.BOOL

        @WNDPROCTYPE
        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_HOTKEY:
                entry = hotkey_table.get(int(wparam))
                if entry:
                    try:
                        entry[2]()   # (modifiers, vk, handler)
                    except Exception:
                        pass
                return 0
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

        # Register global hotkeys on this thread so WM_HOTKEY lands in wndproc.
        registered = []
        for hk_id, (mods, vk, _handler) in hotkey_table.items():
            if user32.RegisterHotKey(hwnd, hk_id, mods, vk):
                registered.append(hk_id)
        if hotkey_table:
            print(f"Hotkeys registered: {len(registered)}/{len(hotkey_table)} combos")

        try:
            msg_obj = MSG()
            while user32.GetMessageW(ctypes.byref(msg_obj), None, 0, 0) != 0:
                user32.TranslateMessage(ctypes.byref(msg_obj))
                user32.DispatchMessageW(ctypes.byref(msg_obj))
        finally:
            for hk_id in registered:
                user32.UnregisterHotKey(hwnd, hk_id)

    t = threading.Thread(target=_thread_main, daemon=True)
    t.start()
    return t


def _enable_system_theme_menus() -> None:
    """Opt the process into dark-mode-aware controls so the native tray
    context menu follows the system light/dark setting.

    Native Win32 popup menus render light by default; the process must opt
    in via undocumented uxtheme.dll ordinals (stable since Win10 1903):
      135 = SetPreferredAppMode(PreferredAppMode)  -> 1 = AllowDark (follow system)
      136 = FlushMenuThemes()                      -> repaint themed menus
    """
    try:
        uxtheme = ctypes.WinDLL("uxtheme.dll")
        set_preferred_app_mode = uxtheme[135]
        set_preferred_app_mode.argtypes = [ctypes.c_int]
        set_preferred_app_mode.restype = ctypes.c_int
        set_preferred_app_mode(1)  # AllowDark = honor system theme, both ways
        flush_menu_themes = uxtheme[136]
        flush_menu_themes.argtypes = []
        flush_menu_themes.restype = None
        flush_menu_themes()
    except Exception as e:
        print(f"Could not enable system-theme menus: {e}")


# ─── TRAY ICON ────────────────────────────────────────────────────────────────

class BrightnessApp:
    def __init__(self):
        self._state_lock = threading.RLock()
        self.brightness, self.warmth = _load_settings()
        self._last_apply_ok = set_brightness(self.brightness, self.warmth)
        _save_settings(self.brightness, self.warmth)  # ensure settings.json exists after first launch
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
                with self._state_lock:
                    brightness = self.brightness
                    warmth = self.warmth
                    self._last_apply_ok = set_brightness(brightness, warmth)
                    if self._last_apply_ok:
                        self.icon.title = (
                            f"NVIDIA Brightness: {brightness}% · warm {warmth}% ✓"
                        )
                        return
                time.sleep(0.5)
            with self._state_lock:
                self.icon.title = (
                    f"NVIDIA Brightness: {self.brightness}% · "
                    f"warm {self.warmth}% ✗ GDI failed"
                )
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
        with self._state_lock:
            if brightness is not None:
                self.brightness = max(10, min(100, int(brightness)))
            if warmth is not None:
                self.warmth = max(0, min(100, int(warmth)))
            self._last_apply_ok = set_brightness(self.brightness, self.warmth)
            self._commit_state_locked()

    def _commit_state_locked(self):
        _save_settings(self.brightness, self.warmth)
        status = "✓" if self._last_apply_ok else "✗ GDI failed"
        self.icon.icon = self._make_icon_image(self.brightness, self.warmth)
        self.icon.title = f"NVIDIA Brightness: {self.brightness}% · warm {self.warmth}% {status}"
        try:
            self.icon.update_menu()
        except Exception:
            pass
        print(f"Brightness → {self.brightness}%  Warmth → {self.warmth}% ({status})")

    def _step(self, delta: int):
        with self._state_lock:
            self._apply(brightness=self.brightness + delta)

    def _toggle_autostart(self, icon, item):
        if is_autostart_enabled():
            disable_autostart()
        else:
            enable_autostart()

    def run(self):
        # One background thread owns the message loop: power/display events
        # AND global hotkeys. RegisterHotKey-based hotkeys survive sleep/resume,
        # so there's nothing to re-arm on wake.
        hotkey_table = _build_hotkey_table(
            on_brighter=lambda: self._step(+STEP),
            on_dimmer=lambda: self._step(-STEP),
        )
        _start_power_event_listener(self._reapply_after_resume, hotkey_table)

        print(f"Tray running. Brightness: {self.brightness}%")
        print("Hotkeys: Ctrl+Alt plus (up), Ctrl+Alt minus (down)")
        self.icon.run()


# ─── ENTRY ───────────────────────────────────────────────────────────────────

def _acquire_single_instance() -> bool:
    """Hold a named mutex for the lifetime of this process."""
    global _instance_mutex
    ERROR_ALREADY_EXISTS = 183
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateMutexW(
        None,
        False,
        r"Local\NvidiaBrightnessTray",
    )
    if not handle:
        return False
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _instance_mutex = handle
    return True


def _release_single_instance() -> None:
    global _instance_mutex
    if _instance_mutex:
        ctypes.windll.kernel32.CloseHandle(_instance_mutex)
        _instance_mutex = None


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
    if not _acquire_single_instance():
        raise SystemExit(0)

    # Require admin for gamma ramp on some systems
    if ctypes.windll.shell32.IsUserAnAdmin() == 0:
        print("Note: Run as Administrator if brightness changes don't apply.")

    try:
        first_run = not os.path.exists(SETTINGS_PATH)

        _enable_system_theme_menus()  # native tray menu follows system dark/light

        app = BrightnessApp()  # writes settings.json, so after this first_run is consumed

        if first_run:
            enable_autostart()

        app.run()
    finally:
        _release_single_instance()
