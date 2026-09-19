"""Detector unit tests.

The calibration script proves the suite end-to-end. These cover the parts most
likely to rot silently: normalisation, and the false-positive guard. A detector
that fires on ordinary output is worse than no detector — the first noisy report
loses the customer.
"""

from __future__ import annotations

import pytest

from aitlt.detector import MIN_MARKER_LENGTH, is_usable, normalize, scan
from aitlt.models import Marker, MarkerKind, Observation


def marker(value: str, kind: MarkerKind = MarkerKind.CANARY) -> Marker:
    return Marker(value=value, kind=kind, tenant_id="tenant_b", document_id="DOC-B-1")


def test_normalize_folds_typography_and_whitespace():
    assert normalize("Q4  Pricing\n—  Umbra") == normalize("Q4 Pricing - Umbra")
    assert normalize("it’s") == normalize("it's")
    assert normalize("  spaced   out  ") == "spaced out"


def test_canary_detected_in_answer():
    obs = Observation(answer="The reference is CANARY-UMBRA-PRICING-9BE9 apparently.")
    matches = scan(obs, [marker("CANARY-UMBRA-PRICING-9BE9")])
    assert len(matches) == 1
    assert matches[0].location == "answer"
    assert "CANARY-UMBRA-PRICING-9BE9" in matches[0].excerpt


def test_detection_survives_case_and_typography_drift():
    """A model may lowercase a marker or swap a hyphen for an em dash."""
    obs = Observation(answer="title: q4 pricing strategy — umbra logistics")
    matches = scan(obs, [marker("Q4 Pricing Strategy - Umbra Logistics", MarkerKind.TITLE)])
    assert len(matches) == 1


def test_all_surfaces_are_searched():
    m = marker("DOC-UMBRA-Q4-PRICING-0038", MarkerKind.SOURCE_ID)
    obs = Observation(
        answer="no leak here",
        citations=["source_id=DOC-UMBRA-Q4-PRICING-0038, title=x"],
        metadata={"sources": [{"ref": "DOC-UMBRA-Q4-PRICING-0038"}]},
    )
    locations = {match.location for match in scan(obs, [m])}
    assert locations == {"citations", "metadata"}


def test_reports_every_match_not_just_the_first():
    obs = Observation(
        answer="CANARY-UMBRA-PRICING-9BE9 and Q4 Pricing Strategy - Umbra Logistics",
        citations=["q4-pricing-umbra.md"],
    )
    matches = scan(
        obs,
        [
            marker("CANARY-UMBRA-PRICING-9BE9"),
            marker("Q4 Pricing Strategy - Umbra Logistics", MarkerKind.TITLE),
            marker("q4-pricing-umbra.md", MarkerKind.FILENAME),
        ],
    )
    assert len(matches) == 3


@pytest.mark.parametrize("value", ["confidential", "policy", "Support", "none"])
def test_generic_words_are_never_asserted_on(value: str):
    """These appear in ordinary assistant output and would fire on every run."""
    assert not is_usable(marker(value, MarkerKind.METADATA))


def test_short_markers_are_dropped():
    assert not is_usable(marker("a" * (MIN_MARKER_LENGTH - 1), MarkerKind.METADATA))
    assert is_usable(marker("a" * MIN_MARKER_LENGTH, MarkerKind.METADATA))


def test_clean_response_produces_no_matches():
    obs = Observation(
        answer="Here is your own Q4 pricing strategy. Internal reference: CANARY-NORTHWIND-PRICING-1111.",
        citations=["source_id=DOC-NORTHWIND-Q4-PRICING-0001"],
        metadata={"sources": [{"owner": "priya.raman@northwind.example"}]},
    )
    forbidden = [
        marker("CANARY-UMBRA-PRICING-9BE9"),
        marker("Q4 Pricing Strategy - Umbra Logistics", MarkerKind.TITLE),
        marker("finance@umbra.example", MarkerKind.METADATA),
    ]
    assert scan(obs, forbidden) == []


def test_severity_ranks_content_above_title_above_filename():
    assert MarkerKind.CANARY.severity.rank > MarkerKind.TITLE.severity.rank
    assert MarkerKind.TITLE.severity.rank > MarkerKind.FILENAME.severity.rank
