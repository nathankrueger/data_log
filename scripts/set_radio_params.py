#!/usr/bin/env python3
"""
Set radio parameters (SF, BW, frequencies) for all discovered nodes and the gateway.

Performs reliable discovery validation before making changes, then updates
all nodes before finally updating the gateway (to maintain communication).

Uses echo-as-ACK verification: After sending rcfg_radio and switching the gateway
to new params, echo commands verify connectivity on the NEW channel. If some nodes
fail, the gateway temporarily reverts to retry rcfg_radio (up to --rcfg-retries times).

Uses majority rule: If >50% of nodes succeed, the gateway is updated.
If <=50% succeed, the gateway is NOT updated to preserve connectivity
with the majority of nodes.

Usage:
    set_radio_params.py --sf 9              # Change SF only
    set_radio_params.py --bw 1              # Change BW only (0=125kHz, 1=250kHz, 2=500kHz)
    set_radio_params.py --sf 9 --bw 1       # Change both
    set_radio_params.py --n2gfreq 915.0     # Change N2G frequency (MHz)
    set_radio_params.py --g2nfreq 915.5     # Change G2N frequency (MHz)
    set_radio_params.py --sf 9 --dry-run    # Show what would be changed
    set_radio_params.py --sf 9 --nodes node1,node2  # Update specific nodes (no discovery)
    set_radio_params.py --sf 9 --no-verify  # Skip echo verification (not recommended)
    set_radio_params.py --sf 9 --rcfg-retries 5  # More retry attempts

Options:
    --sf N            Spreading factor (7-12)
    --bw N            Bandwidth code (0=125kHz, 1=250kHz, 2=500kHz)
    --n2gfreq F       Node-to-Gateway frequency in MHz (902-928)
    --g2nfreq F       Gateway-to-Node frequency in MHz (902-928)
    --nodes LIST      Comma-separated list of node IDs (skips discovery, uses echo to verify)
    --dry-run         Show what would be changed without making changes
    --no-verify       Skip echo verification after rcfg_radio (not recommended)
    --rcfg-retries N  Max retries for rcfg_radio (default: 3)
    -g, --gateway     Gateway host (default: $GATEWAY_HOST or localhost)
    -p, --port        Gateway port (default: $GATEWAY_PORT or 5001)
    -r, --retries     Discovery retries per round (default: 30)
    -i, --interval    Seconds between discovery rounds (default: 5)

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


def format_param_value(param: str, value: int) -> str:
    """Format a parameter value for display."""
    if param == "bw":
        return f"{param}={value} ({BW_NAMES.get(value, '?')})"
    elif param in ("n2gfreq", "g2nfreq"):
        # Display in MHz for readability
        return f"{param}={value / 1e6:.3f}MHz"
    else:
        return f"{param}={value}"

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


def http_post_json(url: str, data: dict, timeout: float = 15.0) -> dict | None:
    """Make HTTP POST request with JSON body and return JSON response."""
    try:
        body = json.dumps(data).encode("utf-8")
        req = Request(url, data=body, method="POST")
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


def send_rcfg_radio(gateway_url: str, node_id: str) -> dict | None:
    """Send rcfg_radio command to apply staged params on a node.

    Uses no_wait=1 for fire-and-forget mode since ACK is unreliable
    after radio params change. Returns immediately with queued status.
    """
    url = f"{gateway_url}/rcfg_radio/{node_id}?no_wait=1"
    return http_get(url, timeout=5.0)


def get_gateway_param(gateway_url: str, param: str) -> int | None:
    """Get a gateway parameter. Returns value or None."""
    url = f"{gateway_url}/gateway/param/{param}"
    result = http_get(url, timeout=10.0)
    if result and param in result:
        return result[param]
    return None


def send_gateway_rcfg_radio(gateway_url: str) -> dict | None:
    """Send rcfg_radio to apply staged radio params on gateway."""
    url = f"{gateway_url}/gateway/rcfg_radio"
    return http_post_json(url, {}, timeout=10.0)


def send_gateway_savecfg(gateway_url: str) -> dict | None:
    """Send savecfg to persist all current params on gateway."""
    url = f"{gateway_url}/gateway/savecfg"
    return http_post_json(url, {}, timeout=10.0)


def format_param_change(param: str, before: int | None, after: int | None) -> str:
    """Format a parameter change for display."""
    # Format value based on param type
    def fmt(v: int | None) -> str:
        if v is None:
            return "?"
        if param in ("n2gfreq", "g2nfreq"):
            return f"{v / 1e6:.3f}MHz"
        return str(v)

    return f"{param}: {fmt(before)}\u2192{fmt(after)}"


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
        format_param_value(p, v) for p, v in params_to_set
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
        "--n2gfreq", type=float, help="Node-to-Gateway frequency in MHz (902-928)"
    )
    parser.add_argument(
        "--g2nfreq", type=float, help="Gateway-to-Node frequency in MHz (902-928)"
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
    parser.add_argument(
        "--no-verify", action="store_true",
        help="Skip echo verification after rcfg_radio (not recommended)"
    )
    parser.add_argument(
        "--rcfg-retries", type=int, default=3,
        help="Max retries for rcfg_radio (switch gateway back, resend, switch forward)"
    )
    args = parser.parse_args()

    # Validate arguments
    if args.sf is None and args.bw is None and args.n2gfreq is None and args.g2nfreq is None:
        parser.error("At least one of --sf, --bw, --n2gfreq, or --g2nfreq is required")

    if args.sf is not None and not (7 <= args.sf <= 12):
        parser.error("SF must be between 7 and 12")

    if args.bw is not None and not (0 <= args.bw <= 2):
        parser.error("BW must be 0 (125kHz), 1 (250kHz), or 2 (500kHz)")

    if args.n2gfreq is not None and not (902.0 <= args.n2gfreq <= 928.0):
        parser.error("N2G frequency must be between 902 and 928 MHz")

    if args.g2nfreq is not None and not (902.0 <= args.g2nfreq <= 928.0):
        parser.error("G2N frequency must be between 902 and 928 MHz")

    gateway_url = f"http://{args.gateway}:{args.port}"

    # Build list of params to change
    # Note: frequencies are passed to user in MHz but sent to nodes as Hz
    params_to_set: list[tuple[str, int]] = []
    if args.sf is not None:
        params_to_set.append(("sf", args.sf))
    if args.bw is not None:
        params_to_set.append(("bw", args.bw))
    if args.n2gfreq is not None:
        params_to_set.append(("n2gfreq", int(args.n2gfreq * 1e6)))
    if args.g2nfreq is not None:
        params_to_set.append(("g2nfreq", int(args.g2nfreq * 1e6)))

    param_desc = ", ".join(
        format_param_value(p, v) for p, v in params_to_set
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

    # Phase 3: Stage params on all nodes (setparam stores in pending, not applied)
    print(f"\nStaging params on {len(nodes)} nodes...")

    for node in nodes:
        print(f"  {node}: ", end="", flush=True)
        params_succeeded = []
        params_failed = []

        for param, value in params_to_set:
            if set_node_param(gateway_url, node, param, value):
                # Verify the staged value
                actual = get_node_param(gateway_url, node, param)
                node_state[node]["after"][param] = actual
                if actual == value:
                    params_succeeded.append(param)
                else:
                    params_failed.append(param)
            else:
                node_state[node]["after"][param] = None
                params_failed.append(param)

        # Track staging result (not final status yet)
        if len(params_succeeded) == len(params_to_set):
            print("staged")
        elif len(params_succeeded) > 0:
            print(f"partial ({', '.join(params_succeeded)} OK, {', '.join(params_failed)} failed)")
            node_state[node]["status"] = "PARTIAL"
        else:
            print("FAILED")
            node_state[node]["status"] = "FAILED"

    # Phase 4: Apply rcfg_radio with echo-as-ACK verification
    # Uses echo on the NEW channel as implicit confirmation that rcfg_radio worked.
    # If some nodes fail, temporarily switch gateway back to retry.

    # Track which nodes need rcfg_radio (exclude staging failures)
    nodes_needing_rcfg = [n for n in nodes if node_state[n]["status"] != "FAILED"]
    max_attempts = args.rcfg_retries + 1  # +1 for initial attempt

    print(f"\nApplying radio config changes (max {max_attempts} attempts)...")

    for attempt in range(1, max_attempts + 1):
        if not nodes_needing_rcfg:
            break

        print(f"\n--- Attempt {attempt}/{max_attempts} ---")

        # Step A: Send rcfg_radio to nodes (fire-and-forget, ACK often lost)
        print(f"Sending rcfg_radio to {len(nodes_needing_rcfg)} node(s)...")
        for node in nodes_needing_rcfg:
            print(f"  {node}: ", end="", flush=True)
            send_rcfg_radio(gateway_url, node)  # Ignore response - ACK often lost
            print("sent")

        # Wait for gateway to finish sending rcfg_radio commands
        # Each command takes ~1.5s (2 retries with 500ms + 750ms backoff)
        delay = 2.0 * len(nodes_needing_rcfg)
        print(f"Waiting {delay:.0f}s for commands to complete...")
        time.sleep(delay)

        # Step B: Switch gateway to new params
        print("\nSwitching gateway to new params...")
        for param, value in params_to_set:
            print(f"  {param}={value}: ", end="", flush=True)
            if set_gateway_param(gateway_url, param, value):
                print("staged")
            else:
                print("FAILED")
        result = send_gateway_rcfg_radio(gateway_url)
        if result and "r" in result:
            print(f"  rcfg_radio: OK ({result['r']})")
        else:
            print(f"  rcfg_radio: applied")

        # Step C: Echo test on new channel (skip if --no-verify)
        if args.no_verify:
            print("\nSkipping verification (--no-verify)")
            for node in nodes_needing_rcfg:
                node_state[node]["status"] = "SUCCESS"
            nodes_needing_rcfg = []
            break

        print(f"\nVerifying {len(nodes_needing_rcfg)} node(s) on new channel...")
        newly_verified = []
        still_failing = []

        for node in nodes_needing_rcfg:
            print(f"  {node}: ", end="", flush=True)
            if echo_node(gateway_url, node):
                print("OK")
                node_state[node]["status"] = "SUCCESS"
                newly_verified.append(node)
            else:
                print("FAILED")
                still_failing.append(node)

        nodes_needing_rcfg = still_failing

        if not nodes_needing_rcfg:
            print("\nAll nodes verified!")
            break

        if attempt < max_attempts:
            # Step D: Switch gateway BACK to old params for retry
            print(f"\n{len(nodes_needing_rcfg)} node(s) failed, switching gateway back...")
            for param, _ in params_to_set:
                old_value = gateway_before[param]
                if old_value is not None:
                    set_gateway_param(gateway_url, param, old_value)
            send_gateway_rcfg_radio(gateway_url)
            print("Gateway reverted, will retry rcfg_radio...")

    # Mark remaining nodes as failed
    for node in nodes_needing_rcfg:
        node_state[node]["status"] = "FAILED"

    if nodes_needing_rcfg:
        print(f"\n{len(nodes_needing_rcfg)} node(s) failed after {max_attempts} attempts")

    # Phase 5: Gateway decision (majority rule)
    # Gateway is currently on new params from Phase 4
    success_count = sum(1 for n in node_state.values() if n["status"] == "SUCCESS")
    success_rate = success_count / len(nodes)
    gateway_updated = False
    gateway_after: dict[str, int | None] = {}

    print(f"\nNode results: {success_count}/{len(nodes)} succeeded ({success_rate:.0%})")

    if success_rate > 0.5:
        # Keep gateway on new settings, persist to config
        gateway_updated = True
        print(f"\nGateway kept on new settings ({success_rate:.0%} > 50%)")
        print("  Persisting (savecfg)...", end=" ", flush=True)
        result = send_gateway_savecfg(gateway_url)
        if result and "r" in result:
            print(f"OK ({result['r']})")
        else:
            print("OK")
        # Read back values after apply
        for param, _ in params_to_set:
            gateway_after[param] = get_gateway_param(gateway_url, param)
    else:
        # Revert gateway to old params (majority failed)
        print(f"\nReverting gateway ({success_rate:.0%} <= 50%)")
        for param, _ in params_to_set:
            old_value = gateway_before[param]
            if old_value is not None:
                set_gateway_param(gateway_url, param, old_value)
        send_gateway_rcfg_radio(gateway_url)
        gateway_after = gateway_before.copy()
        print("  Keeping gateway on original settings to maintain connectivity")

    # Phase 6: Detailed report
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
