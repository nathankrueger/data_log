#!/bin/bash

USAGE="Generic command sender for LoRa nodes via gateway HTTP API.

Usage: node_cmd.sh [-n <node_id>] -c <command> [-a <arg>]... [-e <count>] [-w] [-g <gateway>] [-p <port>]
       node_cmd.sh -d [-a <retries>] [-g <gateway>] [-p <port>]

Options:
  -n <node_id>  Target node ID (optional; omit for broadcast)
  -c <command>  Command name (required unless -d)
  -a <arg>      Command argument (can be repeated; for -d: retry count)
  -e <count>    Expected ACK count for broadcasts (default 1)
  -d            Discover all reachable nodes
  -w            Wait for response (works with -e for broadcast multi-response)
  -g <gateway>  Gateway host (default: \$GATEWAY_HOST or localhost)
  -p <port>     Gateway port (default: \$GATEWAY_PORT or 5001)
  -h            Show this help

Examples:
  node_cmd.sh -n patio -c ping                    # Ping specific node (fire-and-forget)
  node_cmd.sh -c ping                             # Broadcast ping to all nodes
  node_cmd.sh -c ping -e 3                        # Broadcast ping, wait for 3 ACKs
  node_cmd.sh -c params -e 3 -w                   # Get params from all 3 nodes
  node_cmd.sh -n patio -c echo -a \"hello\" -w      # Echo with response wait
  node_cmd.sh -n patio -c set_interval -a 30      # Set broadcast interval
  node_cmd.sh -d                                  # Discover all reachable nodes
  node_cmd.sh -d -a 5                             # Discover with 5 retries"

usage() {
    echo "$USAGE"
    exit 1
}

NODE_ID=""
COMMAND=""
ARGS=()
EXPECTED_ACKS=1
WAIT=false
DISCOVER=false
OPT_HOST=""
OPT_PORT=""

while getopts "n:c:a:e:dwg:p:h" opt; do
    case $opt in
        n)
            NODE_ID="$OPTARG"
            ;;
        c)
            COMMAND="$OPTARG"
            ;;
        a)
            ARGS+=("$OPTARG")
            ;;
        e)
            EXPECTED_ACKS="$OPTARG"
            ;;
        d)
            DISCOVER=true
            ;;
        w)
            WAIT=true
            ;;
        g)
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

# Must specify either -c or -d
if [ -z "$COMMAND" ] && [ "$DISCOVER" = false ]; then
    echo "Error: -c <command> or -d is required"
    usage
fi

if [ -n "$COMMAND" ] && [ "$DISCOVER" = true ]; then
    echo "Error: -c and -d are mutually exclusive"
    usage
fi

# -e (expected_acks) only makes sense for broadcasts
if [ "$EXPECTED_ACKS" != "1" ] && [ -n "$NODE_ID" ]; then
    echo "Error: -e is only valid for broadcasts (don't use with -n)"
    usage
fi

# Command-line options take precedence over env vars
GATEWAY_HOST=${OPT_HOST:-${GATEWAY_HOST:-localhost}}
GATEWAY_PORT=${OPT_PORT:-${GATEWAY_PORT:-5001}}

if [ "$DISCOVER" = true ]; then
    # GET /discover[?retries=N]
    URL="http://$GATEWAY_HOST:$GATEWAY_PORT/discover"
    if [ ${#ARGS[@]} -gt 0 ]; then
        URL="$URL?retries=${ARGS[0]}"
    fi

    STDERR_FILE=$(mktemp)
    RESPONSE=$(curl -sS --max-time 180 -w "\n%{http_code}" "$URL" 2>"$STDERR_FILE")
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
elif [ "$WAIT" = true ] && [ -z "$NODE_ID" ]; then
    # Broadcast wait: GET /{cmd}?expected_acks=N&a=arg1&a=arg2
    URL="http://$GATEWAY_HOST:$GATEWAY_PORT/$COMMAND"

    # Build query string
    QUERY="?expected_acks=$EXPECTED_ACKS"
    if [ ${#ARGS[@]} -gt 0 ]; then
        ALL_ARGS=$(IFS=,; echo "${ARGS[*]}")
        IFS=',' read -ra ARG_ARRAY <<< "$ALL_ARGS"
        for arg in "${ARG_ARRAY[@]}"; do
            QUERY="$QUERY&a=$(printf '%s' "$arg" | jq -sRr @uri)"
        done
    fi

    # Fetch server-side wait_timeout and add buffer for curl
    SERVER_TIMEOUT=$(curl -sS --max-time 5 "http://$GATEWAY_HOST:$GATEWAY_PORT/gateway/param/wait_timeout" 2>/dev/null | jq -r '.wait_timeout // 20 | floor')
    CURL_TIMEOUT=$((SERVER_TIMEOUT + 5))

    # Capture stderr separately to get clean error messages
    STDERR_FILE=$(mktemp)
    RESPONSE=$(curl -sS --max-time "$CURL_TIMEOUT" -w "\n%{http_code}" "$URL$QUERY" 2>"$STDERR_FILE")
    CURL_EXIT=$?
    STDERR=$(cat "$STDERR_FILE")
    rm -f "$STDERR_FILE"

    if [ $CURL_EXIT -ne 0 ]; then
        echo "Error: $STDERR" >&2
        exit 1
    fi

    # Parse response body and HTTP code
    HTTP_CODE=$(echo "$RESPONSE" | tail -n 1)
    BODY=$(echo "$RESPONSE" | sed '$d')

    if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 300 ]; then
        echo "$BODY"
    else
        echo "Error: HTTP $HTTP_CODE - $BODY" >&2
        exit 1
    fi
elif [ "$WAIT" = true ]; then
    # Unicast wait: GET /{cmd}/{node_id}?a=arg1&a=arg2
    URL="http://$GATEWAY_HOST:$GATEWAY_PORT/$COMMAND/$NODE_ID"

    # Build query string for args - split comma-separated values
    QUERY=""
    if [ ${#ARGS[@]} -gt 0 ]; then
        ALL_ARGS=$(IFS=,; echo "${ARGS[*]}")
        IFS=',' read -ra ARG_ARRAY <<< "$ALL_ARGS"
        for arg in "${ARG_ARRAY[@]}"; do
            if [ -z "$QUERY" ]; then
                QUERY="?a=$(printf '%s' "$arg" | jq -sRr @uri)"
            else
                QUERY="$QUERY&a=$(printf '%s' "$arg" | jq -sRr @uri)"
            fi
        done
    fi

    # Fetch server-side wait_timeout and add buffer for curl
    # This ensures curl timeout > server timeout so we get proper 504 responses
    SERVER_TIMEOUT=$(curl -sS --max-time 5 "http://$GATEWAY_HOST:$GATEWAY_PORT/gateway/param/wait_timeout" 2>/dev/null | jq -r '.wait_timeout // 20 | floor')
    CURL_TIMEOUT=$((SERVER_TIMEOUT + 5))

    # Capture stderr separately to get clean error messages
    STDERR_FILE=$(mktemp)
    RESPONSE=$(curl -sS --max-time "$CURL_TIMEOUT" -w "\n%{http_code}" "$URL$QUERY" 2>"$STDERR_FILE")
    CURL_EXIT=$?
    STDERR=$(cat "$STDERR_FILE")
    rm -f "$STDERR_FILE"

    if [ $CURL_EXIT -ne 0 ]; then
        echo "Error: $STDERR" >&2
        exit 1
    fi

    # Parse response body and HTTP code
    HTTP_CODE=$(echo "$RESPONSE" | tail -n 1)
    BODY=$(echo "$RESPONSE" | sed '$d')

    if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 300 ]; then
        echo "$BODY"
    else
        echo "Error: HTTP $HTTP_CODE - $BODY" >&2
        exit 1
    fi
else
    # POST endpoint: /command with JSON body
    # Build args JSON array - split comma-separated values
    if [ ${#ARGS[@]} -gt 0 ]; then
        # Join all -a args with commas, then split by comma
        ALL_ARGS=$(IFS=,; echo "${ARGS[*]}")
        IFS=',' read -ra ARG_ARRAY <<< "$ALL_ARGS"
        ARGS_JSON=$(printf '%s\n' "${ARG_ARRAY[@]}" | jq -R . | jq -s .)
    else
        ARGS_JSON="[]"
    fi

    JSON_BODY=$(jq -n \
        --arg cmd "$COMMAND" \
        --argjson args "$ARGS_JSON" \
        --arg node_id "$NODE_ID" \
        --argjson expected_acks "$EXPECTED_ACKS" \
        '{cmd: $cmd, args: $args, node_id: $node_id, expected_acks: $expected_acks}')

    # Capture stderr separately to get clean error messages
    STDERR_FILE=$(mktemp)
    RESPONSE=$(curl -sS -X POST "http://$GATEWAY_HOST:$GATEWAY_PORT/command" \
        -H "Content-Type: application/json" \
        -d "$JSON_BODY" \
        --max-time 5 \
        -w "\n%{http_code}" 2>"$STDERR_FILE")
    CURL_EXIT=$?
    STDERR=$(cat "$STDERR_FILE")
    rm -f "$STDERR_FILE"

    if [ $CURL_EXIT -ne 0 ]; then
        echo "Error: $STDERR" >&2
        exit 1
    fi

    # Parse response body and HTTP code
    HTTP_CODE=$(echo "$RESPONSE" | tail -n 1)
    BODY=$(echo "$RESPONSE" | sed '$d')

    if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 300 ]; then
        echo "$BODY"
    else
        echo "Error: HTTP $HTTP_CODE - $BODY" >&2
        exit 1
    fi
fi
