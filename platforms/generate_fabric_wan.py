#!/usr/bin/env python3
"""Generate the FABRIC WAN skeleton for a SimGrid JSON platform.

The generated JSON is compatible with:
    https://github.com/frs69wq/SimGrid-JSON-platform-loader

This script creates only the top-level WAN layer:
  * one empty ``facility`` per selected FABRIC site
  * one SimGrid link per physical FABRIC inter-site link
  * one symmetric route per connected pair of selected facilities

A second script can later populate each facility's ``clusters``,
``storage_systems``, internal ``links``, and internal ``routes`` arrays.

Input modes
-----------
1. ``--live`` queries FABRIC through FABlib.
2. ``--input-snapshot`` reads a previously captured JSON snapshot.

A snapshot is recommended for reproducible experiments.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import heapq
import json
import math
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable, Sequence


@dataclass(frozen=True)
class PhysicalLink:
    """Normalized FABRIC inter-site link."""

    original_name: str
    simgrid_name: str
    endpoints: tuple[str, ...]
    bandwidth_gbps: float
    latency_ms: float
    layer: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a loader-compatible FABRIC WAN skeleton containing facilities, "
            "physical links, and inter-facility routes."
        )
    )

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--live",
        action="store_true",
        help="Query current FABRIC sites and links using FABlib.",
    )
    source.add_argument(
        "--input-snapshot",
        type=Path,
        help="Read sites and links from a previously captured FABRIC snapshot.",
    )

    parser.add_argument(
        "--sites",
        nargs="+",
        help="FABRIC site names to include. Omit to include every available site.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("fabric_wan.json"),
        help="Output WAN skeleton (default: fabric_wan.json).",
    )
    parser.add_argument(
        "--snapshot-out",
        type=Path,
        default=Path("fabric_wan_snapshot.json"),
        help="Snapshot written in live mode (default: fabric_wan_snapshot.json).",
    )
    parser.add_argument(
        "--manifest-out",
        type=Path,
        default=Path("fabric_wan_manifest.json"),
        help="Generation manifest (default: fabric_wan_manifest.json).",
    )
    parser.add_argument(
        "--latency-overrides",
        type=Path,
        help=(
            "Optional JSON object mapping FABRIC link names to measured latency in ms, "
            'for example: {"LINK_A": 3.2}.'
        ),
    )
    parser.add_argument(
        "--default-latency-ms",
        type=positive_float,
        default=10.0,
        help="Fallback link latency when no measurement is supplied (default: 10 ms).",
    )
    parser.add_argument(
        "--default-bandwidth-gbps",
        type=positive_float,
        default=100.0,
        help="Fallback link capacity when FABRIC omits bandwidth (default: 100 Gbps).",
    )
    parser.add_argument(
        "--route-metric",
        choices=("latency", "hops"),
        default="latency",
        help="Choose shortest routes by cumulative latency or hop count (default: latency).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Fail instead of using fallback bandwidth, skipping malformed links, or "
            "leaving selected site pairs disconnected."
        ),
    )

    return parser.parse_args()


def positive_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("value must be a finite number greater than zero")
    return value


def decode_fablib_output(value: Any) -> list[dict[str, Any]]:
    """Normalize FABlib JSON, list, dictionary, or DataFrame output."""

    if value is None:
        return []

    parsed = json.loads(value) if isinstance(value, str) else value

    if hasattr(parsed, "to_dict"):
        # pandas.DataFrame returned by some FABlib configurations
        return [dict(row) for row in parsed.to_dict(orient="records")]

    if isinstance(parsed, list):
        return [dict(row) for row in parsed]

    if isinstance(parsed, dict):
        for key in ("data", "rows", "sites", "links"):
            rows = parsed.get(key)
            if isinstance(rows, list):
                return [dict(row) for row in rows]
        return [dict(parsed)]

    raise TypeError(f"Unsupported FABlib output type: {type(parsed).__name__}")


def query_live_fabric() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Query current FABRIC resource summaries."""

    try:
        from fabrictestbed_extensions.fablib.fablib import FablibManager
    except ImportError as exc:
        raise RuntimeError(
            "FABlib is not installed. Install 'fabrictestbed-extensions' in the "
            "FABRIC environment, or use --input-snapshot."
        ) from exc

    fablib = FablibManager()
    sites = fablib.list_sites(
        output="json",
        quiet=True,
        pretty_names=False,
        update=True,
    )
    links = fablib.list_links(
        output="json",
        quiet=True,
        pretty_names=False,
        update=True,
    )
    return decode_fablib_output(sites), decode_fablib_output(links)


def load_snapshot(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load a FABRIC snapshot containing ``sites`` and ``links`` arrays."""

    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    sites = payload.get("sites")
    links = payload.get("links")
    if not isinstance(sites, list) or not isinstance(links, list):
        raise ValueError("Snapshot must contain list fields named 'sites' and 'links'.")

    return [dict(row) for row in sites], [dict(row) for row in links]


def save_snapshot(
    path: Path,
    sites: list[dict[str, Any]],
    links: list[dict[str, Any]],
) -> None:
    """Save the raw live query so future runs can be reproduced offline."""

    payload = {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "FABlib site/link resource query",
        "sites": sites,
        "links": links,
    }
    write_json(path, payload, sort_keys=True)


def first_present(record: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def extract_site_name(record: dict[str, Any]) -> str | None:
    value = first_present(record, ("name", "Name", "site", "Site"))
    if value is None:
        return None
    name = str(value).strip()
    return name or None


def parse_endpoint_list(value: Any) -> tuple[str, ...]:
    """Parse the FABRIC link ``sites`` field across common encodings."""

    if value is None:
        return ()

    if isinstance(value, dict):
        candidates: Iterable[Any] = value.values()
    elif isinstance(value, (list, tuple, set)):
        candidates = value
    elif isinstance(value, str):
        text = value.strip()
        parsed: Any = None
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                try:
                    parsed = ast.literal_eval(text)
                except (SyntaxError, ValueError):
                    parsed = None

        if isinstance(parsed, dict):
            candidates = parsed.values()
        elif isinstance(parsed, (list, tuple, set)):
            candidates = parsed
        else:
            cleaned = text.strip("[](){}")
            candidates = re.split(r"\s*(?:,|->|--|\||;)\s*", cleaned)
    else:
        candidates = [value]

    endpoints: list[str] = []
    for candidate in candidates:
        endpoint = str(candidate).strip().strip("'\"")
        if endpoint and endpoint not in endpoints:
            endpoints.append(endpoint)
    return tuple(endpoints)


def parse_bandwidth_gbps(value: Any) -> float | None:
    """Convert a bandwidth value into Gbps."""

    if value is None:
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) and numeric > 0 else None

    text = str(value).strip().lower().replace(" ", "")
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([a-z/]+)?", text)
    if not match:
        return None

    number = float(match.group(1))
    unit = match.group(2) or "gbps"
    factor = {
        "bps": 1e-9,
        "kbps": 1e-6,
        "mbps": 1e-3,
        "gbps": 1.0,
        "tbps": 1e3,
        "gbit/s": 1.0,
        "gb/s": 1.0,
    }.get(unit)
    return number * factor if factor is not None else None


def make_unique_link_name(original: str, used: set[str]) -> str:
    """Create a globally unique SimGrid-safe link name."""

    base = re.sub(r"[^A-Za-z0-9_]+", "_", original).strip("_")
    base = re.sub(r"_+", "_", base) or "link"
    candidate = f"fabric_{base}"

    if candidate not in used:
        used.add(candidate)
        return candidate

    digest = hashlib.sha1(original.encode("utf-8")).hexdigest()[:8]
    candidate = f"fabric_{base}_{digest}"
    suffix = 2
    while candidate in used:
        candidate = f"fabric_{base}_{digest}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def load_latency_overrides(path: Path | None) -> dict[str, float]:
    if path is None:
        return {}

    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("Latency override file must be a JSON object.")

    result: dict[str, float] = {}
    for key, raw_value in payload.items():
        value = float(raw_value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"Latency override for '{key}' must be greater than zero.")
        result[str(key)] = value
    return result


def normalize_links(
    raw_links: list[dict[str, Any]],
    known_sites: set[str],
    latency_overrides: dict[str, float],
    default_latency_ms: float,
    default_bandwidth_gbps: float,
    strict: bool,
) -> tuple[list[PhysicalLink], list[str], list[str], list[str]]:
    """Filter and normalize the physical links relevant to selected sites."""

    normalized: list[PhysicalLink] = []
    warnings: list[str] = []
    fallback_bandwidth_links: list[str] = []
    skipped_links: list[str] = []
    used_names: set[str] = set()

    for index, record in enumerate(raw_links):
        raw_name = first_present(record, ("name", "Name", "link_name", "Link Name"))
        original_name = str(raw_name or f"unnamed_link_{index}")

        raw_endpoints = first_present(
            record,
            ("sites", "Sites", "endpoints", "Endpoints"),
        )
        all_endpoints = parse_endpoint_list(raw_endpoints)
        endpoints = tuple(endpoint for endpoint in all_endpoints if endpoint in known_sites)

        if len(endpoints) < 2:
            message = f"Skipping malformed link '{original_name}': fewer than two known endpoints."
            if strict:
                raise ValueError(message)
            warnings.append(message)
            skipped_links.append(original_name)
            continue

        raw_bandwidth = first_present(
            record,
            (
                "bandwidth",
                "Bandwidth",
                "capacity",
                "Capacity",
                "Capacity (Gbps)",
            ),
        )
        bandwidth = parse_bandwidth_gbps(raw_bandwidth)
        if bandwidth is None:
            message = (
                f"Link '{original_name}' has no parseable bandwidth; using "
                f"{default_bandwidth_gbps:g} Gbps."
            )
            if strict:
                raise ValueError(message)
            warnings.append(message)
            fallback_bandwidth_links.append(original_name)
            bandwidth = default_bandwidth_gbps

        layer = first_present(record, ("layer", "Layer"))
        latency = latency_overrides.get(original_name, default_latency_ms)

        normalized.append(
            PhysicalLink(
                original_name=original_name,
                simgrid_name=make_unique_link_name(original_name, used_names),
                endpoints=endpoints,
                bandwidth_gbps=bandwidth,
                latency_ms=latency,
                layer=str(layer) if layer is not None else None,
            )
        )

    normalized.sort(key=lambda link: link.simgrid_name)
    return normalized, warnings, fallback_bandwidth_links, skipped_links


def build_graph(
    links: Sequence[PhysicalLink],
    route_metric: str,
) -> dict[str, list[tuple[str, str, float]]]:
    """Build an undirected site graph from FABRIC physical links."""

    graph: dict[str, list[tuple[str, str, float]]] = {}
    for link in links:
        cost = link.latency_ms if route_metric == "latency" else 1.0
        for left, right in combinations(link.endpoints, 2):
            graph.setdefault(left, []).append((right, link.simgrid_name, cost))
            graph.setdefault(right, []).append((left, link.simgrid_name, cost))

    for neighbors in graph.values():
        neighbors.sort(key=lambda item: (item[0], item[1]))
    return graph


def shortest_link_path(
    graph: dict[str, list[tuple[str, str, float]]],
    source: str,
    destination: str,
) -> list[str] | None:
    """Return the deterministic least-cost list of physical link names."""

    # Heap entries include the path tuple to make equal-cost choices deterministic.
    queue: list[tuple[float, tuple[str, ...], str]] = [(0.0, (), source)]
    best: dict[str, tuple[float, tuple[str, ...]]] = {source: (0.0, ())}

    while queue:
        cost, path, node = heapq.heappop(queue)
        if best.get(node) != (cost, path):
            continue
        if node == destination:
            return list(path)

        for neighbor, link_name, edge_cost in graph.get(node, []):
            next_cost = cost + edge_cost
            next_path = path + (link_name,)
            current = best.get(neighbor)
            if current is None or (next_cost, next_path) < current:
                best[neighbor] = (next_cost, next_path)
                heapq.heappush(queue, (next_cost, next_path, neighbor))

    return None


def format_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:g}"


def generate_skeleton(
    selected_sites: Sequence[str],
    links: Sequence[PhysicalLink],
    route_metric: str,
    strict: bool,
) -> tuple[
    dict[str, Any],
    list[str],
    list[PhysicalLink],
    list[PhysicalLink],
    list[str],
]:
    """Create the loader-compatible WAN skeleton."""

    graph = build_graph(links, route_metric)
    routes: list[dict[str, Any]] = []
    disconnected_pairs: list[str] = []
    used_link_names: set[str] = set()

    for source, destination in combinations(sorted(selected_sites), 2):
        path = shortest_link_path(graph, source, destination)
        if path is None:
            disconnected_pairs.append(f"{source} <-> {destination}")
            continue
        routes.append({"src": source, "dst": destination, "links": path})
        used_link_names.update(path)

    if strict and disconnected_pairs:
        raise ValueError(
            "Selected facilities are disconnected: " + ", ".join(disconnected_pairs)
        )

    facilities = [
        {
            "name": site,
            "storage_systems": [],
            "clusters": [],
            "links": [],
            "routes": [],
        }
        for site in sorted(selected_sites)
    ]

    selected_site_set = set(selected_sites)
    used_links = [link for link in links if link.simgrid_name in used_link_names]
    included_links = [
        link
        for link in links
        if link.simgrid_name in used_link_names
        or len(selected_site_set.intersection(link.endpoints)) >= 2
    ]
    transit_sites = sorted(
        {
            endpoint
            for link in used_links
            for endpoint in link.endpoints
            if endpoint not in selected_site_set
        }
    )

    platform_links = [
        {
            "name": link.simgrid_name,
            "bandwidth": f"{format_number(link.bandwidth_gbps)}Gbps",
            "latency": f"{format_number(link.latency_ms)}ms",
        }
        for link in included_links
    ]

    skeleton = {
        "facilities": facilities,
        "storage_systems": [],
        "links": platform_links,
        "routes": routes,
        "filesystems": [],
    }
    validate_skeleton(skeleton)
    return skeleton, disconnected_pairs, included_links, used_links, transit_sites


def validate_skeleton(platform: dict[str, Any]) -> None:
    """Validate names and references required by the JSON platform loader."""

    required_arrays = (
        "facilities",
        "storage_systems",
        "links",
        "routes",
        "filesystems",
    )
    for key in required_arrays:
        if not isinstance(platform.get(key), list):
            raise ValueError(f"Top-level field '{key}' must be a list.")

    facility_names = [facility.get("name") for facility in platform["facilities"]]
    if any(not isinstance(name, str) or not name for name in facility_names):
        raise ValueError("Every facility must have a non-empty string name.")
    if len(facility_names) != len(set(facility_names)):
        raise ValueError("Facility names must be globally unique.")

    link_names = [link.get("name") for link in platform["links"]]
    if any(not isinstance(name, str) or not name for name in link_names):
        raise ValueError("Every physical link must have a non-empty string name.")
    if len(link_names) != len(set(link_names)):
        raise ValueError("Physical link names must be globally unique.")

    facility_set = set(facility_names)
    link_set = set(link_names)
    route_pairs: set[tuple[str, str]] = set()

    for route in platform["routes"]:
        source = route.get("src")
        destination = route.get("dst")
        route_links = route.get("links")

        if source not in facility_set or destination not in facility_set:
            raise ValueError(
                f"Route endpoint is not a facility: {source!r} -> {destination!r}."
            )
        if source == destination:
            raise ValueError(f"Self-route is not allowed for facility '{source}'.")
        if not isinstance(route_links, list) or not route_links:
            raise ValueError(f"Route {source} -> {destination} has no links.")
        unknown_links = [name for name in route_links if name not in link_set]
        if unknown_links:
            raise ValueError(
                f"Route {source} -> {destination} references unknown links: "
                + ", ".join(unknown_links)
            )

        pair = tuple(sorted((source, destination)))
        if pair in route_pairs:
            raise ValueError(f"Duplicate facility route for {pair[0]} <-> {pair[1]}.")
        route_pairs.add(pair)


def write_json(path: Path, payload: Any, *, sort_keys: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=sort_keys) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()

    try:
        if args.live:
            raw_sites, raw_links = query_live_fabric()
            save_snapshot(args.snapshot_out, raw_sites, raw_links)
            source_description = str(args.snapshot_out)
        else:
            raw_sites, raw_links = load_snapshot(args.input_snapshot)
            source_description = str(args.input_snapshot)

        available_sites = sorted(
            {
                name
                for record in raw_sites
                if (name := extract_site_name(record)) is not None
            }
        )
        if not available_sites:
            raise ValueError("No FABRIC sites were found in the selected source.")

        if args.sites:
            selected_sites = list(dict.fromkeys(args.sites))
            unknown_sites = sorted(set(selected_sites) - set(available_sites))
            if unknown_sites:
                raise ValueError(
                    "Unknown FABRIC site(s): "
                    + ", ".join(unknown_sites)
                    + ". Available sites: "
                    + ", ".join(available_sites)
                )
        else:
            selected_sites = available_sites

        if len(selected_sites) < 2:
            raise ValueError("Select at least two FABRIC sites to build a WAN.")

        latency_overrides = load_latency_overrides(args.latency_overrides)
        links, warnings, fallback_bandwidth_links, skipped_links = normalize_links(
            raw_links=raw_links,
            known_sites=set(available_sites),
            latency_overrides=latency_overrides,
            default_latency_ms=args.default_latency_ms,
            default_bandwidth_gbps=args.default_bandwidth_gbps,
            strict=args.strict,
        )
        if not links:
            raise ValueError("No physical FABRIC links remain after filtering the selected sites.")

        skeleton, disconnected_pairs, included_links, used_links, transit_sites = generate_skeleton(
            selected_sites=selected_sites,
            links=links,
            route_metric=args.route_metric,
            strict=args.strict,
        )
        write_json(args.output, skeleton)

        manifest = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "source": source_description,
            "selected_sites": sorted(selected_sites),
            "facility_count": len(selected_sites),
            "available_physical_link_count": len(links),
            "included_physical_link_count": len(included_links),
            "route_used_physical_link_count": len(used_links),
            "route_count": len(skeleton["routes"]),
            "transit_sites": transit_sites,
            "route_metric": args.route_metric,
            "default_latency_ms": args.default_latency_ms,
            "default_bandwidth_gbps": args.default_bandwidth_gbps,
            "latency_override_count": len(latency_overrides),
            "links_using_default_latency": [
                link.original_name
                for link in included_links
                if link.original_name not in latency_overrides
            ],
            "links_using_fallback_bandwidth": fallback_bandwidth_links,
            "skipped_links": skipped_links,
            "disconnected_pairs": disconnected_pairs,
            "link_name_map": {
                link.original_name: link.simgrid_name for link in included_links
            },
            "warnings": warnings,
        }
        write_json(args.manifest_out, manifest, sort_keys=True)

        print(f"Wrote WAN skeleton: {args.output}")
        print(f"Wrote manifest: {args.manifest_out}")
        if args.live:
            print(f"Wrote FABRIC snapshot: {args.snapshot_out}")
        print(
            f"Generated {len(selected_sites)} facilities, {len(included_links)} physical links, "
            f"and {len(skeleton['routes'])} symmetric routes."
        )

        if disconnected_pairs:
            print(
                "Warning: no route was generated for: " + ", ".join(disconnected_pairs),
                file=sys.stderr,
            )
        for warning in warnings:
            print(f"Warning: {warning}", file=sys.stderr)

        return 0

    except (OSError, TypeError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
