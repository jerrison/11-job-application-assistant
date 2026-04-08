# Answer Verification Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a first-pass draft answer verifier that persists verification proof, blocks objectively unsupported generated answers, and surfaces verifier status in draft artifacts.

**Architecture:** Keep the first slice narrow. Add a root-sidecar state file plus an active-submit verification artifact, then integrate a local rule-based verifier into `generate_application_answers()` using existing blockers and `pending_user_input` flows. Surface the verifier result in `draft_summary.md` so draft review shows answer-proof alongside answer-refresh proof.

**Tech Stack:** Python, JSON sidecar artifacts, existing unittest/pytest suite

---

## File Map

| File | Changes |
|------|---------|
| `scripts/answer_verification_state.py` | New durable state helper modeled after `answer_refresh_state.py` |
| `scripts/answer_verifier.py` | New local verifier: lane classification, structured verification artifact, blocker synthesis |
| `scripts/application_submit_common.py` | Run verifier after generated answers are finalized and before returning them |
| `scripts/draft_manager.py` | Add `Answer Verification` section to `draft_summary.md` |
| `scripts/output_layout.py` | Add verification artifact names to shared output helpers |
| `docs/output-structure.md` | Document new proof artifacts |
| `tests/test_answer_verification_state.py` | New tests for state lifecycle |
| `tests/test_answer_verifier.py` | New tests for lane classification and blocker behavior |
| `tests/test_submit_application.py` | Integration tests for verifier execution in generated-answer flow |
| `tests/test_draft_manager.py` | Draft summary rendering tests for verifier proof |

---

### Task 1: Add Durable Answer Verification State

**Files:**
- Create: `scripts/answer_verification_state.py`
- Modify: `scripts/output_layout.py`
- Create: `tests/test_answer_verification_state.py`

- [ ] **Step 1: Write failing state tests**

```python
def test_missing_state_defaults_to_unknown(self):
    verify = load_module("answer_verification_state", "scripts/answer_verification_state.py")
    with tempfile.TemporaryDirectory() as tmpdir:
        state = verify.load_answer_verification_state(Path(tmpdir))
    self.assertEqual(state["status"], verify.STATUS_UNKNOWN)

def test_finalize_verified_persists_metadata(self):
    verify = load_module("answer_verification_state", "scripts/answer_verification_state.py")
    with tempfile.TemporaryDirectory() as tmpdir:
        out_dir = Path(tmpdir)
        pending = verify.mark_answer_verification_pending(out_dir)
        state = verify.finalize_answer_verification(
            out_dir,
            request_id=pending["request_id"],
            status=verify.STATUS_VERIFIED,
            verifier_provider="local_rule_based",
            verified_answer_count=2,
            blocked_answer_count=0,
            proof_submit_dir="submit",
        )
    self.assertEqual(state["status"], verify.STATUS_VERIFIED)
    self.assertEqual(state["verifier_provider"], "local_rule_based")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_answer_verification_state.py -v`
Expected: FAIL because `scripts/answer_verification_state.py` does not exist yet.

- [ ] **Step 3: Write minimal state helper**

```python
STATUS_UNKNOWN = "unknown"
STATUS_PENDING = "pending"
STATUS_VERIFIED = "verified"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_BLOCKED = "blocked"
STATUS_FAILED = "failed"

def mark_answer_verification_pending(out_dir: str | Path) -> dict:
    ...

def finalize_answer_verification(...):
    ...
```

- [ ] **Step 4: Add shared artifact names to `scripts/output_layout.py`**

```python
ANSWER_VERIFICATION_JSON = "answer_verification.json"
ANSWER_VERIFICATION_RAW = "answer_verification_raw.txt"
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_answer_verification_state.py -v`
Expected: PASS

---

### Task 2: Add Rule-Based Answer Verifier And Integrate It

**Files:**
- Create: `scripts/answer_verifier.py`
- Modify: `scripts/application_submit_common.py`
- Create: `tests/test_answer_verifier.py`
- Modify: `tests/test_submit_application.py`

- [ ] **Step 1: Write failing verifier tests**

```python
def test_user_required_question_becomes_blocker():
    verifier = load_module("answer_verifier", "scripts/answer_verifier.py")
    profile = common.parse_application_profile(PROFILE_TEXT)
    spec = {
        "field_name": "question_market_context",
        "label": "Describe the carrier market context you would evaluate here.",
        "required": True,
        "type": "textarea",
    }
    result = verifier.verify_generated_answers(
        out_dir=Path(tmpdir),
        meta={"board": "ashby"},
        question_specs=[spec],
        answers={"question_market_context": "I would review regulatory constraints."},
        application_profile=profile,
        deterministic_field_names=set(),
    )
    assert result["status"] == "blocked"
    assert result["questions"][0]["verdict"] == "blocked_requires_user_input"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_answer_verifier.py::AnswerVerifierTests::test_user_required_question_becomes_blocker -v`
Expected: FAIL because the verifier module does not exist yet.

- [ ] **Step 3: Implement minimal verifier**

```python
def classify_verification_lane(...):
    if question_requires_pending_user_input(question_text, application_profile):
        return "user_required"
    if field_name in deterministic_field_names:
        return "deterministic_rendered_only"
    return "reference_verified_generated_text"

def verify_generated_answers(...):
    # Persist pending state
    # Build per-question results
    # Convert user-required questions into blocker steps
    # Write submit/answer_verification.json
    # Finalize root state as verified / blocked / not_applicable
```

- [ ] **Step 4: Integrate verifier into `generate_application_answers()`**

```python
verification = verify_generated_answers(
    out_dir=out_dir,
    meta=meta,
    question_specs=question_specs,
    answers=answers,
    application_profile=application_profile,
    deterministic_field_names=set(linked_resource_deterministic) | set(classified_text_answers),
)
if verification["blockers"]:
    raise GeneratedAnswerBlockersError(verification["blockers"], valid_answers=answers)
```

- [ ] **Step 5: Add integration tests**

```python
def test_generate_application_answers_blocks_user_required_prompt(self):
    ...
    with self.assertRaises(common.GeneratedAnswerBlockersError) as excinfo:
        common.generate_application_answers(...)
    self.assertIn("market context", str(excinfo.exception).lower())
```

- [ ] **Step 6: Run focused tests**

Run: `uv run python -m pytest tests/test_answer_verification_state.py tests/test_answer_verifier.py tests/test_submit_application.py -k "verification or blocker" -v`
Expected: PASS

---

### Task 3: Surface Verifier Proof In Draft Summary

**Files:**
- Modify: `scripts/draft_manager.py`
- Modify: `tests/test_draft_manager.py`
- Modify: `docs/output-structure.md`

- [ ] **Step 1: Write failing draft-summary test**

```python
def test_generate_draft_summary_includes_answer_verification_blockers(self):
    from draft_manager import generate_draft_summary
    ...
    (out_dir / "answer_verification_status.json").write_text(...)
    (submit_dir / "answer_verification.json").write_text(...)
    generate_draft_summary(out_dir, submit_dir, meta)
    md = (out_dir / "draft_summary.md").read_text()
    self.assertIn("## Answer Verification", md)
    self.assertIn("blocked_requires_user_input", md)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_draft_manager.py::DraftSummaryTests::test_generate_draft_summary_includes_answer_verification_blockers -v`
Expected: FAIL because the summary does not render verifier proof yet.

- [ ] **Step 3: Render verifier section**

```python
def _build_answer_verification_lines(out_dir: Path, submit_dir: Path) -> list[str]:
    state = load_answer_verification_state(out_dir)
    artifact = load_answer_verification_artifact(submit_dir)
    ...
```

- [ ] **Step 4: Update output docs**

```md
answer_verification_status.json            # Durable answer-verification proof state
submit/
  answer_verification.json                # Per-question verifier verdicts for current attempt
```

- [ ] **Step 5: Run focused tests**

Run: `uv run python -m pytest tests/test_draft_manager.py tests/test_answer_verification_state.py tests/test_answer_verifier.py -v`
Expected: PASS

---

### Task 4: Run Final Verification

**Files:**
- Verify only

- [ ] **Step 1: Run targeted test suite**

Run: `uv run python -m pytest tests/test_answer_verification_state.py tests/test_answer_verifier.py tests/test_submit_application.py tests/test_draft_manager.py -v`
Expected: PASS

- [ ] **Step 2: Run lint**

Run: `uv run ruff check scripts/answer_verification_state.py scripts/answer_verifier.py scripts/application_submit_common.py scripts/draft_manager.py tests/test_answer_verification_state.py tests/test_answer_verifier.py tests/test_submit_application.py tests/test_draft_manager.py`
Expected: PASS

- [ ] **Step 3: Run doc health**

Run: `uv run python scripts/check_agent_docs.py`
Expected: PASS
