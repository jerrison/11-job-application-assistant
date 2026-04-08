import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from rendered_state_audit import (
    DeterministicFieldExpectation,
    DeterministicFieldObservation,
    audit_rendered_option_field,
    audit_rendered_option_fields,
)


def test_audit_rendered_option_field_requires_exact_match():
    result = audit_rendered_option_field(
        DeterministicFieldExpectation(
            field_key="work_auth",
            label="Are you legally authorized to work in the US?",
            selected_labels=frozenset({"Yes"}),
            exact_count=1,
        ),
        DeterministicFieldObservation(
            field_key="work_auth",
            label="Are you legally authorized to work in the US?",
            selected_labels=frozenset({"No"}),
            screenshot_path="output/acme/pm/submit/greenhouse_autofill_review.png",
        ),
    )
    assert result.ok is False
    assert result.expected_labels == ["Yes"]
    assert result.observed_labels == ["No"]


def test_audit_rendered_option_field_rejects_extra_selection():
    result = audit_rendered_option_field(
        DeterministicFieldExpectation(
            field_key="countries",
            label="Country selection",
            selected_labels=frozenset({"United States", "Canada"}),
            exact_count=2,
        ),
        DeterministicFieldObservation(
            field_key="countries",
            label="Country selection",
            selected_labels=frozenset({"United States", "Canada", "Mexico"}),
            screenshot_path="output/acme/pm/submit/review.png",
        ),
    )
    assert result.ok is False
    assert "extra selections" in result.reason


def test_audit_rendered_option_field_normalizes_common_equivalents():
    result = audit_rendered_option_field(
        DeterministicFieldExpectation(
            field_key="country",
            label="Country",
            selected_labels=frozenset({"US"}),
            exact_count=1,
        ),
        DeterministicFieldObservation(
            field_key="country",
            label="Country",
            selected_labels=frozenset({"United States"}),
            screenshot_path="output/acme/pm/submit/review.png",
        ),
    )
    assert result.ok is True


def test_audit_rendered_option_field_normalizes_boolean_yes_no_equivalents():
    result = audit_rendered_option_field(
        DeterministicFieldExpectation(
            field_key="over_18",
            label="Are you at least 18 years of age?",
            selected_labels=frozenset({"true"}),
            exact_count=1,
        ),
        DeterministicFieldObservation(
            field_key="over_18",
            label="Are you at least 18 years of age?",
            selected_labels=frozenset({"Yes"}),
            screenshot_path="output/acme/pm/submit/review.png",
        ),
    )
    assert result.ok is True


def test_audit_rendered_option_field_enforces_exact_cardinality():
    result = audit_rendered_option_field(
        DeterministicFieldExpectation(
            field_key="interests",
            label="Areas of interest",
            selected_labels=frozenset({"Platform", "AI", "Growth"}),
            exact_count=3,
        ),
        DeterministicFieldObservation(
            field_key="interests",
            label="Areas of interest",
            selected_labels=frozenset({"Platform", "AI"}),
            screenshot_path="output/acme/pm/submit/review.png",
        ),
    )
    assert result.ok is False
    assert "expected 3 selections" in result.reason


def test_audit_rendered_option_fields_keeps_screenshot_when_expected_field_is_missing():
    result = audit_rendered_option_fields(
        [
            DeterministicFieldExpectation(
                field_key="work_auth",
                label="Work authorization",
                selected_labels=frozenset({"Yes"}),
                exact_count=1,
            )
        ],
        [
            DeterministicFieldObservation(
                field_key="different_field",
                label="Some other field",
                selected_labels=frozenset({"Value"}),
                screenshot_path="output/acme/pm/submit/review.png",
            )
        ],
    )

    assert result.ok is False
    assert result.screenshot_path == "output/acme/pm/submit/review.png"
