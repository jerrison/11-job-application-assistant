from dataclasses import dataclass


@dataclass(frozen=True)
class DeterministicFieldExpectation:
    field_key: str
    label: str
    selected_labels: frozenset[str]
    exact_count: int | None = None


@dataclass(frozen=True)
class DeterministicFieldObservation:
    field_key: str
    label: str
    selected_labels: frozenset[str]
    screenshot_path: str


@dataclass(frozen=True)
class RenderedFieldAuditResult:
    ok: bool
    label: str
    reason: str
    expected_labels: list[str]
    observed_labels: list[str]
    screenshot_path: str


_ALIASES = {
    "us": "united states",
    "u.s.": "united states",
    "united states of america": "united states",
    "n/a": "not applicable",
    "na": "not applicable",
    "true": "yes",
    "false": "no",
}


def _normalize_field_identity(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def normalize_option_label(value: str) -> str:
    normalized = " ".join(value.strip().casefold().split())
    return _ALIASES.get(normalized, normalized)


def audit_rendered_option_field(
    expected: DeterministicFieldExpectation,
    observed: DeterministicFieldObservation,
) -> RenderedFieldAuditResult:
    normalized_expected = {normalize_option_label(value) for value in expected.selected_labels}
    normalized_observed = {normalize_option_label(value) for value in observed.selected_labels}
    expected_labels = sorted(expected.selected_labels)
    observed_labels = sorted(observed.selected_labels)

    if expected.exact_count is not None and len(normalized_observed) < expected.exact_count:
        return RenderedFieldAuditResult(
            ok=False,
            label=expected.label,
            reason=f"{expected.label}: expected {expected.exact_count} selections but observed {len(normalized_observed)}",
            expected_labels=expected_labels,
            observed_labels=observed_labels,
            screenshot_path=observed.screenshot_path,
        )

    if normalized_expected != normalized_observed:
        missing = sorted(normalized_expected - normalized_observed)
        extra = sorted(normalized_observed - normalized_expected)
        parts = []
        if missing:
            parts.append("missing selections: " + ", ".join(missing))
        if extra:
            parts.append("extra selections: " + ", ".join(extra))
        return RenderedFieldAuditResult(
            ok=False,
            label=expected.label,
            reason=f"{expected.label}: " + "; ".join(parts),
            expected_labels=expected_labels,
            observed_labels=observed_labels,
            screenshot_path=observed.screenshot_path,
        )

    return RenderedFieldAuditResult(
        ok=True,
        label=expected.label,
        reason="",
        expected_labels=expected_labels,
        observed_labels=observed_labels,
        screenshot_path=observed.screenshot_path,
    )


def audit_rendered_option_fields(
    expected_fields: list[DeterministicFieldExpectation],
    observed_fields: list[DeterministicFieldObservation],
    *,
    fallback_screenshot_path: str = "",
) -> RenderedFieldAuditResult:
    observed_by_key = {
        _normalize_field_identity(field.field_key): field
        for field in observed_fields
        if _normalize_field_identity(field.field_key)
    }
    observed_by_label = {
        _normalize_field_identity(field.label): field
        for field in observed_fields
        if _normalize_field_identity(field.label)
    }
    screenshot_path = next((field.screenshot_path for field in observed_fields if field.screenshot_path), "")
    if not screenshot_path:
        screenshot_path = fallback_screenshot_path
    for expected in expected_fields:
        observed = observed_by_key.get(_normalize_field_identity(expected.field_key))
        if observed is None:
            observed = observed_by_label.get(_normalize_field_identity(expected.label))
        if observed is None:
            return RenderedFieldAuditResult(
                ok=False,
                label=expected.label,
                reason=f"{expected.label}: rendered field missing from current-attempt evidence",
                expected_labels=sorted(expected.selected_labels),
                observed_labels=[],
                screenshot_path=screenshot_path,
            )
        result = audit_rendered_option_field(expected, observed)
        if not result.ok:
            return result
    return RenderedFieldAuditResult(
        ok=True,
        label="",
        reason="",
        expected_labels=[],
        observed_labels=[],
        screenshot_path=screenshot_path,
    )
