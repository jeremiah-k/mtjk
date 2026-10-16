"""Experimental byte-preserving Meshtastic interface proxy.

Internal implementation: the public CLI is the supported entry point. The proxy
never decodes/re-encodes ``ToRadio`` or ``FromRadio`` payloads.
"""
