import os
from concurrent.futures import CancelledError
from dataclasses import replace
from pathlib import Path
from threading import Event
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from scenescout.app import SceneScoutWindow
from scenescout.diff import compare_scenes
from scenescout.provenance import inspect_opinions
from scenescout.review_document import export_report, load_review, save_review
from scenescout.usd_reader import open_stage, snapshot_stage


SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "review"


def wait_until(app, condition):
    deadline = time.monotonic() + 30
    while not condition():
        assert time.monotonic() < deadline, "Timed out waiting for SceneScout"
        app.processEvents()
        time.sleep(0.005)


def test_progressive_snapshot_can_stop_after_hierarchy():
    batches = []
    stage = open_stage(str(SAMPLES / "signoff_v001.usda"))
    with pytest.raises(CancelledError):
        snapshot_stage(stage, hierarchy=batches.append, cancelled=lambda: bool(batches))
    assert batches[0][0].path == "/Publish"


def test_dependency_resolution_uses_authoring_layer_and_list_edits(tmp_path):
    layer_directory = tmp_path / "assembly"
    layer_directory.mkdir()
    target = layer_directory / "asset.usda"
    target.write_text('#usda 1.0\n(defaultPrim = "Asset")\ndef Xform "Asset" {}\n', encoding="utf-8")
    sublayer = layer_directory / "composition.usda"
    sublayer.write_text('''#usda 1.0
def Xform "Item" (
    prepend references = @./asset.usda@</Asset> (offset = 2; scale = 3)
) {}
''', encoding="utf-8")
    root_metadata = '#usda 1.0\n(subLayers = [@./assembly/composition.usda@ (offset = 7; scale = 5)])\n'
    before_path = tmp_path / "before.usda"
    before_path.write_text(root_metadata, encoding="utf-8")
    after_path = tmp_path / "after.usda"
    after_path.write_text(root_metadata + 'over "Item" (references = None) {}\n', encoding="utf-8")
    before = open_stage(str(before_path))
    after = open_stage(str(after_path))
    snapshots = snapshot_stage(before), snapshot_stage(after)
    edit = snapshots[0].prims["/Item"].dependencies[0]
    arc = edit.targets[0]
    assert Path(edit.layer) == sublayer
    assert Path(arc.resolved_path) == target
    assert arc.composed_prim_path == "/Asset"
    assert (arc.offset, arc.scale) == (2, 3)
    assert (arc.composed_offset, arc.composed_scale) == (17, 15)
    finding = next(change for change in compare_scenes(*snapshots) if change.field == "dependencies.reference")
    _, opinions = inspect_opinions(after, "/Item", finding.field)
    assert any("explicit" in opinion.value and Path(opinion.layer) == after_path for opinion in opinions)
    assert all(" at /Asset" not in opinion.value for opinion in opinions)


def test_unloaded_payload_with_no_resolver_path_is_a_coverage_finding(tmp_path):
    source = tmp_path / "unavailable_payload.usda"
    source.write_text('''#usda 1.0
def Xform "Payload" (prepend payload = @./missing.usda@) {}
''', encoding="utf-8")
    unloaded_stage = open_stage(str(source))
    unloaded = snapshot_stage(unloaded_stage)
    findings = compare_scenes(unloaded, unloaded)
    assert [(finding.kind, finding.field) for finding in findings] == [("Coverage", "dependencies.payload.status")]
    summary, opinions = inspect_opinions(unloaded_stage, "/Payload", findings[0].field)
    assert "intentionally unloaded" in summary
    assert "Unresolved (resolver returned no path)" in opinions[0].value
    assert "Could not open" not in summary

    loaded = snapshot_stage(open_stage(str(source), True))
    assert any(finding.kind == "Issue" for finding in compare_scenes(loaded, loaded))


def test_missing_textures_resolve_from_the_shading_layer_and_accept_sparse_udims(tmp_path):
    from scenescout.review_document import finding_issue

    look = tmp_path / "look"
    look.mkdir()
    # These files exercise resolution. Image decoding is outside this check.
    (look / "present.png").touch()
    (look / "sparse.1012.exr").touch()
    material = look / "material.usda"
    material.write_text('''#usda 1.0
def Material "Look" {
    asset inputs:colorFile = @./present.png@
    def Shader "Image" {
        uniform token info:id = "UsdUVTexture"
        asset inputs:file = @./missing.png@
    }
    def Shader "Tiles" {
        uniform token info:id = "ND_image_color3"
        asset inputs:file = @./sparse.<UDIM>.exr@
    }
    def Shader "MissingTiles" {
        uniform token info:id = "ND_image_color3"
        asset inputs:file = @./missing.<UDIM>.exr@
    }
    def Shader "Connected" {
        uniform token info:id = "UsdUVTexture"
        asset inputs:file = @./unused.png@
        asset inputs:file.connect = </Look.inputs:colorFile>
    }
    def Shader "StudioFile" {
        uniform token info:id = "UsdUVTexture"
        asset inputs:file = @asset://chair/albedo.png@
    }
}
''', encoding="utf-8")
    source = tmp_path / "stage.usda"
    source.write_text('#usda 1.0\n(subLayers = [@./look/material.usda@])\n', encoding="utf-8")
    stage = open_stage(str(source))
    snapshot = snapshot_stage(stage)

    assert Path(snapshot.prims["/Look"].texture_inputs["inputs:colorFile"][0].resolved_path) == look / "present.png"
    findings = compare_scenes(snapshot, snapshot)
    assert {finding.path for finding in findings} == {"/Look/Image", "/Look/MissingTiles"}
    assert any("No UDIM tiles resolved" in finding.after for finding in findings)
    assert all(finding.kind == "Issue" and finding_issue(finding) for finding in findings)
    _, opinions = inspect_opinions(stage, "/Look/Image", "dependencies.texture.inputs:file")
    assert Path(opinions[0].layer) == material
    assert opinions[0].spec_path == "/Look/Image.inputs:file"
    assert "./missing.png" in opinions[0].value


def test_local_review_round_trip_and_stale_pair(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = SceneScoutWindow()
    window.show()
    try:
        window.load_path("A", str(SAMPLES / "pump_v012.usda"))
        window.load_path("A", str(SAMPLES / "signoff_v001.usda"))
        window.load_path("B", str(SAMPLES / "signoff_v002.usda"))
        wait_until(app, lambda: window.comparison_complete)
        assert window.snapshots["A"].source.endswith("signoff_v001.usda")
        assert window.snapshots["A"].prim_count > 500
        assert not window.analysis_pending

        review = window.review
        chair = next(c for c in review.changes if c.path == "/Publish/Chair")
        assert chair.field == "dependencies.reference"
        assert review.select_finding(chair)
        evidence = review.evidence.toPlainText()
        assert all(value in evidence for value in (
            "chair_v012.usda", "chair_v013.usda", "Effective dependency", "Resolved path", "/Publish/Chair",
        ))
        review.find_note.setText("Same geometry, updated source.")
        review.find_reviewed.setChecked(True)
        review.find_category.setCurrentText("Dependency")
        review.find_state.setCurrentText("Open issues")
        assert review.table.filters.rowCount() == 1
        issue = review.table.change_at(0)
        assert issue.path == "/Publish/MissingLamp"
        review.find_state.setCurrentIndex(0)
        coverage = next(c for c in review.changes if c.kind == "Coverage")
        assert review.select_finding(coverage)
        assert "intentionally unloaded" in review.evidence.toPlainText()
        assert "Could not open asset" not in review.evidence.toPlainText()
        review.find_category.setCurrentText("Animation")
        assert review.table.filters.rowCount() == 1
        timed = review.table.change_at(0)
        assert review.select_finding(timed)
        assert review.timeline.times == {"A": 48, "B": 48}

        window.signoff.notes = "Repair the lamp reference before publishing."
        window.signoff.set_decision("Changes requested")
        document = window.signoff.capture()
        path = tmp_path / "test.scenescout.json"
        report = tmp_path / "test.html"
        save_review(path, document)
        saved = load_review(path)
        assert document == saved
        export_report(report, saved)
        html = report.read_text(encoding="utf-8")
        assert all(value in html for value in (
            "Changes requested", "lamp_missing.usda", "intentionally unloaded", "Winning time samples",
            "Same geometry, updated source.", "Effective dependency",
        ))

        window.signoff.resume(saved, path)
        wait_until(app, lambda: window.comparison_complete and not window.analysis_pending)
        assert window.signoff.decision == "Changes requested"
        assert window.signoff.notes == document.notes
        assert review.annotations[chair].reviewed
        assert review.current_finding == timed
        assert review.timeline.times == {"A": 48, "B": 48}

        window.load_path("B", str(SAMPLES / "pump_v013.usda"))
        window.cancel_analysis()
        wait_until(app, lambda: not window._workers and not window._comparison_thread)
        assert review.current_finding == timed
        assert window.comparison_complete
        assert window.signoff.decision == "Changes requested"
        assert window.snapshots["B"].source.endswith("signoff_v002.usda")
    finally:
        window.signoff.dirty = False
        window.close()
        wait_until(app, lambda: not window.isVisible())


def test_unavailable_saved_stage_preserves_the_active_pair_and_review(tmp_path, monkeypatch):
    def write_stage(name, value):
        source = tmp_path / f"{name}.usda"
        source.write_text(f'#usda 1.0\ndef Xform "Item"\n{{\n    custom int version = {value}\n}}\n', encoding="utf-8")
        return str(source)

    source_a = write_stage("active-a", 1)
    source_b = write_stage("active-b", 2)
    archived_a = write_stage("archived-a", 3)
    archived_b = str(tmp_path / "missing-archived-b.usda")
    allow_failure = Event()
    errors = []

    def open_with_delayed_failure(filename, load_payloads):
        if filename == archived_b:
            assert allow_failure.wait(30), "Timed out waiting to fail archived Stage B"
        return open_stage(filename, load_payloads)

    monkeypatch.setattr("scenescout.app.open_stage", open_with_delayed_failure)
    monkeypatch.setattr(QMessageBox, "critical", lambda _parent, title, _message: errors.append(title))
    app = QApplication.instance() or QApplication([])
    window = SceneScoutWindow()
    window.show()
    try:
        window.load_path("A", source_a)
        window.load_path("B", source_b)
        wait_until(app, lambda: window.comparison_complete)
        change = next(c for c in window.review.changes if c.path == "/Item")
        assert window.review.select_finding(change)
        window.review.find_note.setText("Active finding note")
        window.review.find_reviewed.setChecked(True)
        window.signoff.notes = "Active approval"
        window.signoff.path = tmp_path / "active-review.json"
        window.signoff.set_decision("Approved")
        before = window.signoff.draft()
        stages = dict(window.review.stages)
        snapshots = dict(window.snapshots)
        active_path = window.signoff.path
        archived = replace(before, stage_a=archived_a, stage_b=archived_b, notes="Archived approval",
                           settings=replace(before.settings, alignment="frames", load_payloads_on_open=True))

        window.signoff.resume(archived, tmp_path / "archived-review.json")
        wait_until(app, lambda: "A" not in window._load_requests)
        assert window.analysis_pending
        assert window.comparison_complete
        assert window.review.stages == stages
        assert window.snapshots == snapshots
        assert window.signoff.draft() == before

        allow_failure.set()
        wait_until(app, lambda: not window._workers)
        assert errors == ["Unable to load Stage B"]
        assert window.signoff.pending_restore is None
        assert window.comparison_complete
        assert window.review.stages == stages
        assert window.snapshots == snapshots
        assert window.signoff.draft() == before
        assert window.signoff.path == active_path
        assert window.signoff.dirty

        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (archived_b, ""))
        monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.Discard)
        window.open_stage("A")
        wait_until(app, lambda: not window._workers)
        assert errors == ["Unable to load Stage B", "Unable to load Stage A"]
        assert window.comparison_complete
        assert window.review.stages == stages
        assert window.snapshots == snapshots
        assert window.signoff.draft() == before
        assert window.signoff.path == active_path
        assert window.signoff.dirty
    finally:
        allow_failure.set()
        window.signoff.dirty = False
        window.close()
        wait_until(app, lambda: not window.isVisible())
