from base64 import b64encode
from dataclasses import replace
import json

import pytest

from scenescout.diff import Change
from scenescout.review_document import (
    ReviewDocument, ReviewFinding, ReviewPosition, ReviewSettings, ReviewThumbnails, ReviewView,
    export_report, load_review, save_review,
)


def sample_review():
    return ReviewDocument(
        stage_a="missing/<before>.usda",
        stage_b="missing/π after.usda",
        findings=(
            ReviewFinding(
                Change("Modified", "/Pump", "attributes.xformOp:translate", "0", "2", 24, 30),
                evidence="motion.usda\n/Pump.xformOp:translate\nAuthored sample at A 24 and B 30",
                reviewed=True,
            ),
            ReviewFinding(
                Change("Issue", "/Pump", "dependencies.composition", "", 'Could not open "rig.usda"'),
                evidence="assembly.usda\n/Pump\n<source>",
                notes="Ask the asset <lead>",
                issue="Current publish has an unresolved asset.",
            ),
            ReviewFinding(
                Change("Unreadable", "/Pump", "attributes.radius", "2", "<unreadable>", 32, 40)
            ),
            ReviewFinding(
                Change("Removed", "/Pump/Guide", "prim", "Scope", ""),
                open_issue=True,
            ),
        ),
        coverage="Some payloads are outside the current review.",
        notes="Check assembly\n<script>alert(1)</script>",
        decision="Changes requested",
        position=ReviewPosition("B", "/Pump", "dependencies.composition", 24, 30, 1),
        settings=ReviewSettings("frames", (("/", "NoneRule"), ("/Pump", "AllRule")), (), True),
    )


def test_review_round_trips_archived_evidence_without_opening_stages(tmp_path):
    source = sample_review()
    target = tmp_path / "pump.review.json"

    save_review(target, source)

    assert load_review(target) == source
    assert "π after.usda" in target.read_text(encoding="utf-8")


def test_review_round_trips_pinned_view_and_accepts_findings_without_one(tmp_path):
    source = sample_review()
    view = ReviewView((1.0, -2.0, 3.0), 12.0, 31.0, -15.0, 60.0, 68.0, 24.0, 30.0)
    findings = (replace(source.findings[0], view=view), *source.findings[1:])
    source = replace(source, findings=findings)
    target = tmp_path / "pump.review.json"
    save_review(target, source)

    assert load_review(target) == source
    payload = json.loads(target.read_text(encoding="utf-8"))
    records = payload["review"]["findings"]
    assert "view" not in records[1]
    records[0]["view"]["wipe"] = 101.0
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="valid camera and wipe position"):
        load_review(target)


def test_review_thumbnails_reopen_and_export_without_external_images(tmp_path):
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QImage

    image = QImage(4, 4, QImage.Format_RGB32)
    image.fill(0xFF649FA4)
    buffer = QBuffer()
    assert buffer.open(QIODevice.WriteOnly)
    assert image.save(buffer, "JPEG", 75)
    jpeg = b64encode(bytes(buffer.data())).decode("ascii")
    source = sample_review()
    thumbnails = ReviewThumbnails(jpeg, jpeg, 24.0, 30.0)
    findings = (replace(source.findings[0], thumbnails=thumbnails), *source.findings[1:])
    source = replace(source, findings=findings)
    target = tmp_path / "pump.review.json"
    report = tmp_path / "pump.html"

    save_review(target, source)
    assert load_review(target) == source
    export_report(report, load_review(target))
    html = report.read_text(encoding="utf-8")
    assert html.count(f'data:image/jpeg;base64,{jpeg}') == 2
    assert "Stage B preview at 30" in html


@pytest.mark.parametrize(
    ("section", "key", "value", "error"),
    [
        ("review", "stage_b", "", "two valid stage paths"),
        ("review", "decision", "Sent", "Unknown review decision"),
        ("position", "finding_index", 25, "missing finding"),
        ("position", "time", True, "finite numbers"),
        ("position", "time", float("inf"), "finite numbers"),
        ("settings", "alignment", "arbitrary", "Unknown timeline alignment"),
        ("settings", "payload_rules_b", [["/Pump", "BadRule"]], "Invalid stage payload rule"),
        ("settings", "payload_rules_b", {}, "Expected an array"),
    ],
)
def test_load_rejects_invalid_fields(tmp_path, section, key, value, error):
    target = tmp_path / "pump.json"
    save_review(target, sample_review())
    payload = json.loads(target.read_text(encoding="utf-8"))
    fields = payload["review"] if section == "review" else payload["review"][section]
    fields[key] = value
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=error):
        load_review(target)


def test_unsupported_review_version_is_reported(tmp_path):
    target = tmp_path / "unknown.json"
    target.write_text('{"format": "scenescout-review", "version": 2}', encoding="utf-8")

    with pytest.raises(ValueError, match="version is unsupported"):
        load_review(target)


@pytest.mark.parametrize("write", [save_review, export_report])
def test_review_output_cannot_overwrite_a_usd_stage(tmp_path, write):
    stage = tmp_path / "pump.usda"
    stage.write_text("#usda 1.0", encoding="utf-8")

    with pytest.raises(ValueError, match="Choose"):
        write(stage, sample_review())

    assert stage.read_text(encoding="utf-8") == "#usda 1.0"


def test_failed_replace_preserves_previous_review(tmp_path, monkeypatch):
    target = tmp_path / "pump.review.json"
    source = sample_review()
    save_review(target, source)

    def reject_replace(self, _target):
        raise PermissionError("File is busy.")

    monkeypatch.setattr(type(target), "replace", reject_replace)
    with pytest.raises(PermissionError, match="File is busy"):
        save_review(target, ReviewDocument("earlier.usda", "latest.usda"))

    assert load_review(target) == source
    assert list(tmp_path.iterdir()) == [target]


def test_standalone_report_includes_decision_issues_changes_and_escaped_evidence(tmp_path):
    report = tmp_path / "pump.html"
    export_report(report, sample_review())

    html = report.read_text(encoding="utf-8")
    for fragment in (
        "<dd>Changes requested</dd>",
        "1 of 4 findings reviewed",
        "Some payloads are outside the current review.",
        "Match by frame number",
        "Check assembly\n&lt;script&gt;alert(1)&lt;/script&gt;",
        "missing/&lt;before&gt;.usda",
        "<h2>Open issues</h2>",
        'href="#finding-1"',
        "Current publish has an unresolved asset.",
        'href="#finding-2"',
        "Unable to read the composed value.",
        'href="#finding-3"',
        "Flagged for follow-up.",
        "Time codes · A 24 · B 30",
        "Authored sample at A 24 and B 30",
        "assembly.usda\n/Pump\n&lt;source&gt;",
        "Ask the asset &lt;lead&gt;",
        "<h2>Key changes and evidence</h2>",
        "<h3>Structural (1)</h3>",
        "<h3>Animation (2)</h3>",
        "<h3>Dependency (1)</h3>",
    ):
        assert fragment in html
    assert "<script" not in html
    assert 'href="http' not in html
    assert 'src="' not in html


def test_report_distinguishes_current_dependency_issues_from_coverage_gaps(tmp_path):
    report = tmp_path / "pump.html"
    document = ReviewDocument(
        "before.usda", "after.usda",
        (
            ReviewFinding(Change("Modified", "/Old", "dependencies.composition", "Missing asset", "")),
            ReviewFinding(Change("Issue", "/Pump", "dependencies.composition", "", "Missing asset")),
            ReviewFinding(Change("Coverage", "/Payload", "dependencies.payload.status", "", "Unloaded")),
        ),
    )

    export_report(report, document)

    html = report.read_text(encoding="utf-8")
    coverage, issues = html.split("<h2>Open issues</h2>")
    issues, _ = issues.split("<h2>Key changes and evidence</h2>")
    assert 'href="#finding-2"' in coverage
    assert 'href="#finding-1"' in issues
    assert 'href="#finding-0"' not in issues
    assert 'href="#finding-2"' not in issues
    assert "Stage B reported a dependency composition error." in issues
