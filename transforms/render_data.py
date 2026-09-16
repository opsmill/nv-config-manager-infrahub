"""Build NVIDIA Config Manager's provider-neutral RenderData for one device.

The transform receives the response of ``queries/render_data.gql`` and returns
the ``RenderData`` cache envelope (``schema_version``, ``device``,
``location``, ``plugin_data``) as plain dicts. It deliberately does not import
``nv_config_manager_dcim``: the Infrahub worker does not ship it, and the NVCM
provider validates the envelope with ``RenderData.from_cache()`` on its side.

Every section is built by a small pure function so unit tests can exercise
them directly. Missing required data raises ``RenderDataError`` (a
``ValueError``) naming the device, the object and the field; the NVCM provider
maps that to ``DCIMInvalidDataError``.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DEFAULT_VRF = "default"
SERVICE_KEYS = ("dns", "ntp", "syslog", "tacacs", "ztp", "firmware_cache", "provisioning")
LOOPBACK_NAME = re.compile(r"^(lo\d*|loopback\d*)$", re.IGNORECASE)
NATURAL_SORT = re.compile(r"(\d+)")

Node = Mapping[str, Any]


class RenderDataError(ValueError):
    """Required render data is missing or malformed in Infrahub."""

    def __init__(self, device: str, obj: str, field: str, detail: str = "") -> None:
        message = f"device '{device}': {obj} is missing required field '{field}'"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)


# --------------------------------------------------------------------------
# GraphQL response accessors
# --------------------------------------------------------------------------


def _value(node: Node | None, attribute: str) -> Any:
    """Return an attribute value from an Infrahub GraphQL node."""
    if not node:
        return None
    wrapper = node.get(attribute)
    return wrapper.get("value") if isinstance(wrapper, Mapping) else None


def _rel(node: Node | None, relationship: str) -> Node | None:
    """Return the peer node of a cardinality-one relationship."""
    if not node:
        return None
    wrapper = node.get(relationship)
    return wrapper.get("node") if isinstance(wrapper, Mapping) else None


def _edges(node: Node | None, relationship: str) -> list[Node]:
    """Return the peer nodes of a cardinality-many relationship."""
    if not node:
        return []
    wrapper = node.get(relationship)
    if not isinstance(wrapper, Mapping):
        return []
    return [edge["node"] for edge in wrapper.get("edges") or [] if edge.get("node")]


def _name(node: Node | None) -> str | None:
    """Return the ``name`` attribute of a node, or None."""
    return _value(node, "name")


def _names(node: Node | None, relationship: str) -> list[str]:
    """Return the names of every peer of a cardinality-many relationship."""
    return [name for peer in _edges(node, relationship) if (name := _name(peer))]


def _text(value: Any) -> str | None:
    """Normalise a nullable scalar to text, treating the empty string as None."""
    if value is None or value == "":
        return None
    return str(value)


def _natural_key(name: str) -> list[object]:
    """Sort key that orders swp2 before swp10."""
    return [int(part) if part.isdigit() else part for part in NATURAL_SORT.split(name)]


# --------------------------------------------------------------------------
# Shared building blocks
# --------------------------------------------------------------------------


def build_location(node: Node | None) -> dict[str, Any] | None:
    """Build a RenderLocation chain from a nested ``parent`` selection.

    ``kind`` is the Infrahub node kind without the ``Location`` namespace, so
    LocationSite becomes ``Site`` as the template filters expect.
    """
    if not node:
        return None
    typename = str(node.get("__typename") or "")
    return {
        "name": _name(node),
        "id": node.get("id"),
        "kind": typename.removeprefix("Location") or None,
        "tags": _names(node, "tags"),
        "parent": build_location(_rel(node, "parent")),
    }


def site_of(location: Mapping[str, Any] | None, device: str) -> dict[str, Any]:
    """Return the ``Site`` ancestor of a RenderLocation chain."""
    current = location
    while current:
        if current.get("kind") == "Site":
            return dict(current)
        current = current.get("parent")
    raise RenderDataError(device, "location", "Site ancestor")


def build_vrf(node: Node | None) -> dict[str, Any] | None:
    """Build a RenderVrf from an IpamVRF node."""
    if not node:
        return None
    return {
        "name": _name(node),
        "route_distinguisher": _text(_value(node, "vrf_rd")),
        "import_targets": [{"name": name} for name in _names(node, "import_rt")],
        "export_targets": [{"name": name} for name in _names(node, "export_rt")],
    }


def build_vlan(node: Node | None) -> dict[str, Any] | None:
    """Build a RenderVlan from an IpamVLAN node."""
    if not node:
        return None
    return {"vid": int(_value(node, "vlan_id")), "name": _name(node)}


def build_address(node: Node, device: str, owner: str) -> dict[str, Any]:
    """Build a RenderIPAddress with its parent prefixes nearest-first."""
    address = _text(_value(node, "address"))
    if address is None:
        raise RenderDataError(device, owner, "address")
    parsed = ipaddress.ip_interface(address)
    parents: list[str] = []
    prefix = _rel(node, "ip_prefix")
    while prefix:
        parents.append(str(_value(prefix, "prefix")))
        prefix = _rel(prefix, "parent")
    return {
        "address": address,
        "host": str(parsed.ip),
        "version": parsed.version,
        "role": _text(_value(node, "role")),
        "parent_prefixes": parents,
    }


def normalise_interface_type(value: str) -> str:
    """Render an Infrahub interface type as the Nautobot GraphQL enum name.

    The template filter library matches ``A_(\\d+G?)BASE`` on the type, so
    ``1000base-t`` becomes ``A_1000BASE_T`` and ``virtual`` becomes ``VIRTUAL``.
    """
    upper = value.upper().replace("-", "_")
    return f"A_{upper}" if upper[:1].isdigit() else upper


def asn_map(autonomous_systems: Iterable[Node]) -> dict[str, str]:
    """Map device name to ASN from RoutingAutonomousSystem.devices."""
    result: dict[str, str] = {}
    for autonomous_system in autonomous_systems:
        asn = _text(_value(autonomous_system, "asn"))
        if asn is None:
            continue
        for name in _names(autonomous_system, "devices"):
            result[name] = asn
    return result


def device_asn(node: Node | None, asns: Mapping[str, str]) -> str | None:
    """Return a device's ASN from its asn relationship, the AS map or the default."""
    if not node:
        return None
    for candidate in (
        _value(_rel(node, "asn"), "asn"),
        asns.get(_name(node) or ""),
        _value(node, "bgp_default_asn"),
    ):
        if candidate is not None:
            return str(candidate)
    return None


# --------------------------------------------------------------------------
# device.identity, device.interfaces, device.network
# --------------------------------------------------------------------------


def build_identity(device: Node, location: dict[str, Any]) -> dict[str, Any]:
    """Build RenderDeviceIdentity from the DcimDevice node."""
    name = _name(device) or "<unnamed>"
    fields = {
        "platform": _name(_rel(device, "platform")),
        "role": _name(_rel(device, "device_role")),
        "model": _name(_rel(device, "device_type")),
    }
    for field, value in fields.items():
        if value is None:
            raise RenderDataError(name, "device", field)
    return {
        "id": device.get("id"),
        "name": name,
        **fields,
        "location": location,
        "tags": _names(device, "tags"),
    }


def build_connected_interface(
    interface: Node, device: str, asns: Mapping[str, str]
) -> dict[str, Any] | None:
    """Build RenderConnectedInterface from the far end of the interface's cable."""
    connector = _rel(interface, "connector")
    if not connector:
        return None
    far_ends = [
        endpoint
        for endpoint in _edges(connector, "connected_endpoints")
        if endpoint.get("id") != interface.get("id") and _name(endpoint)
    ]
    if not far_ends:
        return None
    far_end = far_ends[0]
    peer_device = _rel(far_end, "device")
    if not peer_device:
        return None
    peer_name = _name(peer_device)
    peer_role = _name(_rel(peer_device, "device_role"))
    if peer_name is None or peer_role is None:
        raise RenderDataError(
            device, f"connected device of interface '{_name(interface)}'", "device_role"
        )
    return {
        "name": _name(far_end),
        "vrf": _name(_rel(far_end, "vrf")),
        "addresses": [
            build_address(address, device, f"interface '{_name(far_end)}' of '{peer_name}'")
            for address in _edges(far_end, "ip_addresses")
        ],
        "device": {
            "id": peer_device.get("id"),
            "name": peer_name,
            "role": peer_role,
            "tenant": _name(_rel(peer_device, "tenant")),
            "tags": _names(peer_device, "tags"),
            "routing_asn": device_asn(peer_device, asns),
        },
    }


def build_interface(node: Node, device: str, asns: Mapping[str, str]) -> dict[str, Any]:
    """Build one RenderInterface from a DcimInterface node."""
    name = _name(node)
    if name is None:
        raise RenderDataError(device, "interface", "name")
    interface_type = _text(_value(node, "interface_type"))
    if interface_type is None:
        raise RenderDataError(device, f"interface '{name}'", "interface_type")
    mtu = _value(node, "mtu")
    return {
        "name": name,
        "type": normalise_interface_type(interface_type),
        "mtu": int(mtu) if mtu is not None else None,
        "enabled": _value(node, "status") != "disabled",
        "tags": _names(node, "tags"),
        "description": _text(_value(node, "description")) or "",
        "role": _text(_value(node, "role")),
        "mac_address": _text(_value(node, "mac_address")),
        "vrf": build_vrf(_rel(node, "vrf")),
        "management_only": False,
        "member_interfaces": _names(node, "bundle_members"),
        "parent_interface": _name(_rel(node, "parent_interface")),
        "untagged_vlan": build_vlan(_rel(node, "untagged_vlan")),
        "tagged_vlans": [build_vlan(vlan) for vlan in _edges(node, "tagged_vlan")],
        "addresses": [
            build_address(address, device, f"interface '{name}'")
            for address in _edges(node, "ip_addresses")
        ],
        "connected_interface": build_connected_interface(node, device, asns),
    }


def build_interfaces(device: Node, asns: Mapping[str, str]) -> list[dict[str, Any]]:
    """Build every interface of the device in natural name order."""
    name = _name(device) or "<unnamed>"
    interfaces = [build_interface(node, name, asns) for node in _edges(device, "interfaces")]
    return sorted(interfaces, key=lambda interface: _natural_key(interface["name"]))


def build_network(device: Node) -> dict[str, Any]:
    """Build RenderNetworkData: device VRFs, console ports and NVLink topology."""
    domains = _edges(device, "nvlink_domains")
    return {
        "vrfs": [vrf for node in _edges(device, "vrfs") if (vrf := build_vrf(node))],
        "console_server_ports": [],
        "nvlink_topology": _text(_value(domains[0], "topology")) if domains else None,
    }


# --------------------------------------------------------------------------
# device.routing
# --------------------------------------------------------------------------


def build_bgp_peer(session: Node, device: str) -> dict[str, Any] | None:
    """Build one RenderBGPPeer from a RoutingBGPSession of the device.

    Returns None, with a warning, when the remote address has no modelled
    interface: the peer device is outside Infrahub, so its name and role are
    unknown and the contract cannot represent it.
    """
    label = f"BGP session '{_value(session, 'description')}'"
    remote_ip = _rel(session, "remote_ip")
    if not remote_ip:
        raise RenderDataError(device, label, "remote_ip")
    remote_interface = _rel(remote_ip, "interface")
    peer_device = _rel(remote_interface, "device")
    if not peer_device:
        LOGGER.warning(
            "device %s: %s remote address %s has no modelled device, peer dropped",
            device,
            label,
            _value(remote_ip, "address"),
        )
        return None
    peer_name = _name(peer_device)
    peer_role = _name(_rel(peer_device, "device_role"))
    if peer_name is None or peer_role is None:
        raise RenderDataError(device, label, "remote device role")
    asn = _text(_value(_rel(session, "remote_as"), "asn"))
    if asn is None:
        raise RenderDataError(device, label, "remote_as")
    peer_group = _name(_rel(session, "peer_group")) or peer_role.upper()
    remote_host = ipaddress.ip_interface(str(_value(remote_ip, "address"))).ip
    local_interface = _rel(_rel(session, "local_ip"), "interface")
    return {
        "name": peer_name,
        "status": str(_value(session, "status") or "active").capitalize(),
        "description": peer_name,
        "peer_group": peer_group,
        "peer_role": peer_role,
        "asn": asn,
        "peer_ipv4": str(remote_host) if remote_host.version == 4 else None,
        "peer_ipv6": str(remote_host) if remote_host.version == 6 else None,
        "source_interface": _name(remote_interface),
        "source_vrf": _name(_rel(local_interface, "vrf")) or DEFAULT_VRF,
        "ttl": None,
    }


def _peer_sort_key(peer: Mapping[str, Any]) -> tuple[str, int]:
    """Order peers by name then address, matching the Nautobot fixture order."""
    host = peer.get("peer_ipv4") or peer.get("peer_ipv6") or "0.0.0.0"
    return (peer["name"], int(ipaddress.ip_address(host)))


def build_bgp_instances(
    device: Node, sessions: Iterable[Node], asns: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Build one RenderBGPInstance per local ASN seen on the device's sessions."""
    name = _name(device) or "<unnamed>"
    fallback_asn = device_asn(device, asns)
    peers_by_asn: dict[str, list[dict[str, Any]]] = {}
    for session in sessions:
        asn = _text(_value(_rel(session, "local_as"), "asn")) or fallback_asn
        if asn is None:
            raise RenderDataError(name, f"BGP session '{_value(session, 'description')}'", "local_as")
        peer = build_bgp_peer(session, name)
        peers_by_asn.setdefault(asn, [])
        if peer is not None:
            peers_by_asn[asn].append(peer)
    router_id_interface = _text(_value(device, "bgp_router_id_interface"))
    instances = []
    for asn, peers in sorted(peers_by_asn.items()):
        peers.sort(key=_peer_sort_key)
        vrfs = sorted({peer["source_vrf"] for peer in peers}) or [DEFAULT_VRF]
        instances.append(
            {
                "status": str(_value(device, "status") or "active").capitalize(),
                "asn": asn,
                "router_id_interface": router_id_interface,
                "vrfs": vrfs,
                "peers": peers,
            }
        )
    return instances


def build_routing(
    device: Node, sessions: Iterable[Node], asns: Mapping[str, str], site_asn: str | None
) -> dict[str, Any]:
    """Build RenderRoutingData from the device, its sessions and the site."""
    df_preference = _value(device, "evpn_df_preference")
    isis_interfaces = [
        {"interface_name": _name(interface), "metric": int(metric)}
        for interface in _edges(device, "interfaces")
        if (metric := _value(interface, "isis_metric")) is not None
    ]
    return {
        "bgp_instances": build_bgp_instances(device, sessions, asns),
        "default_asn": device_asn(device, asns),
        "local_asn": _text(_value(device, "bgp_local_asn")),
        "site_asn": site_asn,
        "isis_interfaces": isis_interfaces,
        "evpn": {
            "esi_base_mac": _text(_value(device, "evpn_esi_base_mac")),
            "fabric_mac": _text(_value(device, "evpn_fabric_mac")),
            "df_preference": int(df_preference) if df_preference is not None else None,
        },
    }


# --------------------------------------------------------------------------
# device.overlays
# --------------------------------------------------------------------------


def _device_vlan_ids(device: Node) -> set[int]:
    """Return every VLAN id attached to a device interface."""
    vids: set[int] = set()
    for interface in _edges(device, "interfaces"):
        vlans = [_rel(interface, "untagged_vlan"), *_edges(interface, "tagged_vlan")]
        vids.update(int(_value(vlan, "vlan_id")) for vlan in vlans if vlan)
    return vids


def _device_vrf_names(device: Node) -> set[str]:
    """Return the device VRFs plus every VRF attached to an interface."""
    names = set(_names(device, "vrfs"))
    for interface in _edges(device, "interfaces"):
        if vrf_name := _name(_rel(interface, "vrf")):
            names.add(vrf_name)
    return names


def build_overlays(
    device: Node, assignments: Iterable[Node], vxlans: Iterable[Node]
) -> dict[str, Any]:
    """Build RenderOverlayData: L2 and L3 VNIs scoped to the device."""
    name = _name(device) or "<unnamed>"
    assigned = {overlay for node in assignments if (overlay := _name(_rel(node, "overlay")))}
    vlan_ids = _device_vlan_ids(device)
    vrf_names = _device_vrf_names(device)
    l2_vnis: list[dict[str, Any]] = []
    l3_vnis: list[dict[str, Any]] = []
    for vxlan in vxlans:
        overlay_name = _name(_rel(vxlan, "overlay"))
        vni = _value(vxlan, "vni")
        targets = {
            "import_targets": [{"name": rt} for rt in _names(vxlan, "import_targets")],
            "export_targets": [{"name": rt} for rt in _names(vxlan, "export_targets")],
        }
        if _value(vxlan, "vni_type") == "l2":
            vlan = build_vlan(_rel(vxlan, "vlan"))
            if vlan is None:
                raise RenderDataError(name, f"L2 VXLAN {vni} of overlay '{overlay_name}'", "vlan")
            if overlay_name in assigned or vlan["vid"] in vlan_ids:
                l2_vnis.append({"vlan": vlan, "vni": vni, "overlay_name": overlay_name, **targets})
        elif _value(vxlan, "vni_type") == "l3":
            vrf = build_vrf(_rel(vxlan, "vrf"))
            if vrf is None:
                raise RenderDataError(name, f"L3 VXLAN {vni} of overlay '{overlay_name}'", "vrf")
            if vrf["name"] in vrf_names:
                l3_vlan_id = _value(vxlan, "l3_vlan_id")
                l3_vnis.append(
                    {
                        "vrf": vrf,
                        "l3_vlan_id": int(l3_vlan_id) if l3_vlan_id is not None else None,
                        "vni": vni,
                        "overlay_name": overlay_name,
                    }
                )
    l2_vnis.sort(key=lambda item: item["vlan"]["vid"])
    l3_vnis.sort(key=lambda item: (item["vni"] or 0, item["vrf"]["name"]))
    l2_vni_vrfs = [
        {
            "name": item["overlay_name"],
            "vni": item["vni"],
            "import_targets": item["import_targets"],
            "export_targets": item["export_targets"],
        }
        for item in l2_vnis
        if item["overlay_name"] in assigned
    ]
    return {"l2_vnis": l2_vnis, "l3_vnis": l3_vnis, "l2_vni_vrfs": l2_vni_vrfs}


# --------------------------------------------------------------------------
# device.firmware
# --------------------------------------------------------------------------


def _firmware_component(node: Node) -> dict[str, Any]:
    """Build a RenderFirmwareComponent from a bundle or override component."""
    return {
        "name": _name(node),
        "artifact": {
            "version": _text(_value(node, "reported_version")),
            "image_file": _text(_value(node, "image_file")),
            "source_path": _text(_value(node, "source_path")),
        },
    }


def build_firmware(device: Node) -> dict[str, Any]:
    """Build RenderFirmwareData from the device's firmware intent."""
    bundle = _rel(device, "firmware_bundle")
    override = _rel(device, "firmware_override")
    bundles = []
    if bundle:
        bundles.append(
            {
                "version": str(_value(bundle, "version")),
                "operating_system": {
                    "version": _text(_value(bundle, "os_version")),
                    "image_file": _text(_value(bundle, "os_image_file")),
                    "source_path": _text(_value(bundle, "os_source_path")),
                },
                "components": [_firmware_component(c) for c in _edges(bundle, "components")],
            }
        )
    return {
        "desired_version": _text(_value(device, "intended_firmware_version")),
        "selected_bundle_version": _text(_value(bundle, "version")) if bundle else None,
        "bundles": bundles,
        "overrides": {
            "skip_components": [str(c) for c in (_value(override, "skip_components") or [])],
            "custom_components": [
                _firmware_component(c) for c in _edges(override, "custom_components")
            ],
        },
    }


# --------------------------------------------------------------------------
# device.services and device.access (scoped profiles)
# --------------------------------------------------------------------------


def scope_matches(
    node: Node, device: str, role: str | None, platform: str | None, locations: set[str]
) -> bool:
    """Return whether a scoped profile applies to the device.

    A profile with no scope at all applies everywhere; otherwise any matching
    location ancestor, device role, platform or device name is enough.
    """
    scopes = {
        "locations": set(_names(node, "locations")),
        "device_roles": set(_names(node, "device_roles")),
        "platforms": set(_names(node, "platforms")),
        "devices": set(_names(node, "devices")),
    }
    if not any(scopes.values()):
        return True
    return bool(
        scopes["locations"] & locations
        or (role and role in scopes["device_roles"])
        or (platform and platform in scopes["platforms"])
        or device in scopes["devices"]
    )


def _weight_key(node: Node) -> tuple[int, str]:
    """Sort profiles by weight then name, so higher weights are applied last."""
    return (int(_value(node, "weight") or 0), _name(node) or "")


def build_services(profiles: Iterable[Node]) -> dict[str, Any]:
    """Merge already-matched service profiles by weight into RenderServicesData."""
    services: dict[str, Any] = {key: None for key in SERVICE_KEYS}
    dhcp: dict[str, dict[str, list[str]]] = {}
    management: dict[str, list[str]] | None = None
    for profile in sorted(profiles, key=_weight_key):
        for endpoint_set in _edges(profile, "endpoint_sets"):
            service = _value(endpoint_set, "service")
            if service in services:
                services[service] = {
                    "ipv4": list(_value(endpoint_set, "ipv4") or []),
                    "ipv6": list(_value(endpoint_set, "ipv6") or []),
                }
        for server_set in _edges(profile, "dhcp_servers"):
            dhcp[_name(server_set) or ""] = {
                "ipv4": list(_value(server_set, "ipv4") or []),
                "ipv6": list(_value(server_set, "ipv6") or []),
            }
        ipv4 = _value(profile, "management_prefixes_ipv4")
        ipv6 = _value(profile, "management_prefixes_ipv6")
        if ipv4 or ipv6:
            management = {"ipv4": list(ipv4 or []), "ipv6": list(ipv6 or [])}
    services["dhcp"] = [
        {"name": name, "endpoints": endpoints} for name, endpoints in sorted(dhcp.items())
    ]
    services["management_prefixes"] = management
    return services


def build_access(mappings: Iterable[Node], device: str) -> dict[str, Any]:
    """Build RenderAccessData from already-matched credential mappings."""
    credentials = []
    for mapping in sorted(mappings, key=_weight_key):
        fields = {
            "username": _text(_value(mapping, "username")),
            "secret_name": _text(_value(mapping, "secret_name")),
            "rotation": _text(_value(mapping, "rotation")),
        }
        for field, value in fields.items():
            if value is None:
                raise RenderDataError(device, f"credential mapping '{_name(mapping)}'", field)
        credentials.append({**fields, "role": _text(_value(mapping, "role"))})
    return {"credentials": credentials}


# --------------------------------------------------------------------------
# location
# --------------------------------------------------------------------------


def _prefix_key(item: Mapping[str, Any]) -> tuple[int, int, int]:
    """Sort prefixes by version, network address and length."""
    network = ipaddress.ip_network(item["prefix"])
    return (network.version, int(network.network_address), network.prefixlen)


def build_address_space(site: Node, device: str) -> dict[str, Any]:
    """Build RenderLocationAddressSpace from the site's prefixes and VLAN groups."""
    prefixes = []
    uc_jumphost = []
    for prefix in _edges(site, "prefixes"):
        value = _text(_value(prefix, "prefix"))
        if value is None:
            raise RenderDataError(device, f"prefix scoped to site '{_name(site)}'", "prefix")
        if _value(prefix, "site_aggregate"):
            prefixes.append({"prefix": value, "role": _text(_value(prefix, "role")), "tags": []})
        if _value(prefix, "uc_jumphost"):
            uc_jumphost.append(value)
    vlans = []
    for group in _edges(site, "vlan_groups"):
        for vlan in _edges(group, "vlans"):
            helpers = [
                str(ipaddress.ip_interface(str(_value(address, "address"))).ip)
                for address in _edges(vlan, "dhcp_helper_addresses")
            ]
            vlans.append({"vlan": build_vlan(vlan), "helper_addresses": helpers})
    return {
        "prefixes": sorted(prefixes, key=_prefix_key),
        "uc_jumphost_prefixes": sorted(uc_jumphost, key=lambda p: _prefix_key({"prefix": p})),
        "vlans": sorted(vlans, key=lambda item: item["vlan"]["vid"]),
    }


def is_loopback(interface: Node) -> bool:
    """Return whether an interface is a loopback by role or by name."""
    role = str(_value(interface, "role") or "")
    return "loopback" in role.lower() or bool(LOOPBACK_NAME.match(_name(interface) or ""))


def build_topology_device(node: Node, asns: Mapping[str, str]) -> dict[str, Any]:
    """Build RenderLocationDevice: name, routing ASN and loopback addresses."""
    loopbacks = [
        str(ipaddress.ip_interface(str(_value(address, "address"))).ip)
        for interface in _edges(node, "interfaces")
        if is_loopback(interface)
        for address in _edges(interface, "ip_addresses")
    ]
    return {"name": _name(node), "routing_asn": device_asn(node, asns), "loopback_addresses": loopbacks}


def at_site(node: Node, site_name: str) -> bool:
    """Return whether a device's location or one of its ancestors is the site."""
    location = _rel(node, "location")
    names = {_name(location), *_names(location, "ancestors")}
    return site_name in names


def build_topology(
    site_name: str, route_servers: Iterable[Node], wan_routers: Iterable[Node], asns: Mapping[str, str]
) -> dict[str, Any]:
    """Build RenderLocationTopology for the devices hosted under the site."""

    def select(devices: Iterable[Node]) -> list[dict[str, Any]]:
        selected = [build_topology_device(d, asns) for d in devices if at_site(d, site_name)]
        return sorted(selected, key=lambda item: item["name"])

    return {"route_servers": select(route_servers), "wan_routers": select(wan_routers)}


def select_site(data: Mapping[str, Any], device: str) -> Node:
    """Return the LocationSite node reached directly or through a module."""
    direct = _edges(data, "site_direct")
    if direct:
        return direct[0]
    for module in _edges(data, "site_via_module"):
        parent = _rel(module, "parent")
        if parent and _name(parent):
            return parent
    raise RenderDataError(device, "location", "LocationSite")


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def build_render_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Assemble the RenderData cache envelope from the query response."""
    devices = _edges(data, "DcimDevice")
    if len(devices) != 1:
        raise ValueError(f"expected exactly one DcimDevice in the query response, got {len(devices)}")
    device = devices[0]
    name = _name(device) or "<unnamed>"
    location = build_location(_rel(device, "location"))
    if location is None:
        raise RenderDataError(name, "device", "location")
    site_chain = site_of(location, name)
    site = select_site(data, name)
    site_asn = _text(_value(site, "site_asn"))
    asns = asn_map(_edges(data, "autonomous_systems"))
    role = _name(_rel(device, "device_role"))
    platform = _name(_rel(device, "platform"))
    ancestors: set[str] = set()
    current: Mapping[str, Any] | None = location
    while current:
        ancestors.add(str(current["name"]))
        current = current.get("parent")

    def matched(relationship: str) -> list[Node]:
        return [
            node
            for node in _edges(data, relationship)
            if scope_matches(node, name, role, platform, ancestors)
        ]

    return {
        "schema_version": SCHEMA_VERSION,
        "device": {
            "identity": build_identity(device, location),
            "interfaces": build_interfaces(device, asns),
            "network": build_network(device),
            "routing": build_routing(device, _edges(data, "bgp_sessions"), asns, site_asn),
            "overlays": build_overlays(
                device, _edges(data, "overlay_assignments"), _edges(data, "vxlans")
            ),
            "firmware": build_firmware(device),
            "services": build_services(matched("service_profiles")),
            "access": build_access(matched("credential_mappings"), name),
        },
        "location": {
            "location": site_chain,
            "routing": {"site_asn": site_asn},
            "address_space": build_address_space(site, name),
            "topology": build_topology(
                str(site_chain["name"]),
                _edges(data, "route_servers"),
                _edges(data, "wan_routers"),
                asns,
            ),
        },
        "plugin_data": {},
    }


class RenderDataTransform(InfrahubTransform):
    """Produce the NVCM RenderData cache envelope for one device."""

    query = "nvcm_render_data"

    async def transform(self, data: dict) -> dict:
        """Return the RenderData cache envelope for the queried device."""
        return build_render_data(data)
