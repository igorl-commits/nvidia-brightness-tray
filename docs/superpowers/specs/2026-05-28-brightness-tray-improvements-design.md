# NVIDIA Brightness Tray — Usability & Functionality Improvements

**Date:** 2026-05-28
**Status:** Approved design, pending implementation plan
**Scope:** Extend the existing single-file tray app (`nvidia_brightness_tray.py`) in place.

## Goal

Improve usability and functionality of the brightness tray app:

1. Finer brightness control via a popup slider window **and** expanded menu presets.
2. Manual color-temperature (warmth) control, applied through the gamma ramp.
3. Robustness & polish: re-apply gamma after display changes, an autostart on/off toggle, a reset action, and a redesigned tray menu.

Out of scope this round: multi-monitor support, warmth scheduling (time-based or sunset/sunrise), warmth hotkeys, on-screen OSD overlay, toast notifications.

## Approach

Chosen approach **A — extend the single file in place**. No new dependencies (Tkinter ships with CPython). Rejected alternatives: splitting into a package (overkill for ~460 lines) and rewriting on PyQt (large dependency + full rewrite).

## Architecture

The app stays one file with these logical units:

- **Gamma layer** — pure functions that build and apply gamma ramps. No app state.
- **Settings layer** — load/save `settings.json`.
- **Power/display listener** — hidden message window on a daemon thread.
- **Slider UI** — hidden Tk root on a daemon thread, shown on demand.
- **`BrightnessApp`** — owns state (`brightness`, `warmth`), the tray icon/menu, hotkeys, and wires the units together.

## Detailed Design

### 1. Gamma math — add warmth

Replace `_brightness_to_ramp(brightness_pct) -> bytes` with:

```python
def _compute_ramp(brightness_pct: int, warmth_pct: int) -> bytes:
    """Build a 3×256 uint16 gamma ramp from brightness (10-100) and warmth (0-100)."""
```

Per-channel scaling, applied on top of the existing brightness scale:

```
red_scale   = 1.0
green_scale = 1 - 0.20 * (warmth_pct / 100)
blue_scale  = 1 - 0.55 * (warmth_pct / 100)
```

For each channel, entry `i` = `clamp(int(i * brightness_scale * channel_scale * 257), 0, 65535)`,
where `brightness_scale = brightness_pct / 100`.

Invariants:
- At `warmth=0`: green_scale = blue_scale = 1.0 → identical to current single-channel behavior for any brightness.
- At `brightness=100, warmth=0`: identity ramp (each channel `i*257`, capped at 65535).
- When `warmth>0`: per index, `blue[i] <= green[i] <= red[i]`.
- Output length: `3 * 256 * 2 = 1536` bytes.

`set_brightness` gains a `warmth` argument:

```python
def set_brightness(brightness_pct: int, warmth_pct: int = 0) -> bool:
```

It clamps `brightness` to 10-100 and `warmth` to 0-100, builds the ramp via `_compute_ramp`, and calls `SetDeviceGammaRamp`. `get_current_brightness` is unchanged (still estimates brightness from the red channel midpoint; red is unaffected by warmth, so the estimate stays valid).

### 2. State + settings

`settings.json` schema grows:

```json
{ "brightness": 91, "warmth": 0 }
```

- `_load_saved_brightness` is generalized to `_load_settings() -> tuple[int, int]` returning `(brightness, warmth)`, clamped, with defaults `(91, 0)`. Missing `warmth` key (old files) defaults to 0 — backward compatible.
- `_save_brightness` becomes `_save_settings(brightness, warmth)`.
- `BrightnessApp` holds `self.brightness` and `self.warmth`. Every apply persists both.

### 3. Slider popup (Tkinter)

A `SliderWindow` unit:

- Creates a single hidden Tk root on a daemon thread at app startup; runs its own `mainloop`.
- Window is `-topmost`, small, titled "Display", with two horizontal sliders:
  - Brightness: range 10-100, live label "Brightness NN%".
  - Warmth: range 0-100, live label "Warmth NN%".
- Slider drag calls back into the app (`on_change(brightness, warmth)`), which applies + saves + updates the icon. GDI calls are thread-safe to call from the Tk thread.
- Closing the window **withdraws** (hides) it rather than destroying, so reopening is instant and the root/mainloop survives.

**Thread-safety:** the menu action runs on the pystray thread, not the Tk thread. To show the window it calls `root.event_generate("<<ShowSliders>>", when="tail")` — `event_generate` is the documented thread-safe Tk cross-thread call. A bound handler on the Tk thread deiconifies, syncs slider positions to current state, and raises the window.

If Tk is unavailable for any reason, the menu item degrades gracefully (logs and no-ops); menu presets and hotkeys still work.

### 4. Re-apply robustness

The existing power-listener window proc gains `WM_DISPLAYCHANGE (0x007E)`:

- On `WM_DISPLAYCHANGE`, re-apply the current `(brightness, warmth)` ramp. This catches gamma resets from fullscreen-exclusive game exit, resolution changes, and monitor hotplug/wake that `WM_POWERBROADCAST` misses.
- The proc needs access to the current state; reuse the same resume callback path (a callback that re-applies current state). Keep the existing power-resume retry loop unchanged.

No polling watchdog (rejected: risk of flicker fighting fullscreen-exclusive games that legitimately own gamma).

### 5. Tray menu redesign

```
☀ Brightness ▸  100 / 90 / 80 / ... / 10   (✓ on the nearest current step)
🌅 Warmth    ▸  Off(0) / Low(25) / Medium(50) / High(75) / Max(100)   (✓ on current)
Open sliders…
─────────────────────────────
Current: NN% · warm NN%        (disabled label)
─────────────────────────────
Brighter   (Ctrl+Alt +)
Dimmer     (Ctrl+Alt -)
Reset display (100%, no warmth)
─────────────────────────────
✓ Start with Windows           (checkable; reflects + toggles startup shortcut)
Quit
```

- Brightness submenu: 10% steps, 100 down to 10. Checkmark via `checked=` on the item nearest `self.brightness`.
- Warmth submenu: 5 presets. Checkmark on current.
- "Reset display": sets brightness 100, warmth 0.
- Hotkeys unchanged: `Ctrl+Alt +` / `Ctrl+Alt -` step brightness by `STEP` (10). No warmth hotkeys.
- Hotkey feedback: icon number + tooltip update only (no popup/toast) — current behavior retained.

### 6. Autostart toggle

`_ensure_autostart` is refactored into three functions:

```python
def is_autostart_enabled() -> bool      # shortcut file exists
def enable_autostart() -> None          # create shortcut (current PowerShell WScript.Shell logic)
def disable_autostart() -> None         # delete shortcut
```

- The tray "Start with Windows" item is checkable: `checked=lambda item: is_autostart_enabled()`, action toggles.
- First-run behavior preserved **without fighting the toggle**: auto-create the shortcut only on a genuine first run, detected by the **absence of `settings.json`** (which the app writes on its first apply). Sequence at startup: (1) check whether `settings.json` exists → `first_run`; (2) load settings; (3) if `first_run`, call `enable_autostart()`. After that, the shortcut's presence is owned entirely by the user via the toggle — a later launch with no shortcut means the user disabled it, and it is **not** re-created.

## Testing

**Testable in sandbox (pure logic, no display/OS):**

- `_compute_ramp`:
  - Output length == 1536 bytes for any input.
  - `brightness=100, warmth=0` → identity (each channel `min(i*257, 65535)`).
  - `warmth=0` for any brightness → all three channels identical (matches old single-channel ramp).
  - `warmth>0` → per index `blue[i] <= green[i] <= red[i]`.
  - Clamping: brightness <10 or >100, warmth <0 or >100 handled by `set_brightness` clamps; `_compute_ramp` values never exceed 65535 or go below 0.
- Settings: `_save_settings` then `_load_settings` round-trips; old `{brightness}`-only file loads with `warmth=0`; corrupt/missing file returns defaults `(91, 0)`.

**Cannot test headless (flag explicitly):**

- `SetDeviceGammaRamp` / `GetDeviceGammaRamp` actually changing the display.
- Tk slider window rendering, `event_generate` show path, topmost behavior.
- pystray icon, menu rendering, checkmarks.
- `keyboard` hotkey registration and firing.
- `WM_POWERBROADCAST` / `WM_DISPLAYCHANGE` event delivery.
- Autostart shortcut creation/deletion via PowerShell.

## Risks & Mitigations

- **Tk threading:** mishandled cross-thread Tk calls crash. Mitigation: single root on its own thread, only `event_generate` crosses threads; all widget ops on the Tk thread.
- **Gamma fight with fullscreen games:** event-driven re-apply (not polling) only fires on display change, minimizing conflict.
- **Backward-compat settings:** explicit default for missing `warmth` key.
