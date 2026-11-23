"""Start the GUI with the configured Windows imaging runtime."""
import os
from pathlib import Path
import subprocess
import sys
import tomllib


def run_with_sdk(arguments):
    """Return a child exit code or a message for the current process."""
    if sys.platform != "win32":
        return None, ""
    src = Path(__file__).resolve().parents[1]
    setting = os.environ.get("SCENESCOUT_USD_SDK") or None
    config = src.parent / "scenescout.local.toml"
    if not setting:
        try:
            with config.open("rb") as stream:
                setting = tomllib.load(stream).get("usd_sdk")
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            return None, f"Could not read {config.name}. {exc}"
    if setting is None:
        return None, "No native OpenUSD SDK configured."
    if not isinstance(setting, str) or not setting.strip():
        return None, "Set usd_sdk or SCENESCOUT_USD_SDK to an absolute SDK path."
    try:
        sdk = Path(setting.strip()).expanduser()
        if not sdk.is_absolute():
            return None, "Set usd_sdk or SCENESCOUT_USD_SDK to an absolute SDK path."
        sdk = sdk.resolve()
        python = sdk / "python" / "python.exe"
        if not python.is_file():
            return None, f"OpenUSD SDK Python was not found at {python}."
        if python.resolve() == Path(sys.executable).resolve():
            return None, f"Using OpenUSD SDK at {sdk}."
    except (OSError, ValueError, RuntimeError) as exc:
        return None, f"Could not resolve the OpenUSD SDK path. {exc}"
    env = os.environ.copy()
    dlls = [sdk / "lib", sdk / "plugin" / "usd", sdk / "bin", sdk / "pip-packages" / "bin"]
    env["PATH"] = os.pathsep.join([*map(str, dlls), env.get("PATH", "")])
    env["PYTHONPATH"] = os.pathsep.join(map(str, [sdk / "lib" / "python", sdk / "pip-packages", src]))
    env["PXR_MTLX_STDLIB_SEARCH_PATHS"] = str(sdk / "libraries")
    env["SCENESCOUT_USD_SDK"] = str(sdk)
    try:
        result = subprocess.call([str(python), "-m", "scenescout", *arguments], env=env)
    except OSError as exc:
        return None, f"Could not launch the OpenUSD SDK at {sdk}. {exc}"
    if result:
        return None, f"OpenUSD SDK at {sdk} exited with code {result}."
    return result, ""
