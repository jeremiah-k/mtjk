"""Security regression tests for PSK parsing."""

import pytest
from meshtastic.util import fromPSK

@pytest.mark.unit
def test_psk_rejects_plaintext_passwords() -> None:
    """Ensure that common password-like strings are rejected by fromPSK."""
    invalid_psks = [
        "mypassword",
        "hunter2",
        "password123",
        "correct horse battery staple",
        " admin ",
        "!@#$%^&*",
    ]
    for psk in invalid_psks:
        with pytest.raises(ValueError, match="Invalid PSK format"):
            fromPSK(psk)

@pytest.mark.unit
def test_psk_rejects_non_byte_types() -> None:
    """Ensure that fromPSK does not return non-byte types."""
    # Although fromPSK takes a str, we want to ensure it doesn't accidentally
    # return something that would be parsed as another type by fromStr if we were still using it.
    invalid_inputs = [
        "123",    # Would be int
        "123.45", # Would be float
        "True",   # Would be bool
        "no",     # Would be bool
    ]
    for val in invalid_inputs:
        with pytest.raises(ValueError, match="Invalid PSK format"):
            fromPSK(val)

@pytest.mark.unit
def test_psk_rejects_invalid_base64_lengths() -> None:
    """Ensure that raw base64 is only accepted for standard AES key lengths."""
    # "AQ==" is 1 byte, should be rejected if raw
    with pytest.raises(ValueError, match="Invalid PSK format"):
        fromPSK("AQ==")

    # "AAECAwQFBgcICQoLDA0ODw==" is 16 bytes, should be accepted
    assert len(fromPSK("AAECAwQFBgcICQoLDA0ODw==")) == 16

@pytest.mark.unit
def test_psk_accepts_valid_formats() -> None:
    """Ensure valid formats are still accepted."""
    assert fromPSK("random")
    assert fromPSK("none") == b"\x00"
    assert fromPSK("default") == b"\x01"
    assert fromPSK("simple1") == b"\x02"
    assert fromPSK("0x1234") == b"\x124"
    assert fromPSK("base64:AQ==") == b"\x01"
    assert fromPSK("") == b""
