"""Portable local review files and self-contained HTML reports."""

from base64 import b64decode
from dataclasses import asdict, dataclass
from html import escape
import json
from math import isfinite
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

from .diff import Change, field_label, finding_kind_label
from .models import SceneSnapshot
from .textures import TEXTURE_FIELD_PREFIX


_FORMAT = "scenescout-review"
_VERSION = 1
REVIEW_DECISIONS = ("Pending", "Approved", "Changes requested")
_CATEGORIES = ("Structural", "Animation", "Dependency")
_ALIGNMENTS = {"seconds": "Match by seconds", "frames": "Match by frame number"}
THUMBNAIL_PAIR_LIMIT = 24 * 1024
THUMBNAIL_REVIEW_LIMIT = 2 * 1024 * 1024


@dataclass(frozen=True)
class ReviewView:
    center: tuple[float, float, float]
    distance: float
    azimuth: float
    elevation: float
    field_of_view: float
    wipe: float
    time: float | None = None
    time_b: float | None = None


@dataclass(frozen=True)
class ReviewThumbnails:
    stage_a: str
    stage_b: str
    time: float | None = None
    time_b: float | None = None

    @property
    def size(self):
        return len(self.stage_a) + len(self.stage_b)


@dataclass(frozen=True)
class ReviewFinding:
    change: Change
    evidence: str = ""
    notes: str = ""
    reviewed: bool = False
    open_issue: bool = False
    issue: str = ""
    view: ReviewView | None = None
    thumbnails: ReviewThumbnails | None = None


@dataclass(frozen=True)
class ReviewPosition:
    slot: str = "A"
    path: str = ""
    field: str = ""
    time: float | None = None
    time_b: float | None = None
    finding_index: int | None = None


@dataclass(frozen=True)
class ReviewSettings:
    alignment: str = "seconds"
    payload_rules_a: tuple[tuple[str, str], ...] = ()
    payload_rules_b: tuple[tuple[str, str], ...] = ()
    load_payloads_on_open: bool = False


@dataclass(frozen=True)
class ReviewDocument:
    stage_a: str
    stage_b: str
    findings: tuple[ReviewFinding, ...] = ()
    coverage: str = ""
    notes: str = ""
    decision: str = "Pending"
    position: ReviewPosition = ReviewPosition()
    settings: ReviewSettings = ReviewSettings()


def finding_category(change: Change) -> str:
    if change.field.startswith("dependencies.") or change.field in ("references", "payloads"):
        return "Dependency"
    if change.time is not None:
        return "Animation"
    return "Structural"


def finding_issue(change: Change) -> str:
    if change.kind == "Unreadable":
        return "Unable to read the composed value."
    if change.kind == "Issue" and change.field == "dependencies.composition":
        return "Stage B reported a dependency composition error."
    if change.kind == "Issue" and change.field.startswith(TEXTURE_FIELD_PREFIX):
        return "Stage B has an unresolved texture path."
    return ""


def _text_fields(record, *names):
    for name in names:
        if not isinstance(getattr(record, name), str):
            raise ValueError(f"{name} must be text.")


def _finite_number(value):
    try:
        return type(value) in (float, int) and isfinite(value)
    except OverflowError:
        return False


def _times(record):
    for time in (record.time, record.time_b):
        if time is not None and not _finite_number(time):
            raise ValueError("Review time codes must be finite numbers.")


def _validate_view(view):
    if not isinstance(view, ReviewView) or not isinstance(view.center, tuple) or len(view.center) != 3:
        raise ValueError("Invalid pinned review view.")
    values = (*view.center, view.distance, view.azimuth, view.elevation, view.field_of_view, view.wipe)
    if (not all(_finite_number(value) for value in values) or view.distance <= 0
            or not 0 < view.field_of_view < 180 or not 0 <= view.wipe <= 100):
        raise ValueError("A pinned view requires a valid camera and wipe position.")
    _times(view)


def _validate_thumbnails(thumbnails):
    if not isinstance(thumbnails, ReviewThumbnails):
        raise ValueError("Invalid review thumbnails.")
    _text_fields(thumbnails, "stage_a", "stage_b")
    if thumbnails.size > THUMBNAIL_PAIR_LIMIT:
        raise ValueError("The thumbnail pair exceeds the size limit.")
    for encoded in (thumbnails.stage_a, thumbnails.stage_b):
        try:
            data = b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ValueError("Invalid JPEG thumbnail.") from exc
        if not data.startswith(b"\xff\xd8\xff") or not data.endswith(b"\xff\xd9"):
            raise ValueError("Invalid JPEG thumbnail.")
    _times(thumbnails)


def _validate(document: ReviewDocument):
    _text_fields(document, "stage_a", "stage_b", "coverage", "notes", "decision")
    if any(not stage.strip() or "\x00" in stage for stage in (document.stage_a, document.stage_b)):
        raise ValueError("A saved review requires two valid stage paths.")
    if document.decision not in REVIEW_DECISIONS:
        raise ValueError("Unknown review decision.")
    thumbnail_bytes = 0
    for finding in document.findings:
        _text_fields(finding, "evidence", "notes", "issue")
        if type(finding.reviewed) is not bool or type(finding.open_issue) is not bool:
            raise ValueError("Finding flags must be true or false.")
        change = finding.change
        _text_fields(change, "kind", "path", "field", "before", "after")
        if not change.kind or not change.path or not change.field:
            raise ValueError("A finding requires a change kind, path and field.")
        if change.identity is not None and (
            not isinstance(change.identity, str) or not re.fullmatch("[0-9a-f]{64}", change.identity)
        ):
            raise ValueError("Invalid finding comparison identity.")
        _times(change)
        if finding.view is not None:
            _validate_view(finding.view)
        if finding.thumbnails is not None:
            _validate_thumbnails(finding.thumbnails)
            thumbnail_bytes += finding.thumbnails.size
            if thumbnail_bytes > THUMBNAIL_REVIEW_LIMIT:
                raise ValueError("Review thumbnails exceed the size limit.")

    position = document.position
    _text_fields(position, "slot", "path", "field")
    if position.slot not in ("A", "B"):
        raise ValueError("The review position must refer to stage A or B.")
    _times(position)
    index = position.finding_index
    if index is not None and (type(index) is not int or not 0 <= index < len(document.findings)):
        raise ValueError("The review position points to a missing finding.")

    settings = document.settings
    _text_fields(settings, "alignment")
    if settings.alignment not in _ALIGNMENTS:
        raise ValueError("Unknown timeline alignment.")
    if type(settings.load_payloads_on_open) is not bool:
        raise ValueError("Load payloads on open must be true or false.")
    for rules in (settings.payload_rules_a, settings.payload_rules_b):
        for path, rule in rules:
            if not isinstance(path, str) or not path.startswith("/") or rule not in ("AllRule", "OnlyRule", "NoneRule"):
                raise ValueError("Invalid stage payload rule.")


def _array(value):
    if not isinstance(value, list):
        raise ValueError("Expected an array in the saved review.")
    return value


def _read_finding(data):
    if not isinstance(data, dict):
        raise ValueError("Invalid finding record.")
    fields = dict(data)
    fields["change"] = Change(**fields["change"])
    if fields.get("view") is not None:
        view = dict(fields["view"])
        view["center"] = tuple(_array(view["center"]))
        fields["view"] = ReviewView(**view)
    if fields.get("thumbnails") is not None:
        fields["thumbnails"] = ReviewThumbnails(**fields["thumbnails"])
    return ReviewFinding(**fields)


def _decode_review(payload):
    if not isinstance(payload, dict) or payload.get("format") != _FORMAT:
        raise ValueError("This file is not a SceneScout review.")
    if type(payload.get("version")) is not int or payload["version"] != _VERSION:
        raise ValueError("This SceneScout review version is unsupported.")
    try:
        data = dict(payload["review"])
        data["findings"] = tuple(_read_finding(item) for item in _array(data["findings"]))
        data["position"] = ReviewPosition(**data["position"])
        settings = dict(data["settings"])
        for name in ("payload_rules_a", "payload_rules_b"):
            settings[name] = tuple(tuple(_array(rule)) for rule in _array(settings[name]))
        data["settings"] = ReviewSettings(**settings)
        document = ReviewDocument(**data)
        _validate(document)
    except (KeyError, TypeError) as exc:
        raise ValueError("The SceneScout review has missing or invalid fields.") from exc
    return document


def _target(path, suffixes):
    target = Path(path)
    if target.suffix.lower() not in suffixes:
        raise ValueError(f"Choose a {' or '.join(suffixes)} file.")
    if os.name == "nt" and ":" in str(target)[len(target.drive):]:
        raise ValueError("Choose a file without a stream name.")
    return target


def _write_text(target, content):
    temporary = None
    try:
        with NamedTemporaryFile(
            mode="w", newline="\n", encoding="utf-8", dir=target.parent,
            prefix=".scenescout-", delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(content)
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_json(target, payload):
    _write_text(target, json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n")


def export_snapshot(path, scene: SceneSnapshot, *, source_layers=()) -> None:
    target = _target(path, (".json",))
    resolved = target.resolve()
    try:
        target_stat = target.stat()
    except FileNotFoundError:
        target_stat = None
    for source in {scene.source, *source_layers} - {""}:
        layer = Path(source)
        if layer.resolve() == resolved:
            raise ValueError("The export destination identifies an open source layer.")
        if target_stat is not None:
            try:
                layer_stat = layer.stat()
            except FileNotFoundError:
                continue
            if os.path.samestat(target_stat, layer_stat):
                raise ValueError("The export destination identifies an open source layer.")

    payload = asdict(scene)
    for prim in payload["prims"].values():
        prim.pop("attribute_fingerprints", None)
        prim.pop("asset_info_fingerprints", None)
    _write_json(target, payload)


def save_review(path, document: ReviewDocument) -> None:
    """Save a review without changing either USD stage."""
    target = _target(path, (".json",))
    _validate(document)
    review = asdict(document)
    for finding in review["findings"]:
        for name in ("view", "thumbnails"):
            if finding[name] is None:
                del finding[name]
        if finding["change"]["identity"] is None:
            del finding["change"]["identity"]
    payload = {"format": _FORMAT, "version": _VERSION, "review": review}
    _write_json(target, payload)


def load_review(path) -> ReviewDocument:
    """Read a saved review independently of stage availability."""
    with Path(path).open(encoding="utf-8-sig") as source:
        return _decode_review(json.load(source))


_REPORT_STYLE = """
    html { color-scheme: light; }
    body { margin: 0 auto; padding: 2rem 1.2rem 3rem; max-width: 68rem;
           color: #182434; background: #f6f8fa; font: 1rem/1.55 system-ui, sans-serif; }
    header, article { background: #fff; border: 1px solid #cbd5e1; border-radius: .6rem;
                      padding: 1.1rem 1.35rem; margin-block: 1rem; }
    header { border-top: .3rem solid #27616b; }
    h1, h2, h3, h4, p { margin: 0 0 .6rem; }
    section { margin-block: 1.6rem; }
    h2 { border-bottom: 1px solid #cbd5e1; padding-bottom: .3rem; }
    h3 { margin-top: 1rem; }
    dt, summary { font-weight: 650; }
    dd { margin: 0 0 .5rem; }
    ul { padding-inline-start: 1.3rem; }
    li { margin-block: .35rem; }
    code, .text, pre { overflow-wrap: anywhere; white-space: pre-wrap; }
    code, pre { font-family: ui-monospace, Consolas, monospace; font-size: .95em; }
    pre { background: #edf2f6; border-radius: .3rem; margin: .3rem 0 .8rem; padding: .6rem; }
    .values { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .75rem; }
    .thumbnails figure { margin: 0 0 .75rem; }
    .thumbnails img { max-width: 100%; height: auto; display: block; }
    .thumbnails figcaption { color: #475569; font-size: .9rem; }
    .issue { color: #8a330c; }
    .muted { color: #475569; }
    a { color: #145e76; }
    @media (max-width: 40rem) { .values { grid-template-columns: 1fr; gap: 0; } }
    @media print { body { max-width: none; padding: 0; background: #fff; }
                   h2, h3, h4, summary { break-after: avoid; } }
"""


def _report_thumbnails(thumbnails):
    if thumbnails is None:
        return ""
    figures = []
    for slot, image, time in zip("AB", (thumbnails.stage_a, thumbnails.stage_b),
                                 (thumbnails.time, thumbnails.time_b)):
        label = f"Stage {slot} preview" if time is None else f"Stage {slot} preview at {time:.15g}"
        figures.append(f'<figure><img alt="{label}" loading="lazy" src="data:image/jpeg;base64,{image}">'
                       f'<figcaption>{label}</figcaption></figure>')
    return f'<div class="values thumbnails">{"".join(figures)}</div>'


def _shared_finding_fields(index, finding):
    reviewed = " checked" if finding.reviewed else ""
    open_issue = " checked" if finding.open_issue else ""
    return (
        f'<label for="notes-{index}">Finding notes</label>'
        f'<input type="text" id="notes-{index}" name="notes-{index}" '
        f'value="{escape(finding.notes, quote=True)}" maxlength="32767">'
        f'<div class="review-flags"><label><input type="checkbox" name="reviewed-{index}" value="1"'
        f'{reviewed}> Reviewed</label>'
        f'<label><input type="checkbox" name="open_issue-{index}" value="1"{open_issue}> Open issue</label></div>'
    )


def _report_finding(index, finding, issue, *, editable=False):
    change = finding.change
    progress = "Reviewed" if finding.reviewed else "Unreviewed"
    timing = ""
    if change.time is not None:
        time_b = f" · B {change.time_b:.15g}" if change.time_b is not None else ""
        timing = f'<p>Time codes · A {change.time:.15g}{time_b}</p>'
    issue_text = f'<p class="issue text">{escape(issue)}</p>' if issue else ""
    if editable:
        notes = _shared_finding_fields(index, finding)
    else:
        notes = f'<p class="text"><strong>Finding notes</strong><br>{escape(finding.notes)}</p>' if finding.notes else ""
    evidence = (
        f"<details open><summary>Supporting evidence</summary><pre>{escape(finding.evidence)}</pre></details>"
        if finding.evidence else '<p class="muted">No supporting evidence was saved for this finding.</p>'
    )
    before = escape(change.before) or "—"
    after = escape(change.after) or "—"
    return (
        f'<article id="finding-{index}"><h4>{escape(finding_kind_label(change.kind))} · '
        f'{escape(field_label(change.field))}</h4>'
        f'<p><code>{escape(change.path)}</code> · {progress}</p>'
        f'{timing}{issue_text}<dl class="values">'
        f'<div><dt>Stage A</dt><dd><pre>{before}</pre></dd></div>'
        f'<div><dt>Stage B</dt><dd><pre>{after}</pre></dd></div>'
        f'</dl>{_report_thumbnails(finding.thumbnails)}{notes}{evidence}</article>'
    )


_SHARED_REVIEW_STYLE = """
    .review-fields { border: 0; min-width: 0; margin: 0; padding: 0; }
    textarea, input, select, button { font: inherit; color: inherit; }
    textarea, input[type="text"] { display: block; box-sizing: border-box; width: 100%;
                                  margin: .25rem 0 .65rem; padding: .45rem; }
    textarea { resize: vertical; }
    textarea, input[type="text"], select { border: 1px solid #94a3b8; border-radius: .3rem;
                                           background: #fff; }
    .review-flags { display: flex; flex-wrap: wrap; gap: .4rem 1rem; margin-bottom: .75rem; }
    .review-save { position: sticky; top: 0; z-index: 1; display: flex; flex-wrap: wrap;
                   align-items: center; gap: .5rem 1rem; padding: .65rem .85rem;
                   border: 1px solid #cbd5e1; border-radius: .4rem; background: #fff; }
    .review-save button { background: #27616b; color: #fff; border: 0;
                          border-radius: .3rem; padding: .35rem .75rem; cursor: pointer; }
    .review-save p { margin: 0; overflow-wrap: anywhere; }
    article { scroll-margin-top: 6rem; }
"""


SHARED_REVIEW_EDIT_LIMIT_BYTES = 8 * 1024 * 1024
SHARED_REVIEW_SCRIPT = """
(() => {
    const form = document.querySelector("form");
    const fields = form.querySelector("fieldset");
    const save = form.querySelector("button[type=submit]");
    const status = form.querySelector("[role=status]");
    const readValue = (field) => field.type === "checkbox" ? (field.checked ? "1" : "0") : field.value;
    const baseline = new Map(Array.from(fields.elements)
        .filter((field) => field.name)
        .map((field) => [field, readValue(field)]));

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (fields.disabled) return;
        const edits = new URLSearchParams({ revision: form.elements.revision.value });
        for (const [field, original] of baseline) {
            const value = readValue(field);
            if (value !== original) edits.set(field.name, value);
        }
        const body = edits.toString();
        if (body.length > Number(form.dataset.maxSaveBytes)) {
            status.textContent = "This save exceeds 8 MiB. Shorten or undo some edits and try again.";
            return;
        }

        save.disabled = true;
        fields.disabled = true;
        status.textContent = "Saving…";
        try {
            const response = await fetch(form.action, {
                method: "POST",
                headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
                body
            });
            if (response.ok) {
                window.location.replace("?saved=1");
                return;
            }
            status.textContent = await response.text();
        } catch {
            status.textContent = "Could not save the review. Your edits are still on this page.";
        }
        fields.disabled = false;
        save.disabled = false;
    });
    save.disabled = false;
})();
"""


def _shared_review_fields(document):
    options = []
    for decision in REVIEW_DECISIONS:
        selected = " selected" if decision == document.decision else ""
        options.append(f'<option{selected}>{escape(decision)}</option>')
    return (
        '<label for="decision">Sign-off decision</label> '
        f'<select id="decision" name="decision">{"".join(options)}</select>'
        f'<textarea id="review-notes" name="notes" rows="4">\n{escape(document.notes)}</textarea>'
    )


def _render_report(document, *, revision=None, message=""):
    _validate(document)
    editable = revision is not None
    total = len(document.findings)
    reviewed = sum(finding.reviewed for finding in document.findings)
    issues = []
    limits = []
    categories = {category: [] for category in _CATEGORIES}
    for index, finding in enumerate(document.findings):
        change = finding.change
        link = (
            f'<a href="#finding-{index}"><code>{escape(change.path)} · '
            f'{escape(field_label(change.field))}</code></a>'
        )
        issue = finding.issue or finding_issue(change)
        if finding.open_issue and not issue:
            issue = "Flagged for follow-up."
        if issue:
            issues.append(f'<li>{link} <span class="text">{escape(issue)}</span></li>')
        if change.kind == "Coverage":
            limits.append(f"<li>{link}</li>")
        categories[finding_category(change)].append(_report_finding(index, finding, issue, editable=editable))

    open_issues = f'<ul>{"".join(issues)}</ul>' if issues else "<p>No open issues recorded.</p>"
    coverage_links = f'<p>Coverage findings</p><ul>{"".join(limits)}</ul>' if limits else ""
    sections = "".join(
        f'<h3>{category} ({len(entries)})</h3>{"".join(entries)}'
        for category, entries in categories.items() if entries
    ) or "<p>No findings were recorded.</p>"
    notes = f'<p class="text">{escape(document.notes) or "No review notes recorded."}</p>'
    style = _REPORT_STYLE
    form_open = form_close = script = ""
    notes_heading = '<h2>Review notes</h2>'
    if editable:
        style += _SHARED_REVIEW_STYLE
        form_open = (
            f'<form method="post" data-max-save-bytes="{SHARED_REVIEW_EDIT_LIMIT_BYTES}">'
            f'<input type="hidden" name="revision" value="{escape(str(revision), quote=True)}">'
            '<div class="review-save"><button type="submit" disabled>Save review</button>'
            f'<p role="status">{escape(message)}</p>'
            '<noscript>Enable JavaScript to save browser edits.</noscript></div>'
            '<fieldset class="review-fields">'
        )
        form_close = "</fieldset></form>\n"
        script = f"<script>{SHARED_REVIEW_SCRIPT}</script>"
        notes_heading = '<h2><label for="review-notes">Review notes</label></h2>'
        notes = _shared_review_fields(document)
    coverage = escape(document.coverage) or "No coverage details recorded."
    alignment = _ALIGNMENTS[document.settings.alignment]
    return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>SceneScout publish review</title>
    <style>{style}</style>
</head>
<body>
<header>
    <h1>SceneScout publish review</h1>
    <dl>
        <dt>Stage A</dt><dd><code>{escape(document.stage_a)}</code></dd>
        <dt>Stage B</dt><dd><code>{escape(document.stage_b)}</code></dd>
        <dt>Review decision</dt><dd>{escape(document.decision)}</dd>
        <dt>Review progress</dt><dd>{reviewed} of {total} findings reviewed</dd>
    </dl>
</header>
<main>{form_open}
    <section>{notes_heading}{notes}</section>
    <section><h2>Comparison coverage</h2><p>{alignment}</p><p class="text">{coverage}</p>{coverage_links}</section>
    <section><h2>Open issues</h2>{open_issues}</section>
    <section><h2>Key changes and evidence</h2>{sections}</section>
{form_close}</main>
{script}
</body>
</html>
"""


def render_report(document: ReviewDocument) -> str:
    """Render the saved review, including evidence and gaps in coverage."""
    return _render_report(document)


def render_shared_review(document: ReviewDocument, revision: int, message: str = "") -> str:
    """Render a browser form for editing the review at the current revision."""
    return _render_report(document, revision=revision, message=message)


def export_report(path, document: ReviewDocument) -> None:
    """Write an HTML report that keeps the saved evidence readable offline."""
    target = _target(path, (".html", ".htm"))
    _write_text(target, render_report(document))
