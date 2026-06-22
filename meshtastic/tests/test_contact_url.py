"""Unit tests for Node.getContactURL() and Node.addContactURL().

Tests cover roundtrip serialization (generate URL, parse it back, verify all
fields), parametrized edge cases, property-based hypothesis testing, URL
transport-format assertions, and error-path coverage.
"""

import base64
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ..mesh_interface import MeshInterface
from ..node import Node
from ..protobuf import config_pb2, mesh_pb2, nanopb_pb2
from ..util import toNodeNum

# Extract nanopb max_size constraints from the User protobuf descriptor.
# nanopb max_size is in bytes; hypothesis text() limits are in characters,
# so we filter by UTF-8 byte length to respect firmware constraints.
_USER_NANOPB = {
    field.name: field.GetOptions().Extensions[nanopb_pb2.nanopb]
    for field in mesh_pb2.User.DESCRIPTOR.fields
}


def _byte_bounded_text(max_bytes: int, min_chars: int = 1) -> st.SearchStrategy[str]:
    """Generate text whose UTF-8 encoding fits within max_bytes."""

    return st.text(min_size=min_chars, max_size=max_bytes).filter(
        lambda s: len(s.encode("utf-8")) <= max_bytes
    )


def _make_mocked_node(
    node_num: int, node_data: dict[str, Any] | None = None
) -> tuple[Node, MagicMock]:
    """Create a Node with a fully mocked MeshInterface for contact URL tests."""

    iface = MagicMock(autospec=MeshInterface)
    if node_data is not None:
        iface.nodesByNum = {node_num: node_data}
    else:
        iface.nodesByNum = {}
    iface.localNode = None
    return Node(iface, node_num, noProto=True), iface


@pytest.mark.unit
@pytest.mark.parametrize(
    "node_id,node_data,should_ignore,manually_verified",
    [
        pytest.param(
            "!830f522a",
            {
                "num": 2198819370,
                "user": {
                    "id": "!830f522a",
                    "longName": "Roadrunner Ridge",
                    "shortName": "RKSN",
                    "macaddr": "AAAAAAAAAAA=",
                    "hwModel": "RAK4631",
                    "role": "ROUTER",
                    "publicKey": "Rx8XD96uBAiFGoFusdqwti3eBT4DLyGuG7g5Wcg9Bw==",
                    "isLicensed": True,
                    "isUnmessagable": False,
                },
            },
            True,
            True,
            id="all_fields_all_flags",
        ),
        pytest.param(
            "!12345678",
            {
                "num": 305419896,
                "user": {
                    "id": "!12345678",
                    "longName": "Test Node",
                    "shortName": "TN",
                    "macaddr": "QkVTVEVWRVI=",
                    "hwModel": "TBEAM",
                },
            },
            False,
            False,
            id="minimal_fields_no_flags",
        ),
        pytest.param(
            305419896,
            {
                "num": 305419896,
                "user": {
                    "id": "!12345678",
                    "longName": "Another Node",
                    "shortName": "AN",
                    "macaddr": "QkVTVEVWRVI=",
                    "hwModel": "HELTEC_V3",
                    "role": "CLIENT",
                    "publicKey": "AAAAAAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
                    "isLicensed": False,
                },
            },
            True,
            False,
            id="int_node_id_licensed_false",
        ),
        pytest.param(
            "!deadbeef",
            {
                "num": 3735928559,
                "user": {
                    "id": "!deadbeef",
                    "longName": "Minimal Contact",
                    "shortName": "MC",
                    "macaddr": "BQYHCAkKCw==",
                    "hwModel": "UNSET",
                    "role": "CLIENT_MUTE",
                },
            },
            False,
            True,
            id="unset_hw_model_verified_only",
        ),
        pytest.param(
            "!1a2b3c4d",
            {
                "num": 439041101,
                "user": {
                    "id": "!1a2b3c4d",
                    "longName": "Licensed Node",
                    "shortName": "LN",
                    "macaddr": "DA0ODxAREg==",
                    "hwModel": "NANO_G1",
                    "isLicensed": True,
                    "isUnmessagable": True,
                },
            },
            False,
            False,
            id="licensed_unmessagable_no_flags",
        ),
    ],
)
def test_contact_url_roundtrip(
    node_id: int | str,
    node_data: dict,
    should_ignore: bool,
    manually_verified: bool,
) -> None:
    """Verify that contact URL generation and parsing is fully reversible."""
    node_num = toNodeNum(node_id)
    anode, _ = _make_mocked_node(node_num, node_data)

    sent_admin: list[Any] = []

    def capture_send(p: Any, *_args: Any, **_kwargs: Any) -> None:
        sent_admin.append(p)

    with patch.object(anode, "_send_admin", side_effect=capture_send):
        url = anode.getContactURL(
            node_id, should_ignore=should_ignore, manually_verified=manually_verified
        )

        # Transport-format assertion: URL fragment must be URL-safe base64
        # (no +, /, or = padding) so it survives copy-paste in browsers.
        fragment = url.split("/#")[-1]
        assert "+" not in fragment
        assert "/" not in fragment
        assert "=" not in fragment

        anode.addContactURL(url)

    assert len(sent_admin) == 1
    contact = sent_admin[0].add_contact
    u = node_data["user"]

    assert contact.node_num == node_num
    assert contact.user.id == u["id"]
    assert contact.user.long_name == u["longName"]
    assert contact.user.short_name == u["shortName"]
    assert contact.user.macaddr == base64.b64decode(u["macaddr"])

    if u.get("hwModel") and u["hwModel"] != "UNSET":
        assert contact.user.hw_model == mesh_pb2.HardwareModel.Value(u["hwModel"])
    if u.get("role"):
        assert contact.user.role == config_pb2.Config.DeviceConfig.Role.Value(u["role"])
    if u.get("publicKey"):
        assert contact.user.public_key == base64.b64decode(u["publicKey"])
    if u.get("isLicensed") is not None:
        assert contact.user.is_licensed == u["isLicensed"]
    if u.get("isUnmessagable") is not None:
        assert contact.user.is_unmessagable == u["isUnmessagable"]

    assert contact.should_ignore == should_ignore
    assert contact.manually_verified == manually_verified


@st.composite
def contact_url_roundtrip_params(draw: st.DrawFn) -> tuple:
    """Hypothesis strategy: generate a full node config and roundtrip flags."""
    should_ignore = draw(st.booleans())
    manually_verified = draw(st.booleans())

    # Skip reserved low IDs (0-5) and broadcast (0xFFFFFFFF)
    node_num = draw(st.integers(min_value=6, max_value=2**32 - 2))
    node_id = f"!{node_num:08x}"

    hw_model = draw(st.sampled_from(list(mesh_pb2.HardwareModel.keys())))
    role = draw(
        st.one_of(
            st.none(),
            st.sampled_from(list(config_pb2.Config.DeviceConfig.Role.keys())),
        )
    )

    long_name = draw(_byte_bounded_text(_USER_NANOPB["long_name"].max_size))
    short_name = draw(_byte_bounded_text(_USER_NANOPB["short_name"].max_size))

    macaddr_bytes = draw(
        st.binary(
            min_size=_USER_NANOPB["macaddr"].max_size,
            max_size=_USER_NANOPB["macaddr"].max_size,
        )
    )
    macaddr_b64 = base64.b64encode(macaddr_bytes).decode("ascii")

    has_public_key = draw(st.booleans())
    public_key_b64 = None
    if has_public_key:
        pk_bytes = draw(
            st.binary(
                min_size=_USER_NANOPB["public_key"].max_size,
                max_size=_USER_NANOPB["public_key"].max_size,
            )
        )
        public_key_b64 = base64.b64encode(pk_bytes).decode("ascii")

    is_licensed = draw(st.booleans())
    is_unmessagable = draw(st.booleans())

    node_data: dict[str, Any] = {
        "num": node_num,
        "user": {
            "id": node_id,
            "longName": long_name,
            "shortName": short_name,
            "macaddr": macaddr_b64,
            "hwModel": hw_model,
            "isLicensed": is_licensed,
            "isUnmessagable": is_unmessagable,
        },
    }
    if role is not None:
        node_data["user"]["role"] = role
    if public_key_b64 is not None:
        node_data["user"]["publicKey"] = public_key_b64

    return node_num, node_data, should_ignore, manually_verified


@pytest.mark.unitslow
@settings(deadline=None)
@given(contact_url_roundtrip_params())
def test_contact_url_roundtrip_hypothesis(params: tuple) -> None:
    """Property: roundtrip preserves data across random field configurations."""
    node_num, node_data, should_ignore, manually_verified = params

    anode, _ = _make_mocked_node(node_num, node_data)

    sent_admin: list[Any] = []

    def capture_send(p: Any, *_args: Any, **_kwargs: Any) -> None:
        sent_admin.append(p)

    with patch.object(anode, "_send_admin", side_effect=capture_send):
        url = anode.getContactURL(
            node_num,
            should_ignore=should_ignore,
            manually_verified=manually_verified,
        )
        anode.addContactURL(url)

    assert len(sent_admin) == 1
    contact = sent_admin[0].add_contact
    u = node_data["user"]

    assert contact.node_num == node_num
    assert contact.user.id == u["id"]
    assert contact.user.long_name == u["longName"]
    assert contact.user.short_name == u["shortName"]
    assert contact.user.macaddr == base64.b64decode(u["macaddr"])
    assert contact.user.hw_model == mesh_pb2.HardwareModel.Value(u["hwModel"])

    if "role" in u:
        assert contact.user.role == config_pb2.Config.DeviceConfig.Role.Value(u["role"])
    if "publicKey" in u:
        assert contact.user.public_key == base64.b64decode(u["publicKey"])
    assert contact.user.is_licensed == u["isLicensed"]
    assert contact.user.is_unmessagable == u["isUnmessagable"]
    assert contact.should_ignore == should_ignore
    assert contact.manually_verified == manually_verified


@pytest.mark.unit
def test_getContactURL_raises_for_missing_node() -> None:
    """GetContactURL should raise when node is not in NodeDB."""

    anode, _ = _make_mocked_node(12345)

    with pytest.raises(MeshInterface.MeshInterfaceError, match="not found in NodeDB"):
        anode.getContactURL(12345)


@pytest.mark.unit
def test_getContactURL_raises_for_node_without_user() -> None:
    """GetContactURL should raise when node exists but has no user data."""

    anode, _ = _make_mocked_node(12345, {"num": 12345})

    with pytest.raises(MeshInterface.MeshInterfaceError, match="not found in NodeDB"):
        anode.getContactURL(12345)


@pytest.mark.unit
def test_addContactURL_raises_for_invalid_url() -> None:
    """AddContactURL should raise for a URL without a /# fragment."""

    anode, _ = _make_mocked_node(12345)

    with pytest.raises(MeshInterface.MeshInterfaceError, match="Invalid URL"):
        anode.addContactURL("https://meshtastic.org/v/")


@pytest.mark.unit
def test_addContactURL_raises_for_empty_fragment() -> None:
    """AddContactURL should raise for a URL with an empty /# fragment."""

    anode, _ = _make_mocked_node(12345)

    with pytest.raises(MeshInterface.MeshInterfaceError, match="empty fragment"):
        anode.addContactURL("https://meshtastic.org/v/#")


@pytest.mark.unit
def test_addContactURL_raises_for_malformed_b64() -> None:
    """AddContactURL should raise for invalid base64 in the fragment."""

    anode, _ = _make_mocked_node(12345)

    with pytest.raises(MeshInterface.MeshInterfaceError, match="Failed to parse"):
        anode.addContactURL("https://meshtastic.org/v/#!@#$invalid-base64@#$")


@pytest.mark.unit
def test_addContactURL_raises_for_oversized_payload() -> None:
    """AddContactURL should reject payloads exceeding the size cap."""

    anode, _ = _make_mocked_node(12345)

    # Craft a URL with a fragment larger than _MAX_CONTACT_URL_PAYLOAD
    huge_fragment = "A" * 5000
    with pytest.raises(MeshInterface.MeshInterfaceError, match="payload too large"):
        anode.addContactURL(f"https://meshtastic.org/v/#{huge_fragment}")
