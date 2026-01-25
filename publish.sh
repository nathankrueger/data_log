#!/bin/bash

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

SSH to remote machines and run 'git pull' in the specified repositories.

Options:
    -h, --help                  Show this help message and exit
    -r, --reinstall             Run './install.sh -r' after git pull to reinstall venv
    --destination USER:HOST:PATH
                                Add an additional destination beyond the defaults.
                                Can be specified multiple times.
                                Format: username:hostname:repo_path

Examples:
    $(basename "$0")
        Pull on all default destinations.

    $(basename "$0") --reinstall
        Pull and reinstall venv on all default destinations.

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
)

# Parse command line arguments
EXTRA_DESTINATIONS=()
REINSTALL=false
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

# Pr
    if [ "$REINSTALL" = true ]; then
        ssh "${username}@${hostname}" "cd ${repo_path} && git pull && ./install.sh -r"
    else
        ssh "${username}@${hostname}" "cd ${repo_path} && git pull"
    fi
for entry in "${DESTINATIONS[@]}"; do
    IFS=':' read -r username hostname repo_path <<< "$entry"

    echo "Updating $hostname..."
    ssh "${username}@${hostname}" "cd ${repo_path} && git pull"

    if [[ $? -eq 0 ]]; then
        echo "Successfully updated $hostname"
    else
        echo "Failed to update $hostname"
    fi
    echo
done
