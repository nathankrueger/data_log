#!/bin/bash
#
# Gateway parameter getter/setter via HTTP API.
#
# Usage: gateway_cmd.sh [-l | -g <param> | -s <param> <value> | -r | -S] [-H <host>] [-p <port>]
#
# Options:
#   -l            List all gateway parameters
#   -g <param>    Get single parameter value
#   -s <param>    Set/stage parameter (requires value argument)
#   -r            Apply staged radio config (rcfg_radio, no persist)
#   -S            Save all params to config file (savecfg)
#   -H <host>     Gateway host (default: $GATEWAY_HOST or localhost)
#   -p <port>     Gateway port (default: $GATEWAY_PORT or 5001)
#   -h            Show this help
#
# Radio params (sf, bw, txpwr, n2g_freq, g2n_freq) use staged config:
#   -s stages the value without applying to hardware
#   -r applies staged radio params to hardware (no persist)
#   -S persists ALL current params to config file
#
# Command_server params (max_retries, initial_retry_ms, etc.) apply immediately
# but do NOT persist until -S is called.
#
# Parameters:
#   sf                Spreading factor (7-12) [staged]
#   bw                Bandwidth code (0=125kHz, 1=250kHz, 2=500kHz) [staged]
#   txpwr             TX power in dBm (5-23) [staged]
#   n2g_freq          Node-to-gateway frequency in MHz [staged]
#   g2n_freq          Gateway-to-node frequency in MHz [staged]
#   max_queue_size    Command queue size [immediate]
#   max_retries       Max command retries [immediate]
#   initial_retry_ms  Initial retry delay ms [immediate]
#   retry_multiplier  Backoff multiplier [immediate]
#   max_retry_ms      Max retry delay ms [immediate]
#   discovery_retries Discovery retry count [immediate]
#
# Examples:
#   gateway_cmd.sh -l                    # List all params
#   gateway_cmd.sh -g sf                 # Get spreading factor
#   gateway_cmd.sh -s sf 9               # Stage spreading factor to 9
#   gateway_cmd.sh -r                    # Apply staged radio params (no persist)
#   gateway_cmd.sh -S                    # Persist all params to config file
#   gateway_cmd.sh -s max_retries 5      # Set max_retries (immediate, no persist)
#   gateway_cmd.sh -r && gateway_cmd.sh -S  # Apply radio AND persist

usage() {
    head -n 42 "$0" | tail -n 41 | sed 's/^# //' | sed 's/^#//'
    exit 1
}

MODE=""
PARAM=""
VALUE=""
OPT_HOST=""
OPT_PORT=""

while getopts "lg:s:rSH:p:h" opt; do
    case $opt in
        l)
            MODE="list"
            ;;
        g)
            MODE="get"
            PARAM="$OPTARG"
            ;;
        s)
            MODE="set"
            PARAM="$OPTARG"
            ;;
        r)
            MODE="rcfg"
            ;;
        S)
            MODE="savecfg"
            ;;
        H)
            OPT_HOST="$OPTARG"
            ;;
        p)
            OPT_PORT="$OPTARG"
            ;;
        h)
            usage
            ;;
        *)
            usage
            ;;
    esac
done

# Shift past options to get positional args (value for -s)
shift $((OPTIND - 1))

# Must specify a mode
if [ -z "$MODE" ]; then
    echo "Error: specify -l, -g <param>, -s <param> <value>, -r, or -S"
    usage
fi

# Set mode requires a value
if [ "$MODE" = "set" ]; then
    if [ $# -lt 1 ]; then
        echo "Error: -s <param> requires a value argument"
        usage
    fi
    VALUE="$1"
fi

# Command-line options take precedence over env vars
GATEWAY_HOST=${OPT_HOST:-${GATEWAY_HOST:-localhost}}
GATEWAY_PORT=${OPT_PORT:-${GATEWAY_PORT:-5001}}

BASE_URL="http://$GATEWAY_HOST:$GATEWAY_PORT"

# Helper function for curl requests
do_curl() {
    local method="$1"
    local url="$2"
    local data="$3"
    local timeout="${4:-5}"

    STDERR_FILE=$(mktemp)

    if [ "$method" = "GET" ]; then
        RESPONSE=$(curl -sS --max-time "$timeout" -w "\n%{http_code}" "$url" 2>"$STDERR_FILE")
    else
        RESPONSE=$(curl -sS -X "$method" "$url" \
            -H "Content-Type: application/json" \
            -d "$data" \
            --max-time "$timeout" \
            -w "\n%{http_code}" 2>"$STDERR_FILE")
    fi
    CURL_EXIT=$?
    STDERR=$(cat "$STDERR_FILE")
    rm -f "$STDERR_FILE"

    if [ $CURL_EXIT -ne 0 ]; then
        echo "Error: $STDERR" >&2
        exit 1
    fi

    HTTP_CODE=$(echo "$RESPONSE" | tail -n 1)
    BODY=$(echo "$RESPONSE" | sed '$d')

    if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 300 ]; then
        echo "$BODY"
    else
        echo "Error: HTTP $HTTP_CODE - $BODY" >&2
        exit 1
    fi
}

case "$MODE" in
    list)
        do_curl GET "$BASE_URL/gateway/params"
        ;;
    get)
        do_curl GET "$BASE_URL/gateway/param/$PARAM"
        ;;
    set)
        # Build JSON body based on value type (int or string)
        if [[ "$VALUE" =~ ^-?[0-9]+$ ]]; then
            JSON_BODY="{\"value\": $VALUE}"
        elif [[ "$VALUE" =~ ^-?[0-9]*\.[0-9]+$ ]]; then
            JSON_BODY="{\"value\": $VALUE}"
        else
            JSON_BODY=$(jq -n --arg v "$VALUE" '{value: $v}')
        fi
        do_curl PUT "$BASE_URL/gateway/param/$PARAM" "$JSON_BODY"
        ;;
    rcfg)
        do_curl POST "$BASE_URL/gateway/rcfg_radio" "{}"
        ;;
    savecfg)
        do_curl POST "$BASE_URL/gateway/savecfg" "{}"
        ;;
esac
