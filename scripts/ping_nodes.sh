#!/bin/bash

usage() {
    echo "Usage: $0 [-n <node_id>] [-b] [-g <gateway>] [-p <port>]"
    echo "  -n <node_id>  Ping a specific node"
    echo "  -b            Broadcast ping to all nodes"
    echo "  -g <gateway>  Gateway host (default: \$GATEWAY_HOST or localhost)"
    echo "  -p <port>     Gateway port (default: \$GATEWAY_PORT or 5001)"
    echo "-n and -b are mutually exclusive."
    exit 1
}

NODE_ID=""
BROADCAST=false
OPT_HOST=""
OPT_PORT=""

while getopts "n:bg:p:h" opt; do
    case $opt in
        n)
            NODE_ID="$OPTARG"
            ;;
        b)
            BROADCAST=true
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

# Command-line options take precedence over env vars
GATEWAY_HOST=${OPT_HOST:-${GATEWAY_HOST:-localhost}}
GATEWAY_PORT=${OPT_PORT:-${GATEWAY_PORT:-5001}}

# Check for mutually exclusive options
if [ -n "$NODE_ID" ] && [ "$BROADCAST" = true ]; then
    echo "Error: -n and -b are mutually exclusive"
    usage
fi

# Require at least one option
if [ -z "$NODE_ID" ] && [ "$BROADCAST" = false ]; then
    echo "Error: Must specify either -n <node_id> or -b"
    usage
fi

if [ "$BROADCAST" = true ]; then
    # Broadcast ping (to all nodes)
    curl -X POST http://$GATEWAY_HOST:$GATEWAY_PORT/command \
      -H "Content-Type: application/json" \
      -d '{"cmd":"ping","args":[],"node_id":""}'
else
    # Targeted ping (to specific node)
    curl -X POST http://$GATEWAY_HOST:$GATEWAY_PORT/command \
      -H "Content-Type: application/json" \
      -d "{\"cmd\":\"ping\",\"args\":[],\"node_id\":\"$NODE_ID\"}"
fi