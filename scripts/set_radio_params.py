#!/usr/bin/env python3
"""
Set radio parameters (SF, BW) for all discovered nodes and the gateway.

Performs reliable discovery validation before making changes, then updates
all nodes before finally updating the gateway (to maintain communication).

Uses majority rule: If >50% of nodes succeed, the gateway is updated.
If <=50% succeed, the gateway is NOT updated to preserve connectivity
with the majority of nodes.

Usage:
    set_radio_params.py --sf 9              # Change SF only
    set_radio_params.py --bw 1              # Change BW only (0=125kHz, 1=250kHz, 2=500kHz)
    set_radio_params.py --sf 9 --bw 1       # Change both
    set_radio_params.py --sf 9 --dry-run    # Show what would be changed
    set_radio_params.py --sf 9 --nodes node1,node2  # Update specific nodes (no discovery)

Options:
    --sf N          Spreading factor (7-12)
    --bw N          Bandwidth code (0=125kHz, 1=250kHz, 2=500kHz)
    --nodes LIST    Comma-separated list of node IDs (skips discovery, uses echo to verify)
    --dry-run       Show what would be changed without making changes
    -g, --gateway   Gateway host (default: $GATEWAY_HOST or localhost)
    -p, --port      Gateway port (default: $GATEWAY_PORT or 5001)
    -r, --retries   Discovery retries per round (default: 30)
    -i, --interval  Seconds between discovery rounds (default: 5)

Exit codes:
    0 - All nodes succeeded, gateway updated (SUCCESS)
    1 - Majority succeeded, gateway updated (PARTIAL SUCCESS)
    2 - Majority failed, gateway NOT updated (PARTIAL FAILURE)
    3 - All nodes failed, gateway NOT updated (FAILURE)
"""

import argparse
import json
import os
import sys
import time
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Bandwidth code to Hz mapping (matches node convention)
BW_NAMES = {0: "125kHz", 1: "250kHz", 2: "500kHz"}

# Node status types
NodeStatus = Literal["PENDING", "SUCCESS", "PARTIAL", "FAILED"]


def http_get(url: str, timeout: float = 15.0) -> dict | None:
    """Make HTTP GET request and return JSON response."""
    try:
        with urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, json.JSONDecodeError) as e:
        print(f"  Error: {e}")
        return None


def http_put_json(url: str, data: dict, timeout: float = 15.0) -> dict | None:
    """Make HTTP PUT request with JSON body and return JSON response."""
    try:
        body = json.dumps(data).encode("utf-8")
        req = Request(url, data=body, method="PUT")
        req.add_header("Content-Type", "application/json")
        with urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, json.JSONDecodeError) as e:
        print(f"  Error: {e}")
        return None


def discover_nodes(gateway_url: str, retries: int = 30) -> list[str] | None:
    """Run discovery and return sorted node list, or None on failure."""
    url = f"{gateway_url}/discover?retries={retries}"
    result = http_get(url, timeout=120.0)  # Discovery can take a while
    if result and "nodes" in result:
        return sorted(result["nodes"])
    return None


def echo_node(gateway_url: str, node_id: str) -> bool:
    """Send echo command to a node and verify response. Returns True on success."""
    # Generate unique data to verify echo
    echo_data = str(int(time.time() * 1000))
    url = f"{gateway_url}/echo/{node_id}?a={echo_data}"
    result = http_get(url, timeout=15.0)
    if result:
        # Check if echoed data matches (response may have 'r', 'data', 'echo', or 'payload' key)
        echoed = result.get("r") or result.get("data") or result.get("echo") or result.get("payload")
        return echoed == echo_data
    return False


def verify_nodes_with_echo(gateway_url: str, nodes: list[str]) -> tuple[list[str], list[str]]:
    """
    Verify all nodes respond to echo command.

    Returns (responding_nodes, failed_nodes).
    """
    responding = []
    failed = []

    print(f"Verifying {len(nodes)} nodes respond to echo...")
    for node in nodes:
        print(f"  {node}: ", end="", flush=True)
        if echo_node(gateway_url, node):
            print("OK")
            responding.append(node)
        else:
            print("FAILED")
            failed.append(node)

    return responding, failed


def validate_discovery(
    gateway_url: str,
    iterations: int = 3,
    interval: float = 5.0,
    retries: int = 30,
) -> list[str] | None:
    """
    Run discovery multiple times and ensure consistent results.

    Returns the confirmed node list, or None if results are inconsistent.
    """
    print(f"Running {iterations} discovery iterations to validate node list...")
    baseline = None

    for i in range(iterations):
        if i > 0:
            print(f"  Waiting {interval}s before next discovery...")
            time.sleep(interval)

        print(f"  Discovery round {i + 1}/{iterations}...", end=" ", flush=True)
        nodes = discover_nodes(gateway_url, retries)

        if nodes is None:
            print("FAILED")
            return None

        print(f"found {len(nodes)} nodes: {', '.join(nodes)}")

        if baseline is None:
            baseline = nodes
        elif nodes != baseline:
            print(f"\nError: Node list changed between rounds!")
            print(f"  Baseline: {baseline}")
            print(f"  Current:  {nodes}")
            missing = set(baseline) - set(nodes)
            extra = set(nodes) - set(baseline)
            if missing:
                print(f"  Missing: {missing}")
            if extra:
                print(f"  Extra: {extra}")
            return None

    print(f"\nDiscovery validated: {len(baseline)} nodes confirmed")
    return baseline


def set_node_param(
    gateway_url: str,
    node_id: str,
    param: str,
    value: int,
) -> bool:
    """Set a parameter on a node via gateway. Returns True on success."""
    url = f"{gateway_url}/setparam/{node_id}?a={param}&a={value}"
    result = http_get(url, timeout=15.0)
    if result and param in result:
        return True
    return False


def get_node_param(
    gateway_url: str,
    node_id: str,
    param: str,
) -> int | None:
    """Get a parameter from a node via gateway. Returns value or None."""
    url = f"{gateway_url}/getparam/{node_id}?a={param}"
    result = http_get(url, timeout=15.0)
    if result and param in result:
        return result[param]
    return None


def set_gateway_param(gateway_url: str, param: str, value: int) -> bool:
    """Set a gateway parameter via HTTP PUT with JSON body. Returns True on success."""
    url = f"{gateway_url}/gateway/param/{param}"
    result = http_put_json(url, {"value": value}, timeout=10.0)
    if result and param in result:
        return True
    return False


def get_gateway_param(gateway_url: str, param: str) -> int | None:
    """Get a gateway parameter. Returns value or None."""
    url = f"{gateway_url}/gateway/param/{param}"
    result = http_get(url, timeout=10.0)
    if result and param in result:
        return result[param]
    return None


def format_param_change(param: str, before: int | None, after: int | None) -> str:
    """Format a parameter change for display."""
    if before is None:
        before_str = "?"
    else:
        before_str = str(before)
    if after is None:
        after_str = "?"
    else:
        after_str = str(after)
    return f"{param}: {before_str}\u2192{after_str}"


def print_report(
    params_to_set: list[tuple[str, int]],
    nodes: list[str],
    node_state: dict[str, dict],
    gateway_before: dict[str, int | None],
    gateway_after: dict[str, int | None],
    gateway_updated: bool,
) -> None:
    """Print the detailed final report."""
    print()
    print("=" * 60)
    print("Radio Parameter Change Report")
    print("=" * 60)

    # Target parameters
    param_desc = ", ".join(
        f"{p}={v}" + (f" ({BW_NAMES[v]})" if p == "bw" else "")
        for p, v in params_to_set
    )
    print(f"Target: {param_desc}")
    print(f"Discovery: {len(nodes)} nodes confirmed ({', '.join(nodes)})")
    print()

    # Node results
    print("Node Results:")
    success_count = 0
    partial_count = 0
    failed_count = 0

    for node in nodes:
        state = node_state[node]
        status = state["status"]
        before = state["before"]
        after = state["after"]

        if status == "SUCCESS":
            success_count += 1
            changes = ", ".join(
                format_param_change(p, before.get(p), after.get(p))
                for p, _ in params_to_set
            )
            print(f"  {node:15} OK ({changes})")
        elif status == "PARTIAL":
            partial_count += 1
            # Show which params succeeded/failed
            details = []
            for p, target_v in params_to_set:
                if after.get(p) == target_v:
                    details.append(f"{p}: {before.get(p, '?')}\u2192{after.get(p, '?')} OK")
                else:
                    details.append(f"{p}: FAILED")
            print(f"  {node:15} PARTIAL ({', '.join(details)})")
        else:  # FAILED
            failed_count += 1
            print(f"  {node:15} FAILED")

    print()

    # Summary
    total = len(nodes)
    success_rate = success_count / total if total > 0 else 0
    print(f"Summary: {success_count}/{total} nodes succeeded ({success_rate:.0%})")
    if partial_count > 0:
        print(f"         {partial_count} nodes partially updated")
    if failed_count > 0:
        print(f"         {failed_count} nodes failed completely")
    print()

    # Gateway decision
    if gateway_updated:
        print(f"Gateway Decision: UPDATED ({success_rate:.0%} > 50% threshold)")
        for p, _ in params_to_set:
            print(f"  {format_param_change(p, gateway_before.get(p), gateway_after.get(p))}")
    else:
        if success_rate > 0:
            print(f"Gateway Decision: NOT UPDATED ({success_rate:.0%} <= 50% threshold)")
        else:
            print("Gateway Decision: NOT UPDATED (all nodes failed)")
        print("  Gateway remains on original settings to maintain connectivity")
    print()

    # Final result
    if success_count == total and gateway_updated:
        print("Result: SUCCESS")
    elif success_count > total / 2 and gateway_updated:
        print("Result: PARTIAL SUCCESS")
        print(f"  - {success_count} nodes on new settings")
        if partial_count > 0:
            print(f"  - {partial_count} nodes on mixed settings")
        if failed_count > 0:
            print(f"  - {failed_count} nodes on original settings (unreachable)")
        print("  - Gateway on new settings")
    elif success_count > 0:
        print("Result: PARTIAL FAILURE")
        print(f"  - {success_count} nodes changed but gateway NOT updated")
        print("  - These nodes may be unreachable until manually reset")
    else:
        print("Result: FAILURE")
        print("  - No nodes were successfully updated")
        print("  - Gateway unchanged, all nodes should still be reachable")

    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Set radio parameters for all nodes and gateway",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--sf", type=int, help="Spreading factor (7-12)")
    parser.add_argument(
        "--bw", type=int, help="Bandwidth (0=125kHz, 1=250kHz, 2=500kHz)"
    )
    parser.add_argument(
        "--nodes", type=str,
        help="Comma-separated list of node IDs (skips discovery, uses echo to verify)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be changed"
    )
    parser.add_argument(
        "-g", "--gateway",
        default=os.environ.get("GATEWAY_HOST", "localhost"),
        help="Gateway host (default: $GATEWAY_HOST or localhost)",
    )
    parser.add_argument(
        "-p", "--port",
        type=int,
        default=int(os.environ.get("GATEWAY_PORT", "5001")),
        help="Gateway port (default: $GATEWAY_PORT or 5001)",
    )
    parser.add_argument(
        "-r", "--retries", type=int, default=30, help="Discovery retries per round"
    )
    parser.add_argument(
        "-i", "--interval", type=float, default=5.0, help="Seconds between discovery rounds"
    )
    args = parser.parse_args()

    # Validate arguments
    if args.sf is None and args.bw is None:
        parser.error("At least one of --sf or --bw is required")

    if args.sf is not None and not (7 <= args.sf <= 12):
        parser.error("SF must be between 7 and 12")

    if args.bw is not None and not (0 <= args.bw <= 2):
        parser.error("BW must be 0 (125kHz), 1 (250kHz), or 2 (500kHz)")

    gateway_url = f"http://{args.gateway}:{args.port}"

    # Build list of params to change
    params_to_set: list[tuple[str, int]] = []
    if args.sf is not None:
        params_to_set.append(("sf", args.sf))
    if args.bw is not None:
        params_to_set.append(("bw", args.bw))

    param_desc = ", ".join(
        f"{p}={v}" + (f" ({BW_NAMES[v]})" if p == "bw" else "")
        for p, v in params_to_set
    )
    print(f"Setting radio parameters: {param_desc}")
    print(f"Gateway: {gateway_url}")
    print()

    # Phase 1: Get node list (either from --nodes or discovery)
    if args.nodes:
        # Parse comma-separated node list
        nodes = sorted([n.strip() for n in args.nodes.split(",") if n.strip()])
        if not nodes:
            print("Error: --nodes list is empty")
            sys.exit(3)

        print(f"Using specified nodes: {', '.join(nodes)}")
        print()

        # Verify all nodes respond to echo
        _, failed = verify_nodes_with_echo(gateway_url, nodes)

        if failed:
            print(f"\nAborted: {len(failed)} node(s) failed to respond to echo: {', '.join(failed)}")
            print("All nodes must respond before updating parameters.")
            sys.exit(3)

        print(f"\nAll {len(nodes)} nodes verified")
    else:
        # Use discovery validation
        nodes = validate_discovery(
            gateway_url,
            iterations=3,
            interval=args.interval,
            retries=args.retries,
        )
        if nodes is None:
            print("\nAborted: Discovery validation failed")
            sys.exit(3)

        if not nodes:
            print("\nNo nodes discovered. Nothing to do.")
            sys.exit(0)

    # Initialize node state tracking
    node_state: dict[str, dict] = {}
    for node in nodes:
        node_state[node] = {
            "before": {},
            "after": {},
            "status": "PENDING",
        }

    # Phase 2: Read current state from all nodes
    print(f"\nReading current parameters from {len(nodes)} nodes...")
    for node in nodes:
        print(f"  {node}: ", end="", flush=True)
        params_read = []
        for param, _ in params_to_set:
            value = get_node_param(gateway_url, node, param)
            node_state[node]["before"][param] = value
            if value is not None:
                params_read.append(f"{param}={value}")
        if params_read:
            print(", ".join(params_read))
        else:
            print("(could not read)")

    # Read gateway current state
    print("\nReading current gateway parameters...")
    gateway_before: dict[str, int | None] = {}
    for param, _ in params_to_set:
        value = get_gateway_param(gateway_url, param)
        gateway_before[param] = value
        print(f"  {param}={value}")

    if args.dry_run:
        print("\n[DRY RUN] Would set the following:")
        for node in nodes:
            before = node_state[node]["before"]
            changes = ", ".join(
                format_param_change(p, before.get(p), v)
                for p, v in params_to_set
            )
            print(f"  Node {node}: {changes}")
        gw_changes = ", ".join(
            format_param_change(p, gateway_before.get(p), v)
            for p, v in params_to_set
        )
        print(f"  Gateway: {gw_changes}")
        sys.exit(0)

    # Phase 3: Update all nodes (never abort early)
    print(f"\nUpdating {len(nodes)} nodes...")

    for node in nodes:
        print(f"  {node}: ", end="", flush=True)
        params_succeeded = []
        params_failed = []

        for param, value in params_to_set:
            if set_node_param(gateway_url, node, param, value):
                # Verify the change
                actual = get_node_param(gateway_url, node, param)
                node_state[node]["after"][param] = actual
                if actual == value:
                    params_succeeded.append(param)
                else:
                    params_failed.append(param)
            else:
                node_state[node]["after"][param] = None
                params_failed.append(param)

        # Determine node status
        if len(params_succeeded) == len(params_to_set):
            node_state[node]["status"] = "SUCCESS"
            print("OK")
        elif len(params_succeeded) > 0:
            node_state[node]["status"] = "PARTIAL"
            print(f"PARTIAL ({', '.join(params_succeeded)} OK, {', '.join(params_failed)} failed)")
        else:
            node_state[node]["status"] = "FAILED"
            print("FAILED")

    # Phase 4: Gateway decision (majority rule)
    success_count = sum(1 for n in node_state.values() if n["status"] == "SUCCESS")
    success_rate = success_count / len(nodes)
    gateway_updated = False
    gateway_after: dict[str, int | None] = {}

    print(f"\nNode results: {success_count}/{len(nodes)} succeeded ({success_rate:.0%})")

    if success_rate > 0.5:
        print(f"\nUpdating gateway ({success_rate:.0%} > 50% threshold)...")
        gateway_success = True

        for param, value in params_to_set:
            print(f"  {param}={value}: ", end="", flush=True)
            if set_gateway_param(gateway_url, param, value):
                # Verify
                actual = get_gateway_param(gateway_url, param)
                gateway_after[param] = actual
                if actual == value:
                    print("OK")
                else:
                    print(f"VERIFY FAILED (expected {value}, got {actual})")
                    gateway_success = False
            else:
                gateway_after[param] = gateway_before.get(param)
                print("FAILED")
                gateway_success = False

        gateway_updated = gateway_success
    else:
        print(f"\nGateway NOT updated ({success_rate:.0%} <= 50% threshold)")
        print("  Keeping gateway on original settings to maintain connectivity")
        gateway_after = gateway_before.copy()

    # Phase 5: Detailed report
    print_report(
        params_to_set,
        nodes,
        node_state,
        gateway_before,
        gateway_after,
        gateway_updated,
    )

    # Exit with appropriate code
    if success_count == len(nodes) and gateway_updated:
        sys.exit(0)  # SUCCESS
    elif success_count > len(nodes) / 2 and gateway_updated:
        sys.exit(1)  # PARTIAL SUCCESS
    elif success_count > 0:
        sys.exit(2)  # PARTIAL FAILURE (some nodes changed, gateway not)
    else:
        sys.exit(3)  # FAILURE


if __name__ == "__main__":
    main()
