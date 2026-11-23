from types import SimpleNamespace

import pytest

from scenescout import launcher


@pytest.fixture
def launcher_config(tmp_path, monkeypatch):
    monkeypatch.setenv("SCENESCOUT_USD_SDK", "")
    monkeypatch.setattr(launcher, "__file__", str(tmp_path / "src" / "scenescout" / "launcher.py"))
    monkeypatch.setattr(launcher, "sys", SimpleNamespace(platform="win32", executable=str(tmp_path / "python.exe")))
    return tmp_path / "scenescout.local.toml"


@pytest.mark.parametrize(("contents", "message"), [
    ("usd_sdk = [", "Could not read scenescout.local.toml"),
    ("usd_sdk = 17", "absolute SDK path"),
    ("usd_sdk = ''", "absolute SDK path"),
    ("usd_sdk = 'relative'", "absolute SDK path"),
])
def test_malformed_or_invalid_sdk_config_opens_regular_app(launcher_config, contents, message):
    launcher_config.write_text(contents, encoding="utf-8")

    result, status = launcher.run_with_sdk([])

    assert result is None
    assert message in status


def test_sdk_override_ignores_local_config_and_reports_launch_failure(launcher_config, monkeypatch):
    launcher_config.write_text("usd_sdk = [", encoding="utf-8")
    sdk = launcher_config.parent.resolve() / "sdk"
    python = sdk / "python" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setenv("SCENESCOUT_USD_SDK", str(sdk))

    def launch(command, env):
        assert command == [str(python), "-m", "scenescout", "a.usda"]
        assert env["SCENESCOUT_USD_SDK"] == str(sdk)
        return 3

    monkeypatch.setattr(launcher.subprocess, "call", launch)
    result, status = launcher.run_with_sdk(["a.usda"])

    assert result is None
    assert str(sdk) in status
    assert "exited with code 3" in status


def test_sdk_reports_current_bundled_interpreter_without_relaunch(launcher_config, monkeypatch):
    sdk = launcher_config.parent.resolve() / "sdk"
    python = sdk / "python" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setenv("SCENESCOUT_USD_SDK", str(sdk))
    monkeypatch.setattr(launcher.sys, "executable", str(python))
    monkeypatch.setattr(launcher.subprocess, "call", lambda *_args, **_kwargs: pytest.fail("Unexpected relaunch"))

    assert launcher.run_with_sdk([]) == (None, f"Using OpenUSD SDK at {sdk}.")
