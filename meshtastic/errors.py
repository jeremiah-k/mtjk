"""Public failures for correlated device requests."""

from __future__ import annotations

from meshtastic._interface_errors import MeshInterfaceError

__all__ = (
    "RequestError",
    "RequestTimeoutError",
    "RequestRejectedError",
    "ResponseDecodeError",
)


class RequestError(MeshInterfaceError):
    """A device request failed, with machine-readable request context.

    Attributes
    ----------
    nodeNum : int | None
        Destination node, when known.
    requestId : int | None
        Allocated packet ID, or None when transmission did not start.
    operation : str
        Administrative request variant, such as get_config_request, or
        ``command`` for an embedded CLI action without a typed request variant.
    """

    def __init__(
        self,
        message: str,
        *,
        nodeNum: int | None = None,
        requestId: int | None = None,
        operation: str = "",
    ) -> None:
        super().__init__(message)
        self.nodeNum = nodeNum
        self.requestId = requestId
        self.operation = operation


class RequestTimeoutError(RequestError, TimeoutError):
    """The operation's budget expired before its requested response arrived."""


class RequestRejectedError(RequestError):
    """The device or routing layer refused the request.

    Attributes
    ----------
    reason : str | int
        Routing error reason carried by the response.
    """

    def __init__(
        self,
        reason: str | int,
        *,
        nodeNum: int | None = None,
        requestId: int | None = None,
        operation: str = "",
    ) -> None:
        super().__init__(
            f"Routing error on response: {reason}",
            nodeNum=nodeNum,
            requestId=requestId,
            operation=operation,
        )
        self.reason = reason


class ResponseDecodeError(RequestError):
    """The correlated response contains an undecodable admin payload."""
