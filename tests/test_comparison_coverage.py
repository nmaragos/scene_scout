import os
import time

from pxr import Sdf, Usd

from scenescout.diff import compare_scenes
from scenescout.provenance import inspect_opinions
from scenescout.review_document import ReviewDocument, load_review, save_review
from scenescout.usd_reader import snapshot_stage


def test_shader_connection_change_includes_authored_list_edits():
    def make_stage(source):
        stage = Usd.Stage.CreateInMemory()
        material = stage.DefinePrim("/Material", "Material")
        surface = material.CreateAttribute("outputs:surface", Sdf.ValueTypeNames.Token)
        for shader_name in ("First", "Second"):
            shader = stage.DefinePrim(f"/Material/{shader_name}", "Shader")
            shader.CreateAttribute("outputs:surface", Sdf.ValueTypeNames.Token)
        assert surface.AddConnection(Sdf.Path(f"/Material/{source}.outputs:surface"))
        return stage

    first, second = make_stage("First"), make_stage("Second")
    change, = compare_scenes(snapshot_stage(first), snapshot_stage(second))
    assert change.kind == "Modified"
    assert (change.path, change.field) == ("/Material", "connections.outputs:surface")
    assert "/Material/First.outputs:surface" in change.before
    assert "/Material/Second.outputs:surface" in change.after
    _, (opinion,) = inspect_opinions(second, change.path, change.field)
    assert opinion.spec_path == "/Material.outputs:surface"
    assert "Authored connection list prepends /Material/Second.outputs:surface" in opinion.value


def test_authored_playback_range_changes_and_names_its_layer():
    stage = Usd.Stage.CreateInMemory()
    blank = snapshot_stage(stage)
    stage.SetStartTimeCode(1)
    stage.SetEndTimeCode(24)
    authored = snapshot_stage(stage)
    appearance, = compare_scenes(blank, authored)
    assert (appearance.path, appearance.field) == ("/", "time_range")
    assert appearance.before == "(no authored range)"
    assert appearance.after == "1.0 to 24.0"

    stage.SetEndTimeCode(48)
    modified, = compare_scenes(authored, snapshot_stage(stage))
    assert modified.after == "1.0 to 48.0"
    _, (opinion,) = inspect_opinions(stage, modified.path, modified.field)
    assert opinion.layer == stage.GetRootLayer().identifier
    assert "startTimeCode = 1.0" in opinion.value
    assert "endTimeCode = 48.0" in opinion.value


def test_property_read_failures_keep_review_pending_and_can_be_archived(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from scenescout.app import SceneScoutWindow

    path = tmp_path / "source.usda"
    path.write_text(
        '#usda 1.0\ndef Xform "Asset" {\n'
        '    custom double amount = 1\n'
        '    custom double animated.timeSamples = {10: 2}\n'
        '    custom rel target = </Asset>\n'
        '}\n',
        encoding="utf-8",
    )
    review_path = tmp_path / "approved.json"
    document = ReviewDocument(str(path), str(path), decision="Approved")
    save_review(review_path, document)

    def fail_property_read(method, name):
        def get(prop, *args):
            if prop.GetName() == name:
                raise RuntimeError("Test read failure")
            return method(prop, *args)
        return get

    monkeypatch.setattr(Usd.Attribute, "Get", fail_property_read(Usd.Attribute.Get, "amount"))
    monkeypatch.setattr(
        Usd.Attribute, "GetTimeSamples", fail_property_read(Usd.Attribute.GetTimeSamples, "animated")
    )
    monkeypatch.setattr(Usd.Relationship, "GetTargets", fail_property_read(Usd.Relationship.GetTargets, "target"))

    app = QApplication.instance() or QApplication([])
    window = SceneScoutWindow()
    window.show()
    try:
        window.signoff.resume(load_review(review_path), review_path)
        deadline = time.monotonic() + 30
        while not window.comparison_available:
            assert time.monotonic() < deadline, "Timed out waiting for comparison"
            app.processEvents()
            time.sleep(0.005)

        assert not window.comparison_complete
        assert "Comparison incomplete" in window.review.coverage_summary
        assert "No findings" not in window.review.coverage_summary
        assert {(change.path, change.field) for change in window.review.changes} == {
            ("/Asset", "attributes.amount"), ("/Asset", "relationships.target"),
            ("/Asset", "sample_times.animated"),
        }
        assert all(change.kind == "Unreadable" and change.before == change.after
                   for change in window.review.changes)
        assert window.signoff.save_action.isEnabled()
        assert window.signoff.export_action.isEnabled()
        assert not window.signoff.share_action.isEnabled()
        assert not window.signoff.decision_action.isEnabled()
        assert not window.review.signoff_button.isEnabled()
        assert window.signoff.decision == "Pending"

        archived_path = tmp_path / "incomplete.json"
        save_review(archived_path, window.signoff.draft())
        archived = load_review(archived_path)
        assert archived.decision == "Pending"
        assert "Comparison incomplete" in archived.coverage
        assert len(archived.findings) == 3
    finally:
        window.signoff.dirty = False
        window.close()
        deadline = time.monotonic() + 30
        while window.isVisible():
            assert time.monotonic() < deadline, "Timed out closing comparison"
            app.processEvents()
            time.sleep(0.005)
