"""Apply the NVCM adaptations to the schema-library copies.

Edits are done on the parsed YAML and written back with ruamel to keep
comments and ordering, so a diff against upstream stays reviewable. The
script is idempotent: re-running it on already adapted files is a no-op.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

ROOT = Path(sys.argv[1])
yaml = YAML()
yaml.preserve_quotes = True
yaml.width = 120
yaml.indent(mapping=2, sequence=4, offset=2)


def load(path: Path):
    with path.open() as handle:
        return yaml.load(handle)


def dump(path: Path, data) -> None:
    with path.open("w") as handle:
        yaml.dump(data, handle)


def find_node(doc, namespace: str, name: str, section: str = "nodes"):
    for node in doc.get(section, []):
        if node["namespace"] == namespace and node["name"] == name:
            return node
    raise KeyError(f"{namespace}{name} not in {section}")


def find_attr(node, name: str):
    for attr in node["attributes"]:
        if attr["name"] == name:
            return attr
    raise KeyError(f"attribute {name} on {node['namespace']}{node['name']}")


def has_field(node, section: str, name: str) -> bool:
    return any(item["name"] == name for item in node.get(section, []))


def choice(name: str, label: str, description: str, color: str) -> dict:
    return {"name": name, "label": label, "description": description, "color": color}


def adapt_dcim(path: Path) -> None:
    doc = load(path)
    device = find_node(doc, "Dcim", "Device")
    status = find_attr(device, "status")
    status["choices"] = [
        choice("active", "Active", "Fully operational and in service.", "#00d25b"),
        choice("planned", "Planned", "Recorded in the DCIM, not yet racked.", "#4d90fe"),
        choice("provisioning", "Provisioning", "Being set up by ZTP.", "#f0ad4e"),
        choice("provisioned", "Provisioned", "ZTP completed, configuration applied.", "#7fbf7f"),
        choice("maintenance", "Maintenance", "Undergoing maintenance or repairs.", "#ff9800"),
        choice("decommissioned", "Decommissioned", "Removed from service.", "#e04040"),
        choice("disabled", "Disabled", "Administratively disabled.", "#6c757d"),
        choice("unknown", "Unknown", "State not yet determined.", "#bfbfbf"),
    ]
    device["attributes"] = [a for a in device["attributes"] if a["name"] != "role"]

    interface = find_node(doc, "Dcim", "Interface", section="generics")
    role = find_attr(interface, "role")
    role["kind"] = "Text"
    role.pop("choices", None)
    role["description"] = (
        "Template-facing interface role as NVCM names it, for example Uplink, "
        "Downlink, Management or VLAN-101"
    )
    mtu = find_attr(interface, "mtu")
    mtu.pop("default_value", None)
    if not has_field(interface, "attributes", "interface_type"):
        interface["attributes"].append(
            CommentedMap(
                {
                    "name": "interface_type",
                    "kind": "Text",
                    "label": "Interface type",
                    "optional": True,
                    "description": (
                        "Physical or virtual interface type as the platform reports it, "
                        "for example 1000base-t, 400gbase-x-osfp, lag or virtual"
                    ),
                    "order_weight": 1250,
                }
            )
        )
    if not has_field(interface, "relationships", "tags"):
        interface["relationships"].append(
            CommentedMap(
                {
                    "name": "tags",
                    "peer": "BuiltinTag",
                    "kind": "Attribute",
                    "cardinality": "many",
                    "optional": True,
                    "order_weight": 2000,
                }
            )
        )

    layer3 = find_node(doc, "Interface", "Layer3", section="generics")
    if not has_field(layer3, "relationships", "vrf"):
        layer3["relationships"].append(
            CommentedMap(
                {
                    "name": "vrf",
                    "peer": "IpamVRF",
                    "label": "VRF",
                    "kind": "Attribute",
                    "cardinality": "one",
                    "optional": True,
                    "identifier": "interface__vrf",
                    "order_weight": 1160,
                }
            )
        )
    dump(path, doc)


def adapt_ipam(path: Path) -> None:
    doc = load(path)
    prefix = find_node(doc, "Ipam", "Prefix")
    prefix_role = find_attr(prefix, "role")
    prefix_role["kind"] = "Text"
    prefix_role.pop("choices", None)
    prefix_role["description"] = (
        "Template-facing prefix role as NVCM names it, for example Site-Aggregate or "
        "OOB-Fabric-P2P"
    )
    address = find_node(doc, "Ipam", "IPAddress")
    address_role = find_attr(address, "role")
    if not any(item["name"] == "management" for item in address_role["choices"]):
        address_role["choices"].append(
            choice("management", "Management", "Address of the management interface.", "#AEC6CF")
        )
    fqdn = find_attr(address, "fqdn")
    if "regex" in fqdn:
        fqdn["parameters"] = {"regex": fqdn.pop("regex")}
    dump(path, doc)


def adapt_vrf(path: Path) -> None:
    doc = load(path)
    vrf = find_node(doc, "Ipam", "VRF")
    if not has_field(vrf, "relationships", "interfaces"):
        vrf["relationships"].append(
            CommentedMap(
                {
                    "name": "interfaces",
                    "peer": "InterfaceLayer3",
                    "kind": "Generic",
                    "cardinality": "many",
                    "optional": True,
                    "identifier": "interface__vrf",
                    "order_weight": 1500,
                }
            )
        )
    dump(path, doc)


def adapt_cable(path: Path) -> None:
    doc = load(path)
    cable = find_node(doc, "Dcim", "Cable")
    status = find_attr(cable, "status")
    names = {item["name"] for item in status["choices"]}
    for extra in (
        choice("disconnected", "Disconnected", "Cable validation found no link.", "#e04040"),
        choice("invalid", "Invalid", "Cable validation found an unexpected neighbour.", "#ff9800"),
    ):
        if extra["name"] not in names:
            status["choices"].append(extra)
    dump(path, doc)


def adapt_routing(path: Path) -> None:
    doc = load(path)
    protocol = find_node(doc, "Routing", "Protocol", section="generics")
    for relationship in protocol["relationships"]:
        if relationship["name"] == "vrf":
            relationship["optional"] = True
    dump(path, doc)


def adapt_bgp(path: Path) -> None:
    doc = load(path)
    autonomous_system = find_node(doc, "Routing", "AutonomousSystem")
    for relationship in autonomous_system["relationships"]:
        if relationship["name"] == "organization":
            relationship["optional"] = True
    peer_group = find_node(doc, "Routing", "BGPPeerGroup")
    name = find_attr(peer_group, "name")
    name["unique"] = False
    peer_group["human_friendly_id"] = ["device__name__value", "name__value"]
    peer_group["display_label"] = "{{ name__value }}"
    peer_group["uniqueness_constraints"] = [["device", "name__value"]]
    dump(path, doc)


def main() -> None:
    adapt_dcim(ROOT / "schemas/base/dcim.yml")
    adapt_ipam(ROOT / "schemas/base/ipam.yml")
    adapt_vrf(ROOT / "schemas/extensions/vrf.yml")
    adapt_cable(ROOT / "schemas/extensions/cable.yml")
    adapt_routing(ROOT / "schemas/extensions/routing.yml")
    adapt_bgp(ROOT / "schemas/extensions/bgp.yml")
    print("adapted dcim, ipam, vrf, cable, routing, bgp")


if __name__ == "__main__":
    main()
