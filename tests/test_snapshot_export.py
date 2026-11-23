import json
from dataclasses import replace
from types import SimpleNamespace

from scenescout import app
from scenescout.models import PrimInfo, SceneSnapshot


def snapshot_window(monkeypatch, target, source, other_layer=None):
    prim = PrimInfo("/Root", "Root", "Scope", True, True, False)
    scene = SceneSnapshot(str(source), "/Root", "Y", "1", "24", [str(source)], {prim.path: prim})
    errors = []
    stages = {"A": SimpleNamespace(GetUsedLayers=lambda: [SimpleNamespace(realPath=str(source))])}
    if other_layer is not None:
        stages["B"] = SimpleNamespace(GetUsedLayers=lambda: [SimpleNamespace(realPath=str(other_layer))])
    window = SimpleNamespace(
        active_slot="A", snapshots={"A": scene}, review=SimpleNamespace(stages=stages), _preview_stages={},
        statusBar=lambda: SimpleNamespace(showMessage=lambda _message: None),
    )
    monkeypatch.setattr(app, "QFileDialog", SimpleNamespace(getSaveFileName=lambda *_: (str(target), "")))
    monkeypatch.setattr(app, "QMessageBox", SimpleNamespace(critical=lambda _, title, message: errors.append((title, message))))
    return window, errors


def test_snapshot_export_rejects_a_usd_output_suffix(tmp_path, monkeypatch):
    source = tmp_path / "stage.usda"
    source.write_bytes(b"#usda 1.0")
    window, errors = snapshot_window(monkeypatch, source, source)

    app.SceneScoutWindow.export_json(window)

    assert errors == [("Export failed", "Choose a .json file.")]
    assert source.read_bytes() == b"#usda 1.0"


def test_snapshot_export_rejects_a_hard_link_to_an_inactive_stage_layer(tmp_path, monkeypatch):
    layer = tmp_path / "sublayer.usda"
    layer.write_bytes(b"#usda 1.0")
    target = tmp_path / "snapshot.json"
    target.hardlink_to(layer)
    window, errors = snapshot_window(monkeypatch, target, tmp_path / "stage.usda", other_layer=layer)

    app.SceneScoutWindow.export_json(window)

    assert errors[0][0] == "Export failed"
    assert "open source layer" in errors[0][1]
    assert target.samefile(layer)
    assert layer.read_bytes() == b"#usda 1.0"


def test_failed_snapshot_replace_preserves_previous_export(tmp_path, monkeypatch):
    target = tmp_path / "snapshot.json"
    window, errors = snapshot_window(monkeypatch, target, tmp_path / "stage.usda")
    app.SceneScoutWindow.export_json(window)
    previous = target.read_bytes()
    payload = json.loads(previous)
    assert payload["default_prim"] == "/Root"
    assert "attribute_fingerprints" not in payload["prims"]["/Root"]
    assert "asset_info_fingerprints" not in payload["prims"]["/Root"]

    def reject_replace(self, _target):
        raise PermissionError("File is busy.")

    window.snapshots["A"] = replace(window.snapshots["A"], default_prim="/Updated")
    monkeypatch.setattr(type(target), "replace", reject_replace)
    app.SceneScoutWindow.export_json(window)

    assert errors == [("Export failed", "File is busy.")]
    assert target.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [target]
