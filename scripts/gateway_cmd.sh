#!/bin/bash

usage() {
    cat <<'EOF'
Gateway parameter getter/setter via HTTP API.

Usage: gateway_cmd.sh [-l | -G | -g <param> | -s <param> <value>] [-r] [-S] [-H <host>] [-p <port>]

Options:
  -l            List all gateway parameters (raw JSON)
  -G            Get all gateway parameters (formatted)
  -g <param>    Get single parameter value
  -s <param>    Set/stage parameter (requires value argument)
  -r            Apply staged radio config (rcfg_radio, no persist)
  -S            Save all params to config file (savecfg)
  -H <host>     Gateway host (default: $GATEWAY_HOST or localhost)
  -p <port>     Gateway port (default: $GATEWAY_PORT or 5001)
  -h            Show this help

The -r and -S flags can be combined with each other and with other operations.
Order of execution: get/set operation first, then -r, then -S.

Radio params (sf, bw, txpwr, n2g_freq, g2n_freq) use staged config:
  -s stages the value without applying to hardware
  -r applies staged radio params to hardware (no persist)
  -S persists ALL current params to config file

Command_server params (max_retries, initial_retry_ms, etc.) apply immediately
but do NOT persist until -S is called.

Use -G to see available parameters.

Examples:
  gateway_cmd.sh -l                    # List all params (raw JSON)
  gateway_cmd.sh -G                    # Get all params (formatted)
  gateway_cmd.sh -g sf                 # Get spreading factor
  gateway_cmd.sh -s sf 9               # Stage spreading factor to 9
  gateway_cmd.sh -s sf 9 -r -S         # Stage SF, apply, and persist
  gateway_cmd.sh -r                    # Apply staged radio params (no persist)
  gateway_cmd.sh -S                    # Persist all params to config file
  gateway_cmd.sh -r -S                 # Apply radio AND persist
  gateway_cmd.sh -s max_retries 5      # Set max_retries (immediate, no persist)
EOF
    exit 1
}

MODE=""
PARAM=""
VALUE=""
OPT_HOST=""
OPT_PORT=""
DO_RCFG=0
DO_SAVE=0

# Use GNU getopt for proper argument reordering (handles "-s sf 9 -r" correctly)
PARSED=$(getopt -o lGg:s:rSH:p:h -n "$(basename "$0")" -- "$@") || usage
eval set -- "$PARSED"

while true; do
    case "$1" in
        -l)
            MODE="list"
            shift
            ;;
        -G)
            MODE="getall"
            shift
            ;;
        -g)
            MODE="get"
            PARAM="$2"
            shift 2
            ;;
        -s)
            MODE="set"
            PARAM="$2"
            shift 2
            ;;
        -r)
            DO_RCFG=1
            shift
            ;;
        -S)
            DO_SAVE=1
            shift
            ;;
        -H)
            OPT_HOST="$2"
            shift 2
            ;;
        -p)
            OPT_PORT="$2"
            shift 2
            ;;
        -h)
            usage
            ;;
        --)
            shift
            break
            ;;
        *)
            usage
            ;;
    esac
done

# Must specify at least one action
if [ -z "$MODE" ] && [ $DO_RCFG -eq 0 ] && [ $DO_SAVE -eq 0 ]; then
    echo "Error: specify -l, -G, -g <param>, -s <param> <value>, -r, or -S"
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
        RESPONSE=$(curl -sS --max-time "$timeout" -H "Connection: close" \
            -w "\n%{http_code}" "$url" 2>"$STDERR_FILE")
    else
        RESPONSE=$(curl -sS -X "$method" "$url" \
            -H "Content-Type: application/json" \
            -H "Connection: close" \
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

# Execute primary mode first (if any)
case "$MODE" in
    list)
        do_curl GET "$BASE_URL/gateway/params"
        ;;
    getall)
        # Get all params and format them nicely
        RESULT=$(do_curl GET "$BASE_URL/gateway/params")
        if [ $? -eq 0 ]; then
            echo "$RESULT" | jq -r 'to_entries | .[] | "\(.key): \(.value)"'
        fi
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
esac

# Apply radio config if requested
if [ $DO_RCFG -eq 1 ]; then
    do_curl POST "$BASE_URL/gateway/rcfg_radio" "{}"
fi

# Save config if requested
if [ $DO_SAVE -eq 1 ]; then
    do_curl POST "$BASE_URL/gateway/savecfg" "{}"
fi
