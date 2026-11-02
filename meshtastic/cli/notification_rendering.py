"""CLI rendering of generic client notifications from the device."""

import logging
from collections.abc import Callable

from meshtastic.protobuf import mesh_pb2
from meshtastic.schema_metadata import _get_enum_value_metadata

logger = logging.getLogger(__name__)


def _notification_type_label(
    notification: mesh_pb2.ClientNotification,
) -> str | None:
    """Return the schema label for a notification's type, when informative.

    Parameters
    ----------
    notification : mesh_pb2.ClientNotification
        Notification whose ``type`` field is inspected.

    Returns
    -------
    str | None
        The enum value's schema label (for example ``"Duty Cycle Limit
        Exceeded"``), or None when the type is unset, unknown, or carries no
        label.
    """
    notification_type = mesh_pb2.ClientNotification.NotificationType
    value = notification.type
    if not value or value not in notification_type.values():
        return None
    descriptor = notification_type.DESCRIPTOR.values_by_number.get(value)
    if descriptor is None:
        return None
    metadata = _get_enum_value_metadata(descriptor)
    if metadata is None:
        return None
    return metadata.label


def _render_client_notification(
    notification: mesh_pb2.ClientNotification,
    cli_print: Callable[[str], None],
) -> None:
    """Render a generic client notification with its type label when known.

    Parameters
    ----------
    notification : mesh_pb2.ClientNotification
        Notification received from the device.
    cli_print : Callable[[str], None]
        Sink for the rendered line.
    """
    label = _notification_type_label(notification)
    if label:
        cli_print(f"Notification ({label}): {notification.message}")
    else:
        cli_print(f"Notification: {notification.message}")
