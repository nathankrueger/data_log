GATEWAY_HOST=${GATEWAY_HOST:-localhost}
GATEWAY_PORT=${GATEWAY_PORT:-5001}

# Broadcast ping (to all nodes)
# curl -X POST http://$GATEWAY_HOST:$GATEWAY_PORT/command \
#   -H "Content-Type: application/json" \
#   -d '{"cmd":"ping","args":[],"node_id":""}'

# Targeted ping (to specific node)
curl -X POST http://$GATEWAY_HOST:$GATEWAY_PORT/command \
  -H "Content-Type: application/json" \
  -d '{"cmd":"ping","args":["hello"],"node_id":"pz2w2-shop"}'