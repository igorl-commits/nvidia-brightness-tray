import struct, sys, os, json
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


def test_settings_write_is_atomic(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    monkeypatch.setattr(app, "SETTINGS_PATH", str(p))
    monkeypatch.setattr(app, "SETTINGS_DIR", str(tmp_path))
    app._save_settings(70, 35)
    assert json.loads(p.read_text(encoding="utf-8")) == {
        "brightness": 70,
        "warmth": 35,
    }
    assert not (tmp_path / "settings.json.tmp").exists()


# ─── TRAY ICON (pure drawing function, testable without display) ─────────────

def test_tray_icon_returns_valid_image():
    img = app._make_tray_icon_image(80, 0)
    assert img.size == (64, 64)
    assert img.mode == "RGBA"


def test_tray_icon_various_inputs():
    for b in (10, 55, 100):
        for w in (0, 30, 100):
            img = app._make_tray_icon_image(b, w)
            assert img.size == (64, 64)


def test_tray_icon_no_warmth_bar_when_zero():
    """When warmth=0 the warmth bar area must be dark (no amber fill drawn)."""
    img = app._make_tray_icon_image(75, 0)
    px = img.load()
    # Sample center of the bar track area (should be dark background)
    # x=32 is middle, y=54 is inside the bar zone
    r, g, b, a = px[32, 54]
    brightness = (r + g + b) / 3
    assert brightness < 50, f"Expected dark bar area, got brightness {brightness}"
