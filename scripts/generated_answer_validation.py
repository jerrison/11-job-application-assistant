from __future__ import annotations

import re

from application_models import ApplicationProfile
from text_normalization_helpers import normalize_text

_CONDITIONAL_FOLLOWUP_RE = re.compile(
    r"^if\s+(yes|no|you answered|other|so|applicable|selected|true)"
    r"|^if\s+\S+.*(?:required|needed|applicable|necessary).*(?:please|provide|confirm|specify)"
    r"|mentioned above|the above question|the previous question"
    r"|if\s+yes.*(?:provide|detail|explain|describe)"
    r"|if you answered.*(?:yes|other)"
    r"|please\s+(?:confirm|specify|explain|describe).*(?:above|mentioned)",
    re.I,
)
_RECENT_GRAD_GPA_FOLLOWUP_RE = re.compile(
    r"^if\s+you(?:'re| are)\s+less than\s+\d+\s+years?\s+out of school,\s*"
    r"(?:what\s+is|please\s+(?:provide|share|enter|list)|(?:provide|share|enter|list))\s+your\s+"
    r"(?:undergraduate\s+)?(?:gpa|grade point average)\b",
    re.I,
)
_NOT_APPLICABLE_OPTION_ALIASES = frozenset({"na", "n a", "not applicable"})


def _is_conditional_followup(spec: dict) -> bool:
    """Return True if the question looks like a conditional follow-up (e.g. 'If yes...')."""
    label = (spec.get("label") or "").strip()
    return bool(_CONDITIONAL_FOLLOWUP_RE.search(label) or _RECENT_GRAD_GPA_FOLLOWUP_RE.search(label))


def _shared_generated_answer_fallback(spec: dict, application_profile: ApplicationProfile | None) -> str | None:
    from application_submit_common import shared_text_answer_for_question

    question_text = "\n".join(
        part.strip()
        for part in (str(spec.get("label") or ""), str(spec.get("description") or ""))
        if part and str(part).strip()
    )
    return shared_text_answer_for_question(question_text, application_profile)


def _generated_answer_step_kind(spec: dict) -> str:
    field_type = str(spec.get("type") or "").strip()
    if field_type == "multi_value_multi_select":
        return "checkbox"
    if field_type == "multi_value_single_select":
        return "combobox"
    if field_type == "textarea":
        return "textarea"
    return "text"


def _textual_generated_answer_spec(spec: dict) -> bool:
    return _generated_answer_step_kind(spec) in {"text", "textarea"}


def _spec_supports_not_applicable_option(spec: dict) -> bool:
    if str(spec.get("type") or "").strip() != "multi_value_single_select":
        return False

    for option in spec.get("options") or spec.get("values") or []:
        if isinstance(option, dict):
            label = option.get("label") or option.get("value") or option.get("text") or ""
        else:
            label = option
        if normalize_text(str(label)) in _NOT_APPLICABLE_OPTION_ALIASES:
            return True
    return False


def _conditional_followup_default(spec: dict) -> str | None:
    if not _is_conditional_followup(spec):
        return None
    if _textual_generated_answer_spec(spec) or _spec_supports_not_applicable_option(spec):
        return "N/A"
    return None


def _generated_answer_blocker_step(spec: dict, *, raw_value: object = None, reason: str) -> dict:
    from autofill_common import GENERATED_ANSWER_BLOCKER_KIND, mark_step_as_draft_blocker

    step = {
        "field_name": str(spec.get("field_name") or "").strip(),
        "label": str(spec.get("label") or spec.get("field_name") or "").strip(),
        "kind": _generated_answer_step_kind(spec),
        "required": bool(spec.get("required")),
        "source": "generated_application_answer",
        "status": "planned",
        "reason": reason,
        "note": reason,
    }
    if isinstance(raw_value, list):
        joined = ", ".join(str(item).strip() for item in raw_value if str(item).strip())
        if joined:
            step["value"] = joined
    else:
        text = str(raw_value or "").strip()
        if text:
            step["value"] = text
    return mark_step_as_draft_blocker(step, blocker_kind=GENERATED_ANSWER_BLOCKER_KIND) or step


def validate_generated_answers_with_blockers(
    question_specs: list[dict],
    answers: dict,
    *,
    application_profile: ApplicationProfile | None = None,
) -> tuple[dict[str, object], list[dict]]:
    validated: dict[str, object] = {}
    blockers: list[dict] = []
    for spec in question_specs:
        field_name = spec["field_name"]
        required = bool(spec.get("required"))
        is_multi_select = spec.get("type") == "multi_value_multi_select"
        shared_fallback = _shared_generated_answer_fallback(spec, application_profile)
        conditional_default = _conditional_followup_default(spec)
        if field_name not in answers:
            if shared_fallback is not None:
                validated[field_name] = shared_fallback
                continue
            if conditional_default is not None:
                validated[field_name] = conditional_default
                continue
            if required:
                blockers.append(
                    _generated_answer_blocker_step(
                        spec,
                        reason=(
                            "The current run routed this required question through generated-answer handling, "
                            "but no answer was returned."
                        ),
                    )
                )
            continue
        value = answers[field_name]
        if value is None:
            if shared_fallback is not None:
                validated[field_name] = shared_fallback
                continue
            if conditional_default is not None:
                validated[field_name] = conditional_default
                continue
            if required:
                blockers.append(
                    _generated_answer_blocker_step(
                        spec,
                        raw_value=value,
                        reason=(
                            "The current run routed this required question through generated-answer handling, "
                            "but the answer came back empty."
                        ),
                    )
                )
            continue
        if is_multi_select:
            if isinstance(value, list):
                items = [str(v).strip() for v in value if str(v).strip()]
            elif isinstance(value, str):
                items = [v.strip() for v in value.split(",") if v.strip()]
            else:
                items = []
            if not items:
                if required:
                    blockers.append(
                        _generated_answer_blocker_step(
                            spec,
                            raw_value=value,
                            reason=(
                                "The current run routed this required multi-select question through generated-answer "
                                "handling, but no usable selections were returned."
                            ),
                        )
                    )
                continue
            validated[field_name] = items
            continue
        if not isinstance(value, str):
            if isinstance(value, bool):
                value = "Yes" if value else "No"
            else:
                value = str(value)
        normalized = value.strip()
        if not normalized:
            if shared_fallback is not None:
                validated[field_name] = shared_fallback
                continue
            if conditional_default is not None:
                validated[field_name] = conditional_default
                continue
            if required:
                blockers.append(
                    _generated_answer_blocker_step(
                        spec,
                        raw_value=value,
                        reason=(
                            "The current run routed this required question through generated-answer handling, "
                            "but the answer came back blank."
                        ),
                    )
                )
            continue
        if not required and _textual_generated_answer_spec(spec) and _is_conditional_followup(spec):
            continue
        validated[field_name] = normalized
    return validated, blockers


def validate_generated_answers(
    question_specs: list[dict],
    answers: dict,
    *,
    application_profile: ApplicationProfile | None = None,
) -> dict[str, object]:
    validated, blockers = validate_generated_answers_with_blockers(
        question_specs,
        answers,
        application_profile=application_profile,
    )
    if blockers:
        first_blocker = blockers[0]
        raise ValueError(
            f"Generated answer regression for {first_blocker.get('field_name')}: {first_blocker.get('reason')}"
        )
    return {str(key): value for key, value in validated.items()}
