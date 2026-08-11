"""Opt-in validation against a corpus of real recordings.

The committed test suite is hermetic and synthetic. That proves the parser
agrees with our own encoder, which is necessary but not sufficient: every
interesting bug in FIT handling comes from what manufacturers actually write,
not from what the specification says they should.

So this module runs the parser over a directory of real files from many
devices, and **skips itself when that directory is absent** — CI stays
hermetic, and the corpus never enters the repository (it is third-party
licensed, and real GPS traces are personal data).

To populate it:

    make corpus

Every quirk this corpus revealed has been reproduced synthetically in
`TestDeviceAgnosticHeuristics`, so a regression is caught even without it.
What this adds is the guarantee that no *unhandled* exception escapes on
hardware we have never seen.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from garmin_mcp.domain.models import ParsedFit
from garmin_mcp.errors import FitParseError
from garmin_mcp.ingest.fit_parser import parse_fit

CORPUS_DIR = Path(__file__).parent.parent / "data" / "corpus"

# Files that are valid FIT but not activities (settings, workouts, weight
# scales, daily monitoring), plus one truncated before its first session.
# Rejecting these is correct behaviour, not a gap.
EXPECTED_REJECTIONS = frozenset(
    {
        "DeveloperData.fit",
        "MonitoringFile.fit",
        "Settings.fit",
        "Settings2.fit",
        "nametest.FIT",
        "WeightScaleMultiUser.fit",
        "WeightScaleSingleUser.fit",
        "WorkoutCustomTargetValues.fit",
        "WorkoutIndividualSteps.fit",
        "WorkoutRepeatGreaterThanStep.fit",
        "WorkoutRepeatSteps.fit",
        "nick.fit",  # truncated before any session message survived
    }
)


def _corpus_files() -> list[Path]:
    if not CORPUS_DIR.is_dir():
        return []
    return sorted(p for p in CORPUS_DIR.iterdir() if p.suffix.lower() == ".fit")


pytestmark = pytest.mark.skipif(
    not _corpus_files(),
    reason="no corpus in data/corpus — run `make corpus` to enable these tests",
)


@pytest.mark.parametrize("path", _corpus_files(), ids=lambda p: p.name)
def test_every_real_file_parses_or_is_cleanly_rejected(path: Path) -> None:
    """No file from real hardware may raise an unexpected exception.

    A FitParseError is an acceptable outcome — it is the parser saying "this is
    not an activity file" in a way callers can handle. Anything else is a bug.
    """
    try:
        parsed = parse_fit(path)
    except FitParseError:
        assert path.name in EXPECTED_REJECTIONS, (
            f"{path.name} is a real activity file but was rejected"
        )
        return

    assert path.name not in EXPECTED_REJECTIONS, (
        f"{path.name} was expected to be rejected but parsed"
    )
    _assert_internally_consistent(parsed)


def _assert_internally_consistent(parsed: ParsedFit) -> None:
    """Invariants that must hold for any file, from any manufacturer."""
    assert parsed.activities, "parsed successfully but produced no activity"

    ids = [a.activity_id for a in parsed.activities]
    assert len(set(ids)) == len(ids), "duplicate activity ids within one file"

    parent = parsed.parent
    for activity in parsed.activities:
        assert activity.start_time_utc.tzinfo is not None, "UTC start must be aware"
        assert activity.start_time_local.tzinfo is None, "local start must be naive"

        # A negative or absurd duration means a scaling bug.
        if activity.total_timer_time_s is not None:
            assert 0 <= activity.total_timer_time_s < 60 * 60 * 48

        # Heart rates outside this range indicate a byte-level misread.
        if activity.avg_heart_rate is not None:
            assert 20 <= activity.avg_heart_rate <= 260

        # Positions must already be degrees, never raw semicircles.
        if activity.start_lat is not None:
            assert -90.0 <= activity.start_lat <= 90.0
        if activity.start_lon is not None:
            assert -180.0 <= activity.start_lon <= 180.0

        # Samples belong to their session's window.
        for record in activity.records:
            assert record.ts >= activity.start_time_utc
            if record.lat is not None:
                assert -90.0 <= record.lat <= 90.0

        if parent is not None and activity is not parent:
            assert activity.parent_activity_id == parent.activity_id

    if parent is not None:
        # The aggregate must own no samples of its own, or totals double.
        assert not parent.records
        assert not parent.laps


def test_corpus_covers_several_manufacturers() -> None:
    """The corpus is only useful if it is actually diverse."""
    products = set()
    for path in _corpus_files():
        try:
            parsed = parse_fit(path)
        except FitParseError:
            continue
        for activity in parsed.activities:
            if activity.device_product:
                products.add(activity.device_product)

    assert len(products) >= 10, f"corpus looks thin: {sorted(products)}"


def test_multisport_files_produce_a_parent() -> None:
    """Known real triathlons must still split into parent plus legs."""
    known_multisport = {
        "activity-large-fenxi2-multisport.fit",
        "sample_mulitple_header.fit",
    }
    present = [p for p in _corpus_files() if p.name in known_multisport]
    if not present:
        pytest.skip("multisport samples not in corpus")

    for path in present:
        parsed = parse_fit(path)
        assert parsed.is_multisport, f"{path.name} lost its multisport structure"
        assert parsed.parent is not None
        legs = [a for a in parsed.activities if a.parent_activity_id is not None]
        assert len(legs) >= 3
