#!/usr/bin/env bash

JOB_ASSET_PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

job_assets_load_local_env() {
    local exports
    exports="$(python3 "$JOB_ASSET_PROJECT_ROOT/scripts/project_env.py" --shell)"
    if [[ -n "$exports" ]]; then
        eval "$exports"
    fi
}

job_assets_load_local_env

job_assets_load_provider_defaults() {
    local provider="$1"
    local exports
    exports="$(python3 "$JOB_ASSET_PROJECT_ROOT/scripts/llm_provider.py" --shell "$provider")"
    if [[ -n "$exports" ]]; then
        eval "$exports"
    fi
}

job_assets_default_provider() {
    printf '%s\n' "${ASSET_LLM_PROVIDER:-openai}"
}

job_assets_resolve_provider_chain() {
    python3 "$JOB_ASSET_PROJECT_ROOT/scripts/llm_provider.py" --automation-chain
}

job_assets_display_provider() {
    local provider="$1"
    if [[ "$provider" == "chain" ]]; then
        printf '%s\n' "chain($(job_assets_resolve_provider_chain))"
    else
        printf '%s\n' "$provider"
    fi
}

job_assets_default_max_parallel() {
    python3 - <<'PY'
import os

for key in ("JOB_ASSETS_MAX_PARALLEL", "MAX_PARALLEL"):
    raw = (os.environ.get(key) or "").strip()
    if not raw:
        continue
    try:
        value = max(int(raw), 1)
    except ValueError:
        continue
    print(value)
    raise SystemExit(0)

cpu_count = os.cpu_count() or 4
print(max(4, min(cpu_count * 2, 16)))
PY
}

job_assets_require_provider() {
    local provider="$1"

    case "$provider" in
        claude|codex|gemini|openai) ;;
        chain)
            # Chain mode — individual providers validated at runtime
            return 0
            ;;
        *)
            echo "ERROR: Unsupported provider '${provider}'. Use 'gemini', 'claude', 'codex', or 'openai'." >&2
            return 1
            ;;
    esac

    # openai uses a Python subprocess shim (openai_provider.py), not a binary
    [[ "$provider" = "openai" ]] && return 0

    if ! command -v "$provider" >/dev/null 2>&1; then
        echo "ERROR: '${provider}' is not installed or not on PATH." >&2
        return 1
    fi
}

job_assets_read_meta() {
    python3 - "$1" "$2" <<'PY'
import json
import sys

with open(sys.argv[1]) as fh:
    data = json.load(fh)

print(data.get(sys.argv[2], ""))
PY
}

job_assets_file_sha256() {
    local path="$1"
    if [[ ! -f "$path" ]]; then
        return 0
    fi
    shasum -a 256 "$path" | awk '{print $1}'
}

job_assets_file_changed() {
    local path="$1"
    local previous_sha="$2"
    local current_sha

    current_sha="$(job_assets_file_sha256 "$path")"
    [[ -n "$current_sha" && "$current_sha" != "$previous_sha" ]]
}

job_assets_chain_log_file() {
    local log_file="$1"
    local chain_index="$2"
    if [[ "$chain_index" -eq 0 ]]; then
        printf '%s\n' "$log_file"
        return
    fi
    case "$log_file" in
        *_raw.txt) printf '%s\n' "${log_file%_raw.txt}_fallback_${chain_index}_raw.txt" ;;
        *.txt) printf '%s\n' "${log_file%.txt}_fallback_${chain_index}.txt" ;;
        *) printf '%s\n' "${log_file}_fallback_${chain_index}" ;;
    esac
}

job_assets_log_contains_provider_capacity_error() {
    local log_file="$1"
    [[ -n "$log_file" && -f "$log_file" ]] || return 1
    python3 - "$log_file" <<'PY'
from pathlib import Path
import sys

text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="ignore").casefold()
patterns = (
    "you're out of extra usage",
    "you’re out of extra usage",
    "you've hit your usage limit",
    "you’ve hit your usage limit",
    "exhausted your capacity on this model",
    "terminalquotaerror",
    "purchase more credits",
    "quota will reset after",
)

raise SystemExit(0 if any(pattern in text for pattern in patterns) else 1)
PY
}

job_assets_cache_is_fresh() {
    local path="$1"
    local max_age_days="${2:-${JOB_ASSETS_RESEARCH_CACHE_TTL_DAYS:-30}}"
    python3 - "$path" "$max_age_days" <<'PY'
from __future__ import annotations

import os
import sys
import time

path = sys.argv[1]
max_age_days = int(sys.argv[2])

if not os.path.exists(path):
    raise SystemExit(1)

age_seconds = time.time() - os.path.getmtime(path)
raise SystemExit(0 if age_seconds <= max_age_days * 86400 else 1)
PY
}

job_assets_role_cache_is_fresh() {
    local role_cache_path="$1"
    local jd_parsed_path="$2"
    local max_age_days="${3:-${JOB_ASSETS_RESEARCH_CACHE_TTL_DAYS:-30}}"
    python3 - "$role_cache_path" "$jd_parsed_path" "$max_age_days" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import sys
import time

role_cache_path = sys.argv[1]
jd_parsed_path = sys.argv[2]
max_age_days = int(sys.argv[3])

if not os.path.exists(role_cache_path):
    raise SystemExit(1)

if not os.path.exists(jd_parsed_path):
    raise SystemExit(1)

age_seconds = time.time() - os.path.getmtime(role_cache_path)
if age_seconds > max_age_days * 86400:
    raise SystemExit(1)

with open(jd_parsed_path, "rb") as f:
    current_hash = hashlib.sha256(f.read()).hexdigest()

with open(role_cache_path, encoding="utf-8") as f:
    stored_hash = json.load(f).get("jd_hash", "")

raise SystemExit(0 if current_hash == stored_hash else 1)
PY
}

job_assets_write_company_research_prompt() {
    local prompt_file="$1"
    local company="$2"
    local role="$3"
    local out_dir="$4"
    local content_dir="${5:-$4}"

    cat > "$prompt_file" <<PROMPT
You are a Job Application Agent.
Treat AGENTS.md as the navigation map, then read docs/resume-generation.md, docs/cover-letter-generation.md, docs/shared-inputs.md, and agent_preferences.md as needed.

The deterministic pipeline has already run for ${company}/${role}. These files exist:
- ${content_dir}/jd_parsed.json (parsed JD)
- ${content_dir}/ranked_bullets.json (bullets ranked by relevance)
- ${content_dir}/resume_content_draft.json (pre-selected bullets, summary is null)

Your task:

1. READ ${content_dir}/jd_parsed.json to understand the role.
2. READ master_resume.md, work_stories.md, and candidate_context.md for source material that helps you evaluate fit and company context.
3. Do the deep company + role research described in docs/resume-generation.md (Phase 1d) and docs/cover-letter-generation.md (CL Phase 1).
   - Focus on company context, product, business model, strategic direction, role fit, and the most relevant public signals for this posting.
   - Use the CLI's built-in web research tools for company research; do not require an interactive session.
4. SAVE the company-level research to output/${company}/research_cache.json as structured JSON.
   Include: company, researched_at, mission, vision, culture, leadership, product, growth, recent_news, tech_stack.
   Do NOT include role_context — that is handled separately.

IMPORTANT:
- Follow the repo-wide rules from AGENTS.md and the deeper docs it points to (meaning preservation, ownership verbs, no fabrication).
- Do NOT tailor the resume or write the cover letter in this step.
- The output must be reusable by a later drafting pass without re-running web research.
- Do NOT run job-assets, apply.sh, scripts/run_pipeline.py, scripts/job_assets_pipeline.py, or any other repo automation entrypoint from inside this task. Read the listed files and write the requested content files directly.
- Use candidate_context.md only as supplemental background for motivations, tone, and narrative color.
- Do NOT include protected characteristics, sexual content or preferences, home address, or other sensitive private details from candidate_context.md unless the user explicitly asks and the detail is materially relevant.
- Do NOT run build scripts - just produce output/${company}/research_cache.json.
- Do NOT show the research in chat - just confirm the file is saved.
PROMPT
}

job_assets_write_role_research_prompt() {
    local prompt_file="$1"
    local company="$2"
    local role="$3"
    local out_dir="$4"
    local content_dir="${5:-$4}"
    local jd_hash="$6"

    cat > "$prompt_file" <<PROMPT
You are a Job Application Agent.
Treat AGENTS.md as the navigation map, then read docs/resume-generation.md, docs/cover-letter-generation.md, docs/shared-inputs.md, and agent_preferences.md as needed.

The company research for ${company} is already saved. Now do role-specific research.

These files exist:
- ${content_dir}/jd_parsed.json (parsed JD)
- output/${company}/research_cache.json (company-level research — read this for context)

Your task:

1. READ ${content_dir}/jd_parsed.json to understand the specific role.
2. READ output/${company}/research_cache.json for company context.
3. Do role-specific research: how this role fits the company strategy, team structure, what skills matter most for this position, competitive landscape.
4. SAVE the role research to ${content_dir}/role_research_cache.json as JSON with these fields:
   - role_context: detailed role-specific research
   - researched_at: current ISO 8601 timestamp
   - jd_hash: "${jd_hash}"

IMPORTANT:
- Do NOT re-research company-level information (mission, culture, leadership). That is already in research_cache.json.
- Do NOT tailor the resume or write the cover letter in this step.
- Do NOT run job-assets, apply.sh, scripts/run_pipeline.py, scripts/job_assets_pipeline.py, or any other repo automation entrypoint.
- Do NOT show the research in chat — just confirm the file is saved.
PROMPT
}

job_assets_write_drafting_prompt() {
    local prompt_file="$1"
    local company="$2"
    local role="$3"
    local out_dir="$4"
    local content_dir="${5:-$4}"

    cat > "$prompt_file" <<PROMPT
You are a Job Application Agent.
Treat AGENTS.md as the navigation map, then read docs/resume-generation.md, docs/cover-letter-generation.md, docs/shared-inputs.md, and agent_preferences.md as needed.

The deterministic pipeline has already run for ${company}/${role}. These files exist:
- ${content_dir}/jd_parsed.json (parsed JD)
- ${content_dir}/ranked_bullets.json (bullets ranked by relevance)
- ${content_dir}/resume_content_draft.json (pre-selected bullets, summary is null)
- output/${company}/research_cache.json (company-level research)
- ${content_dir}/role_research_cache.json (role-specific research — if this file exists, use it for role context; otherwise fall back to role_context in research_cache.json)

Your tasks - do ALL of them in order:

1. READ ${content_dir}/jd_parsed.json to understand the role.
2. READ ${content_dir}/resume_content_draft.json to see the pre-selected bullets.
3. READ output/${company}/research_cache.json for company research.
4. READ ${content_dir}/role_research_cache.json for role-specific research (if it exists; otherwise use role_context from research_cache.json).
5. Use the combined company + role research as input for both the resume and the cover letter.
6. READ master_resume.md, work_stories.md, candidate_context.md, and application_profile.md for source material and application defaults.
7. Do Phase 2: Tailor the resume content.
   - Review and override draft selections if needed.
   - Rewrite bullets to mirror JD language, lead with impact.
   - The "summary" field MUST be a non-null string (2-3 sentences). Write a sharp, high-signal summary that communicates the candidate's fit using JD keywords and saved research. Validation will FAIL if summary is null.
   - The tagline first segment MUST be the TARGET ROLE TITLE from the JD (e.g. "Senior Product Manager", "Staff PM", "Founding PM"), NOT "Principal Product Manager". Adjust the second segment to match the domain. Keep the final credential segment as "Wharton MBA + Penn M.S. Computer Science".
   - Keep at least 6 bullets for Moody's, at least 5 bullets for Kyte, at least 3 bullets for T-Mobile, at least 1 bullet for Lyft, and at least 1 bullet for Allstate.
   - When the page budget is tight, prefer one concise accomplishment sentence per bullet for older roles instead of multi-clause bullets.
   - Prefer a compact 2-sentence summary over a longer 3-sentence summary when required bullets already make the resume dense.
   - Treat page_break_before as a last-step layout decision after bullets and summary are final. Prefer the latest feasible break so page 1 is not sparse, but do not compromise the right job-relevant bullets to do that.
   - Save to ${content_dir}/resume_content.json
8. Do CL Phases 2-4: Write the cover letter.
   - Design narrative, write 4-5 paragraphs (300-450 words), self-review.
   - Ground the letter in the saved research instead of re-running company research from scratch.
   - When writing the cover letter body text, avoid the Unicode em dash character when possible. Prefer commas, periods, parentheses, or hyphens instead. Keep em dashes only in direct quotes or fixed text that must stay verbatim.
   - Save to ${content_dir}/cover_letter_text.txt

IMPORTANT:
- Follow the repo-wide rules from AGENTS.md and the deeper docs it points to (meaning preservation, ownership verbs, no fabrication).
- Moody's must retain at least 6 bullets, Kyte must retain at least 5 bullets, T-Mobile must retain at least 3 bullets, Lyft must retain at least 1 bullet, and Allstate must retain at least 1 bullet.
- The resume must fit exactly 2 pages when built. Choose content volume accordingly.
- Focus on content quality first. The pipeline will rebalance page_break_before after the final resume content is saved.
- Use output/${company}/research_cache.json as the research source for this step. Do not re-run company research unless that file is unreadable or missing.
- Do NOT run job-assets, apply.sh, scripts/run_pipeline.py, scripts/job_assets_pipeline.py, or any other repo automation entrypoint from inside this task. Read the listed files and write the requested content files directly.
- Use candidate_context.md only as supplemental background for motivations, tone, and narrative color.
- Do NOT include protected characteristics, sexual content or preferences, home address, or other sensitive private details from candidate_context.md unless the user explicitly asks and the detail is materially relevant.
- When the role is explicitly LATAM, Latin America, or Spanish-market focused, and candidate_context.md supports a Panama/Panamanian background, that regional context is materially relevant and may be mentioned briefly.
- Use application_profile.md for factual application-form defaults such as work authorization, sponsorship, pronouns, and voluntary self-ID answers.
- Do NOT surface protected characteristics from application_profile.md in resumes or cover letters unless the user explicitly asks or the output field specifically requires them.
- Do NOT run build scripts - just produce the two content files.
- Do NOT show the full resume or cover letter in chat - just confirm the files are saved.
PROMPT
}

job_assets_write_fix_prompt() {
    local prompt_file="$1"
    local content_dir="$2"
    local validation_output="$3"

    cat > "$prompt_file" <<PROMPT
The resume at ${content_dir}/resume_content.json failed validation:

${validation_output}

Fix the resume content to pass validation:
- If too many pages: shorten bullets or cut low-value bullets. Only remove summary as an absolute last resort.
- If too few pages: add bullets from master_resume.md first; only expand the summary if needed, and keep it as tight as possible without dropping important signal.
- The summary field MUST be a non-null string (2-3 sentences) — validation will fail if null.
- Keep the summary to exactly 2 short sentences while fixing an overlong resume.
- The tagline first segment MUST match the target role title from the JD, not "Principal Product Manager".
- Do not reduce Moody's below 6 bullets, Kyte below 5 bullets, T-Mobile below 3 bullets, Lyft below 1 bullet, or Allstate below 1 bullet.
- Rewrite older-role bullets down to one concise accomplishment sentence before you remove any required bullet.
- Cut secondary clauses, implementation detail, and extra qualifiers before you cut impact, metrics, or required bullets.

Do not spend effort micro-optimizing page_break_before. The pipeline recomputes the page break after the content is saved.

Read ${content_dir}/resume_content.json, fix it, and save the corrected version to the same path.
Do NOT change the cover letter. Do NOT run build scripts or recurse into job-assets, apply.sh, scripts/run_pipeline.py, or scripts/job_assets_pipeline.py.
PROMPT
}

job_assets_run_prompt() {
    local provider="$1"
    local prompt_file="$2"
    local mode="${3:-content}"
    local log_file="${4:-}"
    local timeout_override="${5:-}"

    job_assets_require_provider "$provider" || return 1
    job_assets_load_provider_defaults "$provider"

    local provider_timeout_seconds="${JOB_ASSETS_PROVIDER_TIMEOUT_SECONDS:-0}"
    local asset_timeout_seconds="${JOB_ASSETS_ASSET_PROVIDER_TIMEOUT_SECONDS:-$provider_timeout_seconds}"
    local selected_timeout_seconds="$provider_timeout_seconds"
    if [[ "$mode" = "content" || "$mode" = "research" || "$mode" = "draft" || "$mode" = "fix" ]]; then
        selected_timeout_seconds="$asset_timeout_seconds"
    fi
    if [[ -n "$timeout_override" && "$timeout_override" =~ ^[0-9]+$ ]]; then
        selected_timeout_seconds="$timeout_override"
    fi
    local cmd_file
    cmd_file="$(mktemp)"
    if ! python3 "$JOB_ASSET_PROJECT_ROOT/scripts/llm_provider.py" \
        "$provider" \
        --command \
        --prompt-file "$prompt_file" \
        --project-root "$JOB_ASSET_PROJECT_ROOT" \
        --mode "$mode" >"$cmd_file"; then
        rm -f "$cmd_file"
        return 1
    fi

    local -a cmd=()
    while IFS= read -r -d '' part; do
        cmd+=("$part")
    done <"$cmd_file"
    rm -f "$cmd_file"

    if [[ ${#cmd[@]} -eq 0 ]]; then
        echo "ERROR: Failed to build ${provider} command for mode '${mode}'." >&2
        return 1
    fi

    local -a runner_cmd=(
        python3
        "$JOB_ASSET_PROJECT_ROOT/scripts/run_command_with_timeout.py"
        --cwd
        "$JOB_ASSET_PROJECT_ROOT"
        --timeout-seconds
        "$selected_timeout_seconds"
    )
    if [[ -n "$log_file" ]]; then
        runner_cmd+=(--log-file "$log_file")
    fi
    runner_cmd+=(-- "${cmd[@]}")
    env JOB_ASSETS_FORBID_RECURSIVE_ENTRYPOINTS=1 "${runner_cmd[@]}"
}

job_assets_run_prompt_with_fallback() {
    local provider="$1"
    local prompt_file="$2"
    local mode="$3"
    local log_file="$4"
    shift 4
    local changed_paths=("$@")

    # Single-provider mode: provider is explicit (not "chain")
    if [[ "$provider" != "chain" ]]; then
        _job_assets_run_single_provider_with_legacy_fallback \
            "$provider" "$prompt_file" "$mode" "$log_file" "${changed_paths[@]}"
        return $?
    fi

    # Chain mode: iterate through the resolved automation provider chain
    local chain_str
    chain_str="$(job_assets_resolve_provider_chain)"
    IFS=',' read -ra chain <<< "$chain_str"

    local -a pre_shas=()
    local path
    for path in "${changed_paths[@]}"; do
        pre_shas+=("$(job_assets_file_sha256 "$path")")
    done

    local chain_index=0
    local last_status=1
    for current_provider in "${chain[@]}"; do
        current_provider="$(echo "$current_provider" | tr -d '[:space:]')"
        if [[ -z "$current_provider" ]]; then
            continue
        fi
        if [[ "$current_provider" != "openai" ]] && ! command -v "$current_provider" >/dev/null 2>&1; then
            echo "WARNING: Provider '${current_provider}' is not installed; skipping." >&2
            chain_index=$((chain_index + 1))
            continue
        fi

        local current_log_file
        current_log_file="$(job_assets_chain_log_file "$log_file" "$chain_index")"

        local provider_timeout="${JOB_ASSETS_PROVIDER_TIMEOUT_SECONDS:-600}"
        echo "INFO: Trying provider '${current_provider}' (chain position ${chain_index})." >&2

        last_status=0
        job_assets_run_prompt "$current_provider" "$prompt_file" "$mode" "$current_log_file" "$provider_timeout" || last_status=$?

        if job_assets_log_contains_provider_capacity_error "$current_log_file"; then
            echo "WARNING: ${current_provider} ${mode} generation hit a provider capacity limit; trying next provider." >&2
            last_status=1
        fi

        if [[ $last_status -eq 0 ]]; then
            return 0
        fi

        # Check if the provider wrote output files despite non-zero exit
        local wrote_files=false
        local index
        for index in "${!changed_paths[@]}"; do
            if job_assets_file_changed "${changed_paths[$index]}" "${pre_shas[$index]}"; then
                wrote_files=true
                break
            fi
        done
        if $wrote_files; then
            return "$last_status"
        fi

        echo "WARNING: ${current_provider} ${mode} generation failed before writing output files." >&2
        chain_index=$((chain_index + 1))
    done

    return "$last_status"
}

_job_assets_run_single_provider_with_legacy_fallback() {
    local provider="$1"
    local prompt_file="$2"
    local mode="$3"
    local log_file="$4"
    shift 4
    local changed_paths=("$@")
    local -a pre_shas=()
    local path

    job_assets_load_provider_defaults "$provider"

    for path in "${changed_paths[@]}"; do
        pre_shas+=("$(job_assets_file_sha256 "$path")")
    done

    local primary_status=0
    local primary_capacity_error=false
    local primary_asset_timeout="${JOB_ASSETS_CLAUDE_PRIMARY_ASSET_TIMEOUT_SECONDS:-}"
    if [[ ( "$mode" == "content" || "$mode" == "research" || "$mode" == "draft" || "$mode" == "fix" ) && "$primary_asset_timeout" =~ ^[0-9]+$ && "$primary_asset_timeout" -gt 0 ]]; then
        job_assets_run_prompt "$provider" "$prompt_file" "$mode" "$log_file" "$primary_asset_timeout" || primary_status=$?
    else
        job_assets_run_prompt "$provider" "$prompt_file" "$mode" "$log_file" || primary_status=$?
    fi
    if job_assets_log_contains_provider_capacity_error "$log_file"; then
        primary_capacity_error=true
        echo "WARNING: ${provider} ${mode} generation hit a provider capacity limit." >&2
        primary_status=1
    fi
    if [[ $primary_status -eq 0 ]]; then
        return 0
    fi

    local wrote_files=false
    local index
    for index in "${!changed_paths[@]}"; do
        if job_assets_file_changed "${changed_paths[$index]}" "${pre_shas[$index]}"; then
            wrote_files=true
            break
        fi
    done
    if $wrote_files; then
        return "$primary_status"
    fi

    local fallback_provider=""
    if [[ "$mode" == "content" || "$mode" == "research" || "$mode" == "draft" || "$mode" == "fix" ]]; then
        fallback_provider="${JOB_ASSETS_CLAUDE_ASSET_FALLBACK_PROVIDER:-}"
        case "$fallback_provider" in
            ""|0|false|False|FALSE|none|None|NONE)
                fallback_provider=""
                ;;
        esac
    fi

    if [[ -z "$fallback_provider" || "$fallback_provider" == "$provider" ]]; then
        return "$primary_status"
    fi
    if [[ "$fallback_provider" != "openai" ]] && ! command -v "$fallback_provider" >/dev/null 2>&1; then
        echo "WARNING: ${provider} ${mode} generation failed and fallback provider '${fallback_provider}' is not available." >&2
        return "$primary_status"
    fi

    local fallback_log_file
    fallback_log_file="$(job_assets_chain_log_file "$log_file" 1)"
    if $primary_capacity_error; then
        echo "WARNING: ${provider} ${mode} generation hit a provider capacity limit before writing output files; retrying with ${fallback_provider}." >&2
    else
        echo "WARNING: ${provider} ${mode} generation failed before writing output files; retrying with ${fallback_provider}." >&2
    fi
    local fallback_status=0
    job_assets_run_prompt "$fallback_provider" "$prompt_file" "$mode" "$fallback_log_file" || fallback_status=$?
    if job_assets_log_contains_provider_capacity_error "$fallback_log_file"; then
        echo "WARNING: ${fallback_provider} ${mode} generation hit a provider capacity limit." >&2
        fallback_status=1
    fi
    return "$fallback_status"
}

job_assets_finalize_resume_content() {
    local resume_json="$1"
    uv run scripts/enforce_resume_policy.py "$resume_json"
    uv run scripts/optimize_page_break.py "$resume_json"
}
