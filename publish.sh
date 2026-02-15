#!/bin/bash

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

SSH to remote machines and run 'git pull' in the specified repositories.

Options:
    -h, --help                  Show this help message and exit
    -r, --reinstall             Run './install.sh -r' after git pull to reinstall venv
    -R, --restart               Restart the running service after pull (if exactly one is active)
    --destination USER:HOST:PATH
                                Add an additional destination beyond the defaults.
                                Can be specified multiple times.
                                Format: username:hostname:repo_path

Examples:
    $(basename "$0")
        Pull on all default destinations.

    $(basename "$0") --reinstall
        Pull and reinstall venv on all default destinations.

    $(basename "$0") --restart
        Pull and restart the active service on each destination.

    $(basename "$0") --destination user:host:/path/to/repo
        Pull on default destinations plus the specified one.

    $(basename "$0") -r --destination user:h1:/path --destination user:h2:/path
        Pull and reinstall on default destinations plus two additional ones.
EOF
}

# Default destinations (username:hostname:repo_path)
DEFAULT_DESTINATIONS=(
    "nkrueger:pz2w1:/home/nkrueger/dev/data_log"
    "nkrueger:pz2w2:/home/nkrueger/dev/data_log"
    "nkrueger:pz2w3:/home/nkrueger/dev/data_log"
    "nkrueger:pz2w4:/home/nkrueger/dev/data_log"
)

# Parse command line arguments
EXTRA_DESTINATIONS=()
REINSTALL=false
RESTART=false
while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help)
            usage
            exit 0
            ;;
        -r|--reinstall)
            REINSTALL=true
            shift
            ;;
        -R|--restart)
            RESTART=true
            shift
            ;;
        --destination)
            EXTRA_DESTINATIONS+=("$2")
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            echo
            usage
            exit 1
            ;;
    esac
done

# Combine default and extra destinations
DESTINATIONS=("${DEFAULT_DESTINATIONS[@]}" "${EXTRA_DESTINATIONS[@]}")

# Discover known service names from services/ folder
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KNOWN_SERVICES=()
for f in "$SCRIPT_DIR"/services/*.service; do
    [ -f "$f" ] && KNOWN_SERVICES+=("$(basename "$f")")
done

# SSH options for reliability on weak wifi connections
SSH_OPTS="-o ServerAliveInterval=60 -o ServerAliveCountMax=5 -o ConnectTimeout=10 -o ConnectionAttempts=10"

# Process each destination
for entry in "${DESTINATIONS[@]}"; do
    IFS=':' read -r username hostname repo_path <<< "$entry"

    echo "Updating $hostname..."

    if [ "$REINSTALL" = true ]; then
        ssh $SSH_OPTS "${username}@${hostname}" "cd ${repo_path} && git pull && ./install.sh -r"
    else
        ssh $SSH_OPTS "${username}@${hostname}" "cd ${repo_path} && git pull"
    fi

    if [[ $? -eq 0 ]]; then
        echo "Successfully updated $hostname"
    else
        echo "Failed to update $hostname"
        echo
        continue
    fi

    # Restart the active service if requested
    if [ "$RESTART" = true ]; then
        # Build a systemctl check for all known services in a single SSH call
        service_list=$(printf " %s" "${KNOWN_SERVICES[@]}")
        active_services=$(ssh $SSH_OPTS "${username}@${hostname}" \
            "for svc in ${service_list}; do systemctl is-active \$svc 2>/dev/null | grep -q '^active$' && echo \$svc; done")

        # Count active services
        count=$(echo "$active_services" | grep -c '\.service$')

        if [[ $count -eq 1 ]]; then
            svc_name=$(echo "$active_services" | tr -d '[:space:]')
            echo "Restarting $svc_name on $hostname..."
            ssh $SSH_OPTS "${username}@${hostname}" "sudo systemctl restart $svc_name"
            if [[ $? -eq 0 ]]; then
                echo "Successfully restarted $svc_name on $hostname"
            else
                echo "Failed to restart $svc_name on $hostname"
            fi
        elif [[ $count -eq 0 ]]; then
            echo "No active services found on $hostname, skipping restart"
        else
            echo "Multiple active services on $hostname, skipping restart:"
            echo "$active_services" | sed 's/^/  /'
        fi
    fi
    echo
done
