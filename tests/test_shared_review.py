from base64 import b64encode
from hashlib import sha256
from http import HTTPStatus
from threading import Lock

import pytest

from scenescout.diff import Change
from scenescout.review_document import (
    SHARED_REVIEW_EDIT_LIMIT_BYTES, ReviewDocument, ReviewFinding, render_report, render_shared_review,
)
from scenescout.shared_review import SharedReview, _CONTENT_POLICY


def make_session(document):
    session = SharedReview.__new__(SharedReview)
    session._document = document
    session._revision = 0
    session._lock = Lock()
    session.closed = False
    return session


def test_small_edit_saves_a_review_with_more_than_eight_mib_of_existing_notes():
    existing_note = "x" * 32767
    findings = tuple(
        ReviewFinding(Change("Added", f"/Item{i}", "prim", "", "Xform"), notes=existing_note,
                      reviewed=True, open_issue=True)
        for i in range(300)
    )
    assert len(existing_note) * len(findings) > SHARED_REVIEW_EDIT_LIMIT_BYTES
    session = make_session(ReviewDocument("first.usda", "second.usda", findings, notes="Publish notes"))

    assert session.submit({"revision": ["0"], "notes-0": [""], "reviewed-0": ["0"]}) == HTTPStatus.OK

    saved, revision = session.snapshot()
    assert revision == 1
    assert saved.notes == "Publish notes"
    assert saved.findings[0].notes == ""
    assert saved.findings[0].reviewed is False
    assert saved.findings[0].open_issue is True
    assert saved.findings[1:] == findings[1:]

    assert session.submit({"revision": ["0"], "notes-0": ["Stale edits"]}) == HTTPStatus.CONFLICT
    assert session.snapshot() == (saved, revision)
    assert session.submit({"revision": ["1"]}) == HTTPStatus.OK
    assert session.snapshot() == (saved, revision)


@pytest.mark.parametrize("invalid_edit", [
    {"notes-1": ["Unknown finding"]},
    {"open_issue-0": ["invalid"]},
    {"notes-0": ["First value", "Duplicate value"]},
])
def test_invalid_sparse_edit_does_not_apply_other_fields(invalid_edit):
    finding = ReviewFinding(Change("Added", "/Item", "prim", "", "Xform"))
    document = ReviewDocument("first.usda", "second.usda", (finding,))
    session = make_session(document)

    with pytest.raises(ValueError):
        session.submit({"revision": ["0"], "decision": ["Approved"], **invalid_edit})
    assert session.snapshot() == (document, 0)


def test_browser_report_allows_only_its_edit_script():
    document = ReviewDocument("first.usda", "second.usda")
    shared = render_shared_review(document, 0)
    script = shared.partition("<script>")[2].partition("</script>")[0]
    digest = b64encode(sha256(script.encode("utf-8")).digest()).decode("ascii")

    assert script
    assert f"script-src 'sha256-{digest}'" in _CONTENT_POLICY
    assert f'data-max-save-bytes="{SHARED_REVIEW_EDIT_LIMIT_BYTES}"' in shared
    assert "<script>" not in render_report(document)
