/* Copyright (c) 2026. The SWAT Team. All rights reserved.          */

/* This program is free software; you can redistribute it and/or modify it
 * under the terms of the license (GNU LGPL) which comes with this package. */

#include <dlfcn.h>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include <fsmod/FileSystem.hpp>
#include <fsmod/JBODStorage.hpp>
#include <fsmod/OneDiskStorage.hpp>
#include <simgrid/s4u.hpp>

namespace sg4  = simgrid::s4u;
namespace sgfs = simgrid::fsmod;
using json     = nlohmann::json;

// Forward declaration for dladdr
extern "C" void load_platform(const sg4::Engine& e);

std::string get_config_path()
{
  // First, check environment variable
  if (const char* env_path = std::getenv("PLATFORM_CONFIG")) {
    return env_path;
  }

  // Fall back to platform_config.json relative to .so location
  Dl_info info;
  if (dladdr((void*)load_platform, &info) && info.dli_fname) {
    std::filesystem::path so_path(info.dli_fname);
    return (so_path.parent_path() / "platform_config.json").string();
  }

  // Last resort: current directory
  return "platform_config.json";
}

// Storage tracking for filesystem mounting
std::map<std::string, std::shared_ptr<sgfs::Storage>> storage_map;
std::map<std::string, sg4::NetZone*> zone_map;
std::map<std::string, const sg4::Link*> link_map;

void create_storage_system_zone(sg4::NetZone* parent, const json& storage_config)
{
  const std::string name = storage_config["name"];
  auto* zone             = parent->add_netzone_full(name);
  zone_map[name]         = zone;

  // Infer names from the storage system name
  const std::string server_name  = name + "_server";
  const std::string storage_name = name + "_storage";
  const std::string disk_name_base = name + "_disk";

  // Create server host
  const std::string server_speed = storage_config["server_speed"];
  auto* server = zone->add_host(server_name, server_speed);

  // Create storage
  const std::string storage_type = storage_config["type"];
  int disk_count                 = storage_config["disk_count"];
  const std::string read_bw      = storage_config["read_bandwidth"];
  const std::string write_bw     = storage_config["write_bandwidth"];

  if (storage_type == "JBOD") {
    std::vector<sg4::Disk*> disks;
    for (int i = 0; i < disk_count; i++) {
      std::string disk_name = (disk_count == 1) ? disk_name_base : disk_name_base + std::to_string(i);
      disks.push_back(server->add_disk(disk_name, read_bw, write_bw));
    }
    storage_map[storage_name] = sgfs::JBODStorage::create(storage_name, disks);
  } else if (storage_type == "OneDisk") {
    auto* disk                = server->add_disk(disk_name_base, read_bw, write_bw);
    storage_map[storage_name] = sgfs::OneDiskStorage::create(storage_name, disk);
  }

  // Always add gateway router for consistent routing behavior
  const std::string router_name = name + "_router";
  zone->set_gateway(zone->add_router(router_name));

  zone->seal();
}

void create_cluster_zone(sg4::NetZone* parent, const json& cluster_config)
{
  const std::string name   = cluster_config["name"];
  const std::string prefix = cluster_config["prefix"];
  const std::string suffix = cluster_config["suffix"];
  int count                = cluster_config["count"];

  auto* cluster  = parent->add_netzone_star(name);
  zone_map[name] = cluster;

  // Create backbone
  const auto& backbone_cfg       = cluster_config["backbone"];
  const std::string backbone_bw  = backbone_cfg["bandwidth"];
  const std::string backbone_lat = backbone_cfg.value("latency", "0s");
  const std::string backbone_name = name + "_backbone";
  const auto* backbone = cluster->add_link(backbone_name, backbone_bw)->set_latency(backbone_lat);

  // Node configuration
  const auto& node_cfg = cluster_config["node"];

  const std::string host_speed = node_cfg["speed"];
  int host_cores               = node_cfg["cores"];

  const auto& private_link_cfg   = node_cfg["private_link"];
  const std::string link_bw      = private_link_cfg["bandwidth"];
  const std::string link_lat     = private_link_cfg.value("latency", "0s");

  const auto& loopback_cfg       = node_cfg["loopback"];
  const std::string loopback_bw  = loopback_cfg["bandwidth"];
  const std::string loopback_lat = loopback_cfg.value("latency", "0s");

  // Check for node storage (always OneDisk for node-local storage)
  bool has_storage = node_cfg.contains("storage");
  std::string storage_base_name;
  std::string storage_read_bw;
  std::string storage_write_bw;

  if (has_storage) {
    const auto& storage_cfg = node_cfg["storage"];
    storage_base_name       = storage_cfg["name"];
    storage_read_bw         = storage_cfg["read_bandwidth"];
    storage_write_bw        = storage_cfg["write_bandwidth"];
  }

  // Create nodes
  for (int i = 0; i < count; i++) {
    std::string hostname = prefix + std::to_string(i) + suffix;
    auto* host           = cluster->add_host(hostname, host_speed)->set_core_count(host_cores);

    // Create node storage if configured (always OneDisk for node-local storage)
    if (has_storage) {
      std::string storage_name = hostname + "_" + storage_base_name;
      std::string disk_name    = storage_name + "_disk";
      auto* disk               = host->add_disk(disk_name, storage_read_bw, storage_write_bw);
      storage_map[storage_name] = sgfs::OneDiskStorage::create(storage_name, disk);
    }

    // Create links (up/down as separate links for compatibility)
    auto* link_up   = cluster->add_link(hostname + "_LinkUP", link_bw)->set_latency(link_lat);
    auto* link_down = cluster->add_link(hostname + "_LinkDOWN", link_bw)->set_latency(link_lat);
    auto* loopback  = cluster->add_link(hostname + "_loopback", loopback_bw)
                          ->set_latency(loopback_lat)
                          ->set_sharing_policy(sg4::Link::SharingPolicy::FATPIPE);

    // Add routes
    cluster->add_route(host, nullptr, {sg4::LinkInRoute(link_up), sg4::LinkInRoute(backbone)}, false);
    cluster->add_route(nullptr, host, {sg4::LinkInRoute(backbone), sg4::LinkInRoute(link_down)}, false);
    cluster->add_route(host, host, {loopback});
  }

  // Set gateway
  const std::string router_name = name + "_router";
  cluster->set_gateway(cluster->add_router(router_name));
  cluster->seal();
}

void create_inter_zone_links(sg4::NetZone* datacenter, const json& links_config)
{
  for (const auto& link_cfg : links_config) {
    const std::string link_name = link_cfg["name"];
    const std::string bandwidth = link_cfg["bandwidth"];
    const std::string latency   = link_cfg.value("latency", "0s");
    const auto* link = datacenter->add_link(link_name, bandwidth)->set_latency(latency);
    link_map[link_name] = link;
  }
}

void create_routes(sg4::NetZone* datacenter, const json& routes_config)
{
  for (const auto& route_cfg : routes_config) {
    const std::string src_name  = route_cfg["src"];
    const std::string dst_name  = route_cfg["dst"];

    auto* src_zone = zone_map[src_name];
    auto* dst_zone = zone_map[dst_name];

    std::vector<sg4::LinkInRoute> route_links;
    for (const auto& link_name : route_cfg["links"]) {
      route_links.emplace_back(link_map[link_name.get<std::string>()]);
    }

    datacenter->add_route(src_zone, dst_zone, route_links);
  }
}

void create_filesystems(const json& filesystems_config, const json& platform_config)
{
  for (const auto& fs_cfg : filesystems_config) {
    const std::string fs_name            = fs_cfg["name"];
    const std::string mount_point_pattern = fs_cfg["mount_point"];
    const std::string size               = fs_cfg["size"];
    constexpr int max_open_files         = 100000000;

    auto fs = sgfs::FileSystem::create(fs_name, max_open_files);

    if (fs_cfg.contains("storage_system")) {
      // Filesystem on a storage system (single partition)
      const std::string storage_system_name = fs_cfg["storage_system"];
      const std::string storage_name        = storage_system_name + "_storage";

      auto* zone = zone_map[storage_system_name];
      fs->mount_partition(mount_point_pattern, storage_map[storage_name], size);
      sgfs::FileSystem::register_file_system(zone, fs);

    } else if (fs_cfg.contains("cluster")) {
      // Filesystem on a cluster (per-node partitions)
      const std::string cluster_name = fs_cfg["cluster"];

      // Find cluster config to get node info and storage name
      std::string prefix, suffix, storage_base_name;
      int count = 0;

      for (const auto& dc : platform_config["facilities"]) {
        if (dc.contains("clusters")) {
          for (const auto& cluster : dc["clusters"]) {
            if (cluster["name"] == cluster_name) {
              prefix = cluster["prefix"];
              suffix = cluster["suffix"];
              count  = cluster["count"];
              if (cluster["node"].contains("storage")) {
                storage_base_name = cluster["node"]["storage"]["name"];
              }
              break;
            }
          }
        }
      }

      // Create partition for each node
      for (int i = 0; i < count; i++) {
        std::string hostname     = prefix + std::to_string(i) + suffix;
        std::string storage_name = hostname + "_" + storage_base_name;

        // Replace {hostname} in mount point pattern
        std::string mount_point = mount_point_pattern;
        size_t pos;
        while ((pos = mount_point.find("{hostname}")) != std::string::npos) {
          mount_point.replace(pos, 10, hostname);
        }

        fs->mount_partition(mount_point, storage_map[storage_name], size);
      }

      auto* zone = zone_map[cluster_name];
      sgfs::FileSystem::register_file_system(zone, fs);
    }
  }
}

void load_platform(const sg4::Engine& e)
{
  // Load configuration
  std::string config_path = get_config_path();
  std::ifstream config_file(config_path);
  if (!config_file.is_open()) {
    throw std::runtime_error("Cannot open config file: " + config_path);
  }

  json config = json::parse(config_file);

  // Process each facility (always uses Full routing)
  for (const auto& dc_config : config["facilities"]) {
    const std::string dc_name    = dc_config["name"];
    sg4::NetZone* datacenter     = e.get_netzone_root()->add_netzone_full(dc_name);
    zone_map[dc_name] = datacenter;

    // Create storage system zones
    if (dc_config.contains("storage_systems")) {
      for (const auto& storage_cfg : dc_config["storage_systems"]) {
        create_storage_system_zone(datacenter, storage_cfg);
      }
    }

    // Create cluster zones
    if (dc_config.contains("clusters")) {
      for (const auto& cluster_cfg : dc_config["clusters"]) {
        create_cluster_zone(datacenter, cluster_cfg);
      }
    }

    // Create inter-zone links
    if (dc_config.contains("links")) {
      create_inter_zone_links(datacenter, dc_config["links"]);
    }

    // Create routes between zones
    if (dc_config.contains("routes")) {
      create_routes(datacenter, dc_config["routes"]);
    }

    // Add gateway router for inter-facility routing
    const std::string router_name = dc_name + "_router";
    datacenter->set_gateway(datacenter->add_router(router_name));

    datacenter->seal();
  }

  // Create top-level storage system zones (shared across facilities)
  if (config.contains("storage_systems")) {
    for (const auto& storage_cfg : config["storage_systems"]) {
      create_storage_system_zone(e.get_netzone_root(), storage_cfg);
    }
  }

  // Create top-level inter-facility links
  if (config.contains("links")) {
    for (const auto& link_cfg : config["links"]) {
      const std::string link_name = link_cfg["name"];
      const std::string bandwidth = link_cfg["bandwidth"];
      const std::string latency   = link_cfg.value("latency", "0s");
      const auto* link = e.get_netzone_root()->add_link(link_name, bandwidth)->set_latency(latency);
      link_map[link_name] = link;
    }
  }

  // Create top-level routes (between facilities or between facility and shared storage)
  if (config.contains("routes")) {
    for (const auto& route_cfg : config["routes"]) {
      const std::string src_name = route_cfg["src"];
      const std::string dst_name = route_cfg["dst"];
      auto* src_zone = zone_map[src_name];
      auto* dst_zone = zone_map[dst_name];

      std::vector<sg4::LinkInRoute> route_links;
      for (const auto& link_name : route_cfg["links"]) {
        route_links.emplace_back(link_map[link_name.get<std::string>()]);
      }

      e.get_netzone_root()->add_route(src_zone, dst_zone, route_links, true);  // symmetric = true
    }
  }

  // Create filesystems (mount partitions)
  if (config.contains("filesystems")) {
    create_filesystems(config["filesystems"], config);
  }
}