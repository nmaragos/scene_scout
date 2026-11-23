from dataclasses import replace
from types import SimpleNamespace

import pytest

from scenescout import signoff
from scenescout.diff import Change
from scenescout.review_document import ReviewDocument, ReviewFinding, ReviewThumbnails


@pytest.mark.parametrize(("save_result", "still_dirty", "may_leave"), [
    (False, True, False),
    (True, True, False),
    (True, False, True),
])
def test_save_before_leaving_keeps_browser_review_until_the_caller_leaves(
    monkeypatch, save_result, still_dirty, may_leave,
):
    shared = object()
    review = SimpleNamespace(
        window=SimpleNamespace(comparison_available=True),
        dirty=True,
        _shared=shared,
        _shared_revision=7,
        _sync_shared_review=lambda: None,
        stop_sharing=lambda: pytest.fail("Sharing stopped before the caller left"),
    )

    def save():
        assert review._shared is shared
        review.dirty = still_dirty
        return save_result

    review.save = save
    monkeypatch.setattr(signoff.QMessageBox, "question", lambda *_: signoff.QMessageBox.Save)

    assert signoff.Signoff.confirm_discard(review) is may_leave
    assert review._shared is shared
    assert review._shared_revision == 7


@pytest.mark.parametrize("superseded", [False, True])
def test_capture_merges_thumbnail_that_finishes_during_evidence_capture(monkeypatch, superseded):
    change = Change("Modified", "/Item", "attributes.size", "1", "2")
    finding = ReviewFinding(change, notes="Draft note")
    draft = ReviewDocument("a.usda", "b.usda", (finding,))
    thumbnail = ReviewThumbnails("stage A thumbnail", "stage B thumbnail")
    window = SimpleNamespace(comparison_available=True, _diff_generation=1)
    review = SimpleNamespace(stages={"A": object(), "B": object()}, annotations={})

    def no_op(*_):
        pass

    signal = SimpleNamespace(connect=lambda _: None)
    progress = SimpleNamespace(
        canceled=signal, setWindowModality=no_op, setMinimumDuration=no_op, setAutoClose=no_op,
        setAutoReset=no_op, setValue=no_op, show=no_op, wasCanceled=lambda: False, close=no_op,
        deleteLater=no_op,
    )
    job = SimpleNamespace(
        progress=signal, finished=signal, requestInterruption=no_op, start=no_op, error="",
        captured=replace(draft, findings=(replace(finding, evidence="Captured evidence"),)),
        deleteLater=no_op,
    )

    def capture_events():
        review.annotations[change] = replace(finding, notes="Later note", thumbnails=thumbnail)
        if superseded:
            window._diff_generation += 1

    monkeypatch.setattr(signoff, "CaptureReview", lambda *_: job)
    monkeypatch.setattr(signoff, "QProgressDialog", lambda *_: progress)
    monkeypatch.setattr(signoff, "QEventLoop", lambda: SimpleNamespace(quit=no_op, exec=capture_events))
    workflow = SimpleNamespace(window=window, review=review, draft=lambda: draft)

    captured = signoff.Signoff.capture(workflow)

    if superseded:
        assert captured is None
    else:
        assert captured.findings == (replace(finding, evidence="Captured evidence", thumbnails=thumbnail),)
