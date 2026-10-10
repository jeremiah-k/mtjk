"""Internal convenience exports for Node runtime implementations.

The ``node_runtime`` package is an implementation detail and its ``__all__``
list is not public API. Runtime compatibility guarantees are limited to entries
in ``meshtastic/_runtime_compatibility.json`` and documented in
``docs/compatibility.md``.
"""

from .shared import (
    DELETE_FILE_PATH_TOO_LONG_MSG,
    EMPTY_LONG_NAME_MSG,
    EMPTY_SHORT_NAME_MSG,
    FACTORY_RESET_REQUEST_VALUE,
    MAX_CANNED_MESSAGE_LENGTH,
    MAX_CHANNELS,
    MAX_DELETE_FILE_PATH_BYTES,
    MAX_INPUT_EVENT_CODE,
    MAX_INPUT_KB_CHAR,
    MAX_INPUT_TOUCH_X,
    MAX_INPUT_TOUCH_Y,
    MAX_LONG_NAME_LEN,
    MAX_RINGTONE_LENGTH,
    MAX_SHORT_NAME_LEN,
    METADATA_STDOUT_COMPAT_WAIT_SECONDS,
    NAMED_ADMIN_CHANNEL_NAME,
)

__all__ = [
    "DELETE_FILE_PATH_TOO_LONG_MSG",
    "EMPTY_LONG_NAME_MSG",
    "EMPTY_SHORT_NAME_MSG",
    "FACTORY_RESET_REQUEST_VALUE",
    "MAX_CANNED_MESSAGE_LENGTH",
    "MAX_CHANNELS",
    "MAX_DELETE_FILE_PATH_BYTES",
    "MAX_INPUT_EVENT_CODE",
    "MAX_INPUT_KB_CHAR",
    "MAX_INPUT_TOUCH_X",
    "MAX_INPUT_TOUCH_Y",
    "MAX_LONG_NAME_LEN",
    "MAX_RINGTONE_LENGTH",
    "MAX_SHORT_NAME_LEN",
    "METADATA_STDOUT_COMPAT_WAIT_SECONDS",
    "NAMED_ADMIN_CHANNEL_NAME",
]
