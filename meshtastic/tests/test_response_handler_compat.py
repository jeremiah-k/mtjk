"""Test ResponseHandler backward compatibility for tuple unpacking.

This file documents that ResponseHandler maintains backward compatibility
for 2-element tuple unpacking.

IMPORTANT: ResponseHandler has 2 fields (callback, ackPermitted) and
2-element tuple unpacking is supported:
    callback, ack_permitted = handler

This pattern works correctly and is the expected behavior.
"""

from typing import Any

from meshtastic import ResponseHandler


def test_response_handler_two_element_unpacking_works() -> None:
    """Verify that ResponseHandler supports 2-element tuple unpacking.

    Historical code doing:
        callback, ack_permitted = handler

    Should work correctly with the 2-field ResponseHandler.
    """

    def dummy_callback(_response: dict[str, Any]) -> None:
        pass

    handler = ResponseHandler(callback=dummy_callback, ackPermitted=True)

    # 2-element unpacking works correctly
    callback, ack_permitted = handler
    assert callback is dummy_callback
    assert ack_permitted is True


def test_response_handler_fields_can_be_accessed_by_name() -> None:
    """Verify ResponseHandler fields can be accessed by name."""

    def dummy_callback(_response: dict[str, Any]) -> None:
        pass

    handler = ResponseHandler(callback=dummy_callback, ackPermitted=False)

    # Named access works correctly
    assert handler.callback is dummy_callback
    assert handler.ackPermitted is False


def test_response_handler_iteration_yields_all_field_values() -> None:
    """Verify that iterating over ResponseHandler yields all 2 field values."""

    def dummy_callback(_response: dict[str, Any]) -> None:
        pass

    handler = ResponseHandler(callback=dummy_callback, ackPermitted=True)

    # Iterating yields all 2 values
    values = list(handler)
    assert len(values) == 2
    assert values[0] is dummy_callback
    assert values[1] is True


def test_response_handler_length_is_two() -> None:
    """Verify len() returns 2 (all fields including defaults)."""

    def dummy_callback(_response: dict[str, Any]) -> None:
        pass

    handler = ResponseHandler(callback=dummy_callback, ackPermitted=True)
    assert len(handler) == 2
