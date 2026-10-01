"""Value-aware local-apply verification tests for ``configure_verify``."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

import meshtastic.configure_verify as configure_verify
from meshtastic.configure_verify import (
    LocalApplyStatus,
    LocalApplyVerification,
    verify_local_config_apply,
)
from meshtastic.mesh_interface import MeshInterface
from meshtastic.mesh_interface_runtime.request_wait import _RequestWaitRuntime
from meshtastic.protobuf import (
    config_pb2,
    localonly_pb2,
    module_config_pb2,
)
from meshtastic.util import Acknowledgment, Timeout

_ERROR = MeshInterface.MeshInterfaceError("send failed")


class _ScriptedLocalNode:
    """Local node double with real protobuf caches and scripted device replies.

    Readback requests follow the bounded production path: the verification
    engine calls ``_send_admin_with_ack_scope`` with ``scope_ack=False``,
    which lands on this node's ``_send_admin`` (the transport seam) carrying
    ``wantResponse=True`` and ``onResponse=node.onResponseRequestSettings``
    with NO ``responseWaitAttr`` enrollment. Replies are applied exactly the
    way ``onResponseRequestSettings`` applies a correlated config response:
    ``root.<section>.CopyFrom(payload)``. Replies with ``due_at`` are
    delivered by the fake clock's sleep hook; replies with ``due_at=None``
    are applied synchronously inside ``_send_admin`` (mirroring the scoped
    stack where the request blocks until the correlated response lands).
    """

    def __init__(self) -> None:
        self.localConfig = localonly_pb2.LocalConfig()
        self.moduleConfig = localonly_pb2.LocalModuleConfig()
        self._timeout = Timeout(maxSecs=300)
        self.iface = SimpleNamespace()
        self.sent_admin: list[tuple[Any, dict[str, Any]]] = []
        self.handler_packets: list[Any] = []
        self.legacy_requests: list[Any] = []
        self.fail_next_send: Exception | None = None
        self._next_packet_id = 0
        self._replies: list[tuple[float | None, str, str, Any]] = []

    # test helpers ---------------------------------------------------------

    def queue_reply(
        self,
        root_attr: str,
        section_snake: str,
        payload: Any,
        *,
        due_at: float | None = None,
    ) -> None:
        """Queue one device reply; ``due_at=None`` delivers synchronously."""
        self._replies.append((due_at, root_attr, section_snake, payload))

    def apply_due_replies(self, now: float) -> None:
        """Apply every reply whose delivery time has arrived."""
        pending: list[tuple[float | None, str, str, Any]] = []
        for reply in self._replies:
            due_at, root_attr, section_snake, payload = reply
            if due_at is not None and due_at <= now:
                getattr(getattr(self, root_attr), section_snake).CopyFrom(payload)
            else:
                pending.append(reply)
        self._replies = pending

    def apply_immediate_replies(self) -> None:
        """Apply replies marked for synchronous delivery."""
        pending: list[tuple[float | None, str, str, Any]] = []
        for reply in self._replies:
            due_at, root_attr, section_snake, payload = reply
            if due_at is None:
                getattr(getattr(self, root_attr), section_snake).CopyFrom(payload)
            else:
                pending.append(reply)
        self._replies = pending

    # node surface ---------------------------------------------------------

    def onResponseRequestSettings(self, packet: Any) -> None:
        """Record the packets the send machinery routes to the handler."""
        self.handler_packets.append(packet)

    def _send_admin(self, message: Any, **kwargs: Any) -> Any:
        """Transport seam: register the handler, record, deliver replies.

        Mirrors the real pipeline: the response handler is registered in the
        interface's request-wait runtime keyed to the sent packet id, and
        the sent packet carrying that id is returned.
        """
        if self.fail_next_send is not None:
            failure, self.fail_next_send = self.fail_next_send, None
            raise failure
        self.sent_admin.append((message, kwargs))
        self.apply_immediate_replies()
        self._next_packet_id += 1
        request_id = self._next_packet_id
        runtime = getattr(self.iface, "_request_wait_runtime", None)
        if runtime is not None:
            runtime.add_response_handler(
                request_id, kwargs["onResponse"], ack_permitted=False
            )
        return SimpleNamespace(id=request_id)

    def requestConfig(self, field_desc: Any) -> None:
        """Public fallback path, kept for fallback-path tests only."""
        self.legacy_requests.append(field_desc)
        self.apply_immediate_replies()


class _FakeClock:
    """Deterministic ``time`` replacement advancing only on sleep."""

    def __init__(self, node: _ScriptedLocalNode, start: float = 0.0) -> None:
        self.now = start
        self.sleeps: list[float] = []
        self._node = node

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        self._node.apply_due_replies(self.now)

    @property
    def elapsed(self) -> float:
        return self.now - 0.0


def _install_clock(monkeypatch: pytest.MonkeyPatch, node: Any) -> _FakeClock:
    """Install the fake clock for ``configure_verify`` wait loops."""
    clock = _FakeClock(node)
    monkeypatch.setattr(
        configure_verify,
        "time",
        SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep),
    )
    return clock


# ---------------------------------------------------------------------------
# Optimistic cache cannot fake success


@pytest.mark.unit
def test_staged_cache_holding_desired_value_never_verifies_without_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dropped write with the value already staged must not report success."""
    node = _ScriptedLocalNode()
    node.localConfig.lora.hop_limit = 5
    clock = _install_clock(monkeypatch, node)

    result = verify_local_config_apply(
        node,
        config_fields={"lora": {"hopLimit": 5}},
        module_config_fields=None,
        timeout_sec=0.5,
    )

    assert result.status is LocalApplyStatus.RELOAD_FAILED
    assert result.missing_sections == ("lora",)
    assert result.mismatched_fields == ()
    assert clock.elapsed <= 0.5
    # The cache must not pretend the section still exists.
    assert not node.localConfig.HasField("lora")


@pytest.mark.unit
def test_default_valued_cache_snapshot_does_not_verify_without_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Staged default (zero) values are not fresh evidence either."""
    node = _ScriptedLocalNode()
    node.moduleConfig.telemetry.device_update_interval = 0
    _install_clock(monkeypatch, node)

    result = verify_local_config_apply(
        node,
        config_fields=None,
        module_config_fields={"telemetry": {"device_update_interval": 0}},
        timeout_sec=0.2,
    )

    assert result.status is LocalApplyStatus.RELOAD_FAILED


# ---------------------------------------------------------------------------
# Fresh reloads verify


@pytest.mark.unit
def test_fresh_default_valued_section_verifies_when_truly_repopulated() -> None:
    """A real section carrying only defaults verifies after a true reload."""
    node = _ScriptedLocalNode()
    node.queue_reply(
        "moduleConfig",
        "telemetry",
        module_config_pb2.ModuleConfig.TelemetryConfig(),
    )

    result = verify_local_config_apply(
        node,
        config_fields=None,
        module_config_fields={"telemetry": {"device_update_interval": 0}},
        timeout_sec=5.0,
    )

    assert result == LocalApplyVerification(LocalApplyStatus.VERIFIED, (), ())
    assert node.moduleConfig.HasField("telemetry")


@pytest.mark.unit
def test_async_reply_within_budget_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A section repopulated mid-wait under the shared deadline verifies."""
    node = _ScriptedLocalNode()
    lora = config_pb2.Config.LoRaConfig()
    lora.hop_limit = 5
    node.queue_reply("localConfig", "lora", lora, due_at=0.15)
    clock = _install_clock(monkeypatch, node)

    result = verify_local_config_apply(
        node,
        config_fields={"lora": {"hop_limit": 5}},
        module_config_fields=None,
        timeout_sec=1.0,
    )

    assert result.status is LocalApplyStatus.VERIFIED
    assert clock.elapsed < 1.0
    assert node.localConfig.HasField("lora")
    # The async path ran through the bounded send, not public requestConfig.
    assert len(node.sent_admin) == 1
    assert "responseWaitAttr" not in node.sent_admin[0][1]
    assert node.legacy_requests == []


@pytest.mark.unit
def test_untouched_fields_are_not_required_to_match() -> None:
    """Only requested fields are compared; unrelated device values differ freely."""
    node = _ScriptedLocalNode()
    lora = config_pb2.Config.LoRaConfig()
    lora.hop_limit = 5
    lora.region = config_pb2.Config.LoRaConfig.RegionCode.Value("EU_868")
    lora.tx_enabled = True
    node.queue_reply("localConfig", "lora", lora)

    result = verify_local_config_apply(
        node,
        config_fields={"lora": {"hop_limit": 5}},
        module_config_fields=None,
        timeout_sec=5.0,
    )

    assert result.status is LocalApplyStatus.VERIFIED


# ---------------------------------------------------------------------------
# Mismatch detection


@pytest.mark.unit
def test_mismatch_reports_dotted_path_with_snake_field_names() -> None:
    """Mismatched fields are dotted paths; field names are snake_case."""
    node = _ScriptedLocalNode()
    lora = config_pb2.Config.LoRaConfig()
    lora.hop_limit = 3
    node.queue_reply("localConfig", "lora", lora)

    result = verify_local_config_apply(
        node,
        config_fields={"lora": {"hopLimit": 5}},
        module_config_fields=None,
        timeout_sec=5.0,
    )

    assert result.status is LocalApplyStatus.MISMATCH
    assert result.mismatched_fields == ("lora.hop_limit",)
    assert result.missing_sections == ()


@pytest.mark.unit
def test_mismatch_collects_every_failing_field() -> None:
    """All failing requested fields are reported, not just the first."""
    node = _ScriptedLocalNode()
    lora = config_pb2.Config.LoRaConfig()
    lora.hop_limit = 3
    lora.region = config_pb2.Config.LoRaConfig.RegionCode.Value("EU_868")
    node.queue_reply("localConfig", "lora", lora)

    result = verify_local_config_apply(
        node,
        config_fields={"lora": {"hop_limit": 7, "region": 6}},
        module_config_fields=None,
        timeout_sec=5.0,
    )

    assert result.status is LocalApplyStatus.MISMATCH
    assert result.mismatched_fields == ("lora.hop_limit", "lora.region")


@pytest.mark.unit
def test_replayed_old_value_yields_mismatch_not_false_success() -> None:
    """A stale reply carrying pre-write values cannot count as verified."""
    node = _ScriptedLocalNode()
    node.localConfig.lora.hop_limit = 5
    stale = config_pb2.Config.LoRaConfig()
    stale.hop_limit = 3
    node.queue_reply("localConfig", "lora", stale)

    result = verify_local_config_apply(
        node,
        config_fields={"lora": {"hop_limit": 5}},
        module_config_fields=None,
        timeout_sec=5.0,
    )

    assert result.status is LocalApplyStatus.MISMATCH
    assert result.mismatched_fields == ("lora.hop_limit",)


# ---------------------------------------------------------------------------
# Value domain: repeated/bytes/nested/enum


@pytest.mark.unit
def test_repeated_bytes_and_scalar_bytes_survive_verification() -> None:
    """Bytes scalars and repeated bytes compare without corruption."""
    node = _ScriptedLocalNode()
    security = config_pb2.Config.SecurityConfig()
    security.public_key = b"\xaa\xbb\xcc"
    security.admin_key.extend([b"\x01\x02", b"\x03\x04"])
    node.queue_reply("localConfig", "security", security)

    result = verify_local_config_apply(
        node,
        config_fields={
            "security": {
                "public_key": b"\xaa\xbb\xcc",
                "admin_key": [b"\x01\x02", b"\x03\x04"],
            }
        },
        module_config_fields=None,
        timeout_sec=5.0,
    )

    assert result.status is LocalApplyStatus.VERIFIED
    # Frozen inputs were not mangled by the comparator.
    assert node.localConfig.security.admin_key == [b"\x01\x02", b"\x03\x04"]


@pytest.mark.unit
def test_repeated_bytes_order_mismatch_detected() -> None:
    """Repeated bytes compare as ordered lists."""
    node = _ScriptedLocalNode()
    security = config_pb2.Config.SecurityConfig()
    security.admin_key.extend([b"\x02\x01", b"\x01\x02"])
    node.queue_reply("localConfig", "security", security)

    result = verify_local_config_apply(
        node,
        config_fields={"security": {"admin_key": [b"\x01\x02", b"\x02\x01"]}},
        module_config_fields=None,
        timeout_sec=5.0,
    )

    assert result.status is LocalApplyStatus.MISMATCH
    assert result.mismatched_fields == ("security.admin_key",)


@pytest.mark.unit
def test_nested_mapping_and_enum_number_values_verify() -> None:
    """Nested submessage dicts and enum-number values compare correctly."""
    node = _ScriptedLocalNode()
    network = config_pb2.Config.NetworkConfig()
    network.address_mode = config_pb2.Config.NetworkConfig.AddressMode.Value("DHCP")
    network.ipv4_config.ip = 0xC0A8010A  # 192.168.1.10 as fixed32
    network.ipv4_config.gateway = 0xC0A80101  # 192.168.1.1 as fixed32
    node.queue_reply("localConfig", "network", network)

    result = verify_local_config_apply(
        node,
        config_fields={
            "network": {
                "address_mode": config_pb2.Config.NetworkConfig.AddressMode.Value(
                    "DHCP"
                ),
                "ipv4_config": {
                    "ip": 0xC0A8010A,
                    "gateway": 0xC0A80101,
                },
            }
        },
        module_config_fields=None,
        timeout_sec=5.0,
    )

    assert result.status is LocalApplyStatus.VERIFIED


@pytest.mark.unit
def test_neighbor_info_effective_default_equivalence_flows_through() -> None:
    """NeighborInfo 0/default equivalence holds inside the new operation."""
    node = _ScriptedLocalNode()
    neighbor_info = module_config_pb2.ModuleConfig.NeighborInfoConfig()
    neighbor_info.update_interval = 21600
    node.queue_reply("moduleConfig", "neighbor_info", neighbor_info)

    result = verify_local_config_apply(
        node,
        config_fields=None,
        module_config_fields={"neighborInfo": {"update_interval": 0}},
        timeout_sec=5.0,
    )

    assert result.status is LocalApplyStatus.VERIFIED


@pytest.mark.unit
def test_neighbor_info_non_equivalent_interval_still_mismatches() -> None:
    """Only the documented firmware default equivalence is allowed."""
    node = _ScriptedLocalNode()
    neighbor_info = module_config_pb2.ModuleConfig.NeighborInfoConfig()
    neighbor_info.update_interval = 3600
    node.queue_reply("moduleConfig", "neighbor_info", neighbor_info)

    result = verify_local_config_apply(
        node,
        config_fields=None,
        module_config_fields={"neighbor_info": {"update_interval": 0}},
        timeout_sec=5.0,
    )

    assert result.status is LocalApplyStatus.MISMATCH
    assert result.mismatched_fields == ("neighbor_info.update_interval",)


# ---------------------------------------------------------------------------
# Bounded shared budget


@pytest.mark.unit
def test_silent_device_bounded_by_single_budget_not_per_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No-reply sections together consume at most the one shared budget."""
    node = _ScriptedLocalNode()
    clock = _install_clock(monkeypatch, node)

    result = verify_local_config_apply(
        node,
        config_fields={"lora": {"hop_limit": 5}, "position": {"gps_mode": 1}},
        module_config_fields={"telemetry": {"device_update_interval": 900}},
        timeout_sec=0.6,
    )

    assert result.status is LocalApplyStatus.RELOAD_FAILED
    assert result.missing_sections == ("lora", "position", "telemetry")
    assert clock.elapsed <= 0.6 + 1e-9
    assert clock.elapsed < 2 * 0.6


@pytest.mark.unit
def test_reload_failed_takes_precedence_over_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One silent section wins over another section's value mismatch."""
    node = _ScriptedLocalNode()
    lora = config_pb2.Config.LoRaConfig()
    lora.hop_limit = 3
    node.queue_reply("localConfig", "lora", lora)
    _install_clock(monkeypatch, node)

    result = verify_local_config_apply(
        node,
        config_fields={"lora": {"hop_limit": 5}},
        module_config_fields={"telemetry": {"device_update_interval": 900}},
        timeout_sec=0.4,
    )

    assert result.status is LocalApplyStatus.RELOAD_FAILED
    assert result.missing_sections == ("telemetry",)
    assert result.mismatched_fields == ()


@pytest.mark.unit
def test_late_reply_cannot_flip_decided_reload_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply delivered after the budget cannot change the decided outcome."""
    node = _ScriptedLocalNode()
    lora = config_pb2.Config.LoRaConfig()
    lora.hop_limit = 5
    node.queue_reply("localConfig", "lora", lora, due_at=5.0)
    clock = _install_clock(monkeypatch, node)

    result = verify_local_config_apply(
        node,
        config_fields={"lora": {"hop_limit": 5}},
        module_config_fields=None,
        timeout_sec=0.3,
    )

    assert result.status is LocalApplyStatus.RELOAD_FAILED
    assert clock.elapsed <= 0.3
    # The late reply lands after the operation already decided.
    node.apply_due_replies(99.0)
    assert node.localConfig.HasField("lora")
    assert result.status is LocalApplyStatus.RELOAD_FAILED


@pytest.mark.unit
def test_duplicate_replies_are_idempotent() -> None:
    """Duplicate replies for one section cannot corrupt the verified state."""
    node = _ScriptedLocalNode()
    for _ in range(2):
        security = config_pb2.Config.SecurityConfig()
        security.public_key = b"\xaa"
        node.queue_reply("localConfig", "security", security)

    result = verify_local_config_apply(
        node,
        config_fields={"security": {"public_key": b"\xaa"}},
        module_config_fields=None,
        timeout_sec=5.0,
    )

    assert result.status is LocalApplyStatus.VERIFIED
    assert node.localConfig.security.public_key == b"\xaa"


# ---------------------------------------------------------------------------
# Failure and cleanup behavior


class _WaitHarness:
    """Real request-wait runtime wired to plain dictionaries.

    Exercises the same ``RequestWaitRuntime`` machinery the interface uses,
    so retirement assertions inspect real handler/wait bookkeeping rather
    than any test double's own list.
    """

    def __init__(self) -> None:
        self.response_handlers: dict[int, Any] = {}
        self.wait_errors: dict[tuple[str, int], str] = {}
        self.wait_acks: set[tuple[str, int]] = set()
        self.active_ids: dict[str, set[int]] = {}
        self.retired_ids: dict[str, dict[int, float]] = {}
        self.acknowledgment = Acknowledgment()
        self.timeout = Timeout(maxSecs=300)
        self.runtime = _RequestWaitRuntime(
            lock=threading.RLock(),
            get_response_handlers=lambda: self.response_handlers,
            get_wait_errors=lambda: self.wait_errors,
            get_wait_acks=lambda: self.wait_acks,
            get_active_wait_request_ids=lambda: self.active_ids,
            get_retired_wait_request_ids=lambda: self.retired_ids,
            get_acknowledgment=lambda: self.acknowledgment,
            get_timeout=lambda: self.timeout,
            retired_wait_ttl_seconds=60.0,
        )


@pytest.mark.unit
def test_reload_failed_outcome_retires_registered_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out readback leaves no handler in the real wait registry."""
    harness = _WaitHarness()
    node = _ScriptedLocalNode()
    node.iface = SimpleNamespace(_request_wait_runtime=harness.runtime)
    clock = _install_clock(monkeypatch, node)
    registry_midwait: list[dict[int, Any]] = []

    def _sleep_and_snapshot(seconds: float) -> None:
        clock.sleep(seconds)
        registry_midwait.append(dict(harness.response_handlers))

    monkeypatch.setattr(
        configure_verify,
        "time",
        SimpleNamespace(monotonic=clock.monotonic, sleep=_sleep_and_snapshot),
    )

    result = verify_local_config_apply(
        node,
        config_fields={"lora": {"hop_limit": 5}},
        module_config_fields=None,
        timeout_sec=0.2,
    )

    assert result.status is LocalApplyStatus.RELOAD_FAILED
    # The send registered a managed handler in the REAL registry (observed
    # mid-wait), and the operation retired it before returning.
    assert any(registry_midwait)
    assert harness.response_handlers == {}


@pytest.mark.unit
def test_verified_outcome_retires_registered_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful verification also retires its readback request ids."""
    harness = _WaitHarness()
    node = _ScriptedLocalNode()
    node.iface = SimpleNamespace(_request_wait_runtime=harness.runtime)
    lora = config_pb2.Config.LoRaConfig()
    lora.hop_limit = 5
    node.queue_reply("localConfig", "lora", lora)
    _install_clock(monkeypatch, node)

    result = verify_local_config_apply(
        node,
        config_fields={"lora": {"hop_limit": 5}},
        module_config_fields=None,
        timeout_sec=1.0,
    )

    assert result.status is LocalApplyStatus.VERIFIED
    assert harness.response_handlers == {}


@pytest.mark.unit
def test_wrong_variant_reply_does_not_satisfy_presence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config response for a different section cannot verify the request."""
    node = _ScriptedLocalNode()
    bluetooth = config_pb2.Config.BluetoothConfig()
    bluetooth.enabled = True
    # The device answers a different config variant for the readback.
    node.queue_reply("localConfig", "bluetooth", bluetooth)
    _install_clock(monkeypatch, node)

    result = verify_local_config_apply(
        node,
        config_fields={"lora": {"hop_limit": 5}},
        module_config_fields=None,
        timeout_sec=0.2,
    )

    assert result.status is LocalApplyStatus.RELOAD_FAILED
    assert result.missing_sections == ("lora",)
    assert result.mismatched_fields == ()
    # The wrong-variant reply was applied faithfully to its own section,
    # but the requested section stayed cleared: presence was never met.
    assert node.localConfig.HasField("bluetooth")
    assert not node.localConfig.HasField("lora")


@pytest.mark.unit
def test_section_absent_at_compare_time_reports_missing_not_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Presence loss between reload and compare uses missing-section wording."""

    class _FlakyRoot:
        """Config root whose section is present only for the first probe."""

        def __init__(self) -> None:
            self.descriptor = SimpleNamespace(fields_by_name={"lora": object()})
            self.probes = 0

        @property
        def DESCRIPTOR(self) -> Any:
            return self.descriptor

        def ClearField(self, _name: str) -> None:
            self.probes = 0

        def HasField(self, _name: str) -> bool:
            self.probes += 1
            return self.probes == 1

    node = SimpleNamespace(
        localConfig=_FlakyRoot(),
        moduleConfig=localonly_pb2.LocalModuleConfig(),
        _timeout=Timeout(maxSecs=300),
        iface=SimpleNamespace(),
        apply_due_replies=lambda _now: None,
    )
    monkeypatch.setattr(
        configure_verify,
        "_send_section_refresh_request",
        lambda _target, _field_desc: (True, None),
    )
    _install_clock(monkeypatch, node)

    result = verify_local_config_apply(
        node,
        config_fields={"lora": {"hop_limit": 5}},
        module_config_fields=None,
        timeout_sec=1.0,
    )

    # The presence probe passed during the reload wait, but the section was
    # gone at comparison time: missing-section vocabulary, never a mismatch.
    assert result.status is LocalApplyStatus.RELOAD_FAILED
    assert result.missing_sections == ("lora",)
    assert result.mismatched_fields == ()


@pytest.mark.unit
def test_send_failure_propagates_and_leaves_no_pending_state() -> None:
    """A readback send failure raises and leaves the op reusable."""
    node = _ScriptedLocalNode()
    node.localConfig.lora.hop_limit = 5
    node.localConfig.device.role = config_pb2.Config.DeviceConfig.Role.CLIENT_MUTE
    node.fail_next_send = _ERROR

    with pytest.raises(MeshInterface.MeshInterfaceError):
        verify_local_config_apply(
            node,
            config_fields={"lora": {"hop_limit": 5}, "device": {"role": 1}},
            module_config_fields=None,
            timeout_sec=5.0,
        )

    # The engine stopped at the failing section: the later section was not
    # sent and its cache was not cleared. Nothing was enrolled anywhere.
    assert node.sent_admin == []
    assert node.legacy_requests == []
    assert node.localConfig.HasField("lora") is False
    assert node.localConfig.HasField("device")
    assert node.localConfig.device.role == 1

    # No lingering operation state: a healthy verification still completes.
    lora = config_pb2.Config.LoRaConfig()
    lora.hop_limit = 5
    node.queue_reply("localConfig", "lora", lora)
    result = verify_local_config_apply(
        node,
        config_fields={"lora": {"hop_limit": 5}},
        module_config_fields=None,
        timeout_sec=5.0,
    )
    assert result.status is LocalApplyStatus.VERIFIED


@pytest.mark.unit
def test_unknown_section_reported_without_mutating_cache() -> None:
    """Unknown sections are named as missing and never cleared."""
    node = _ScriptedLocalNode()
    node.localConfig.lora.hop_limit = 5
    before = node.localConfig.SerializeToString()

    result = verify_local_config_apply(
        node,
        config_fields={"futureConfig": {"x": 1}},
        module_config_fields=None,
        timeout_sec=1.0,
    )

    assert result.status is LocalApplyStatus.RELOAD_FAILED
    assert result.missing_sections == ("futureConfig",)
    assert node.localConfig.SerializeToString() == before
    assert node.sent_admin == []
    assert node.legacy_requests == []


@pytest.mark.unit
def test_node_without_any_send_seam_reports_missing_without_mutation() -> None:
    """A node that can neither bounded-send nor requestConfig fails fast."""
    node = SimpleNamespace(
        localConfig=localonly_pb2.LocalConfig(),
        moduleConfig=localonly_pb2.LocalModuleConfig(),
    )
    node.localConfig.lora.hop_limit = 5
    before = node.localConfig.SerializeToString()

    result = verify_local_config_apply(
        node,
        config_fields={"lora": {"hop_limit": 5}},
        module_config_fields={"telemetry": {"device_update_interval": 0}},
        timeout_sec=1.0,
    )

    assert result.status is LocalApplyStatus.RELOAD_FAILED
    assert result.missing_sections == ("lora", "telemetry")
    assert node.localConfig.SerializeToString() == before


@pytest.mark.unit
def test_readback_send_enrolls_no_scoped_wait_and_bounded_by_budget() -> None:
    """The bounded send never opens the scoped ACK wait; budget is the only wait.

    With the node's wait owner deliberately huge (300s, the real default),
    a silent device must still return RELOAD_FAILED within the operation
    budget, and the send must carry no ``responseWaitAttr`` enrollment.
    """
    node = _ScriptedLocalNode()
    assert node._timeout.expireTimeout == 300

    start = time.monotonic()
    result = verify_local_config_apply(
        node,
        config_fields={"lora": {"hop_limit": 5}},
        module_config_fields=None,
        timeout_sec=0.3,
    )
    elapsed = time.monotonic() - start

    assert result.status is LocalApplyStatus.RELOAD_FAILED
    assert result.missing_sections == ("lora",)
    assert elapsed < 1.5

    # The readback went through the transport seam with wantResponse and
    # the node's own response handler, but NO scoped wait enrollment.
    assert len(node.sent_admin) == 1
    message, kwargs = node.sent_admin[0]
    assert "responseWaitAttr" not in kwargs
    assert kwargs["wantResponse"] is True
    handler = kwargs["onResponse"]
    assert handler == node.onResponseRequestSettings
    assert getattr(handler, "__self__", None) is node
    assert message.get_config_request == 5  # LORA_CONFIG
    assert node.legacy_requests == []


# ---------------------------------------------------------------------------
# Budget and argument validation


@pytest.mark.unit
def test_empty_request_is_vacuously_verified() -> None:
    """Nothing requested means nothing to verify."""
    result = verify_local_config_apply(
        _ScriptedLocalNode(),
        config_fields=None,
        module_config_fields=None,
        timeout_sec=1.0,
    )

    assert result == LocalApplyVerification(LocalApplyStatus.VERIFIED, (), ())


@pytest.mark.unit
@pytest.mark.parametrize(
    ("bad_timeout", "expected_error"),
    [
        (0, ValueError),
        (-1.0, ValueError),
        (float("nan"), ValueError),
        ("1", TypeError),
    ],
)
def test_invalid_budget_rejected(
    bad_timeout: Any, expected_error: type[Exception]
) -> None:
    """Non-positive, non-finite, or non-numeric budgets are rejected."""
    with pytest.raises(expected_error):
        verify_local_config_apply(
            _ScriptedLocalNode(),
            config_fields={"lora": {"hop_limit": 5}},
            module_config_fields=None,
            timeout_sec=bad_timeout,
        )
