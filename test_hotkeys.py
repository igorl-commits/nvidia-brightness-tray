"""
Unit tests for the pure hotkey-table logic in nvidia_brightness_tray.

What is covered (no display / OS hook needed):
  - _build_hotkey_table maps stable ids -> (modifiers, vk, handler)
  - WM_HOTKEY dispatch (simulated) calls the right handler for each id
  - modifiers are Ctrl+Alt for every combo
  - all expected +/- virtual keys (main row + numpad) are present, ids unique

What CANNOT be tested here (requires a real Windows session, a display, and
an actual sleep/resume cycle):
  - RegisterHotKey / UnregisterHotKey succeeding against the live OS
  - WM_HOTKEY actually being delivered to the message loop
  - that hotkeys keep firing after a real suspend/resume (the bug being fixed)
These must be verified manually on the machine.
"""

import nvidia_brightness_tray as app


def test_table_shape_modifiers_and_vks():
    table = _make()
    # ids are unique and stable
    assert sorted(table.keys()) == [1, 2, 3, 4]
    # every combo is Ctrl+Alt
    expected_mods = app.MOD_CONTROL | app.MOD_ALT
    assert all(mods == expected_mods for (mods, vk, h) in table.values())
    # both main-row and numpad +/- are registered
    vks = {vk for (mods, vk, h) in table.values()}
    assert vks == {app.VK_OEM_PLUS, app.VK_ADD, app.VK_OEM_MINUS, app.VK_SUBTRACT}


def test_dispatch_routes_to_correct_handler():
    calls = []
    table = app._build_hotkey_table(
        on_brighter=lambda: calls.append("up"),
        on_dimmer=lambda: calls.append("down"),
    )
    # Simulate the wndproc WM_HOTKEY dispatch: handler = table[wparam][2]
    for wparam in (1, 2, 3, 4):
        table[wparam][2]()
    assert calls.count("up") == 2     # VK_OEM_PLUS + VK_ADD
    assert calls.count("down") == 2   # VK_OEM_MINUS + VK_SUBTRACT


def test_plus_keys_brighten_minus_keys_dim():
    calls = []
    table = app._build_hotkey_table(
        on_brighter=lambda: calls.append("up"),
        on_dimmer=lambda: calls.append("down"),
    )
    by_vk = {vk: h for (mods, vk, h) in table.values()}
    by_vk[app.VK_OEM_PLUS]();  by_vk[app.VK_ADD]()
    assert calls == ["up", "up"]
    calls.clear()
    by_vk[app.VK_OEM_MINUS]();  by_vk[app.VK_SUBTRACT]()
    assert calls == ["down", "down"]


def _make():
    return app._build_hotkey_table(on_brighter=lambda: None, on_dimmer=lambda: None)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    raise SystemExit(1 if failures else 0)
