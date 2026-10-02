"""Readback verification with real Node callbacks and request correlation."""

from types import SimpleNamespace
from typing import Any

import pytest

import meshtastic.configure_verify as verify
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import admin_pb2


@pytest.mark.unit
@pytest.mark.parametrize("overwrite_cache", [False, True])
@pytest.mark.parametrize(
    ("fresh_value", "status"),
    [
        (None, verify.LocalApplyStatus.RELOAD_FAILED),
        (3, verify.LocalApplyStatus.MISMATCH),
        (5, verify.LocalApplyStatus.VERIFIED),
    ],
)
def test_earlier_settings_reply_cannot_verify_fresh_readback(
    monkeypatch: pytest.MonkeyPatch,
    fresh_value: int | None,
    status: verify.LocalApplyStatus,
    overwrite_cache: bool,
) -> None:
    """A pending earlier request can populate the cache without proving a write."""
    with MeshInterface(noProto=True) as iface:
        node = iface.localNode
        node.noProto = False
        node.nodeNum = 1234
        iface.nodes = {}
        iface.nodesByNum = {}
        sent: list[Any] = []

        def _send(packet: Any, *_args: Any, **_kwargs: Any) -> Any:
            sent.append(packet)
            return packet

        monkeypatch.setattr(iface, "_send_packet", _send)
        field = node.localConfig.DESCRIPTOR.fields_by_name["lora"]
        verify._send_section_refresh_request(node, field)
        prior_id = sent[-1].id
        clock = [0.0]
        delivered: set[int] = set()

        def _deliver(request_id: int, hop_limit: int) -> None:
            raw = admin_pb2.AdminMessage()
            raw.get_config_response.lora.hop_limit = hop_limit
            iface._request_wait_runtime.correlate_inbound_response(
                packet_dict={
                    "from": node.nodeNum,
                    "decoded": {
                        "requestId": request_id,
                        "admin": {
                            "raw": raw,
                            "getConfigResponse": {"lora": {"hopLimit": hop_limit}},
                        },
                    },
                },
                skip_response_callback_for_decode_failure=False,
                extract_request_id=iface._extract_request_id_from_packet,
            )

        def _sleep(seconds: float) -> None:
            clock[0] += seconds
            if prior_id not in delivered:
                delivered.add(prior_id)
                _deliver(prior_id, 5)
            elif fresh_value is not None and sent[-1].id not in delivered:
                delivered.add(sent[-1].id)
                _deliver(sent[-1].id, fresh_value)
                if overwrite_cache:
                    node.localConfig.lora.hop_limit = 5 if fresh_value != 5 else 3

        monkeypatch.setattr(
            verify, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=_sleep)
        )
        result = verify.verify_local_config_apply(
            node,
            config_fields={"lora": {"hop_limit": 5}},
            module_config_fields=None,
            timeout_sec=1,
        )
        assert result.status is status
        assert len(sent) == 2
        assert sent[-1].id not in iface.responseHandlers
