"""Client helpers for the firmware PKI key-verification handshake.

Firmware 2.8 lets an operator verify that the public key a node holds for a
remote node matches the key that remote node actually owns.  The handshake is
driven by ``AdminMessage.key_verification`` messages sent to the *local* node
(the ``remote_nodenum`` field names the peer being verified) and the device
reports progress through ``ClientNotification`` payloads published on the
``meshtastic.clientNotification`` topic.
"""

from __future__ import annotations

import math
import threading
import time
from _thread import LockType
from typing import Literal
from weakref import WeakKeyDictionary

from pubsub import pub

from meshtastic._topics import CLIENT_NOTIFICATION_TOPIC
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import admin_pb2, mesh_pb2, portnums_pb2

DEFAULT_KEY_VERIFICATION_TIMEOUT_SECONDS = 60.0

_INITIATE_LOCKS_GUARD = threading.Lock()
_INITIATE_LOCKS: WeakKeyDictionary[MeshInterface, LockType] = WeakKeyDictionary()

STAGE_INITIATE = "initiate"
STAGE_PROVIDE = "provide"
STAGE_VERIFY = "verify"
STAGE_NO_VERIFY = "no-verify"
KEY_VERIFICATION_STAGES = (STAGE_INITIATE, STAGE_PROVIDE, STAGE_VERIFY, STAGE_NO_VERIFY)

_STAGE_MESSAGE_TYPES = {
    STAGE_INITIATE: admin_pb2.KeyVerificationAdmin.INITIATE_VERIFICATION,
    STAGE_PROVIDE: admin_pb2.KeyVerificationAdmin.PROVIDE_SECURITY_NUMBER,
    STAGE_VERIFY: admin_pb2.KeyVerificationAdmin.DO_VERIFY,
    STAGE_NO_VERIFY: admin_pb2.KeyVerificationAdmin.DO_NOT_VERIFY,
}

SECURITY_NUMBER_MIN = 1
SECURITY_NUMBER_MAX = 999999
_NODE_NUM_MAX = 0xFFFFFFFF
_NONCE_MAX = 0xFFFFFFFFFFFFFFFF
_NOTIFICATION_PAYLOAD_FIELDS: tuple[
    Literal["key_verification_number_inform"],
    Literal["key_verification_number_request"],
    Literal["key_verification_final"],
] = (
    "key_verification_number_inform",
    "key_verification_number_request",
    "key_verification_final",
)
_EXPECTED_NOTIFICATION_FIELDS = {
    admin_pb2.KeyVerificationAdmin.INITIATE_VERIFICATION: (
        "key_verification_number_request"
    ),
    admin_pb2.KeyVerificationAdmin.PROVIDE_SECURITY_NUMBER: ("key_verification_final"),
}


def buildKeyVerificationAdmin(
    stage: str,
    *,
    remote_nodenum: int = 0,
    nonce: int = 0,
    security_number: int | None = None,
) -> admin_pb2.KeyVerificationAdmin:
    """Build and validate one stage of the key-verification handshake.

    Parameters
    ----------
    stage : str
        One of ``initiate``, ``provide``, ``verify``, or ``no-verify``.
    remote_nodenum : int
        Node number of the peer being verified; required for ``initiate``.
    nonce : int
        Handshake nonce echoed from the device notification; required for
        every stage after ``initiate``.
    security_number : int | None
        The six-digit number compared out of band; required for ``provide``.

    Returns
    -------
    admin_pb2.KeyVerificationAdmin
        The validated handshake message.

    Raises
    ------
    ValueError
        If the stage is unknown or its required inputs are missing/invalid.
    """
    if stage not in _STAGE_MESSAGE_TYPES:
        raise ValueError(f"unknown key-verification stage: {stage!r}")
    if not 0 <= remote_nodenum <= _NODE_NUM_MAX:
        raise ValueError("remote_nodenum must be a 32-bit node number")
    if not 0 <= nonce <= _NONCE_MAX:
        raise ValueError("nonce must be an unsigned 64-bit value")

    message = admin_pb2.KeyVerificationAdmin()
    message.message_type = _STAGE_MESSAGE_TYPES[stage]

    if stage == STAGE_INITIATE:
        if remote_nodenum == 0:
            raise ValueError("initiate requires the node number of the peer to verify")
        message.remote_nodenum = remote_nodenum
    else:
        if nonce == 0:
            raise ValueError(f"{stage} requires the nonce from the device notification")
        message.nonce = nonce

    if stage == STAGE_PROVIDE:
        if security_number is None:
            raise ValueError(
                "provide requires the security number shown on the remote node"
            )
        if not SECURITY_NUMBER_MIN <= security_number <= SECURITY_NUMBER_MAX:
            raise ValueError(
                "security_number must be "
                f"{SECURITY_NUMBER_MIN}..{SECURITY_NUMBER_MAX} (six digits)"
            )
        message.security_number = security_number
    return message


def _notification_nonce(notification: mesh_pb2.ClientNotification) -> int | None:
    """Return the handshake nonce carried by a key-verification notification."""
    for field in _NOTIFICATION_PAYLOAD_FIELDS:
        if notification.HasField(field):
            nonce: int | None = getattr(notification, field).nonce
            return nonce
    return None


def _initiate_lock_for(interface: MeshInterface) -> LockType:
    """Return the lock serializing nonce-zero initiations for one interface.

    Parameters
    ----------
    interface : MeshInterface
        Connected interface whose initiation handshake must remain exclusive.

    Returns
    -------
    LockType
        Shared lock for initiation handshakes on ``interface``.
    """
    with _INITIATE_LOCKS_GUARD:
        lock = _INITIATE_LOCKS.get(interface)
        if lock is None:
            lock = threading.Lock()
            _INITIATE_LOCKS[interface] = lock
        return lock


def sendKeyVerification(
    interface: MeshInterface,
    request: admin_pb2.KeyVerificationAdmin,
    *,
    timeout: float = DEFAULT_KEY_VERIFICATION_TIMEOUT_SECONDS,
) -> mesh_pb2.ClientNotification | None:
    """Send one handshake stage to the local node and await its notification.

    Parameters
    ----------
    interface : MeshInterface
        Connected mesh interface owning the local node.
    request : admin_pb2.KeyVerificationAdmin
        Handshake stage built by :func:`buildKeyVerificationAdmin`.
    timeout : float
        Seconds to wait for the device's key-verification notification.

    Returns
    -------
    mesh_pb2.ClientNotification | None
        The stage-specific key-verification notification for ``initiate`` or
        ``provide``. Returns ``None`` immediately after sending ``verify`` or
        ``no-verify``, because firmware emits no notification for decisions.

    Raises
    ------
    ValueError
        If ``timeout`` is not finite and positive.
    TimeoutError
        If an expected stage-specific notification does not arrive before
        ``timeout``.
    RuntimeError
        If the interface has not completed its initial node handshake.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    my_info = interface.myInfo
    if my_info is None:
        raise RuntimeError("device did not provide my_info")

    deadline = time.monotonic() + timeout
    initiate_lock: LockType | None = None
    if request.message_type == admin_pb2.KeyVerificationAdmin.INITIATE_VERIFICATION:
        initiate_lock = _initiate_lock_for(interface)
        if not initiate_lock.acquire(timeout=timeout):
            raise TimeoutError(
                "timed out waiting for another key-verification initiation to finish"
            )

    subscribed = False
    try:
        if initiate_lock is not None and time.monotonic() >= deadline:
            raise TimeoutError(
                "timed out waiting for another key-verification initiation to finish"
            )
        expected_notification_field = _EXPECTED_NOTIFICATION_FIELDS.get(
            request.message_type
        )
        event = threading.Event()
        result: mesh_pb2.ClientNotification | None = None
        local_interface: object = interface

        def _on_notification(
            *,
            interface: object,
            notification: mesh_pb2.ClientNotification,
            **_kwargs: object,
        ) -> None:
            nonlocal result
            if interface is not local_interface:
                return
            if (
                notification.WhichOneof("payload_variant")
                != expected_notification_field
            ):
                return
            nonce = _notification_nonce(notification)
            if nonce is None:
                return
            if request.nonce and nonce != request.nonce:
                # Later stages must match the handshake they belong to; the
                # initiate stage (nonce 0) accepts the first fresh notification.
                return
            copied = mesh_pb2.ClientNotification()
            copied.CopyFrom(notification)
            result = copied
            event.set()

        if expected_notification_field is not None:
            pub.subscribe(_on_notification, CLIENT_NOTIFICATION_TOPIC)
            subscribed = True

        admin = admin_pb2.AdminMessage()
        admin.key_verification.CopyFrom(request)
        interface.sendData(
            admin,
            my_info.my_node_num,
            portNum=portnums_pb2.PortNum.ADMIN_APP,
            wantAck=True,
            channelIndex=0,
            pkiEncrypted=False,
            priority=mesh_pb2.MeshPacket.Priority.RELIABLE,
        )
        if expected_notification_field is None:
            return None
        remaining = max(deadline - time.monotonic(), 0.0)
        if event.wait(remaining):
            return result
        raise TimeoutError("no key-verification notification received before timeout")
    finally:
        if subscribed:
            pub.unsubscribe(_on_notification, CLIENT_NOTIFICATION_TOPIC)
        if initiate_lock is not None:
            initiate_lock.release()
