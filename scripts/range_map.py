#!/usr/bin/env python3
"""
Generate an interactive HTML map from LoRa range test CSV files.

Reads CSV files produced by range_test.py and plots each GPS coordinate
as a colored circle on an OpenStreetMap tile layer.  Color encodes RSSI
signal strength: green = strong, yellow = moderate, red = weak.

Usage:
    python3 range_map.py results.csv                           # Auto-name output
    python3 range_map.py results.csv -o my_map.html            # Custom output
    python3 range_map.py results.csv --rssi node               # Color by node RSSI
    python3 range_map.py results.csv --min-rssi -120 --max-rssi -30
    python3 range_map.py results.csv --radius 10 --title "Walk Test #3"
"""

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path

import folium
from branca.element import MacroElement, Template


@dataclass
class RangePoint:
    """A single range test measurement with valid GPS coordinates."""
    timestamp: str
    seq: int
    gateway_rssi: int | None
    node_rssi: int | None
    latitude: float
    longitude: float
    altitude: str
    satellites: str
    ack: bool
    round_trip_ms: str


def parse_csv(csv_path: str) -> list[RangePoint]:
    """Parse range test CSV file into a list of valid GPS points.

    Filters out rows with missing or invalid GPS coordinates,
    including the (0.0, 0.0) sentinel used when the node has no GPS fix.
    """
    points = []
    total = 0

    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            total += 1
            try:
                lat = float(row["latitude"])
                lon = float(row["longitude"])
            except (ValueError, TypeError):
                continue

            if lat == 0.0 and lon == 0.0:
                continue

            try:
                gw_rssi = int(row["gateway_rssi"]) if row["gateway_rssi"] else None
            except (ValueError, TypeError):
                gw_rssi = None

            try:
                node_rssi = int(float(row["node_rssi"])) if row["node_rssi"] else None
            except (ValueError, TypeError):
                node_rssi = None

            points.append(RangePoint(
                timestamp=row.get("timestamp", ""),
                seq=int(row.get("seq", 0)),
                gateway_rssi=gw_rssi,
                node_rssi=node_rssi,
                latitude=lat,
                longitude=lon,
                altitude=row.get("altitude", ""),
                satellites=row.get("satellites", ""),
                ack=row.get("ack", "").strip() == "True",
                round_trip_ms=row.get("round_trip_ms", ""),
            ))

    print(f"  {len(points)}/{total} rows have valid GPS coordinates")
    return points


def rssi_to_color(rssi: int, min_rssi: int, max_rssi: int) -> str:
    """Convert RSSI to a hex color on a green-yellow-red gradient.

    Green (#00cc00) = strong signal (near max_rssi)
    Yellow (#cccc00) = moderate
    Red (#cc0000) = weak signal (near min_rssi)
    """
    t = (rssi - min_rssi) / (max_rssi - min_rssi)
    t = max(0.0, min(1.0, t))

    if t < 0.5:
        # Red to yellow
        s = t / 0.5
        r, g, b = 204, int(204 * s), 0
    else:
        # Yellow to green
        s = (t - 0.5) / 0.5
        r, g, b = int(204 * (1 - s)), 204, 0

    return f"#{r:02x}{g:02x}{b:02x}"


def build_popup_html(point: RangePoint) -> str:
    """Build HTML table for a marker popup."""
    gw = f"{point.gateway_rssi} dBm" if point.gateway_rssi is not None else "--"
    nd = f"{point.node_rssi} dBm" if point.node_rssi is not None else "--"
    alt = f"{float(point.altitude):.1f} m" if point.altitude else "--"
    sats = point.satellites if point.satellites else "--"
    ack = "Yes" if point.ack else "No"
    rtt = f"{point.round_trip_ms} ms" if point.round_trip_ms else "--"

    return (
        '<table style="font-size:12px; border-collapse:collapse;">'
        f"<tr><td><b>Time</b></td><td style='padding-left:8px'>{point.timestamp}</td></tr>"
        f"<tr><td><b>Seq</b></td><td style='padding-left:8px'>{point.seq}</td></tr>"
        f"<tr><td><b>GW RSSI</b></td><td style='padding-left:8px'>{gw}</td></tr>"
        f"<tr><td><b>Node RSSI</b></td><td style='padding-left:8px'>{nd}</td></tr>"
        f"<tr><td><b>Lat</b></td><td style='padding-left:8px'>{point.latitude:.6f}</td></tr>"
        f"<tr><td><b>Lon</b></td><td style='padding-left:8px'>{point.longitude:.6f}</td></tr>"
        f"<tr><td><b>Alt</b></td><td style='padding-left:8px'>{alt}</td></tr>"
        f"<tr><td><b>Sats</b></td><td style='padding-left:8px'>{sats}</td></tr>"
        f"<tr><td><b>ACK</b></td><td style='padding-left:8px'>{ack}</td></tr>"
        f"<tr><td><b>RTT</b></td><td style='padding-left:8px'>{rtt}</td></tr>"
        "</table>"
    )


class ColorLegend(MacroElement):
    """Floating RSSI color-scale legend for the map."""

    _template = Template("""
    {% macro header(this, kwargs) %}
    <style>
        .rssi-legend {
            position: fixed;
            bottom: 30px;
            right: 10px;
            z-index: 1000;
            background: white;
            padding: 10px;
            border-radius: 5px;
            box-shadow: 0 0 15px rgba(0,0,0,0.2);
            font-family: Arial, sans-serif;
            font-size: 12px;
        }
        .rssi-legend .gradient-bar {
            width: 20px;
            height: 120px;
            background: linear-gradient(to bottom, #00cc00, #cccc00, #cc0000);
            display: inline-block;
            vertical-align: top;
            border: 1px solid #999;
        }
        .rssi-legend .labels {
            display: inline-block;
            vertical-align: top;
            margin-left: 5px;
            height: 120px;
            position: relative;
        }
        .rssi-legend .labels span {
            position: absolute;
            white-space: nowrap;
        }
        .rssi-legend .labels .top { top: 0; }
        .rssi-legend .labels .mid { top: 50%; transform: translateY(-50%); }
        .rssi-legend .labels .bot { bottom: 0; }
        .rssi-legend .title {
            text-align: center;
            font-weight: bold;
            margin-bottom: 5px;
        }
    </style>
    {% endmacro %}

    {% macro html(this, kwargs) %}
    <div class="rssi-legend">
        <div class="title">RSSI (dBm)</div>
        <div>
            <div class="gradient-bar"></div>
            <div class="labels">
                <span class="top">{{ this.max_rssi }}</span>
                <span class="mid">{{ this.mid_rssi }}</span>
                <span class="bot">{{ this.min_rssi }}</span>
            </div>
        </div>
    </div>
    {% endmacro %}
    """)

    def __init__(self, min_rssi: int, max_rssi: int):
        super().__init__()
        self.min_rssi = min_rssi
        self.max_rssi = max_rssi
        self.mid_rssi = (min_rssi + max_rssi) // 2


def generate_map(
    points: list[RangePoint],
    rssi_column: str = "gateway",
    min_rssi: int | None = None,
    max_rssi: int | None = None,
    radius: int = 8,
    title: str | None = None,
) -> folium.Map:
    """Build a Folium map with RSSI-colored markers for each point."""

    # Filter to points that have RSSI for the chosen column
    def get_rssi(p: RangePoint) -> int | None:
        return p.gateway_rssi if rssi_column == "gateway" else p.node_rssi

    filtered = [p for p in points if get_rssi(p) is not None]
    if not filtered:
        raise ValueError(
            f"No points have valid '{rssi_column}' RSSI values. "
            f"Try --rssi {'node' if rssi_column == 'gateway' else 'gateway'}"
        )

    rssi_values = [get_rssi(p) for p in filtered]

    if min_rssi is None:
        min_rssi = min(rssi_values)
    if max_rssi is None:
        max_rssi = max(rssi_values)
    if min_rssi == max_rssi:
        min_rssi -= 1
        max_rssi += 1

    print(f"  RSSI range: {min_rssi} to {max_rssi} dBm ({rssi_column})")
    print(f"  Plotting {len(filtered)} points")

    center_lat = sum(p.latitude for p in filtered) / len(filtered)
    center_lon = sum(p.longitude for p in filtered) / len(filtered)

    m = folium.Map(location=[center_lat, center_lon], tiles="OpenStreetMap")

    sw = [min(p.latitude for p in filtered), min(p.longitude for p in filtered)]
    ne = [max(p.latitude for p in filtered), max(p.longitude for p in filtered)]
    m.fit_bounds([sw, ne], padding=[20, 20])

    for point in filtered:
        rssi = get_rssi(point)
        color = rssi_to_color(rssi, min_rssi, max_rssi)

        folium.CircleMarker(
            location=[point.latitude, point.longitude],
            radius=radius,
            color=color,
            fill=True,
            fill_color=color,
            fill_opacity=0.8,
            weight=1,
            popup=folium.Popup(build_popup_html(point), max_width=250),
        ).add_to(m)

    m.get_root().add_child(ColorLegend(min_rssi, max_rssi))

    if title:
        m.get_root().html.add_child(
            folium.Element(f"<title>{title}</title>")
        )

    return m


def generate_map_from_csv(
    csv_path: str,
    output_path: str | None = None,
    rssi_column: str = "gateway",
    min_rssi: int | None = None,
    max_rssi: int | None = None,
    radius: int = 8,
    title: str | None = None,
) -> str:
    """Generate an HTML map from a range test CSV file.

    Convenience function for programmatic use (e.g. from range_test.py).
    Returns the path to the generated HTML file.
    """
    if output_path is None:
        output_path = str(Path(csv_path).with_suffix(".html"))

    points = parse_csv(csv_path)
    if not points:
        raise ValueError(f"No valid GPS points found in {csv_path}")

    m = generate_map(points, rssi_column, min_rssi, max_rssi, radius, title)
    m.save(output_path)
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate an interactive HTML map from LoRa range test CSV files"
    )
    parser.add_argument(
        "csv_file",
        help="Path to the range test CSV file",
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help="Output HTML file path (default: same as CSV with .html extension)",
    )
    parser.add_argument(
        "--rssi", choices=["gateway", "node"], default="gateway",
        help="RSSI column for coloring: 'gateway' or 'node' (default: gateway)",
    )
    parser.add_argument(
        "--min-rssi", type=int, default=None,
        help="Lower bound of color scale in dBm (default: auto from data)",
    )
    parser.add_argument(
        "--max-rssi", type=int, default=None,
        help="Upper bound of color scale in dBm (default: auto from data)",
    )
    parser.add_argument(
        "--radius", type=int, default=8,
        help="Marker radius in pixels (default: 8)",
    )
    parser.add_argument(
        "--title", default=None,
        help="Map title shown in the HTML page",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    csv_path = Path(args.csv_file)
    if not csv_path.exists():
        print(f"Error: CSV file not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    output_path = args.output or str(csv_path.with_suffix(".html"))

    print(f"Reading {csv_path}...")
    points = parse_csv(str(csv_path))

    if not points:
        print("Error: No valid GPS points found in CSV", file=sys.stderr)
        sys.exit(1)

    m = generate_map(
        points=points,
        rssi_column=args.rssi,
        min_rssi=args.min_rssi,
        max_rssi=args.max_rssi,
        radius=args.radius,
        title=args.title,
    )

    m.save(output_path)
    print(f"Map saved to {output_path}")


if __name__ == "__main__":
    main()
