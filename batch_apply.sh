#!/usr/bin/env bash
# High-throughput batch processor: uses the same parallel worker pool as parallel_apply.sh.
#
# Usage:
#   ./batch_apply.sh [--provider claude|codex]
#   ./batch_apply.sh --dry-run [--provider claude|codex]
#   ./batch_apply.sh [--provider claude|codex] [--max-parallel N]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
source "$SCRIPT_DIR/scripts/llm_common.sh"

DRY_RUN=false
PROVIDER="$(job_assets_default_provider)"
MAX_PARALLEL="$(job_assets_default_max_parallel)"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=true
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
            echo "Usage: $0 [--dry-run] [--provider claude|codex] [--max-parallel N]"
            exit 0
            ;;
        *)
            echo "ERROR: Unknown option '$1'" >&2
            exit 1
            ;;
    esac
done

job_assets_require_provider "$PROVIDER"

cmd=(
    "bash"
    "$SCRIPT_DIR/parallel_apply.sh"
    "--provider"
    "$PROVIDER"
    "--max-parallel"
    "$MAX_PARALLEL"
)
if $DRY_RUN; then
    cmd+=("--dry-run")
fi

exec "${cmd[@]}"
