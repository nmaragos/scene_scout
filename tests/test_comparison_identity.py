from dataclasses import replace
import json
import os
import time

import pytest
from pxr import Sdf, Usd, Vt

from scenescout.animation import compare_animation
from scenescout.diff import compare_scenes
from scenescout.review_document import ReviewDocument, ReviewFinding, load_review, save_review
from scenescout.usd_reader import snapshot_stage


PREFIX = "x" * 700


def make_stage(tail):
    stage = Usd.Stage.CreateInMemory()
    prim = stage.DefinePrim("/Asset", "Xform")
    prim.CreateAttribute("description", Sdf.ValueTypeNames.String).Set(PREFIX + tail)
    return stage


def test_long_default_and_asset_info_values_compare_past_the_display_limit():
    first = make_stage("before")
    second = make_stage("after")
    for stage, tail in ((first, "before"), (second, "after")):
        prim = stage.GetPrimAtPath("/Asset")
        prim.SetAssetInfoByKey("description", PREFIX + tail)
        prim.CreateAttribute("numbers", Sdf.ValueTypeNames.IntArray).Set(
            Vt.IntArray([1000] * 200 + [len(tail)])
        )

    changes = compare_scenes(snapshot_stage(first), snapshot_stage(second))
    assert {change.field for change in changes} == {
        "attributes.description", "attributes.numbers", "asset_info.description",
    }
    for change in changes:
        assert change.before == change.after
        assert change.before.endswith("…")
        assert change.identity is not None


def test_animation_identity_includes_sample_content_past_the_display_limit():
    first, second = make_stage("default"), make_stage("default")
    attr_a = first.GetPrimAtPath("/Asset").GetAttribute("description")
    attr_b = second.GetPrimAtPath("/Asset").GetAttribute("description")
    attr_a.Set(PREFIX + "before", 48)
    attr_b.Set(PREFIX + "after", 48)
    snapshots = snapshot_stage(first), snapshot_stage(second)
    original, = compare_animation(first, snapshots[0], second, snapshots[1], 1)

    attr_b.Set(PREFIX + "edited", 48)
    edited, = compare_animation(first, snapshots[0], second, snapshots[1], 1)

    assert original.before == original.after
    assert replace(original, identity=None) == replace(edited, identity=None)
    assert original.identity != edited.identity


@pytest.mark.parametrize(("tail", "legacy", "expected"), [
    ("after", False, "Approved"),
    ("edited", False, "Pending"),
    ("after", True, "Pending"),
])
def test_resume_uses_saved_comparison_identity(tmp_path, tail, legacy, expected):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from scenescout.app import SceneScoutWindow

    first, second = make_stage("before"), make_stage("after")
    original, = compare_scenes(snapshot_stage(first), snapshot_stage(second))
    before_path, after_path = tmp_path / "a.usda", tmp_path / "b.usda"
    assert first.GetRootLayer().Export(str(before_path))
    assert make_stage(tail).GetRootLayer().Export(str(after_path))
    path = tmp_path / "review.json"
    document = ReviewDocument(
        str(before_path), str(after_path), (ReviewFinding(original),),
        coverage="A · a.usda     B · b.usda", decision="Approved",
    )
    save_review(path, document)
    if legacy:
        payload = json.loads(path.read_text(encoding="utf-8"))
        del payload["review"]["findings"][0]["change"]["identity"]
        path.write_text(json.dumps(payload), encoding="utf-8")
    saved = load_review(path)
    assert saved.decision == "Approved"
    assert saved.findings[0].change.identity == (None if legacy else original.identity)

    app = QApplication.instance() or QApplication([])
    window = SceneScoutWindow()
    try:
        window.signoff.resume(saved, path)
        deadline = time.monotonic() + 30
        while not window.comparison_complete:
            assert time.monotonic() < deadline, "Timed out waiting for the resumed comparison"
            app.processEvents()
            time.sleep(0.005)
        current, = window.review.changes
        assert replace(current, identity=None) == replace(original, identity=None)
        assert window.signoff.decision == expected
    finally:
        window.signoff.dirty = False
        window.close()
