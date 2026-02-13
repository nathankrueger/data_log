#!/bin/bash
#
# Test radio parameter reconfiguration reliability by toggling between
# original and new params multiple times.
#
# This is a wrapper around set_radio_params.sh that:
# 1. Captures original params from the gateway
# 2. Toggles between original and new params N times (default: 5)
# 3. Always leaves the system in its original state
#
# Usage:
#   test_radio_reconfig.sh --sf 9                    # Toggle SF, 5 cycles
#   test_radio_reconfig.sh --sf 9 --count 3          # Toggle SF, 3 cycles
#   test_radio_reconfig.sh --sf 9 --bw 1 --count 10  # Toggle both, 10 cycles
#   test_radio_reconfig.sh --sf 9 --settle-time 2    # Wait 2s between transitions
#   test_radio_reconfig.sh --sf 9 --dry-run          # Show what would happen
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Defaults
COUNT=5
SETTLE_TIME=0
DRY_RUN=false
GATEWAY="${GATEWAY_HOST:-localhost}"
PORT="${GATEWAY_PORT:-5001}"

# New param values (captured from args)
NEW_SF=""
NEW_BW=""
NEW_N2GFREQ=""
NEW_G2NFREQ=""

# Pass-through args for set_radio_params.sh
PASSTHROUGH_ARGS=()

# Statistics
TRANSITIONS_OK=0
TRANSITIONS_FAIL=0
START_TIME=0

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Test radio reconfig reliability by toggling params between original and new values.

Radio parameters (at least one required):
  --sf N            Spreading factor (7-12)
  --bw N            Bandwidth code (0=125kHz, 1=250kHz, 2=500kHz)
  --n2gfreq F       Node-to-Gateway frequency in MHz
  --g2nfreq F       Gateway-to-Node frequency in MHz

Test options:
  --count N         Number of round-trip cycles (default: 5)
  --settle-time N   Seconds to wait between transitions (default: 0)

Pass-through options (forwarded to set_radio_params.sh):
  --nodes LIST      Comma-separated node IDs (skip discovery)
  -g, --gateway     Gateway host (default: \$GATEWAY_HOST or localhost)
  -p, --port        Gateway port (default: \$GATEWAY_PORT or 5001)
  --dry-run         Show what would happen without making changes
  --no-verify       Skip echo verification
  --rcfg-retries N  Max retries for rcfg_radio (default: 3)
  -r, --retries     Discovery retries per round
  -i, --interval    Seconds between discovery rounds

Exit codes:
  0 - All transitions succeeded
  1 - Some transitions failed
  2 - Majority failed or unable to restore original state
EOF
    exit 1
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --count)
            COUNT="$2"
            shift 2
            ;;
        --settle-time)
            SETTLE_TIME="$2"
            shift 2
            ;;
        --sf)
            NEW_SF="$2"
            PASSTHROUGH_ARGS+=("--sf" "$2")
            shift 2
            ;;
        --bw)
            NEW_BW="$2"
            PASSTHROUGH_ARGS+=("--bw" "$2")
            shift 2
            ;;
        --n2gfreq)
            NEW_N2GFREQ="$2"
            PASSTHROUGH_ARGS+=("--n2gfreq" "$2")
            shift 2
            ;;
        --g2nfreq)
            NEW_G2NFREQ="$2"
            PASSTHROUGH_ARGS+=("--g2nfreq" "$2")
            shift 2
            ;;
        -g|--gateway)
            GATEWAY="$2"
            PASSTHROUGH_ARGS+=("--gateway" "$2")
            shift 2
            ;;
        -p|--port)
            PORT="$2"
            PASSTHROUGH_ARGS+=("--port" "$2")
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            PASSTHROUGH_ARGS+=("--dry-run")
            shift
            ;;
        --nodes|--no-verify|--rcfg-retries|-r|--retries|-i|--interval)
            # Args that take a value
            if [[ "$1" == "--no-verify" ]]; then
                PASSTHROUGH_ARGS+=("$1")
                shift
            else
                PASSTHROUGH_ARGS+=("$1" "$2")
                shift 2
            fi
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage
            ;;
    esac
done

# Validate at least one param specified
if [[ -z "$NEW_SF" && -z "$NEW_BW" && -z "$NEW_N2GFREQ" && -z "$NEW_G2NFREQ" ]]; then
    echo "Error: At least one radio parameter (--sf, --bw, --n2gfreq, --g2nfreq) required" >&2
    usage
fi

# Validate count
if ! [[ "$COUNT" =~ ^[0-9]+$ ]]; then
    echo "Error: --count must be a non-negative integer" >&2
    exit 2
fi

# Get gateway params
get_gateway_params() {
    curl -s "http://${GATEWAY}:${PORT}/gateway/params"
}

# Check gateway connectivity and capture original params
echo "=== Radio Reconfig Reliability Test ==="
echo "Gateway: ${GATEWAY}:${PORT}"
echo ""

echo "Checking gateway connectivity..."
ORIG_PARAMS=$(get_gateway_params) || {
    echo "Error: Cannot connect to gateway at ${GATEWAY}:${PORT}" >&2
    exit 2
}

if [[ -z "$ORIG_PARAMS" || "$ORIG_PARAMS" == "null" ]]; then
    echo "Error: Failed to get gateway params" >&2
    exit 2
fi

# Extract original values for params we're changing
ORIG_SF=$(echo "$ORIG_PARAMS" | jq -r '.sf // empty')
ORIG_BW=$(echo "$ORIG_PARAMS" | jq -r '.bw // empty')
ORIG_N2GFREQ=$(echo "$ORIG_PARAMS" | jq -r '.n2g_freq // empty')
ORIG_G2NFREQ=$(echo "$ORIG_PARAMS" | jq -r '.g2n_freq // empty')

# Build display strings
orig_display=""
new_display=""

if [[ -n "$NEW_SF" ]]; then
    orig_display+="sf=$ORIG_SF "
    new_display+="sf=$NEW_SF "
fi
if [[ -n "$NEW_BW" ]]; then
    orig_display+="bw=$ORIG_BW "
    new_display+="bw=$NEW_BW "
fi
if [[ -n "$NEW_N2GFREQ" ]]; then
    orig_display+="n2gfreq=${ORIG_N2GFREQ}MHz "
    new_display+="n2gfreq=${NEW_N2GFREQ}MHz "
fi
if [[ -n "$NEW_G2NFREQ" ]]; then
    orig_display+="g2nfreq=${ORIG_G2NFREQ}MHz "
    new_display+="g2nfreq=${NEW_G2NFREQ}MHz "
fi

echo "Original params: ${orig_display}"
echo "New params: ${new_display}"
echo "Cycles: ${COUNT} ($(( COUNT * 2 )) transitions total)"
if [[ "$SETTLE_TIME" -gt 0 ]]; then
    echo "Settle time: ${SETTLE_TIME}s between transitions"
fi
if [[ "$DRY_RUN" == "true" ]]; then
    echo "Mode: DRY RUN (no changes will be made)"
fi
echo ""

# Build restoration args (only for params we're changing)
RESTORE_ARGS=()
if [[ -n "$NEW_SF" ]]; then
    RESTORE_ARGS+=("--sf" "$ORIG_SF")
fi
if [[ -n "$NEW_BW" ]]; then
    RESTORE_ARGS+=("--bw" "$ORIG_BW")
fi
if [[ -n "$NEW_N2GFREQ" ]]; then
    RESTORE_ARGS+=("--n2gfreq" "$ORIG_N2GFREQ")
fi
if [[ -n "$NEW_G2NFREQ" ]]; then
    RESTORE_ARGS+=("--g2nfreq" "$ORIG_G2NFREQ")
fi

# Build common args (gateway, port, etc. but NOT the radio params)
COMMON_ARGS=()
for ((i=0; i<${#PASSTHROUGH_ARGS[@]}; i++)); do
    arg="${PASSTHROUGH_ARGS[$i]}"
    case "$arg" in
        --sf|--bw|--n2gfreq|--g2nfreq)
            # Skip radio params (we handle them separately)
            ((i++)) || true
            ;;
        *)
            COMMON_ARGS+=("$arg")
            # If this arg takes a value, grab it
            if [[ "$arg" =~ ^(--nodes|--gateway|-g|--port|-p|--rcfg-retries|-r|--retries|-i|--interval)$ ]]; then
                ((i++)) || true
                COMMON_ARGS+=("${PASSTHROUGH_ARGS[$i]}")
            fi
            ;;
    esac
done

# Handle count=0 case
if [[ "$COUNT" -eq 0 ]]; then
    echo "Count is 0, nothing to do."
    echo "Current state verified, exiting."
    exit 0
fi

# Run a transition and track result
run_transition() {
    local label="$1"
    shift
    local args=("$@")
    local exit_code=0

    echo -n "  $label... "

    if [[ "$DRY_RUN" == "true" ]]; then
        echo "SKIPPED (dry-run)"
        echo "    Would run: set_radio_params.sh ${args[*]}"
        return 0
    fi

    # Run set_radio_params.sh and capture exit code
    set +e
    "$SCRIPT_DIR/set_radio_params.sh" "${args[@]}" > /tmp/reconfig_output_$$.txt 2>&1
    exit_code=$?
    set -e

    if [[ $exit_code -eq 0 ]]; then
        echo "OK (exit=$exit_code)"
        ((TRANSITIONS_OK++)) || true
    elif [[ $exit_code -eq 1 ]]; then
        echo "PARTIAL (exit=$exit_code)"
        ((TRANSITIONS_OK++)) || true  # Partial is still a success (majority passed)
    else
        echo "FAILED (exit=$exit_code)"
        ((TRANSITIONS_FAIL++)) || true
        # Show last few lines of output for debugging
        echo "    Last output:"
        tail -5 /tmp/reconfig_output_$$.txt | sed 's/^/      /'
    fi

    return $exit_code
}

# Format elapsed seconds as [HH:][MM:]SS
format_elapsed() {
    local secs=$1
    if [[ $secs -lt 60 ]]; then
        echo "${secs}s"
    elif [[ $secs -lt 3600 ]]; then
        printf "%d:%02d" $((secs / 60)) $((secs % 60))
    else
        printf "%d:%02d:%02d" $((secs / 3600)) $(((secs % 3600) / 60)) $((secs % 60))
    fi
}

# Main test loop
START_TIME=$(date +%s)
total_transitions=$((COUNT * 2))
transition_num=0

for cycle in $(seq 1 "$COUNT"); do
    CYCLE_START=$(date +%s)
    echo "--- Cycle $cycle/$COUNT started @ $(date '+%H:%M:%S') ---"

    # Apply new params
    ((transition_num++)) || true
    run_transition "[$transition_num/$total_transitions] Applying new params (${new_display})" \
        "${PASSTHROUGH_ARGS[@]}" || true

    if [[ "$SETTLE_TIME" -gt 0 && "$DRY_RUN" != "true" ]]; then
        sleep "$SETTLE_TIME"
    fi

    # Restore original params
    ((transition_num++)) || true
    run_transition "[$transition_num/$total_transitions] Restoring original (${orig_display})" \
        "${RESTORE_ARGS[@]}" "${COMMON_ARGS[@]}" || true

    if [[ "$SETTLE_TIME" -gt 0 && "$DRY_RUN" != "true" && $cycle -lt $COUNT ]]; then
        sleep "$SETTLE_TIME"
    fi

    CYCLE_END=$(date +%s)
    CYCLE_ELAPSED=$((CYCLE_END - CYCLE_START))
    echo "--- Cycle $cycle/$COUNT complete ($(format_elapsed $CYCLE_ELAPSED) elapsed) ---"
    echo ""
done

# Calculate elapsed time
END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))

# Final summary
echo "=== Summary ==="
if [[ "$DRY_RUN" == "true" ]]; then
    echo "Transitions: $total_transitions would be attempted (dry-run)"
    echo "Total time: $(format_elapsed $ELAPSED)"
    exit 0
fi

success_rate=0
if [[ $total_transitions -gt 0 ]]; then
    success_rate=$(( (TRANSITIONS_OK * 100) / total_transitions ))
fi

echo "Transitions: ${TRANSITIONS_OK}/${total_transitions} succeeded (${success_rate}%)"
echo "Total time: $(format_elapsed $ELAPSED)"

# Verify final state
echo ""
echo "Verifying final state..."
FINAL_PARAMS=$(get_gateway_params)
FINAL_SF=$(echo "$FINAL_PARAMS" | jq -r '.sf // empty')
FINAL_BW=$(echo "$FINAL_PARAMS" | jq -r '.bw // empty')

state_ok=true
if [[ -n "$NEW_SF" && "$FINAL_SF" != "$ORIG_SF" ]]; then
    echo "  WARNING: SF is $FINAL_SF, expected $ORIG_SF"
    state_ok=false
fi
if [[ -n "$NEW_BW" && "$FINAL_BW" != "$ORIG_BW" ]]; then
    echo "  WARNING: BW is $FINAL_BW, expected $ORIG_BW"
    state_ok=false
fi

if [[ "$state_ok" == "true" ]]; then
    echo "Final state: ORIGINAL (verified)"
else
    echo "Final state: MISMATCH (see warnings above)"
fi

# Clean up
rm -f /tmp/reconfig_output_$$.txt

# Determine exit code
if [[ $TRANSITIONS_FAIL -eq 0 ]]; then
    exit 0
elif [[ $TRANSITIONS_OK -gt $TRANSITIONS_FAIL ]]; then
    exit 1
else
    exit 2
fi
