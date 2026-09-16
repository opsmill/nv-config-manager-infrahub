# nv-config-manager-infrahub

Infrahub repository for [NVIDIA Config Manager](https://github.com/dsx-ai-factory/nv-config-manager)
(NVCM). It holds everything Infrahub needs to act as NVCM's DCIM provider:

- `schemas/` the data model. `base/` and `extensions/` are adapted copies from
  the OpsMill [schema-library](https://github.com/opsmill/schema-library);
  `nvcm/` is the `Nvcm` namespace that replaces Nautobot config contexts, tags,
  custom fields and the Nautobot apps NVCM depended on.
- `queries/` and `transforms/` the GraphQL query and Python transform that
  build NVCM's provider-neutral `RenderData` for one device. Mapping rules and
  the remaining differences from the Nautobot provider are in
  `transforms/README.md`.
- `groups/` the `nvcm_managed_devices` group the artifact definition targets.
- `tests/` Resources Testing Framework definitions, the captured unit fixture
  and the pytest contract test against the NVCM SDK.
- `.infrahub.yml` registers all of it, including the artifact definition that
  generates one `RenderData` artifact per managed device.

The NVCM side (the `infrahub` DCIM provider plugin, Helm and installer
changes) lives in the NVCM repository. The design is written up in
`INFRAHUB_PROVIDER_PLAN.md` there.

## Adaptations to the schema-library copies

Applied by `tools/adapt_schema_library.py` to fresh copies of the upstream files;
re-run it when refreshing from upstream.

| Schema-library node | Change | Why |
| :--- | :--- | :--- |
| `DcimDevice.status` choices | `active, planned, provisioning, provisioned, maintenance, decommissioned, disabled, unknown` | ZTP writes `provisioned`; backups filter on `provisioned` and `active` |
| `DcimDevice.role` | remove the Dropdown; `nvcm_roles.yml` adds a relationship to `NvcmDeviceRole` | NVCM ships 88 roles and templates select on them, a Dropdown cannot carry `managed_by_nvcm` or `category` |
| `DcimInterface.role` | Dropdown becomes free Text | NVCM interface roles (`Uplink`, `Downlink`, `VLAN-101`, `Switch-Loopback`) are template-facing names |
| `DcimInterface.interface_type` | new Text attribute | `RenderInterface.type` is required and carries the platform's type string |
| `DcimInterface.tags` | new relationship to `BuiltinTag` | `RenderInterface.tags` and the `breakout-disable` convention |
| `DcimInterface.mtu` | default removed | The fixture carries `null` MTU for interfaces that never set one |
| `InterfaceLayer3.vrf` | new relationship to `IpamVRF`, inverse `IpamVRF.interfaces` | `RenderInterface.vrf` and BGP `source_vrf` derivation |
| `IpamPrefix.role` | Dropdown becomes free Text | `RenderPrefix.role` carries names such as `Site-Aggregate` or `OOB-Fabric-P2P` |
| `IpamPrefix.site_aggregate`, `uc_jumphost` | new Booleans (in `nvcm_location_intent.yml`) | Replace the `role-aggregate` and `uc-jumphost` tags |
| `IpamIPAddress.role` choices | add `management` | Management interface address selection |
| `IpamIPAddress.fqdn` | `regex` moved under `parameters` | Top-level `regex` is deprecated |
| `DcimCable.status` choices | add `disconnected` and `invalid` | Upstream PR #285 cable-status persistence |
| `RoutingBGPPeerGroup` | unique per `(device, name)`, HFID `[device__name__value, name__value]` | Nautobot peer groups are per routing instance, so `UNDERLAY` exists on every device |
| `RoutingProtocol.vrf` | optional | Nautobot BGP instances in the default VRF have no VRF object |
| `RoutingAutonomousSystem.organization` | optional | Peer ASNs from unloaded neighbours have no owner |

The `location_site` extension is not loaded; `nvcm_locations.yml` defines
`LocationSite` inside the Provider, Region, Site, Module hierarchy.

## Validate and load

```bash
export INFRAHUB_ADDRESS=http://localhost:8000
export INFRAHUB_API_TOKEN=<token>
uvx --from 'infrahub-sdk[ctl]' infrahubctl schema check schemas/base schemas/extensions schemas/nvcm
uvx --from 'infrahub-sdk[ctl]' infrahubctl schema load  schemas/base schemas/extensions schemas/nvcm
```

Validated against Infrahub 1.11.2 on 2026-09-15.

## Managed device group

`groups/nvcm_managed_devices.yml` defines the `CoreStandardGroup` that the
`nvcm-render-data` artifact definition targets. Its members are the devices with
an `NvcmDeviceStatus`, NVCM's definition of a managed device. Infrahub does not
derive the membership; load the group and add the members:

```bash
uvx --from 'infrahub-sdk[ctl]' infrahubctl object load groups/nvcm_managed_devices.yml
```

```graphql
# ids from: { DcimDevice(nvcm_status__isnull: false) { edges { node { id } } } }
# group id from: { CoreStandardGroup(name__value: "nvcm_managed_devices") { edges { node { id } } } }
mutation {
  RelationshipAdd(data: { id: "<group id>", name: "members", nodes: [{ id: "<device id>" }] }) {
    ok
  }
}
```

A generator keyed on `NvcmDeviceStatus`, or the NVCM bootstrap that creates the
status objects, should keep the membership in sync; until then re-run the
mutation after adding devices.

Running the integration test in `tests/test_transforms.yml` without a
registered repository needs the query stored server-side, which a repository
sync normally does:

```graphql
mutation($q: String!) {
  CoreGraphQLQueryCreate(data: { name: { value: "nvcm_render_data" }, query: { value: $q } }) {
    ok
  }
}
```
