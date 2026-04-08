#!/usr/bin/env bash
# Fully automated job application pipeline — single URL input.
#
# Usage:
#   ./apply.sh [--provider claude|codex] <url_or_file>
#   ./apply.sh [--provider claude|codex] <url_or_file> [company] [role-slug]
#
# Examples:
#   ./apply.sh --provider claude "https://samsara.com/careers/roles/7269221"
#   ./apply.sh --provider codex tmp/samsara_jd.md
#   ./apply.sh "https://example.com/jobs/123" acme senior-pm
#
# Company and role-slug are auto-detected from the parsed JD when omitted.
#
# Runs the full pipeline end-to-end with no manual steps:
#   1. Deterministic: parse JD, rank bullets, draft resume
#   2. LLM: research company, tailor resume, write cover letter
#   3. Build: generate .docx + .pdf, validate
#   4. If validation fails, LLM fixes and rebuilds (up to 2 retries)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
source "$SCRIPT_DIR/scripts/llm_common.sh"
ASSET_STATE_SCRIPT="$SCRIPT_DIR/scripts/asset_pipeline_state.py"
CANDIDATE_NAME="${JOB_ASSETS_CANDIDATE_NAME:-Candidate Name}"

case "${JOB_ASSETS_FORBID_RECURSIVE_ENTRYPOINTS:-}" in
    ""|0|false|False|FALSE) ;;
    *)
        echo "ERROR: apply.sh cannot be invoked from inside a non-interactive provider subtask. Write the requested output files directly instead of recursing into repo entrypoints." >&2
        exit 1
        ;;
esac

usage() {
    echo "Usage: $0 [--provider gemini|claude|codex] [--skip-sync] <url_or_file> [company] [role-slug]"
    echo ""
    echo "  --provider   LLM CLI to use for asset generation (default: $(job_assets_default_provider))"
    echo "  --skip-sync  Skip syncing work_stories.md and candidate_context.md before the run"
    echo "  url_or_file  URL or file path to the job description"
    echo "  company      (optional) Company slug — auto-detected if omitted"
    echo "  role-slug    (optional) Role slug — auto-detected if omitted"
}

# ─── Args ─────────────────────────────────────────────────────────────────────

PROVIDER="$(job_assets_default_provider)"
SKIP_SYNC=false
META_PATH_FILE=""
POSITIONAL=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --provider)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --provider requires a value." >&2
                exit 1
            fi
            PROVIDER="${2:-}"
            shift 2
            ;;
        --provider=*)
            PROVIDER="${1#*=}"
            shift
            ;;
        --skip-sync)
            SKIP_SYNC=true
            shift
            ;;
        --meta-path-file)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --meta-path-file requires a value." >&2
                exit 1
            fi
            META_PATH_FILE="${2:-}"
            shift 2
            ;;
        --meta-path-file=*)
            META_PATH_FILE="${1#*=}"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            while [[ $# -gt 0 ]]; do
                POSITIONAL+=("$1")
                shift
            done
            ;;
        -*)
            echo "ERROR: Unknown option '$1'" >&2
            usage >&2
            exit 1
            ;;
        *)
            POSITIONAL+=("$1")
            shift
            ;;
    esac
done

set -- "${POSITIONAL[@]}"

if [[ $# -lt 1 || $# -gt 3 ]]; then
    usage >&2
    exit 1
fi

job_assets_require_provider "$PROVIDER"

JD_SOURCE="$1"
COMPANY_ARG="${2:-}"
ROLE_ARG="${3:-}"
MAX_RETRIES=2
TMP_META_PATH_FILE=""

if [[ -n "$META_PATH_FILE" ]]; then
    META_CAPTURE_FILE="$META_PATH_FILE"
else
    TMP_META_PATH_FILE="$(mktemp)"
    META_CAPTURE_FILE="$TMP_META_PATH_FILE"
fi

trap 'rm -f "$TMP_META_PATH_FILE"' EXIT

# Build the optional flags for run_pipeline.py
PIPELINE_FLAGS=()
if [[ -n "$COMPANY_ARG" ]]; then
    PIPELINE_FLAGS+=(-c "$COMPANY_ARG")
fi
if [[ -n "$ROLE_ARG" ]]; then
    PIPELINE_FLAGS+=(-r "$ROLE_ARG")
fi
if $SKIP_SYNC; then
    PIPELINE_FLAGS+=(--skip-sync)
fi
PIPELINE_FLAGS+=(--meta-path-file "$META_CAPTURE_FILE")

echo "============================================================"
echo "  AUTOMATED JOB APPLICATION"
echo "  Provider: ${PROVIDER}"
echo "  JD:       ${JD_SOURCE}"
if [[ -n "$COMPANY_ARG" ]]; then
    echo "  Company:  ${COMPANY_ARG} (manual)"
else
    echo "  Company:  (auto-detect)"
fi
if [[ -n "$ROLE_ARG" ]]; then
    echo "  Role:     ${ROLE_ARG} (manual)"
else
    echo "  Role:     (auto-detect)"
fi
echo "============================================================"

# ─── Progress reporting ──────────────────────────────────────────────────────
# Writes a progress file the TUI can read for real-time status updates.
_PROGRESS_FILE=""
_progress() {
    local step="$1" pct="$2" detail="${3:-}"
    echo "── ${detail:-$step} ──"
    if [[ -n "$_PROGRESS_FILE" ]]; then
        printf '{"step":"%s","pct":%d,"detail":"%s","ts":"%s"}\n' \
            "$step" "$pct" "$detail" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$_PROGRESS_FILE"
    fi
}

# ─── Step 1: Deterministic pipeline ───────────────────────────────────────────

echo ""
_progress "deterministic" 5 "Step 1: Parsing JD and ranking bullets"
uv run scripts/run_pipeline.py "${JD_SOURCE}" ${PIPELINE_FLAGS[@]+"${PIPELINE_FLAGS[@]}"}

# ─── Read auto-detected metadata ─────────────────────────────────────────────
# run_pipeline.py writes .pipeline_meta.json with the resolved company/role and
# reports the exact path through --meta-path-file.
META_FILE=""
if [[ -f "$META_CAPTURE_FILE" ]]; then
    META_FILE="$(<"$META_CAPTURE_FILE")"
fi

if [[ -z "$META_FILE" || ! -f "$META_FILE" ]]; then
    echo "ERROR: Could not resolve .pipeline_meta.json — pipeline may have failed."
    exit 1
fi

# Extract values from metadata
COMPANY=$(job_assets_read_meta "$META_FILE" company)
COMPANY_PROPER=$(job_assets_read_meta "$META_FILE" company_proper)
ROLE=$(job_assets_read_meta "$META_FILE" role)
OUT_DIR=$(job_assets_read_meta "$META_FILE" out_dir)
CONTENT_DIR=$(job_assets_read_meta "$META_FILE" content_dir)
DOCUMENTS_DIR=$(job_assets_read_meta "$META_FILE" documents_dir)
if [[ -z "$CONTENT_DIR" ]]; then
    CONTENT_DIR="$OUT_DIR"
fi
if [[ -z "$DOCUMENTS_DIR" ]]; then
    DOCUMENTS_DIR="$OUT_DIR"
fi

_PROGRESS_FILE="${OUT_DIR}/.progress.json"

echo ""
echo "── Resolved: ${COMPANY}/${ROLE} (${COMPANY_PROPER}) ──"
echo "── Output:   ${OUT_DIR}/ ──"
echo "── Content:  ${CONTENT_DIR}/ ──"
echo "── Docs:     ${DOCUMENTS_DIR}/ ──"
_progress "resolved" 15 "Step 1 done: ${COMPANY_PROPER} / ${ROLE}"

# ─── Step 2: LLM — company research, then drafting ───────────────────────────

echo ""
if python3 "$ASSET_STATE_SCRIPT" can-reuse-content "$OUT_DIR" >/dev/null 2>&1; then
    _progress "reuse_content" 60 "Step 2: Reusing existing content"
    echo "Existing content matches current deterministic draft; skipping LLM regeneration."
else
    _progress "llm_start" 20 "Step 2: LLM content generation"
    python3 "$ASSET_STATE_SCRIPT" stash-generated-content "$OUT_DIR" >/dev/null
    RESEARCH_CACHE="${JOB_ASSET_PROJECT_ROOT}/output/${COMPANY}/research_cache.json"
    ROLE_RESEARCH_CACHE="${CONTENT_DIR}/role_research_cache.json"
    JD_PARSED="${CONTENT_DIR}/jd_parsed.json"
    CACHE_TTL_DAYS="${JOB_ASSETS_RESEARCH_CACHE_TTL_DAYS:-30}"
    RESEARCH_LOG_FILE="${CONTENT_DIR}/llm_research_raw.txt"
    DRAFT_LOG_FILE="${CONTENT_DIR}/llm_drafting_raw.txt"
    PRE_RESUME_SHA="$(job_assets_file_sha256 "${CONTENT_DIR}/resume_content.json")"
    PRE_CL_SHA="$(job_assets_file_sha256 "${CONTENT_DIR}/cover_letter_text.txt")"
    if job_assets_cache_is_fresh "$RESEARCH_CACHE" "$CACHE_TTL_DAYS"; then
        _progress "reuse_research" 30 "Step 2a: Reusing company research cache"
    else
        _progress "company_research" 25 "Step 2a: Researching company"
        PROMPT_FILE="$(mktemp)"
        PRE_RESEARCH_SHA="$(job_assets_file_sha256 "$RESEARCH_CACHE")"
        job_assets_write_company_research_prompt "$PROMPT_FILE" "$COMPANY" "$ROLE" "$OUT_DIR" "$CONTENT_DIR"
        if ! job_assets_run_prompt_with_fallback \
            "$PROVIDER" \
            "$PROMPT_FILE" \
            research \
            "$RESEARCH_LOG_FILE" \
            "$RESEARCH_CACHE"; then
            if ! job_assets_file_changed "$RESEARCH_CACHE" "$PRE_RESEARCH_SHA"; then
                rm -f "$PROMPT_FILE"
                echo "ERROR: LLM company research failed. See ${RESEARCH_LOG_FILE}" >&2
                exit 1
            fi
            echo "WARNING: LLM company research exited non-zero after writing updated research cache; continuing."
        fi
        rm -f "$PROMPT_FILE"

        if [[ ! -f "$RESEARCH_CACHE" ]]; then
            echo "ERROR: LLM did not produce ${RESEARCH_CACHE}"
            exit 1
        fi
    fi

    if ! job_assets_role_cache_is_fresh "$ROLE_RESEARCH_CACHE" "$JD_PARSED" "$CACHE_TTL_DAYS"; then
        _progress "role_research" 35 "Step 2a: Researching role"
        PROMPT_FILE="$(mktemp)"
        JD_HASH="$(job_assets_file_sha256 "$JD_PARSED")"
        job_assets_write_role_research_prompt "$PROMPT_FILE" "$COMPANY" "$ROLE" "$OUT_DIR" "$CONTENT_DIR" "$JD_HASH"
        ROLE_RESEARCH_LOG="${CONTENT_DIR}/llm_role_research_raw.txt"
        if ! job_assets_run_prompt_with_fallback \
            "$PROVIDER" \
            "$PROMPT_FILE" \
            research \
            "$ROLE_RESEARCH_LOG" \
            "$ROLE_RESEARCH_CACHE"; then
            rm -f "$PROMPT_FILE"
            echo "ERROR: LLM role-specific research failed. See ${ROLE_RESEARCH_LOG}" >&2
            exit 1
        fi
        rm -f "$PROMPT_FILE"
    else
        _progress "reuse_role_research" 35 "Step 2a: Reusing role research cache"
    fi

    _progress "llm_drafting" 45 "Step 2b: Generating resume and cover letter"
    PROMPT_FILE="$(mktemp)"
    job_assets_write_drafting_prompt "$PROMPT_FILE" "$COMPANY" "$ROLE" "$OUT_DIR" "$CONTENT_DIR"
    if ! job_assets_run_prompt_with_fallback \
        "$PROVIDER" \
        "$PROMPT_FILE" \
        draft \
        "$DRAFT_LOG_FILE" \
        "${CONTENT_DIR}/resume_content.json" \
        "${CONTENT_DIR}/cover_letter_text.txt"; then
        if ! job_assets_file_changed "${CONTENT_DIR}/resume_content.json" "$PRE_RESUME_SHA" || \
            ! job_assets_file_changed "${CONTENT_DIR}/cover_letter_text.txt" "$PRE_CL_SHA"; then
            rm -f "$PROMPT_FILE"
            echo "ERROR: LLM drafting failed. See ${DRAFT_LOG_FILE}" >&2
            exit 1
        fi
        echo "WARNING: LLM drafting exited non-zero after writing updated content files; continuing."
    fi
    rm -f "$PROMPT_FILE"

    # Verify LLM produced the files
    if [[ ! -f "${CONTENT_DIR}/resume_content.json" ]]; then
        echo "ERROR: LLM did not produce ${CONTENT_DIR}/resume_content.json"
        exit 1
    fi
    if [[ ! -f "${CONTENT_DIR}/cover_letter_text.txt" ]]; then
        echo "ERROR: LLM did not produce ${CONTENT_DIR}/cover_letter_text.txt"
        exit 1
    fi

    echo "── Finalizing page break after content selection ──"
    job_assets_finalize_resume_content "${CONTENT_DIR}/resume_content.json"
    python3 "$ASSET_STATE_SCRIPT" record-content "$OUT_DIR" >/dev/null

    _progress "llm_done" 65 "Step 2 done: Content generated"
fi

# ─── Step 3: Build + validate (with retry loop) ──────────────────────────────

echo ""
RESUME_PDF="${DOCUMENTS_DIR}/${CANDIDATE_NAME} Resume - ${COMPANY_PROPER}.pdf"
RESUME_DOCX="${DOCUMENTS_DIR}/${CANDIDATE_NAME} Resume - ${COMPANY_PROPER}.docx"
CL_DOCX="${DOCUMENTS_DIR}/${CANDIDATE_NAME} Cover Letter - ${COMPANY_PROPER}.docx"

if python3 "$ASSET_STATE_SCRIPT" can-reuse-build "$OUT_DIR" "$COMPANY_PROPER" >/dev/null 2>&1; then
    _progress "reuse_docs" 90 "Step 3: Reusing existing documents"
    echo "Existing documents match current content hashes; skipping rebuild."
    echo ""
    echo "── Validation PASSED ──"
else
    _progress "building" 70 "Step 3: Building resume and cover letter docs"

    attempt=0
    build_ok=false
    compact_fallback_used=false
    while true; do
        # Build resume
        uv run scripts/build_resume.py "${CONTENT_DIR}/resume_content.json" \
            -o "${RESUME_DOCX}"

        # Build cover letter
        uv run scripts/build_cover_letter.py "${CONTENT_DIR}/cover_letter_text.txt" \
            -o "${CL_DOCX}"

        # Validate
        if uv run scripts/validate_resume.py "${RESUME_PDF}" 2>&1; then
            _progress "validated" 95 "Step 3 done: Documents built and validated"
            build_ok=true
            python3 "$ASSET_STATE_SCRIPT" record-build "$OUT_DIR" "$COMPANY_PROPER" >/dev/null
            break
        fi

        VALIDATION_OUTPUT=$(uv run scripts/validate_resume.py "${RESUME_PDF}" 2>&1 || true)
        if [[ $attempt -ge $MAX_RETRIES ]]; then
            if ! $compact_fallback_used; then
                echo ""
                echo "── Validation still failing after ${MAX_RETRIES} LLM fixes — applying deterministic compact fallback ──"
                uv run python scripts/compact_resume_content.py "${CONTENT_DIR}/resume_content.json"
                job_assets_finalize_resume_content "${CONTENT_DIR}/resume_content.json"
                python3 "$ASSET_STATE_SCRIPT" record-content "$OUT_DIR" >/dev/null
                compact_fallback_used=true
                continue
            fi

            echo ""
            echo "ERROR: Validation failed after ${MAX_RETRIES} retries and deterministic compact fallback." >&2
            echo "$VALIDATION_OUTPUT" >&2
            exit 1
        fi

        attempt=$((attempt + 1))
        echo ""
        echo "── Validation FAILED — asking LLM to fix (attempt ${attempt}/${MAX_RETRIES}) ──"

        FIX_PROMPT_FILE="$(mktemp)"
        FIX_LOG_FILE="${CONTENT_DIR}/llm_fix_attempt_${attempt}_raw.txt"
        PRE_FIX_SHA="$(job_assets_file_sha256 "${CONTENT_DIR}/resume_content.json")"
        job_assets_write_fix_prompt "$FIX_PROMPT_FILE" "$CONTENT_DIR" "$VALIDATION_OUTPUT"
        if ! job_assets_run_prompt_with_fallback \
            "$PROVIDER" \
            "$FIX_PROMPT_FILE" \
            fix \
            "$FIX_LOG_FILE" \
            "${CONTENT_DIR}/resume_content.json"; then
            if ! job_assets_file_changed "${CONTENT_DIR}/resume_content.json" "$PRE_FIX_SHA"; then
                rm -f "$FIX_PROMPT_FILE"
                echo "ERROR: LLM resume-fix pass failed. See ${FIX_LOG_FILE}" >&2
                exit 1
            fi
            echo "WARNING: LLM resume-fix pass exited non-zero after updating resume content; continuing."
        fi
        rm -f "$FIX_PROMPT_FILE"
        job_assets_finalize_resume_content "${CONTENT_DIR}/resume_content.json"
        python3 "$ASSET_STATE_SCRIPT" record-content "$OUT_DIR" >/dev/null
    done
fi

if ! $build_ok; then
    echo "ERROR: Resume validation never succeeded." >&2
    exit 1
fi

# ─── Done ─────────────────────────────────────────────────────────────────────

echo ""
echo "============================================================"
echo "  COMPLETE"
echo "============================================================"
echo "  Provider:     ${PROVIDER}"
echo "  Resume:       ${RESUME_DOCX}"
echo "  Resume PDF:   ${RESUME_PDF}"
echo "  Cover Letter: ${CL_DOCX}"
echo "  Cover Letter: ${DOCUMENTS_DIR}/${CANDIDATE_NAME} Cover Letter - ${COMPANY_PROPER}.pdf"
echo ""
echo "  Content:      ${CONTENT_DIR}/resume_content.json"
echo "  CL Text:      ${CONTENT_DIR}/cover_letter_text.txt"
echo "============================================================"
