#!/usr/bin/env bash
#
# run_tests.sh — Run the Robothor Memory System test suite
#
# Usage:
#   ./run_tests.sh                    Run all unit + integration tests (skip slow/llm)
#   ./run_tests.sh --all              Run everything including slow LLM tests
#   ./run_tests.sh --layer unit       Run only fast unit tests (no DB, no LLM)
#   ./run_tests.sh --layer ingest     Run ingest service tests
#   ./run_tests.sh --layer intel      Run intelligence layer tests
#   ./run_tests.sh --layer serve      Run RAG serving tests
#   ./run_tests.sh --layer e2e        Run end-to-end pipeline tests
#   ./run_tests.sh --layer existing   Run existing phase tests
#   ./run_tests.sh --slow             Include slow tests (real LLM calls)
#   ./run_tests.sh --llm              Include tests requiring Ollama/Qwen
#   ./run_tests.sh -v                 Verbose output
#   ./run_tests.sh -k "pattern"       Run tests matching pattern
#
# Exit codes:
#   0 = all tests passed
#   1 = some tests failed
#   2 = no tests collected
#   5 = no tests matched filters

set -euo pipefail
cd "$(dirname "$0")"

# Activate venv
if [ -f venv/bin/activate ]; then
    source venv/bin/activate
else
    echo "ERROR: Virtual environment not found at venv/"
    echo "Create it: python3 -m venv venv && venv/bin/pip install -r requirements.txt"
    exit 1
fi

# Defaults
LAYER=""
INCLUDE_SLOW=false
INCLUDE_LLM=false
VERBOSE=""
EXTRA_ARGS=()
RUN_ALL=false

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --layer)
            LAYER="$2"
            shift 2
            ;;
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
        -v|--verbose)
            VERBOSE="-v"
            shift
            ;;
        -k)
            EXTRA_ARGS+=("-k" "$2")
            shift 2
            ;;
        -x|--exitfirst)
            EXTRA_ARGS+=("-x")
            shift
            ;;
        --help|-h)
            head -20 "$0" | grep '^#' | sed 's/^# \?//'
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
    MARK_EXPR=""  # No filtering
    INCLUDE_SLOW=true
    INCLUDE_LLM=true
elif [ "$INCLUDE_SLOW" = false ] && [ "$INCLUDE_LLM" = false ]; then
    MARK_EXPR="not slow and not llm and not e2e"
elif [ "$INCLUDE_SLOW" = true ] && [ "$INCLUDE_LLM" = false ]; then
    MARK_EXPR="not llm"
elif [ "$INCLUDE_SLOW" = false ] && [ "$INCLUDE_LLM" = true ]; then
    MARK_EXPR="not slow and not e2e"
fi

# Map layer to test file(s)
TEST_FILES=""
case "${LAYER}" in
    unit)
        # Only fast unit tests from all files (no DB, no LLM markers)
        MARK_EXPR="not slow and not llm and not integration and not e2e"
        ;;
    ingest)
        TEST_FILES="test_ingest_service.py"
        ;;
    intel|intelligence)
        TEST_FILES="test_intelligence.py"
        ;;
    serve|rag)
        TEST_FILES="test_rag_serve.py"
        ;;
    e2e|pipeline)
        TEST_FILES="test_e2e_pipeline.py"
        INCLUDE_SLOW=true
        INCLUDE_LLM=true
        MARK_EXPR=""
        ;;
    existing|phases)
        TEST_FILES="test_phase1_fact_extraction.py test_phase2_conflict_resolution.py test_phase3_mcp_server.py test_phase4_entity_graph.py test_phase5_ingestion.py test_phase6_lifecycle.py"
        ;;
    "")
        # Run all test files
        ;;
    *)
        echo "ERROR: Unknown layer '$LAYER'"
        echo "Valid layers: unit, ingest, intel, serve, e2e, existing"
        exit 1
        ;;
esac

# Build pytest command
CMD=(python -m pytest)

if [ -n "$VERBOSE" ]; then
    CMD+=("$VERBOSE")
fi

if [ -n "$MARK_EXPR" ]; then
    CMD+=(-m "$MARK_EXPR")
fi

if [ -n "$TEST_FILES" ]; then
    # shellcheck disable=SC2086
    CMD+=($TEST_FILES)
fi

CMD+=("${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}")

# Add common options
CMD+=(
    --tb=short
    --no-header
    -q
)

# Print what we're running
echo "═══════════════════════════════════════════════════"
echo "  Robothor Memory System — Test Suite"
echo "═══════════════════════════════════════════════════"
echo ""
if [ -n "$LAYER" ]; then
    echo "  Layer:   $LAYER"
fi
if [ -n "$MARK_EXPR" ]; then
    echo "  Filter:  $MARK_EXPR"
fi
echo "  Command: ${CMD[*]}"
echo ""
echo "───────────────────────────────────────────────────"

# Run
"${CMD[@]}"
EXIT_CODE=$?

echo ""
echo "───────────────────────────────────────────────────"
if [ $EXIT_CODE -eq 0 ]; then
    echo "  ✅ All tests passed"
elif [ $EXIT_CODE -eq 5 ]; then
    echo "  ⚠️  No tests matched the filters"
else
    echo "  ❌ Some tests failed (exit code: $EXIT_CODE)"
fi
echo "═══════════════════════════════════════════════════"

exit $EXIT_CODE
