#!/bin/bash
# service_mod.sh - Manage systemd services from the services/ folder

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICES_DIR="$SCRIPT_DIR/../services"

usage() {
    cat << EOF
Usage: $(basename "$0") [OPTIONS]

Manage systemd services from the services/ folder.

OPTIONS:
    -i, --install SERVICE_NAME      Install a service from services/ folder
    -u, --uninstall SERVICE_NAME    Uninstall and fully remove a service
    -U, --uninstall-all             Uninstall all registered services
    -r, --restart SERVICE_NAME      Restart a service
    -l, --list                      List all services in services/ folder and their status
    -f, --follow SERVICE_NAME       Follow service logs in real-time (Ctrl+C to stop)
    -b, --logs SERVICE_NAME         Show service logs since last boot
    -h, --help                      Show this help message

EXAMPLES:
    $(basename "$0") --install gateway
    $(basename "$0") --uninstall data_log
    $(basename "$0") --uninstall-all
    $(basename "$0") --restart gateway
    $(basename "$0") --list
    $(basename "$0") --follow node
    $(basename "$0") --logs gateway

EOF
}

list_services() {
    echo "Services in $SERVICES_DIR:"
    echo "=========================================="
    
    if [ ! -d "$SERVICES_DIR" ]; then
        echo "Error: Services directory not found: $SERVICES_DIR"
        exit 1
    fi
    
    for service_file in "$SERVICES_DIR"/*.service; do
        if [ -f "$service_file" ]; then
            service_name=$(basename "$service_file")
            echo ""
            echo "Service: $service_name"
            
            # Check if installed
            if [ -f "/etc/systemd/system/$service_name" ]; then
                echo "  Installed: Yes"
                
                # Get service status
                if systemctl is-enabled "$service_name" &>/dev/null; then
                    enabled_status=$(systemctl is-enabled "$service_name" 2>/dev/null)
                    echo "  Enabled: $enabled_status"
                else
                    echo "  Enabled: disabled"
                fi
                
                if systemctl is-active "$service_name" &>/dev/null; then
                    active_status=$(systemctl is-active "$service_name" 2>/dev/null)
                    echo "  Active: $active_status"
                else
                    echo "  Active: inactive"
                fi
            else
                echo "  Installed: No"
            fi
        fi
    done
    
    if [ -z "$(ls -A "$SERVICES_DIR"/*.service 2>/dev/null)" ]; then
        echo "No .service files found in $SERVICES_DIR"
    fi
}

install_service() {
    local service_name="${1}.service"
    local service_file="$SERVICES_DIR/$service_name"
    
    if [ ! -f "$service_file" ]; then
        echo "Error: Service file not found: $service_file"
        exit 1
    fi
    
    echo "Installing service: $service_name"
    
    # Copy service file
    sudo cp "$service_file" /etc/systemd/system/
    if [ $? -ne 0 ]; then
        echo "Error: Failed to copy service file"
        exit 1
    fi
    
    # Reload systemd
    sudo systemctl daemon-reload
    if [ $? -ne 0 ]; then
        echo "Error: Failed to reload systemd"
        exit 1
    fi
    
    # Enable service
    sudo systemctl enable "$service_name"
    if [ $? -ne 0 ]; then
        echo "Error: Failed to enable service"
        exit 1
    fi
    
    # Start service
    sudo systemctl start "$service_name"
    if [ $? -ne 0 ]; then
        echo "Warning: Failed to start service (may need manual intervention)"
    fi
    
    echo ""
    echo "Service installed successfully!"
    echo "Status:"
    sudo systemctl status "$service_name" --no-pager -l
}

uninstall_service() {
    local service_name="${1}.service"

    if [ ! -f "/etc/systemd/system/$service_name" ]; then
        echo "Error: Service not installed: $service_name"
        exit 1
    fi

    echo "Uninstalling service: $service_name"

    # Stop service
    echo "Stopping service..."
    sudo systemctl stop "$service_name"

    # Disable service
    echo "Disabling service..."
    sudo systemctl disable "$service_name"

    # Remove service file
    echo "Removing service file..."
    sudo rm /etc/systemd/system/"$service_name"
    if [ $? -ne 0 ]; then
        echo "Error: Failed to remove service file"
        exit 1
    fi

    # Reload systemd
    sudo systemctl daemon-reload
    if [ $? -ne 0 ]; then
        echo "Error: Failed to reload systemd"
        exit 1
    fi

    # Reset failed state if any
    sudo systemctl reset-failed "$service_name" 2>/dev/null

    echo ""
    echo "Service uninstalled successfully!"
}

uninstall_all_services() {
    if [ ! -d "$SERVICES_DIR" ]; then
        echo "Error: Services directory not found: $SERVICES_DIR"
        exit 1
    fi

    local installed_services=()

    # Find all installed services
    for service_file in "$SERVICES_DIR"/*.service; do
        if [ -f "$service_file" ]; then
            local service_name=$(basename "$service_file")
            if [ -f "/etc/systemd/system/$service_name" ]; then
                installed_services+=("$service_name")
            fi
        fi
    done

    if [ ${#installed_services[@]} -eq 0 ]; then
        echo "No services are currently installed."
        exit 0
    fi

    echo "Found ${#installed_services[@]} installed service(s):"
    for svc in "${installed_services[@]}"; do
        echo "  - $svc"
    done
    echo ""

    # Uninstall each service
    for service_name in "${installed_services[@]}"; do
        echo "Uninstalling: $service_name"

        # Stop service
        sudo systemctl stop "$service_name" 2>/dev/null

        # Disable service
        sudo systemctl disable "$service_name" 2>/dev/null

        # Remove service file
        sudo rm /etc/systemd/system/"$service_name"
        if [ $? -ne 0 ]; then
            echo "  Warning: Failed to remove $service_name"
        else
            echo "  Removed $service_name"
        fi

        # Reset failed state if any
        sudo systemctl reset-failed "$service_name" 2>/dev/null
    done

    # Reload systemd once at the end
    sudo systemctl daemon-reload

    echo ""
    echo "Uninstalled ${#installed_services[@]} service(s)."
}

restart_service() {
    local service_name="${1}.service"

    if [ ! -f "/etc/systemd/system/$service_name" ]; then
        echo "Error: Service not installed: $service_name"
        exit 1
    fi

    echo "Restarting service: $service_name"
    sudo systemctl restart "$service_name"
    if [ $? -ne 0 ]; then
        echo "Error: Failed to restart service"
        exit 1
    fi

    echo ""
    echo "Service restarted successfully!"
    sudo systemctl status "$service_name" --no-pager -l
}

follow_logs() {
    local service_name="${1}.service"

    echo "Following logs for $service_name (Ctrl+C to stop)..."
    echo ""
    journalctl -u "$service_name" -f
}

show_logs() {
    local service_name="${1}.service"

    echo "Logs for $service_name since last boot:"
    echo ""
    journalctl -u "$service_name" -b
}

# Main script logic
if [ $# -eq 0 ]; then
    usage
    exit 1
fi

case "$1" in
    -h|--help)
        usage
        exit 0
        ;;
    -l|--list)
        list_services
        exit 0
        ;;
    -i|--install)
        if [ -z "$2" ]; then
            echo "Error: Service name required for install"
            usage
            exit 1
        fi
        install_service "$2"
        exit 0
        ;;
    -u|--uninstall)
        if [ -z "$2" ]; then
            echo "Error: Service name required for uninstall"
            usage
            exit 1
        fi
        uninstall_service "$2"
        exit 0
        ;;
    -U|--uninstall-all)
        uninstall_all_services
        exit 0
        ;;
    -r|--restart)
        if [ -z "$2" ]; then
            echo "Error: Service name required for restart"
            usage
            exit 1
        fi
        restart_service "$2"
        exit 0
        ;;
    -f|--follow)
        if [ -z "$2" ]; then
            echo "Error: Service name required for follow"
            usage
            exit 1
        fi
        follow_logs "$2"
        exit 0
        ;;
    -b|--logs)
        if [ -z "$2" ]; then
            echo "Error: Service name required for logs"
            usage
            exit 1
        fi
        show_logs "$2"
        exit 0
        ;;
    *)
        echo "Error: Unknown option: $1"
        usage
        exit 1
        ;;
esac
