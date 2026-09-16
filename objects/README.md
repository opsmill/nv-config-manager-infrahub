# Object data

Infrahub object files for the NVCM mock topologies, one directory per
blueprint. The files are generated; edit the converter or the source data,
not the YAML.

## Regenerate

The source is the NVCM repository's mock topology context
(`development/mock_topology/context/<blueprint>/` plus `context/common/`).
Run the converter with `uv` (Python 3.13, PyYAML declared inline in the
script):

```bash
uv run tools/convert_mock_topology.py \
  /path/to/nv-config-manager/development/mock_topology/context/superpod
```

Options: `--deployment-name` (default `test`, fills the
`{{ deployment_name }}` placeholder the design job uses in Module names),
`--common-dir`, `--output-dir`, `--device-glob` and `-v`. The run ends with a
list of source fields that have no home in the schema; nothing is invented to
fill them.

## Load

```bash
export INFRAHUB_ADDRESS=http://localhost:8000
export INFRAHUB_API_TOKEN=<token>
uvx --from 'infrahub-sdk[ctl]' infrahubctl object validate objects/superpod
uvx --from 'infrahub-sdk[ctl]' infrahubctl object load objects/superpod --branch <branch>
```

Loads are upserts, so re-running is safe, which depends on every kind having a
human-friendly ID the loader can resolve from the fields these files write. See
"Identity attributes" in the repository README for the nodes that need an
explicit `name` to get one. The numeric prefixes are the dependency order:

| File | Kinds |
| :--- | :--- |
| `00-organizations.yml` | `OrganizationManufacturer`, `OrganizationTenant` |
| `10-locations.yml` | `LocationProvider` > `LocationRegion` > `LocationSite` > `LocationModule`, nested |
| `20-roles.yml` | `NvcmDeviceRole` (and `BuiltinTag` when a device carries tags) |
| `30-platforms-device-types.yml` | `DcimPlatform`, `DcimDeviceType` |
| `40-namespaces-vrfs-vlans.yml` | `IpamVRF`, `IpamVLANGroup` with inline `IpamVLAN` |
| `50-prefixes.yml` | `IpamPrefix` |
| `55-ip-addresses.yml` | `IpamIPAddress` (loaded before devices so interfaces can reference them) |
| `60-devices.yml` | `DcimDevice` with inline `InterfacePhysical`, `InterfaceLag`, `InterfaceVirtual` |
| `80-cables.yml` | `DcimCable` with inline `connected_endpoints` |
| `85-bgp.yml` | `RoutingAutonomousSystem`, `RoutingBGPPeerGroup`, `RoutingBGPSession` |
| `90-nvcm-state.yml` | `NvcmDeviceStatus` |
| `91-service-profiles.yml` | `NvcmServiceProfile` with endpoint sets and DHCP server sets |
| `92-dhcp.yml` | `NvcmDhcpOptionDefinition`, `NvcmDhcpScope` with inline `NvcmDhcpPool` ranges |
| `93-firmware.yml` | `NvcmFirmwareTarget` (only when a blueprint has firmware-target contexts) |
| `94-credentials.yml` | `NvcmCredentialMapping` |
| `95-overlays.yml` | `NvcmOverlay` with `NvcmVxlan`, `NvcmInfinibandPkey`, `NvcmOverlayAssignment` |

## Mapping notes

- Every device matched by `--device-glob` is loaded, including the WAN
  routers, GPU nodes and UFM appliances the switches connect to. Only the
  Cumulus Linux devices get an `NvcmDeviceStatus`.
- Region nesting collapses to the site's parent Region; the full chain is
  kept in the Region description.
- Devices whose source has no location land on the Site; everything else
  lands on its Module.
- BGP sessions come from the `bgp_routing_instances[].endpoints` block of
  each device JSON (plus both directions of `bgp_peerings.yaml`). Peer
  addresses that belong to devices outside the blueprint are created as bare
  `IpamIPAddress` objects with a description naming their owner.
- `UNDERLAY` and `EVPN` peer groups are created per device that has sessions
  (`RoutingBGPPeerGroup` is unique on device and name).
- Nautobot prefix roles (`Site-Aggregate`, `ipminet0`, ...) go into
  `IpamPrefix.role` verbatim; the `role-aggregate` and `uc-jumphost` tags become
  the `site_aggregate` and `uc_jumphost` booleans.
- Nautobot interface roles and types are kept verbatim in `role` and
  `interface_type` (type lowercased, `a_` prefix stripped, underscores to
  hyphens, as the design template renders it). Interface tags such as
  `breakout-disable` become `BuiltinTag` objects.
- Sessions and peer groups in the Nautobot default VRF leave `vrf` unset; only
  named VRFs (`EXIT`, `OOB`, ...) are created.
