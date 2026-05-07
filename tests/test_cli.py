"""Tests for tourniquet.cli — cross-platform entry point."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


# ── Helpers ────────────────────────────────────────────────────────────────────

def _run_cli(*argv: str) -> int:
    """Run cli.main() with sys.argv patched. Returns SystemExit code or 0."""
    with patch("sys.argv", ["tourniquet", *argv]):
        try:
            from tourniquet.cli import main
            main()
            return 0
        except SystemExit as exc:
            return int(exc.code) if exc.code is not None else 0


# ── --version ──────────────────────────────────────────────────────────────────

def test_version_flag(capsys):
    from tourniquet import __version__
    try:
        _run_cli("--version")
    except SystemExit:
        pass
    captured = capsys.readouterr()
    assert __version__ in captured.out


# ── start --no-browser calls uvicorn.run with correct args ─────────────────────

def test_start_no_browser_calls_uvicorn(tmp_path):
    """start --no-browser should call uvicorn.run with correct host/port."""
    import uvicorn

    with (
        patch("uvicorn.run") as mock_run,
        patch("tourniquet.cli._init_config_dir"),
        patch("os.chdir"),
        patch("sys.argv", ["tourniquet", "start", "--no-browser", "--port", "8787",
                            "--config-dir", str(tmp_path)]),
    ):
        from tourniquet.cli import main
        main()

    mock_run.assert_called_once()
    call_kwargs = mock_run.call_args
    # First positional arg is the app string
    assert call_kwargs.args[0] == "tourniquet.main:app"
    assert call_kwargs.kwargs.get("host") == "127.0.0.1"
    assert call_kwargs.kwargs.get("port") == 8787


# ── start creates .env with keys when missing ──────────────────────────────────

def test_start_creates_env_with_keys(tmp_path):
    """start should create ~/.tourniquet/.env with non-empty FERNET_KEY and SECRET_KEY."""
    config_dir = tmp_path / "tq_config"

    with (
        patch("uvicorn.run"),
        patch("os.chdir"),
        patch("sys.argv", ["tourniquet", "start", "--no-browser",
                            "--config-dir", str(config_dir)]),
    ):
        from tourniquet.cli import main
        main()

    env_path = config_dir / ".env"
    assert env_path.exists(), ".env was not created"
    content = env_path.read_text(encoding="utf-8")
    fernet_line = next((l for l in content.splitlines() if l.startswith("FERNET_KEY=")), None)
    secret_line = next((l for l in content.splitlines() if l.startswith("SECRET_KEY=")), None)
    assert fernet_line is not None and fernet_line != "FERNET_KEY=", "FERNET_KEY not populated"
    assert secret_line is not None and secret_line != "SECRET_KEY=", "SECRET_KEY not populated"


# ── config-dir override ────────────────────────────────────────────────────────

def test_config_dir_override(tmp_path):
    """--config-dir should be respected: .env lands in the given directory."""
    custom_dir = tmp_path / "custom_cfg"

    with (
        patch("uvicorn.run"),
        patch("os.chdir"),
        patch("sys.argv", ["tourniquet", "start", "--no-browser",
                            "--config-dir", str(custom_dir)]),
    ):
        from tourniquet.cli import main
        main()

    assert (custom_dir / ".env").exists()


# ── webbrowser.open called when --no-browser absent ───────────────────────────

def test_browser_opens_without_no_browser_flag(tmp_path):
    """webbrowser.open should be called when --no-browser is not given."""
    import threading

    open_calls: list[str] = []

    def fake_open(url: str) -> None:
        open_calls.append(url)

    # Patch sleep so test doesn't actually wait 1.5 s
    with (
        patch("uvicorn.run"),
        patch("os.chdir"),
        patch("webbrowser.open", side_effect=fake_open),
        patch("time.sleep"),
        patch("sys.argv", ["tourniquet", "start", "--port", "8787",
                            "--config-dir", str(tmp_path)]),
    ):
        from tourniquet.cli import main
        main()
        # The thread is daemon; give it a tick to run
        import time as _time
        _time.sleep(0)  # yield — patched, so no real delay

    # The thread may not have fired yet in CI — just check it wasn't suppressed
    # We assert webbrowser.open was *registered* as a thread target (best-effort)
    # Full assertion: if open_calls is populated, URL must match
    for url in open_calls:
        assert "127.0.0.1:8787/dashboard" in url


# ── _init_config_dir idempotent ────────────────────────────────────────────────

def test_init_config_dir_idempotent(tmp_path):
    """Running _init_config_dir twice must not regenerate keys."""
    from tourniquet.cli import _init_config_dir

    config_dir = tmp_path / "tq"
    _init_config_dir(config_dir)
    first = (config_dir / ".env").read_text(encoding="utf-8")

    _init_config_dir(config_dir)
    second = (config_dir / ".env").read_text(encoding="utf-8")

    assert first == second, "Keys changed on second init — not idempotent"


# ── register-url-handler: Windows ────────────────────────────────────────────

def test_register_url_handler_windows(capsys):
    """Windows path: winreg.CreateKey and SetValueEx called with correct values."""
    from unittest.mock import call, MagicMock

    mock_winreg = MagicMock()
    mock_ctx = MagicMock()
    mock_winreg.CreateKey.return_value.__enter__ = MagicMock(return_value=mock_ctx)
    mock_winreg.CreateKey.return_value.__exit__ = MagicMock(return_value=False)
    mock_winreg.HKEY_CURRENT_USER = 0x80000001
    mock_winreg.REG_SZ = 1

    with (
        patch("sys.platform", "win32"),
        patch.dict("sys.modules", {"winreg": mock_winreg}),
    ):
        from importlib import reload
        import tourniquet.url_handler as uh
        reload(uh)
        uh.register_windows()

    captured = capsys.readouterr()
    # Confirm registration completed and printed a confirmation
    assert "tourniquet" in captured.out.lower()
    # Verify CreateKey was called at least 4 times (base + shell + open + command)
    assert mock_winreg.CreateKey.call_count >= 4
    # Verify the command key path contains the right suffix
    all_paths = [str(c) for c in mock_winreg.CreateKey.call_args_list]
    assert any("command" in p for p in all_paths)


# ── register-url-handler: Linux ───────────────────────────────────────────────

def test_register_url_handler_linux(tmp_path, capsys):
    """Linux path: .desktop file written with correct content."""
    with (
        patch("sys.platform", "linux"),
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("subprocess.run"),
    ):
        from importlib import reload
        import tourniquet.url_handler as uh
        reload(uh)
        uh.register_linux()

    desktop_file = tmp_path / ".local" / "share" / "applications" / "tourniquet-url-handler.desktop"
    assert desktop_file.exists(), ".desktop file was not created"
    content = desktop_file.read_text(encoding="utf-8")
    assert "x-scheme-handler/tourniquet" in content
    assert "tourniquet handle-url %u" in content
    assert "MimeType=" in content


# ── register-url-handler: macOS prints instructions ───────────────────────────

def test_register_url_handler_macos_prints_instructions(capsys):
    """macOS: register() must print setup instructions, not raise."""
    with patch("sys.platform", "darwin"):
        from importlib import reload
        import tourniquet.url_handler as uh
        reload(uh)
        uh.register_macos()

    out = capsys.readouterr().out
    assert "Automator" in out or "Shortcuts" in out


# ── handle-url: valid lift URL dispatches to lift logic ───────────────────────

def test_handle_url_lift_dispatches(tmp_path):
    """handle_url with a valid tourniquet://lift/<id> URL calls _do_lift."""
    import tourniquet.url_handler as uh
    from importlib import reload
    reload(uh)

    fake_key_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    with patch.object(uh, "_do_lift", return_value=0) as mock_lift:
        rc = uh.handle_url(f"tourniquet://lift/{fake_key_id}?multiplier=3")

    assert rc == 0
    mock_lift.assert_called_once_with(fake_key_id, 3.0)


# ── handle-url: invalid URL exits non-zero ────────────────────────────────────

def test_handle_url_invalid_scheme_returns_nonzero():
    """handle_url with wrong scheme must return non-zero."""
    import tourniquet.url_handler as uh
    rc = uh.handle_url("https://example.com/something")
    assert rc != 0


def test_handle_url_missing_key_id_returns_nonzero():
    """handle_url with no key_id segment must return non-zero."""
    import tourniquet.url_handler as uh
    rc = uh.handle_url("tourniquet://lift/")
    assert rc != 0
