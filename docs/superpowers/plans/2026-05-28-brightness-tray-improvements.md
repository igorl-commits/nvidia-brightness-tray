# Brightness Tray Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add color-temperature (warmth) control, a popup slider window, expanded menu presets, display-change gamma re-apply, an autostart toggle, and a reset action to the single-file NVIDIA brightness tray app.

**Architecture:** Extend `nvidia_brightness_tray.py` in place. Pure gamma + settings functions stay module-level and unit-tested; GUI (Tkinter sliders) runs on its own daemon thread shown via thread-safe `event_generate`; display/power events re-apply the current ramp.

**Tech Stack:** Python 3.14, ctypes (GDI gamma), pystray, Pillow, keyboard, Tkinter (stdlib), pytest.

---

## File Structure

- Modify: `nvidia_brightness_tray.py` — all app code.
- Create: `tests/test_brightness_logic.py` — pure-logic tests (ramp math + settings).

Pure functions (`_compute_ramp`, `_load_settings`, `_save_settings`) carry the testable logic. GUI, GDI, tray, hotkeys, and OS events are integration-only (flagged untestable headless in the spec).

---

## Task 1: Warmth gamma math

**Files:**
- Modify: `nvidia_brightness_tray.py` (replace `_brightness_to_ramp`, update `set_brightness`)
- Test: `tests/test_brightness_logic.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_brightness_logic.py
import struct, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import nvidia_brightness_tray as app


def _channels(ramp):
    vals = struct.unpack("<768H", ramp)
    return vals[0:256], vals[256:512], vals[512:768]  # R, G, B


def test_ramp_length():
    assert len(app._compute_ramp(50, 0)) == 1536


def test_identity_at_full_no_warmth():
    r, g, b = _channels(app._compute_ramp(100, 0))
    assert r == g == b
    assert r[0] == 0
    assert r[255] == 65535  # 255*257 = 65535


def test_warmth_zero_channels_equal():
    r, g, b = _channels(app._compute_ramp(60, 0))
    assert r == g == b


def test_warmth_orders_channels():
    r, g, b = _channels(app._compute_ramp(100, 80))
    for i in range(256):
        assert b[i] <= g[i] <= r[i]


def test_values_in_uint16_range():
    for bpct in (10, 55, 100):
        for w in (0, 50, 100):
            for v in struct.unpack("<768H", app._compute_ramp(bpct, w)):
                assert 0 <= v <= 65535


def test_compute_ramp_clamps_inputs():
    # out-of-range inputs do not raise and produce valid ramps
    assert len(app._compute_ramp(-20, 999)) == 1536
    assert len(app._compute_ramp(200, -5)) == 1536
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `python -m pytest tests/test_brightness_logic.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_compute_ramp'`

- [ ] **Step 3: Implement `_compute_ramp` and update `set_brightness`**

Replace `_brightness_to_ramp` (lines ~91-109) with:

```python
def _compute_ramp(brightness_pct: int, warmth_pct: int = 0) -> bytes:
    """
    Build a 3×256 uint16 gamma ramp from brightness (10-100) and warmth (0-100).
    Red is untouched; green and blue are scaled down as warmth rises (blue most),
    producing an amber, lower-blue-light tint. At warmth=0 all channels are equal
    (matches the original single-channel ramp). At brightness=100, warmth=0 the
    ramp is identity.
    """
    brightness_pct = max(10, min(100, int(brightness_pct)))
    warmth_pct = max(0, min(100, int(warmth_pct)))
    b_scale = brightness_pct / 100.0
    green_scale = 1.0 - 0.20 * (warmth_pct / 100.0)
    blue_scale = 1.0 - 0.55 * (warmth_pct / 100.0)
    channel_scales = (1.0, green_scale, blue_scale)  # R, G, B order
    data = b""
    for cs in channel_scales:
        for i in range(256):
            val = int(i * b_scale * cs * 257)  # 257 = 65535/255
            val = max(0, min(65535, val))
            data += struct.pack("<H", val)
    return data
```

Update `set_brightness` (lines ~111-123):

```python
def set_brightness(brightness_pct: int, warmth_pct: int = 0) -> bool:
    """Apply brightness + warmth via GDI gamma ramp. Returns True on success."""
    brightness_pct = max(10, min(100, brightness_pct))
    warmth_pct = max(0, min(100, warmth_pct))
    hdc = _get_primary_dc()
    if not hdc:
        return False
    ramp = _compute_ramp(brightness_pct, warmth_pct)
    GammaArray = ctypes.c_uint16 * (3 * 256)
    gamma = GammaArray(*struct.unpack("<768H", ramp))
    result = gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(gamma))
    _release_dc(hdc)
    return bool(result)
```

- [ ] **Step 4: Run tests, verify pass**

Run: `python -m pytest tests/test_brightness_logic.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add nvidia_brightness_tray.py tests/test_brightness_logic.py
git commit -m "feat: add warmth (color temperature) to gamma ramp"
```

---

## Task 2: Settings with warmth + migration

**Files:**
- Modify: `nvidia_brightness_tray.py` (replace `_load_saved_brightness`, `_save_brightness`)
- Test: `tests/test_brightness_logic.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_brightness_logic.py`:

```python
import json


def test_settings_round_trip(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    monkeypatch.setattr(app, "SETTINGS_PATH", str(p))
    monkeypatch.setattr(app, "SETTINGS_DIR", str(tmp_path))
    app._save_settings(42, 30)
    assert app._load_settings() == (42, 30)


def test_settings_migration_old_format(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"brightness": 70}), encoding="utf-8")
    monkeypatch.setattr(app, "SETTINGS_PATH", str(p))
    assert app._load_settings() == (70, 0)  # warmth defaults to 0


def test_settings_missing_returns_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "SETTINGS_PATH", str(tmp_path / "nope.json"))
    assert app._load_settings() == (91, 0)


def test_settings_clamped_on_save_load(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    monkeypatch.setattr(app, "SETTINGS_PATH", str(p))
    monkeypatch.setattr(app, "SETTINGS_DIR", str(tmp_path))
    app._save_settings(500, -10)
    assert app._load_settings() == (100, 0)
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `python -m pytest tests/test_brightness_logic.py -k settings -v`
Expected: FAIL — `_load_settings` / `_save_settings` not defined.

- [ ] **Step 3: Implement settings functions**

Replace `_load_saved_brightness` and `_save_brightness` (lines ~142-160) with:

```python
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
    """Persist brightness + warmth for next launch."""
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
```

- [ ] **Step 4: Run tests, verify pass**

Run: `python -m pytest tests/test_brightness_logic.py -k settings -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add nvidia_brightness_tray.py tests/test_brightness_logic.py
git commit -m "feat: persist warmth in settings with backward-compatible load"
```

---

## Task 3: Autostart toggle functions

**Files:**
- Modify: `nvidia_brightness_tray.py` (refactor `_ensure_autostart`)

No unit test — filesystem/PowerShell side effects, flagged untestable headless. Verified via smoke import (Task 7) and manual run.

- [ ] **Step 1: Replace `_ensure_autostart` (lines ~421-448) with three functions**

```python
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
```

- [ ] **Step 2: Commit**

```bash
git add nvidia_brightness_tray.py
git commit -m "refactor: split autostart into enable/disable/is_enabled"
```

---

## Task 4: Slider popup window (Tkinter)

**Files:**
- Modify: `nvidia_brightness_tray.py` (add `SliderWindow` class before `BrightnessApp`)

No unit test — GUI, flagged untestable headless. Verified by manual run.

- [ ] **Step 1: Add `SliderWindow` class**

Insert after the power-listener section, before `class BrightnessApp`:

```python
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
        except Exception as e:
            print(f"Tkinter unavailable — sliders disabled: {e}")
            return
        self._tk = tk
        self.root = tk.Tk()
        self.root.title("Display")
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)

        b0, w0 = self.get_state()
        self.b_var = tk.IntVar(value=b0)
        self.w_var = tk.IntVar(value=w0)

        tk.Scale(self.root, label="Brightness", from_=10, to=100,
                 orient=tk.HORIZONTAL, length=240, variable=self.b_var,
                 command=self._on_slider).pack(padx=12, pady=(10, 4))
        tk.Scale(self.root, label="Warmth", from_=0, to=100,
                 orient=tk.HORIZONTAL, length=240, variable=self.w_var,
                 command=self._on_slider).pack(padx=12, pady=(4, 12))

        self.root.protocol("WM_DELETE_WINDOW", self._hide)
        self.root.bind("<<ShowSliders>>", lambda e: self._show())
        self.root.withdraw()
        self.root.mainloop()

    def _on_slider(self, _value=None):
        if self._syncing:
            return
        self.on_change(self.b_var.get(), self.w_var.get())

    def _show(self):
        self._syncing = True
        b, w = self.get_state()
        self.b_var.set(b)
        self.w_var.set(w)
        self._syncing = False
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)

    def _hide(self):
        if self.root:
            self.root.withdraw()

    def request_show(self):
        if self.root is None:
            return
        try:
            self.root.event_generate("<<ShowSliders>>", when="tail")
        except Exception as e:
            print(f"Could not open sliders: {e}")
```

- [ ] **Step 2: Commit**

```bash
git add nvidia_brightness_tray.py
git commit -m "feat: add Tkinter brightness/warmth slider window"
```

---

## Task 5: Re-apply on display change

**Files:**
- Modify: `nvidia_brightness_tray.py` (power listener: add `WM_DISPLAYCHANGE`)

- [ ] **Step 1: Add the constant**

Near the other window-message constants (line ~165-169), add:

```python
WM_DISPLAYCHANGE = 0x007E
```

- [ ] **Step 2: Handle it in `wndproc`**

In `_start_power_event_listener._thread_main.wndproc`, after the `WM_POWERBROADCAST` block and before `WM_DESTROY`:

```python
            if msg == WM_DISPLAYCHANGE:
                try:
                    on_resume_callback()
                except Exception:
                    pass
                return 0
```

(Reuses the same re-apply callback the resume path uses — its retry loop is harmless here.)

- [ ] **Step 3: Commit**

```bash
git add nvidia_brightness_tray.py
git commit -m "feat: re-apply gamma on WM_DISPLAYCHANGE (game/resolution resets)"
```

---

## Task 6: Wire app state, menu, and entry point

**Files:**
- Modify: `nvidia_brightness_tray.py` (`BrightnessApp`, menu, `__main__`)

- [ ] **Step 1: Update `BrightnessApp.__init__`**

```python
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
```

- [ ] **Step 2: Update `_reapply_after_resume` to pass warmth**

Replace the `set_brightness(self.brightness)` call inside the retry loop with:

```python
                if set_brightness(self.brightness, self.warmth):
```

- [ ] **Step 3: Replace `_apply` and `_step`**

```python
    def _apply(self, brightness=None, warmth=None):
        if brightness is not None:
            self.brightness = max(10, min(100, int(brightness)))
        if warmth is not None:
            self.warmth = max(0, min(100, int(warmth)))
        ok = set_brightness(self.brightness, self.warmth)
        _save_settings(self.brightness, self.warmth)
        status = "✓" if ok else "✗ GDI failed"
        self.icon.icon = self._make_icon_image(self.brightness)
        self.icon.title = f"NVIDIA Brightness: {self.brightness}% · warm {self.warmth}% {status}"
        print(f"Brightness → {self.brightness}%  Warmth → {self.warmth}% ({status})")

    def _step(self, delta: int):
        self._apply(brightness=self.brightness + delta)

    def _toggle_autostart(self, icon, item):
        if is_autostart_enabled():
            disable_autostart()
        else:
            enable_autostart()
```

- [ ] **Step 4: Replace the menu in `_create_icon`**

Replace the `preset_items` / `menu` block (lines ~350-364) with:

```python
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
            item("Open sliders…", lambda i, it: self.slider.request_show()),
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
```

- [ ] **Step 5: Update `__main__` block for first-run autostart**

Replace the `__main__` block (lines ~451-459):

```python
if __name__ == "__main__":
    if ctypes.windll.shell32.IsUserAnAdmin() == 0:
        print("Note: Run as Administrator if brightness changes don't apply.")

    first_run = not os.path.exists(SETTINGS_PATH)

    app = BrightnessApp()  # writes settings.json, so after this first_run is consumed

    if first_run:
        enable_autostart()

    app.run()
```

- [ ] **Step 6: Run the logic tests again (no regressions)**

Run: `python -m pytest tests/test_brightness_logic.py -v`
Expected: PASS (10 passed)

- [ ] **Step 7: Commit**

```bash
git add nvidia_brightness_tray.py
git commit -m "feat: wire warmth, slider, expanded menu, reset, autostart toggle"
```

---

## Task 7: Smoke import + manual run handoff

**Files:** none changed.

- [ ] **Step 1: Smoke import (catches syntax/name errors without launching the tray)**

Run:
```bash
python -c "import nvidia_brightness_tray as a; print('import OK'); print(len(a._compute_ramp(50,30)))"
```
Expected: prints `import OK` then `1536`. No traceback.

- [ ] **Step 2: Full test run**

Run: `python -m pytest tests/test_brightness_logic.py -v`
Expected: 10 passed.

- [ ] **Step 3: Hand off for manual launch**

The tray icon, sliders, hotkeys, and gamma all require a real display and cannot run headless. Manual smoke test for the user:
```
pythonw nvidia_brightness_tray.py        # normal (no console)
python  nvidia_brightness_tray.py        # with console logs for debugging
```
Manual checklist: tray icon shows number; Brightness submenu changes screen + checkmark tracks; Warmth submenu tints amber; "Open sliders…" shows draggable window that updates live; hotkeys Ctrl+Alt +/- step; Reset returns to 100%/no warmth; "Start with Windows" toggles the shortcut in `shell:startup`; values survive an app restart.

---

## Self-Review

**Spec coverage:**
- Warmth gamma math → Task 1 ✓
- Settings + migration → Task 2 ✓
- Slider popup (Tk, event_generate) → Task 4 + wired in Task 6 ✓
- WM_DISPLAYCHANGE re-apply → Task 5 ✓
- Menu redesign (10% steps, warmth presets, reset, autostart toggle) → Task 6 ✓
- Autostart enable/disable/is_enabled + first-run via settings.json absence → Task 3 + Task 6 ✓
- Icon-only hotkey feedback (no popup) → retained, no change needed ✓
- Testing (ramp + settings sandbox; rest flagged) → Tasks 1,2,7 ✓

**Placeholder scan:** none — every code step has full content.

**Type consistency:** `_compute_ramp(brightness, warmth)`, `set_brightness(brightness, warmth)`, `_load_settings()->(b,w)`, `_save_settings(b,w)`, `_apply(brightness=, warmth=)`, `SliderWindow(on_change, get_state)` with `request_show()` — names consistent across Tasks 1, 2, 4, 6.
