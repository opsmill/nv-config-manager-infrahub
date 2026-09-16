# nvcm_render_data transform

`transforms/render_data.py` turns the response of `queries/render_data.gql`
into NVIDIA Config Manager's provider-neutral `RenderData` cache envelope
(`schema_version`, `device`, `location`, `plugin_data`) for one device. The
artifact definition `nvcm-render-data` in `.infrahub.yml` renders it as
`application/json` for every member of the `nvcm_managed_devices` group.

The transform builds plain dicts and never imports `nv_config_manager_dcim`;
the NVCM `infrahub` provider validates the envelope with
`RenderData.from_cache()` on its side. Missing required data raises a
`RenderDataError` (a `ValueError`) naming the device, the object and the field.

## Run it

```bash
export INFRAHUB_ADDRESS=http://localhost:8000
export INFRAHUB_API_TOKEN=<token>
uvx --from 'infrahub-sdk[ctl]' infrahubctl transform nvcm_render_data device=a04-u44-p01-tor-01
```

Resources Testing Framework (smoke and unit tests need no server; the
integration test needs the query stored as a `CoreGraphQLQuery`, which a
repository sync does, or the mutation in the root README):

```bash
uv run --with 'infrahub-sdk[ctl]' --with pytest pytest tests/test_transforms.yml
```

Contract test against the NVCM SDK, with `$F` the NVCM checkout:

```bash
uv run --with "$F/packages/dcim" --with "infrahub-sdk[ctl]" --with pytest \
  pytest tests/test_render_data_contract.py -s
```

Regenerate the unit fixture after a query or data change:

```bash
uv run --with 'infrahub-sdk[ctl]' python - <<'EOF'
import asyncio, json, sys
sys.path.insert(0, "transforms")
from infrahub_sdk import InfrahubClient
from render_data import build_render_data
async def main():
    data = await InfrahubClient().execute_graphql(
        query=open("queries/render_data.gql").read(), variables={"device": "a04-u44-p01-tor-01"})
    base = "tests/fixtures/render_data/a04-u44-p01-tor-01/"
    json.dump(data, open(base + "input.json", "w"), indent=2)
    json.dump(build_render_data(data), open(base + "output.json", "w"), indent=2)
asyncio.run(main())
EOF
```

## Mapping rules

| RenderData field | Infrahub source | Rule |
| :--- | :--- | :--- |
| `identity.location` | `DcimDevice.location` and its `parent` chain | `kind` is the node kind without the `Location` prefix (`Module`, `Site`, `Region`, `Provider`); the chain starts at the device's own location |
| `location.location` | the `Site` element of that chain | Raises when the device has no `LocationSite` ancestor |
| `interfaces[].type` | `DcimInterface.interface_type` | Rendered as the Nautobot GraphQL enum the template filters match on: `1000base-t` becomes `A_1000BASE_T`, `virtual` becomes `VIRTUAL` |
| `interfaces[].enabled` | `DcimInterface.status` | `false` only for `disabled` |
| `interfaces[].addresses[].parent_prefixes` | `IpamIPAddress.ip_prefix` and `IpamPrefix.parent` | Nearest first, up to three levels |
| `interfaces[].connected_interface` | `DcimEndpoint.connector.connected_endpoints` | The endpoint that is not the interface itself; `routing_asn` from the peer's `asn`, then `RoutingAutonomousSystem.devices`, then `bgp_default_asn` |
| `interfaces[].management_only` | none | Always `false`, no field models it |
| `network.vrfs` | `DcimDevice.vrfs` | |
| `routing.bgp_instances` | `RoutingBGPSession(device=...)` | One instance per local ASN (`local_as`, falling back to the device ASN); `router_id_interface` from `bgp_router_id_interface`; `vrfs` from the peers' source VRFs |
| `routing.bgp_instances[].peers[]` | one session each | `name`, `peer_role` from the remote address's interface device; `source_interface` is the remote interface name (Nautobot semantics); `source_vrf` from the local address's interface VRF, `default` when unset; `description` is the peer device name; `status` capitalised |
| `routing.default_asn` | `bgp_default_asn`, else `RoutingAutonomousSystem.devices` | |
| `routing.site_asn`, `location.routing.site_asn` | `LocationSite.site_asn` | |
| `routing.evpn` | `evpn_esi_base_mac`, `evpn_fabric_mac`, `evpn_df_preference` | MACs lower-cased |
| `overlays.l2_vnis` | `NvcmVxlan(vni_type=l2)` | Overlay assigned to the device, or its VLAN attached to a device interface |
| `overlays.l3_vnis` | `NvcmVxlan(vni_type=l3)` | VRF in `DcimDevice.vrfs` or on a device interface |
| `overlays.l2_vni_vrfs` | L2 VNIs whose overlay is assigned to the device | |
| `firmware` | `intended_firmware_version`, `firmware_bundle`, `firmware_override` | |
| `services` | `NvcmServiceProfile(is_active=true)` | Profiles whose scope matches by location ancestry, role, platform or device (unscoped profiles match everything), merged in ascending weight, the highest weight wins per service |
| `access.credentials` | `NvcmCredentialMapping` | Same scope rule, ordered by weight then name |
| `location.address_space.prefixes` | `LocationSite.prefixes` with `site_aggregate` | `role` is the free-text prefix role, `tags` is empty |
| `location.address_space.uc_jumphost_prefixes` | `LocationSite.prefixes` with `uc_jumphost` | |
| `location.address_space.vlans` | `LocationSite.vlan_groups[].vlans` | Helper addresses from `IpamVLAN.dhcp_helper_addresses` |
| `location.topology` | `DcimDevice(route_server=true)` and `DcimDevice(device_role__category=wan_router)` | Kept when the site is the device's location or an ancestor; loopbacks are interfaces with a role containing `loopback` or named `lo*` / `Loopback*` |

Sessions whose remote address has no modelled interface (a peer outside
Infrahub) are dropped with a warning instead of failing the render: the
contract needs the peer's name and role and Infrahub has neither.

## Differences from the Nautobot fixture

Compared with `packages/templates/tests/resources/render-data/a04-u44-p01-tor-01.json`
in NVCM, using the superpod mock topology loaded from `objects/superpod`.
Matching sections: identity scalars (name, platform, role, model, tags), the
54 interface names with their types, roles, VLANs, VRFs and addresses,
`network`, `firmware`, `overlays.l2_vnis` and `l2_vni_vrfs`, `services.dns`,
`ntp` and `ztp`, `access`, the BGP instance ASN and `default_asn`, the WAN
router names.

| Field | Difference | Reason |
| :--- | :--- | :--- |
| `identity.location` | Starts at the Module `TEST-SITE SUPERPOD 1 - test`, Nautobot started at the Site; Region is `Country-A`, not `Example Region`; Provider level present; ids set | The converter places devices on their Module; region name and ids come from the loaded data. Filters walk to `kind == "Site"` |
| `interfaces[].addresses[].parent_prefixes` | One or two levels instead of three, first hop is the /20 or /23 | Missing source data: the /32, /27, /26 and /25 prefixes and `0.0.0.0/0` are not in the mock topology |
| `interfaces[eth0].addresses[0].role` | `management`, Nautobot `null` | Extra data: the converter sets the IPAM role |
| `interfaces[eth0].mac_address` | Set, Nautobot `null` | Extra data from the device serial |
| `interfaces[].connected_interface.device.id`, `.tenant` | Set, Nautobot `null` | Extra data |
| `interfaces[swp1..swp48].connected_interface` | `null`, Nautobot had NVSwitch and DGX peers | Missing source data: those devices are not in the blueprint |
| `interfaces[swp51].connected_interface` | `null`, Nautobot had `b08-u28-p01-oobspine-02` | Missing source data: that spine is not in the blueprint |
| `interfaces[swp49].connected_interface.addresses[].parent_prefixes` | Populated, Nautobot `[]` | Deliberate: the same address rule applies to far-end addresses |
| `routing.bgp_instances[0].peers` | 2 peers, Nautobot 4 | Missing source data: both `b08-u28-p01-oobspine-02` sessions are dropped with a warning |
| `routing.bgp_instances[0].peers[].description` | Peer device name | Deliberate: the Infrahub session description is a mandatory unique label, not the peer description |
| `routing.bgp_instances[0].status` | Device status, capitalised | Infrahub has no routing-instance object |
| `routing.evpn.fabric_mac` | Lower case | Deliberate: Infrahub stores MACs upper-cased, the transform lower-cases both EVPN MACs |
| `routing.site_asn`, `location.routing.site_asn` | `4230000001`, Nautobot `null` | Extra data: `LocationSite.site_asn` is modelled |
| `services.provisioning`, `dhcp`, `management_prefixes` | Populated from the site profile, Nautobot `null` / `[]` | Extra data loaded by the converter into the site service profile |
| `overlays.l3_vnis` | `OOB-L3` only, Nautobot also `INBAND-L3` and `STORAGE-L3`; `route_distinguisher` set | Missing source data: only the OOB VRF is on the device; RD is extra data |
| `location.address_space.prefixes` | 12 prefixes, Nautobot 1; `tags` empty | The converter flags every site-scoped aggregate with `site_aggregate`; the `role-aggregate` tag became that Boolean and no template reads the tag |
| `location.address_space.vlans` | 7 VLANs (101, 102, 150, 201, 202, 203, 301) with names, Nautobot 12 without names and with helper addresses on 101 to 103, 201, 202 | Missing source data: VLANs 11 to 14 and 103 and the helper addresses have no source in the mock topology; names are extra data |
| `location.topology.wan_routers[].routing_asn` | `4265355123`, Nautobot `null` | Extra data: the WAN AS is modelled |
| Interface order | Natural sort by name | Nautobot's ordering is not reproducible; consumers index by name |

## Schema gaps hit

- `DcimGenericDevice.asn` and `RoutingAutonomousSystem.devices` have no shared
  identifier, so loading the AS side leaves `asn` empty on the device. The
  transform reads `RoutingAutonomousSystem.devices` instead.
- No field models Nautobot's `mgmt_only` interface flag; `management_only` is
  always `false`.
- No console-server-port model; `network.console_server_ports` is always empty.
- Infrahub 1.11 returns `null` attributes when a named fragment is spread
  inside an inline fragment on a hierarchical `parent` relationship, so the
  site fields are spelled out inline in `site_via_module`.
