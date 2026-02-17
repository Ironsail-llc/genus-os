#!/usr/bin/env bash
#
# run_tests.sh — Unified test runner for all Robothor modules
#
# Usage:
#   ./run_tests.sh                    Run fast tests across all modules
#   ./run_tests.sh --all              Run everything including slow/llm
#   ./run_tests.sh --slow             Include slow tests
#   ./run_tests.sh --llm              Include LLM tests
#   ./run_tests.sh --layer bridge     Run only bridge tests
#   ./run_tests.sh --layer memory     Run only memory system tests
#   ./run_tests.sh --layer crm        Run only CRM bash tests
#   ./run_tests.sh -v                 Verbose output

set -uo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"

# Defaults
RUN_ALL=false
INCLUDE_SLOW=false
INCLUDE_LLM=false
LAYER=""
VERBOSE=""
EXTRA_ARGS=()

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --all)
            RUN_ALL=true
            shift
            ;;
        --slow)
            INCLUDE_SLOW=true
            shift
            ;;
        --llm)
            INCLUDE_LLM=true
            shift
            ;;
        --layer)
            LAYER="$2"
            shift 2
            ;;
        -v|--verbose)
            VERBOSE="-v"
            shift
            ;;
        --help|-h)
            head -12 "$0" | grep '^#' | sed 's/^# \?//'
            exit 0
            ;;
        *)
            EXTRA_ARGS+=("$1")
            shift
            ;;
    esac
done

# Build marker expression
MARK_EXPR=""
if [ "$RUN_ALL" = true ]; then
    MARK_EXPR=""
elif [ "$INCLUDE_SLOW" = false ] && [ "$INCLUDE_LLM" = false ]; then
    MARK_EXPR="not slow and not llm and not e2e"
elif [ "$INCLUDE_SLOW" = true ] && [ "$INCLUDE_LLM" = false ]; then
    MARK_EXPR="not llm"
elif [ "$INCLUDE_SLOW" = false ] && [ "$INCLUDE_LLM" = true ]; then
    MARK_EXPR="not slow and not e2e"
fi

# Counters
TOTAL_PASS=0
TOTAL_FAIL=0
TOTAL_SKIP=0
MODULES_RUN=0
MODULES_FAILED=0

run_module() {
    local name="$1"
    local cmd="$2"

    echo ""
    echo "--- $name ---"
    echo "  Command: $cmd"

    eval "$cmd"
    local rc=$?

    MODULES_RUN=$((MODULES_RUN + 1))
    if [ $rc -ne 0 ] && [ $rc -ne 5 ]; then
        MODULES_FAILED=$((MODULES_FAILED + 1))
        echo "  FAILED (exit $rc)"
    elif [ $rc -eq 5 ]; then
        echo "  No tests matched filters"
    else
        echo "  PASSED"
    fi

    return $rc
}

echo ""
echo "======================================================="
echo "  Robothor — Unified Test Runner"
echo "======================================================="
if [ -n "$LAYER" ]; then
    echo "  Layer: $LAYER"
fi
if [ -n "$MARK_EXPR" ]; then
    echo "  Filter: $MARK_EXPR"
fi

BRIDGE_VENV="$ROOT/crm/bridge/venv/bin"
MEMORY_DIR="$ROOT/brain/memory_system"
ANY_FAILED=false

# ─── Module 1: Bridge tests (pytest via bridge venv) ───────────────────

if [ -z "$LAYER" ] || [ "$LAYER" = "bridge" ]; then
    BRIDGE_CMD="$BRIDGE_VENV/pytest $ROOT/crm/bridge/tests/ --tb=short -q"
    if [ -n "$MARK_EXPR" ]; then
        BRIDGE_CMD="$BRIDGE_CMD -m \"$MARK_EXPR\""
    fi
    if [ -n "$VERBOSE" ]; then
        BRIDGE_CMD="$BRIDGE_CMD $VERBOSE"
    fi
    run_module "Bridge Service Tests" "$BRIDGE_CMD" || ANY_FAILED=true
fi

# ─── Module 2: CRM integration tests (pytest via bridge venv) ─────────

if [ -z "$LAYER" ] || [ "$LAYER" = "crm" ]; then
    if [ -d "$ROOT/crm/tests" ] && ls "$ROOT/crm/tests"/test_*.py >/dev/null 2>&1; then
        CRM_CMD="$BRIDGE_VENV/pytest $ROOT/crm/tests/ --tb=short -q"
        if [ -n "$MARK_EXPR" ]; then
            CRM_CMD="$CRM_CMD -m \"$MARK_EXPR\""
        fi
        if [ -n "$VERBOSE" ]; then
            CRM_CMD="$CRM_CMD $VERBOSE"
        fi
        run_module "CRM Integration Tests" "$CRM_CMD" || ANY_FAILED=true
    fi
fi

# ─── Module 3: Memory system tests (own venv, own runner) ─────────────

if [ -z "$LAYER" ] || [ "$LAYER" = "memory" ]; then
    if [ -f "$MEMORY_DIR/run_tests.sh" ]; then
        MEM_CMD="bash $MEMORY_DIR/run_tests.sh"
        if [ "$RUN_ALL" = true ]; then
            MEM_CMD="$MEM_CMD --all"
        fi
        if [ "$INCLUDE_SLOW" = true ]; then
            MEM_CMD="$MEM_CMD --slow"
        fi
        if [ "$INCLUDE_LLM" = true ]; then
            MEM_CMD="$MEM_CMD --llm"
        fi
        if [ -n "$VERBOSE" ]; then
            MEM_CMD="$MEM_CMD -v"
        fi
        run_module "Memory System Tests" "$MEM_CMD" || ANY_FAILED=true
    fi
fi

# ─── Summary ──────────────────────────────────────────────────────────

echo ""
echo "======================================================="
echo "  Modules run: $MODULES_RUN"
if [ "$ANY_FAILED" = true ]; then
    echo "  Modules failed: $MODULES_FAILED"
    echo "  RESULT: FAILED"
    echo "======================================================="
    exit 1
else
    echo "  RESULT: ALL PASSED"
    echo "======================================================="
    exit 0
fi
