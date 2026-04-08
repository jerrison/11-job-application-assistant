#!/usr/bin/env bash
# Parallel batch processor: runs company research and drafting concurrently, then builds sequentially.
#
# Usage:
#   ./parallel_apply.sh [--provider claude|codex]
#   ./parallel_apply.sh --dry-run [--provider claude|codex]
#   ./parallel_apply.sh --build-only [--provider claude|codex]
#
# Phase 1: Launch non-interactive company research and drafting in parallel
# Phase 2: Build .docx + .pdf sequentially (LibreOffice can only run one instance)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
source "$SCRIPT_DIR/scripts/llm_common.sh"

MAX_RETRIES=2
DRY_RUN=false
BUILD_ONLY=false
MAX_PARALLEL="$(job_assets_default_max_parallel)"
PROVIDER="$(job_assets_default_provider)"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --build-only)
            BUILD_ONLY=true
            shift
            ;;
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
        --max-parallel)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --max-parallel requires a value." >&2
                exit 1
            fi
            MAX_PARALLEL="${2:-}"
            shift 2
            ;;
        --max-parallel=*)
            MAX_PARALLEL="${1#*=}"
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [--dry-run] [--build-only] [--provider gemini|claude|codex] [--max-parallel N]"
            exit 0
            ;;
        *)
            echo "ERROR: Unknown option '$1'" >&2
            exit 1
            ;;
    esac
done

job_assets_require_provider "$PROVIDER"

# ─── Helpers ─────────────────────────────────────────────────────────────────

read_meta() {
    job_assets_read_meta "$1" "$2"
}

# ─── Discover jobs → writes meta file paths to a temp file ───────────────────

discover_jobs() {
    local mode="$1"  # "llm" or "build"
    local outfile="$2"
    > "$outfile"
    for meta_file in output/*/*/.pipeline_meta.json; do
        [ -f "$meta_file" ] || continue
        local out_dir
        out_dir=$(read_meta "$meta_file" out_dir)
        local content_dir
        content_dir=$(read_meta "$meta_file" content_dir)
        [ -n "$content_dir" ] || content_dir="$out_dir"
        [ -f "$content_dir/resume_content_draft.json" ] || continue

        if [ "$mode" = "llm" ]; then
            [ -f "$content_dir/resume_content.json" ] && [ -f "$content_dir/cover_letter_text.txt" ] && continue
        elif [ "$mode" = "build" ]; then
            [ -f "$content_dir/resume_content.json" ] || continue
            [ -f "$content_dir/cover_letter_text.txt" ] || continue
            local company_proper
            company_proper=$(read_meta "$meta_file" company_proper)
            local documents_dir
            documents_dir=$(read_meta "$meta_file" documents_dir)
            [ -n "$documents_dir" ] || documents_dir="$out_dir"
            [ -f "$documents_dir/Jerrison Li Resume - ${company_proper}.pdf" ] && continue
        fi
        echo "$meta_file" >> "$outfile"
    done
}

# ─── Main ────────────────────────────────────────────────────────────────────

TMP_LLM=$(mktemp)
TMP_BUILD=$(mktemp)
trap "rm -f '$TMP_LLM' '$TMP_BUILD'" EXIT

if ! $BUILD_ONLY; then
    discover_jobs "llm" "$TMP_LLM"
    LLM_COUNT=$(wc -l < "$TMP_LLM" | tr -d ' ')
    echo "Phase 1: ${LLM_COUNT} jobs need LLM asset generation with ${PROVIDER} (max ${MAX_PARALLEL} parallel)"

    if $DRY_RUN; then
        while IFS= read -r mf; do
            echo "  $(read_meta "$mf" company)/$(read_meta "$mf" role)"
        done < "$TMP_LLM"
    else
        if [ "$LLM_COUNT" -gt 0 ]; then
            echo "Launching parallel research/drafting calls at $(date '+%H:%M:%S')..."
            echo ""
            xargs -P "$MAX_PARALLEL" -I {} "$SCRIPT_DIR/scripts/llm_worker.sh" {} "$PROVIDER" < "$TMP_LLM"
            echo ""
            echo "Phase 1 complete at $(date '+%H:%M:%S')."
        fi
    fi
fi

# Re-discover after LLM phase (some may have completed)
discover_jobs "build" "$TMP_BUILD"
BUILD_COUNT=$(wc -l < "$TMP_BUILD" | tr -d ' ')
echo ""
echo "Phase 2: ${BUILD_COUNT} jobs need document building (sequential — LibreOffice constraint)"

if $DRY_RUN; then
    while IFS= read -r mf; do
        echo "  $(read_meta "$mf" company)/$(read_meta "$mf" role)"
    done < "$TMP_BUILD"
else
    BUILD_FAILED=0
    SUCCEEDED=0
    while IFS= read -r mf; do
        COMPANY=$(read_meta "$mf" company)
        COMPANY_PROPER=$(read_meta "$mf" company_proper)
        ROLE=$(read_meta "$mf" role)
        OUT_DIR=$(read_meta "$mf" out_dir)
        CONTENT_DIR=$(read_meta "$mf" content_dir)
        DOCUMENTS_DIR=$(read_meta "$mf" documents_dir)
        [ -n "$CONTENT_DIR" ] || CONTENT_DIR="$OUT_DIR"
        [ -n "$DOCUMENTS_DIR" ] || DOCUMENTS_DIR="$OUT_DIR"

        RESUME_DOCX="${DOCUMENTS_DIR}/Jerrison Li Resume - ${COMPANY_PROPER}.docx"
        RESUME_PDF="${DOCUMENTS_DIR}/Jerrison Li Resume - ${COMPANY_PROPER}.pdf"
        CL_DOCX="${DOCUMENTS_DIR}/Jerrison Li Cover Letter - ${COMPANY_PROPER}.docx"

        echo "[$(date '+%H:%M:%S')] BUILD  ${COMPANY}/${ROLE}"
        job_assets_finalize_resume_content "${CONTENT_DIR}/resume_content.json" 2>&1

        attempt=0
        build_ok=false
        while [ $attempt -le $MAX_RETRIES ]; do
            uv run scripts/build_resume.py "${CONTENT_DIR}/resume_content.json" -o "${RESUME_DOCX}" 2>&1
            uv run scripts/build_cover_letter.py "${CONTENT_DIR}/cover_letter_text.txt" -o "${CL_DOCX}" 2>&1

            if uv run scripts/validate_resume.py "${RESUME_PDF}" 2>&1; then
                echo "[$(date '+%H:%M:%S')] BUILT  ${COMPANY}/${ROLE} ✓"
                build_ok=true
                break
            fi

            attempt=$((attempt + 1))
            if [ $attempt -gt $MAX_RETRIES ]; then
                echo "[$(date '+%H:%M:%S')] BFAIL  ${COMPANY}/${ROLE} (validation failed after retries)"
                break
            fi

            echo "[$(date '+%H:%M:%S')] RETRY  ${COMPANY}/${ROLE} (attempt ${attempt}/${MAX_RETRIES})"
            VALIDATION_OUTPUT=$(uv run scripts/validate_resume.py "${RESUME_PDF}" 2>&1 || true)
            FIX_PROMPT_FILE="$(mktemp)"
            job_assets_write_fix_prompt "$FIX_PROMPT_FILE" "$CONTENT_DIR" "$VALIDATION_OUTPUT"
            job_assets_run_prompt_with_fallback "$PROVIDER" "$FIX_PROMPT_FILE" fix "${CONTENT_DIR}/llm_fix_attempt_${attempt}_raw.txt" "${CONTENT_DIR}/resume_content.json" 2>&1
            rm -f "$FIX_PROMPT_FILE"
            job_assets_finalize_resume_content "${CONTENT_DIR}/resume_content.json" 2>&1
        done

        if $build_ok; then
            SUCCEEDED=$((SUCCEEDED + 1))
        else
            BUILD_FAILED=$((BUILD_FAILED + 1))
        fi
    done < "$TMP_BUILD"

    echo ""
    echo "============================================================"
    echo "  BATCH COMPLETE"
    echo "  Provider:           ${PROVIDER}"
    echo "  Built successfully: ${SUCCEEDED}"
    echo "  Build failures:     ${BUILD_FAILED}"
    echo "  Check each role content directory for llm_*_raw.txt provider output"
    echo "============================================================"
fi
