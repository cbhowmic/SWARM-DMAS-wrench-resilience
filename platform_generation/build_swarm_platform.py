#!/usr/bin/env python3
"""Build a loader-compatible SWARM SimGrid JSON platform.

Input 1: fabric_network.json (Step 1: FABRIC/NRTWsim WAN model)
Input 2: synthetic_platform_spec.json (Step 2: distributions/profiles)
Output : swarm_platform.json (consumable by SimGrid-JSON-platform-loader)

The generator keeps nodes homogeneous inside each cluster while allowing
clusters and facilities to be heterogeneous. Facility-level storage is shared
by all clusters in that facility and is sized from total compute-node count.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def weighted_choice(rng: random.Random, names: Sequence[str], weights: Sequence[float]) -> str:
    if len(names) != len(weights) or not names:
        raise ValueError("Choice names and weights must have the same non-zero length")
    if any(float(w) < 0 for w in weights) or sum(float(w) for w in weights) <= 0:
        raise ValueError("Choice weights must be non-negative and sum to > 0")
    return rng.choices(list(names), weights=list(weights), k=1)[0]


def sample_value(rng: random.Random, cfg: Any) -> Any:
    """Sample one value from a compact distribution specification."""
    if not isinstance(cfg, Mapping) or "distribution" not in cfg:
        return cfg

    dist = cfg["distribution"]
    if dist == "choice":
        values = cfg["values"]
        weights = cfg.get("weights", [1.0] * len(values))
        return rng.choices(values, weights=weights, k=1)[0]
    if dist == "uniform_int":
        return rng.randint(int(cfg["min"]), int(cfg["max"]))
    if dist == "uniform":
        return rng.uniform(float(cfg["min"]), float(cfg["max"]))
    if dist == "lognormal":
        # Parameters are natural-log-space mu and sigma, matching random.lognormvariate.
        return rng.lognormvariate(float(cfg["mu"]), float(cfg["sigma"]))

    raise ValueError(f"Unsupported distribution: {dist}")


def choose_weighted_profile(rng: random.Random, profiles: Mapping[str, Mapping[str, Any]]) -> tuple[str, Dict[str, Any]]:
    names = list(profiles.keys())
    weights = [float(profiles[name].get("weight", 1.0)) for name in names]
    selected = weighted_choice(rng, names, weights)
    return selected, deepcopy(dict(profiles[selected]))


def fmt_number(value: float, digits: int = 6) -> str:
    value = float(value)
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def gbps(value: float) -> str:
    return f"{fmt_number(value)}Gbps"


def ms(value: float) -> str:
    return f"{fmt_number(value)}ms"


def us(value: float) -> str:
    return f"{fmt_number(value)}us"


def tb(value: float) -> str:
    return f"{fmt_number(value, 3)}TB"


def choose_facility_class(rng: random.Random, classes: Mapping[str, Mapping[str, Any]]) -> tuple[str, Dict[str, Any]]:
    names = list(classes.keys())
    weights = [float(classes[name].get("weight", 1.0)) for name in names]
    selected = weighted_choice(rng, names, weights)
    return selected, deepcopy(dict(classes[selected]))


def make_cluster(
    site_name: str,
    cluster_index: int,
    node_count: int,
    profile_name: str,
    profile: Mapping[str, Any],
    loopback_cfg: Mapping[str, Any],
) -> tuple[Dict[str, Any], float]:
    cluster_name = f"{site_name}_{cluster_index}"
    node_bw = float(profile["private_link_bandwidth_gbps"])
    oversub = float(profile["cluster_oversubscription"])
    if oversub <= 0:
        raise ValueError("cluster_oversubscription must be > 0")

    # Aggregate cluster egress. This intentionally grows sublinearly with the
    # number of node NICs so that simultaneous transfers can contend.
    backbone_bw = node_count * node_bw / oversub

    cluster = {
        "name": cluster_name,
        "prefix": f"{site_name.lower()}-c{cluster_index}-node-",
        "suffix": "",
        "count": int(node_count),

        "properties": {
        "site": site_name,
        "type": "HPC",
        "memory_amount_in_gb": str(int(profile["memory_gb"])),
        "storage_amount_in_gb": "0",
        "has_gpu": "False",
        "network_interconnect": profile.get(
            "network_interconnect",
            "Ethernet"
        )
        },

        "node": {
            "speed": profile["speed"],
            "cores": int(profile["cores"]),
            # The current JSON loader ignores memory_gb. It is retained as
            # platform metadata for SWARM and for a future loader extension.
            "memory_gb": float(profile["memory_gb"]),
            "profile": profile_name,
            "private_link": {
                "bandwidth": gbps(node_bw),
                "latency": us(float(profile.get("private_link_latency_us", 0.0))),
                "sharing_policy": "SPLITDUPLEX"
            },
            "loopback": {
                "bandwidth": gbps(float(loopback_cfg["bandwidth_gbps"])),
                "latency": us(float(loopback_cfg["latency_us"]))
            }
        },
        "backbone": {
            "bandwidth": gbps(backbone_bw),
            "latency": us(float(profile.get("private_link_latency_us", 0.0)))
        },
        "synthetic_metadata": {
            "profile": profile_name,
            "cluster_oversubscription": oversub,
            "derived_backbone_bandwidth_gbps": round(backbone_bw, 6)
        }
    }
    return cluster, backbone_bw


def build_facility(
    site: Mapping[str, Any],
    spec: Mapping[str, Any],
    rng: random.Random,
) -> tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]]:
    site_name = str(site["name"])
    test_mode = spec.get("test_mode", {})
    test_enabled = bool(test_mode.get("enabled", False))

    if test_enabled:
        class_name = "test"
        class_cfg = None
        cluster_count = int(test_mode.get("clusters_per_facility", 2))
    else:
        class_name, class_cfg = choose_facility_class(rng, spec["facility_classes"])
        cluster_count = int(sample_value(rng, class_cfg["cluster_count"]))

    if cluster_count <= 0:
        raise ValueError(f"Facility {site_name} generated non-positive cluster_count")

    clusters: List[Dict[str, Any]] = []
    cluster_names: List[str] = []
    cluster_backbones: List[float] = []
    cluster_summary: List[Dict[str, Any]] = []
    total_nodes = 0

    for idx in range(cluster_count):
        if test_enabled:
            node_count = int(test_mode.get("nodes_per_cluster", 4))
            requested_profile = test_mode.get("node_profile")
            if requested_profile:
                if requested_profile not in spec["node_profiles"]:
                    raise ValueError(
                        f"Unknown test_mode node_profile: {requested_profile}"
                    )
                profile_name = str(requested_profile)
                profile = deepcopy(dict(spec["node_profiles"][profile_name]))
            else:
                profile_name, profile = choose_weighted_profile(rng, spec["node_profiles"])
        else:
            node_count = int(sample_value(rng, class_cfg["cluster_node_count"]))
            profile_name, profile = choose_weighted_profile(rng, spec["node_profiles"])
        cluster, backbone_bw = make_cluster(
            site_name, idx, node_count, profile_name, profile, spec["loopback"]
        )
        clusters.append(cluster)
        cluster_names.append(cluster["name"])
        cluster_backbones.append(backbone_bw)
        total_nodes += node_count
        cluster_summary.append({
            "name": cluster["name"],
            "nodes": node_count,
            "profile": profile_name,
            "cores_per_node": int(profile["cores"]),
            "memory_gb_per_node": float(profile["memory_gb"]),
            "node_bandwidth_gbps": float(profile["private_link_bandwidth_gbps"]),
            "backbone_bandwidth_gbps": round(backbone_bw, 6),
        })

    if test_enabled:
        facility_oversub = float(test_mode.get("facility_fabric_oversubscription", 1.0))
    else:
        facility_oversub = float(
            sample_value(rng, class_cfg["facility_fabric_oversubscription"])
        )

    if facility_oversub <= 0:
        raise ValueError("facility_fabric_oversubscription must be > 0")
    facility_fabric_bw = sum(cluster_backbones) / facility_oversub

    facility: Dict[str, Any] = {
        "name": site_name,
        "synthetic_metadata": {
            "facility_class": class_name,
            "source_site_metadata": {
                "address": site.get("address"),
                "location": site.get("location"),
                "network": site.get("network")
            },
            "total_compute_nodes": total_nodes,
            "facility_fabric_oversubscription": facility_oversub,
            "derived_facility_fabric_bandwidth_gbps": round(facility_fabric_bw, 6)
        },
        "clusters": clusters,
        "storage_systems": [],
        "links": [],
        "routes": []
    }

    # A single shared facility fabric link is intentionally reused by all
    # inter-cluster and cluster<->PFS routes, creating realistic contention.
    fabric_link_name = f"{site_name}__facility_fabric"
    facility["links"].append({
        "name": fabric_link_name,
        "bandwidth": gbps(facility_fabric_bw),
        "latency": us(float(spec["facility_network"]["latency_us"]))
    })

    # Pairwise inter-cluster reachability over the shared facility fabric.
    # Route entries are directional, so emit both directions explicitly.
    for i in range(len(cluster_names)):
        for j in range(i + 1, len(cluster_names)):
            facility["routes"].append({
                "src": cluster_names[i],
                "dst": cluster_names[j],
                "links": [fabric_link_name]
            })

    filesystems: List[Dict[str, Any]] = []
    storage_summary: Dict[str, Any] = {"enabled": False}
    storage_cfg = spec.get("storage", {})
    if storage_cfg.get("enabled", False):
        pfs_name = f"{site_name}_pfs"
        capacity_per_node = float(sample_value(rng, storage_cfg["capacity_tb_per_compute_node"]))
        total_capacity_tb = total_nodes * capacity_per_node
        disk_profile_name, disk_profile = choose_weighted_profile(rng, storage_cfg["disk_profiles"])
        disk_capacity_tb = float(disk_profile["disk_capacity_tb"])
        disk_count = max(1, int(math.ceil(total_capacity_tb / disk_capacity_tb)))

        facility["storage_systems"].append({
            "name": pfs_name,
            "server_speed": storage_cfg["server_speed"],
            "type": storage_cfg.get("type", "JBOD"),
            "disk_count": disk_count,
            "read_bandwidth": gbps(float(disk_profile["read_bandwidth_gbps"])),
            "write_bandwidth": gbps(float(disk_profile["write_bandwidth_gbps"]))
        })

        # Every cluster reaches the same site-local PFS via the shared fabric.
        # Emit both directions explicitly.
        for cluster_name in cluster_names:
            facility["routes"].append({
                "src": cluster_name,
                "dst": pfs_name,
                "links": [fabric_link_name]
            })

        filesystems.append({
            "name": f"{site_name}_fs",
            "storage_system": pfs_name,
            "mount_point": storage_cfg.get("filesystem_mount_point", "/pfs/"),
            "size": tb(total_capacity_tb)
        })

        storage_summary = {
            "enabled": True,
            "storage_system": pfs_name,
            "disk_profile": disk_profile_name,
            "capacity_tb_per_compute_node": round(capacity_per_node, 6),
            "filesystem_capacity_tb": round(total_capacity_tb, 3),
            "disk_capacity_tb": disk_capacity_tb,
            "disk_count": disk_count
        }

    summary = {
        "facility": site_name,
        "facility_class": class_name,
        "cluster_count": cluster_count,
        "total_nodes": total_nodes,
        "clusters": cluster_summary,
        "facility_fabric_bandwidth_gbps": round(facility_fabric_bw, 6),
        "storage": storage_summary
    }
    return facility, filesystems, summary


def _parse_gbps(value: str) -> float:
    if not value.endswith("Gbps"):
        raise ValueError(f"Expected Gbps value, got: {value}")
    return float(value[:-4])


def convert_fabric_links_and_routes(
    fabric: Mapping[str, Any],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Create top-level links/routes as direct virtual links between facilities.

    SimGrid is sensitive to multi-link inter-zone routes in this topology. We
    therefore collapse each modeled site path into a single virtual link with
    path-equivalent latency and bottleneck bandwidth.
    """
    physical_links = {
        link["name"]: {
            "bandwidth_gbps": float(link["bandwidth_gbps"]),
            "latency_ms": float(link["latency_ms"]),
        }
        for link in fabric["links"]
    }

    virtual_links: List[Dict[str, Any]] = []
    routes: List[Dict[str, Any]] = []

    for route in fabric["routes"]:
        src = route["source"]
        dst = route["destination"]
        route_link_names = list(route["links"])

        if not route_link_names:
            raise ValueError(f"Fabric route has no links: {src} -> {dst}")

        missing = [name for name in route_link_names if name not in physical_links]
        if missing:
            raise ValueError(f"Fabric route references unknown links {missing}: {src} -> {dst}")

        bottleneck_gbps = min(physical_links[name]["bandwidth_gbps"] for name in route_link_names)
        modeled_latency_ms = route.get("modeled_one_way_latency_ms")
        if modeled_latency_ms is None:
            modeled_latency_ms = sum(physical_links[name]["latency_ms"] for name in route_link_names)

        virtual_link_name = f"{src}__to__{dst}__virt"

        virtual_links.append({
            "name": virtual_link_name,
            "bandwidth": gbps(float(bottleneck_gbps)),
            "latency": ms(float(modeled_latency_ms)),
            "synthetic_metadata": {
                "kind": "virtual_path_link",
                "source": src,
                "destination": dst,
                "site_path": route.get("site_path", []),
                "physical_links": route_link_names,
                "modeled_one_way_latency_ms": route.get("modeled_one_way_latency_ms"),
                "symmetric": route.get("symmetric", True),
            },
        })

        routes.append({
            "src": src,
            "dst": dst,
            "links": [virtual_link_name],
            "synthetic_metadata": {
                "site_path": route.get("site_path", []),
                "modeled_one_way_latency_ms": route.get("modeled_one_way_latency_ms"),
                "symmetric": route.get("symmetric", True),
                "physical_links": route_link_names,
            },
        })

    if len({l["name"] for l in virtual_links}) != len(virtual_links):
        raise ValueError("Virtual top-level link names are not unique")

    return virtual_links, routes


def validate_platform(platform: Mapping[str, Any]) -> None:
    facilities = platform.get("facilities", [])
    if not facilities:
        raise ValueError("Generated platform contains no facilities")

    facility_names = {f["name"] for f in facilities}
    if len(facility_names) != len(facilities):
        raise ValueError("Facility names are not unique")

    global_link_names = {l["name"] for l in platform.get("links", [])}
    if len(global_link_names) != len(platform.get("links", [])):
        raise ValueError("Top-level link names are not unique")

    for route in platform.get("routes", []):
        if route["src"] not in facility_names or route["dst"] not in facility_names:
            raise ValueError(f"Invalid top-level route endpoints: {route}")
        missing = set(route["links"]) - global_link_names
        if missing:
            raise ValueError(f"Top-level route references missing links: {sorted(missing)}")

    all_cluster_names = set()
    all_storage_names = set()

    for facility in facilities:
        for cluster in facility.get("clusters", []):
            cluster_name = cluster["name"]

            if cluster_name in all_cluster_names:
                raise ValueError(
                    f"Duplicate cluster name across facilities: {cluster_name}"
                )
            all_cluster_names.add(cluster_name)

            cluster_site = cluster.get("properties", {}).get("site")
            if cluster_site != facility["name"]:
                raise ValueError(
                    f"Cluster {cluster_name} belongs to facility "
                    f"{facility['name']} but properties.site={cluster_site!r}"
                )

    for facility in facilities:
        zone_names = {c["name"] for c in facility.get("clusters", [])}
        zone_names |= {s["name"] for s in facility.get("storage_systems", [])}
        if len(zone_names) != len(facility.get("clusters", [])) + len(facility.get("storage_systems", [])):
            raise ValueError(f"Duplicate zone names within facility {facility['name']}")
        all_storage_names |= {s["name"] for s in facility.get("storage_systems", [])}

        link_names = {l["name"] for l in facility.get("links", [])}
        for route in facility.get("routes", []):
            if route["src"] not in zone_names or route["dst"] not in zone_names:
                raise ValueError(f"Invalid facility route endpoints in {facility['name']}: {route}")
            missing = set(route["links"]) - link_names
            if missing:
                raise ValueError(f"Facility route references missing links in {facility['name']}: {sorted(missing)}")

    for fs in platform.get("filesystems", []):
        storage_name = fs.get("storage_system")
        if storage_name and storage_name not in all_storage_names:
            raise ValueError(f"Filesystem {fs['name']} references unknown storage {storage_name}")


def build_platform(fabric: Mapping[str, Any], spec: Mapping[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    rng = random.Random(int(spec.get("seed", 0)))
    facilities = []
    filesystems = []
    summaries = []

    # Sort by name before sampling so a fixed seed is stable even if Step 1's
    # site array ordering changes.
    for site in sorted(fabric["sites"], key=lambda s: s["name"]):
        facility, site_filesystems, summary = build_facility(site, spec, rng)
        facilities.append(facility)
        filesystems.extend(site_filesystems)
        summaries.append(summary)

    top_links, top_routes = convert_fabric_links_and_routes(fabric)

    cluster_to_facility = {
        cluster["name"]: facility["name"]
        for facility in facilities
        for cluster in facility.get("clusters", [])
    }

    platform = {
        "facilities": facilities,
        "storage_systems": [],
        "links": top_links,
        "routes": top_routes,
        "filesystems": filesystems,
        "synthetic_metadata": {
            "schema_version": "1.0",
            "seed": int(spec.get("seed", 0)),
            "fabric_source": fabric.get("name", "fabric_network"),
            "generator": "build_swarm_platform.py",
            "cluster_to_facility": cluster_to_facility
        }
    }
    validate_platform(platform)

    summary = {
        "seed": int(spec.get("seed", 0)),
        "facility_count": len(facilities),
        "cluster_count": sum(x["cluster_count"] for x in summaries),
        "compute_node_count": sum(x["total_nodes"] for x in summaries),
        "facility_class_counts": {},
        "facilities": summaries,
        "cluster_to_facility": cluster_to_facility
    }
    for x in summaries:
        cls = x["facility_class"]
        summary["facility_class_counts"][cls] = summary["facility_class_counts"].get(cls, 0) + 1
    return platform, summary


def parse_args() -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fabric",
        type=Path,
        default=here / "generated" / "fabric_network.json",
        help="Step 1 fabric_network.json",
    )
    parser.add_argument(
        "--spec",
        type=Path,
        default=here / "specs" / "platform_spec.json",
        help="Synthetic generation specification",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=here / "generated" / "swarm_platform.json",
        help="Loader-compatible output JSON",
    )
    parser.add_argument(
        "--summary",
        type=Path,
        default=here / "generated" / "swarm_platform_summary.json",
        help="Human-readable generation summary JSON",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    fabric = load_json(args.fabric)
    spec = load_json(args.spec)
    platform, summary = build_platform(fabric, spec)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        json.dump(platform, f, indent=2)
        f.write("\n")
    with args.summary.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
        f.write("\n")

    print(f"Generated: {args.out}")
    print(f"Summary:   {args.summary}")
    print(f"Facilities: {summary['facility_count']}")
    print(f"Clusters:   {summary['cluster_count']}")
    print(f"Nodes:      {summary['compute_node_count']}")
    print(f"Classes:    {summary['facility_class_counts']}")


if __name__ == "__main__":
    main()