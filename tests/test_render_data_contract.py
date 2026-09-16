"""Contract test: the transform output is a valid NVCM RenderData envelope.

The NVCM DCIM SDK is not a dependency of this repository, so run the test with
the SDK package added on the command line (``$F`` is the NVCM checkout):

    uv run --with "$F/packages/dcim" --with "infrahub-sdk[ctl]" --with pytest \
        pytest tests/test_render_data_contract.py -s

Two things are checked. First, ``RenderData.from_cache()`` accepts
``tests/fixtures/render_data/a04-u44-p01-tor-01/output.json`` and round-trips
it unchanged. Second, the output is compared with the Nautobot provider's
fixture for the same device (``NVCM_NAUTOBOT_FIXTURE`` or the default path in
``$F``): sections the loaded data can reproduce are asserted, everything else
is printed as a readable diff rather than failed, because the superpod mock
data legitimately lacks parts of what the Nautobot database held.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "render_data" / "a04-u44-p01-tor-01" / "output.json"
)
INPUT_PATH = OUTPUT_PATH.with_name("input.json")
DEFAULT_FORK = Path(
    "/Users/pete/.superset/worktrees/423a630e-33c7-419b-abe2-52d7a45c0c28/pmc/nosy-eye"
)
NAUTOBOT_FIXTURE = Path(
    os.environ.get(
        "NVCM_NAUTOBOT_FIXTURE",
        DEFAULT_FORK / "packages/templates/tests/resources/render-data/a04-u44-p01-tor-01.json",
    )
)

sys.path.insert(0, str(REPO_ROOT / "transforms"))

render = pytest.importorskip(
    "nv_config_manager_dcim.render", reason="run with --with <nvcm>/packages/dcim"
)


@pytest.fixture(scope="module")
def output() -> dict[str, Any]:
    """The committed transform output for a04-u44-p01-tor-01."""
    return json.loads(OUTPUT_PATH.read_text())


@pytest.fixture(scope="module")
def nautobot() -> dict[str, Any]:
    """The Nautobot provider's RenderData fixture for the same device."""
    if not NAUTOBOT_FIXTURE.exists():
        pytest.skip(f"Nautobot fixture not found at {NAUTOBOT_FIXTURE}")
    return json.loads(NAUTOBOT_FIXTURE.read_text())


def test_output_matches_unit_fixture_input() -> None:
    """output.json is what the transform produces from input.json."""
    pytest.importorskip("infrahub_sdk", reason="run with --with infrahub-sdk[ctl]")
    from render_data import build_render_data

    assert build_render_data(json.loads(INPUT_PATH.read_text())) == json.loads(
        OUTPUT_PATH.read_text()
    )


def test_from_cache_accepts_output(output: dict[str, Any]) -> None:
    """RenderData.from_cache validates the envelope and round-trips it."""
    render_data = render.RenderData.from_cache(output)
    assert render_data.to_cache() == output


def _site(location: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the Site element of a RenderLocation chain."""
    while location is not None:
        if location.get("kind") == "Site":
            return location
        location = location.get("parent")
    return None


def _diff(label: str, ours: Any, theirs: Any, path: str = "") -> Iterable[str]:
    """Yield one line per leaf that differs between two JSON values."""
    if isinstance(ours, dict) and isinstance(theirs, dict):
        for key in sorted(set(ours) | set(theirs)):
            yield from _diff(label, ours.get(key), theirs.get(key), f"{path}.{key}")
    elif isinstance(ours, list) and isinstance(theirs, list):
        if len(ours) != len(theirs):
            yield f"{label}{path}: {len(ours)} items here, {len(theirs)} in Nautobot"
        for index, (mine, other) in enumerate(zip(ours, theirs, strict=False)):
            yield from _diff(label, mine, other, f"{path}[{index}]")
    elif ours != theirs:
        yield f"{label}{path}: {ours!r} here, {theirs!r} in Nautobot"


def test_identity_matches_nautobot(output: dict[str, Any], nautobot: dict[str, Any]) -> None:
    """Identity scalars and the site name match; ids and the region differ by data."""
    ours, theirs = output["device"]["identity"], nautobot["device"]["identity"]
    for field in ("name", "platform", "role", "model", "tags"):
        assert ours[field] == theirs[field], field
    ours_site, theirs_site = _site(ours["location"]), _site(theirs["location"])
    assert ours_site is not None and theirs_site is not None
    assert ours_site["name"] == theirs_site["name"]
    print("\n".join(_diff("identity", ours["location"], theirs["location"], ".location")))


def test_interfaces_match_nautobot(output: dict[str, Any], nautobot: dict[str, Any]) -> None:
    """Same interface count and names; per-field differences are reported."""
    ours = {interface["name"]: interface for interface in output["device"]["interfaces"]}
    theirs = {interface["name"]: interface for interface in nautobot["device"]["interfaces"]}
    assert len(ours) == len(theirs)
    assert set(ours) == set(theirs)
    lines = [
        line
        for name in sorted(ours)
        for line in _diff(f"interfaces[{name}]", ours[name], theirs[name])
    ]
    print("\n".join(lines))


def test_services_and_access_match_nautobot(
    output: dict[str, Any], nautobot: dict[str, Any]
) -> None:
    """The services Nautobot carried are present with the same endpoints."""
    ours, theirs = output["device"]["services"], nautobot["device"]["services"]
    for service in ("dns", "ntp", "ztp", "syslog", "tacacs"):
        if theirs[service] is not None:
            assert ours[service] == theirs[service], service
    print("\n".join(_diff("services", ours, theirs)))

    def by_user(access: dict[str, Any]) -> dict[str, Any]:
        return {item["username"]: item for item in access["credentials"]}

    assert by_user(output["device"]["access"]) == by_user(nautobot["device"]["access"])


def test_routing_matches_nautobot(output: dict[str, Any], nautobot: dict[str, Any]) -> None:
    """One BGP instance with the same ASN; the peer count is reported, not asserted."""
    ours, theirs = output["device"]["routing"], nautobot["device"]["routing"]
    assert [i["asn"] for i in ours["bgp_instances"]] == [i["asn"] for i in theirs["bgp_instances"]]
    assert ours["default_asn"] == theirs["default_asn"]
    assert {k: str(v).lower() for k, v in ours["evpn"].items()} == {
        k: str(v).lower() for k, v in theirs["evpn"].items()
    }
    for mine, other in zip(ours["bgp_instances"], theirs["bgp_instances"], strict=True):
        peers = f"{len(mine['peers'])} here, {len(other['peers'])} in Nautobot"
        print(f"routing.bgp_instances peers: {peers}")
    print("\n".join(_diff("routing", ours, theirs)))


def test_location_sections_reported(output: dict[str, Any], nautobot: dict[str, Any]) -> None:
    """Address space and topology are compared and any difference is printed."""
    ours, theirs = output["location"], nautobot["location"]
    assert [d["name"] for d in ours["topology"]["wan_routers"]] == [
        d["name"] for d in theirs["topology"]["wan_routers"]
    ]
    assert ours["topology"]["route_servers"] == theirs["topology"]["route_servers"]
    print(
        "\n".join(_diff("location.address_space", ours["address_space"], theirs["address_space"]))
    )
    print("\n".join(_diff("location.topology", ours["topology"], theirs["topology"])))
    print("\n".join(_diff("location.routing", ours["routing"], theirs["routing"])))


def test_remaining_sections_reported(output: dict[str, Any], nautobot: dict[str, Any]) -> None:
    """Network, overlays and firmware differences are printed for the README."""
    for section in ("network", "overlays", "firmware"):
        print("\n".join(_diff(section, output["device"][section], nautobot["device"][section])))
