#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/llm_common.sh"

if [[ $# -lt 2 ]]; then
    echo "Usage: $0 <meta_file> <provider>" >&2
    exit 1
fi

meta_file="$1"
provider="$2"

company=$(job_assets_read_meta "$meta_file" company)
role=$(job_assets_read_meta "$meta_file" role)
out_dir=$(job_assets_read_meta "$meta_file" out_dir)
content_dir=$(job_assets_read_meta "$meta_file" content_dir)
if [[ -z "$content_dir" ]]; then
    content_dir="$out_dir"
fi
research_cache="${PROJECT_ROOT}/output/${company}/research_cache.json"
role_research_cache="${PROJECT_ROOT}/${content_dir}/role_research_cache.json"
jd_parsed="${PROJECT_ROOT}/${content_dir}/jd_parsed.json"
cache_ttl_days="${JOB_ASSETS_RESEARCH_CACHE_TTL_DAYS:-30}"
research_lock_dir="${research_cache}.lock"
research_log_file="${content_dir}/llm_research_raw.txt"
draft_log_file="${content_dir}/llm_drafting_raw.txt"
prompt_file=""
owns_research_lock=false

cleanup() {
    rm -f "$prompt_file"
    if $owns_research_lock && [[ -d "$research_lock_dir" ]]; then
        rmdir "$research_lock_dir" 2>/dev/null || true
    fi
}

trap cleanup EXIT

echo "[$(date '+%H:%M:%S')] START  ${company}/${role} (${provider})"

research_wait_seconds="${JOB_ASSETS_RESEARCH_LOCK_WAIT_SECONDS:-900}"
research_deadline=$(( $(date +%s) + research_wait_seconds ))
poll_interval=2
poll_max="${JOB_ASSETS_RESEARCH_POLL_MAX_SECONDS:-30}"

while true; do
    if job_assets_cache_is_fresh "$research_cache" "$cache_ttl_days"; then
        echo "[$(date '+%H:%M:%S')] CACHE  ${company}/${role} (fresh research cache)"
        break
    fi

    if mkdir "$research_lock_dir" 2>/dev/null; then
        owns_research_lock=true
        echo "[$(date '+%H:%M:%S')] RESEARCH ${company}/${role} (claiming company research lock)"
        prompt_file="$(mktemp)"
        job_assets_write_company_research_prompt "$prompt_file" "$company" "$role" "$out_dir" "$content_dir"
        if ! job_assets_run_prompt_with_fallback "$provider" "$prompt_file" research "$research_log_file" "$research_cache"; then
            echo "[$(date '+%H:%M:%S')] FAIL   ${company}/${role} (${provider}, research error)"
            exit 1
        fi
        rm -f "$prompt_file"
        prompt_file=""
        owns_research_lock=false
        rmdir "$research_lock_dir" 2>/dev/null || true
        break
    fi

    if (( $(date +%s) >= research_deadline )); then
        echo "[$(date '+%H:%M:%S')] FAIL   ${company}/${role} (${provider}, timed out waiting for company research lock)"
        exit 1
    fi

    echo "[$(date '+%H:%M:%S')] WAIT   ${company}/${role} (waiting ${poll_interval}s for fresh company research cache)"
    sleep "$poll_interval"
    poll_interval=$(( poll_interval * 2 > poll_max ? poll_max : poll_interval * 2 ))
done

if [[ ! -f "$research_cache" ]]; then
    echo "[$(date '+%H:%M:%S')] FAIL   ${company}/${role} (${provider}, missing research cache)"
    exit 1
fi

# Check role-specific cache
if ! job_assets_role_cache_is_fresh "$role_research_cache" "$jd_parsed" "$cache_ttl_days"; then
    echo "[$(date '+%H:%M:%S')] ROLE_RESEARCH ${company}/${role} (role-specific research needed)"
    prompt_file="$(mktemp)"
    jd_hash="$(job_assets_file_sha256 "$jd_parsed")"
    job_assets_write_role_research_prompt "$prompt_file" "$company" "$role" "$out_dir" "$content_dir" "$jd_hash"
    role_research_log="${content_dir}/llm_role_research_raw.txt"
    if ! job_assets_run_prompt_with_fallback "$provider" "$prompt_file" research "$role_research_log" "$role_research_cache"; then
        echo "[$(date '+%H:%M:%S')] FAIL   ${company}/${role} (${provider}, role research error)"
        exit 1
    fi
    rm -f "$prompt_file"
    prompt_file=""
else
    echo "[$(date '+%H:%M:%S')] CACHE  ${company}/${role} (fresh role research cache)"
fi

prompt_file="$(mktemp)"
job_assets_write_drafting_prompt "$prompt_file" "$company" "$role" "$out_dir" "$content_dir"

if job_assets_run_prompt_with_fallback "$provider" "$prompt_file" draft "$draft_log_file" "${PROJECT_ROOT}/${content_dir}/resume_content.json" "${PROJECT_ROOT}/${content_dir}/cover_letter_text.txt"; then
    if [[ -f "${PROJECT_ROOT}/${content_dir}/resume_content.json" && -f "${PROJECT_ROOT}/${content_dir}/cover_letter_text.txt" ]]; then
        echo "[$(date '+%H:%M:%S')] DONE   ${company}/${role} (${provider}) OK"
        exit 0
    fi

    echo "[$(date '+%H:%M:%S')] FAIL   ${company}/${role} (${provider}, missing output files)"
    exit 1
fi

echo "[$(date '+%H:%M:%S')] FAIL   ${company}/${role} (${provider} drafting error)"
exit 1
