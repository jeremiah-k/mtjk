# Experimental interface proxy

The interface proxy forwards the **raw Meshtastic device protocol**, not mesh
application messages. It allows a single physical or remote radio to be accessed
through a different local transport without creating a second `MeshInterface`
instance, rewriting protobufs, or running a separate MQTT broker.

## Transport layout

```text
Meshtastic device <--USB serial / TCP / BLE central--> mtjk proxy
                                                     |
                                                     +-- TCP listener
                                                     +-- POSIX virtual serial PTY
                                                     +-- BLE peripheral (experimental)
```

The source is configured with `--interface-proxy-in`, the client-facing endpoint
with `--interface-proxy-out`. Both are **required** and replace the ordinary
`--port`, `--host`, and `--ble` selectors in this mode.

```shell
# Make a USB-connected rooftop node accessible to existing TCP clients.
mtjk --interface-proxy-in serial:/dev/ttyUSB0 \
     --interface-proxy-out tcp:127.0.0.1:4403
# In another process:
mtjk --host localhost:4403 --info

# Turn an upstream TCP radio into a virtual serial port (Linux/macOS).
mtjk --interface-proxy-in tcp:192.168.1.20:4403 \
     --interface-proxy-out pty
# The proxy prints a temporary path such as /dev/pts/4; use that path:
mtjk --port /dev/pts/4 --info

# Expose a real BLE radio locally through TCP.
mtjk --interface-proxy-in ble:AA:BB:CC:DD:EE:FF \
     --interface-proxy-out tcp:127.0.0.1:4403

# Advertise a USB radio as an experimental Meshtastic BLE peripheral.
# Install Bless 0.3.0 in the SAME environment first (not a core dependency).
uv pip install 'bless==0.3.0'
mtjk --interface-proxy-in serial:/dev/ttyUSB0 \
     --interface-proxy-out ble-peripheral \
     --interface-proxy-ble-name Meshtastic_0123 \
     --interface-proxy-allow-public
```

TCP endpoint syntax is `tcp:HOST:PORT`; use brackets for IPv6, for example
`tcp:[::1]:4403`. Only one downstream TCP client can own the source radio; an
additional connection is immediately closed. A new TCP session creates a new
upstream connection. TCP binds to loopback unless explicitly allowed with
`--interface-proxy-allow-public`.

## On-wire handling

Serial and TCP use the existing `0x94 0xC3` prefix followed by an unsigned
big-endian 16-bit length and the protobuf payload. BLE GATT transports the
protobuf **without** this four-byte stream header. The proxy converts only
that framing and enforces the existing 512-byte stream message maximum. It
does not parse, de-duplicate, decrypt, transform, or synthesize `ToRadio` and
`FromRadio` messages.

The BLE-central reader buffers up to 256 raw `FROMRADIO` packets, applying GATT
read backpressure when downstream delivery is slow. Backpressure bounds host
memory, but cannot guarantee that a radio's own smaller queue will never fill;
slow downstream clients should be avoided. Already-buffered messages drain
before disconnect is reported. A failed or over-limit GATT read is surfaced as
a session error rather than mistaken for a clean device disconnect.

The BLE service (`6ba1b218-15a8-461f-9fa8-5dcae273eafd`) and its three
transport characteristics are the same as in the Android client and mtjk:

| Characteristic | UUID                                   | Operation                           |
| -------------- | -------------------------------------- | ----------------------------------- |
| `TORADIO`      | `f75c76d2-129e-4dad-a1dd-7866124401e7` | unframed write                      |
| `FROMRADIO`    | `2c55e69e-4993-11ed-b878-0242ac120002` | unframed consuming read             |
| `FROMNUM`      | `ed9da18c-a800-4f66-a670-aa7547e34453` | 4-byte little-endian counter/notify |

The gateway deliberately does **not** advertise the OTA or logging services of
a physical radio. It is not a drop-in OTA transport or a replacement for the
firmware's complete BLE peripheral implementation. Device-name conventions
follow Android's `…_XXXX` hexadecimal suffix.

`MqttClientProxyMessage` is a regular `ToRadio`/`FromRadio` protobuf variant;
it passes unchanged. A client application must still implement its MQTT proxy
logic (including broker access, authentication, and subscriptions). This
feature does **not** turn mtjk into an MQTT gateway by itself.

## Platform and security matrix

| Endpoint                     | Linux        | macOS        | Windows      | Notes                                                              |
| ---------------------------- | ------------ | ------------ | ------------ | ------------------------------------------------------------------ |
| TCP listener / upstream TCP  | Yes          | Yes          | Yes          | Works on any host with sockets                                     |
| Physical USB serial upstream | Yes          | Yes          | Yes          | Existing pyserial dependency; OS port permissions apply            |
| BLE upstream (central)       | Expected     | Expected     | Expected     | Existing Bleak dependency; pair/device access depends on OS        |
| Virtual serial output `pty`  | Yes          | Yes          | No           | `/dev/pts/N` or `/dev/ttysNNN`; no physical USB CDC device         |
| BLE GATT peripheral output   | Experimental | Experimental | Experimental | Optional Bless 0.3.0; hardware/OS privileges may block advertising |

**Security:** no user authentication, authorization, or packet filtering is
provided. A connected client may operate/configure the upstream device with
whatever authority the upstream transport exposes. Do not bind TCP publicly
without a firewall/VPN and appropriate operational controls. BLE advertising
also requires explicit `--interface-proxy-allow-public`. That flag is an
operator acknowledgment **not** an authentication mechanism. Do not expose a
sensitive admin radio through an untrusted network or Bluetooth environment.

**BLE limitations:** Bless wraps BlueZ D-Bus on Linux and the native GATT APIs
on macOS/Windows. Adapter peripheral-role support, pairing/bonding, advertisement
permissions, GATT characteristic read length/ATT MTU, and multi-central behavior
are platform-specific. Only **one central** is intended; the current GATT
backend cannot reliably enforce a central identity across all systems. A
BLE session ends when its client or upstream connection ends; restart the proxy
rather than switching physical radios behind an active GATT client. Some adapters
cannot be a BLE central and peripheral simultaneously. The BLE peripheral is a
**hardware-validation candidate**, not a production compatibility guarantee.
Windows Bless installation may additionally require `pysetupdi` as documented
by Bless.

**PTY limitations:** Unix pseudo-terminals cannot enforce a single external
reader at the kernel API level. Use exactly one serial client, and open the
slave while the proxy is running. The PTY path is ephemeral, disappears when
the proxy exits, and is not a Windows COM-device emulation. Windows virtual COM
endpoints require an external signed virtual-port driver and are not included.

## Hardware qualification before production use

1. Verify serial→TCP and TCP→PTY with ordinary `mtjk --info`, node discovery,
   send/receive, local config reads, reconnect, and graceful disconnect.
2. Repeat with an awake and sleeping USB radio; check serial wake behavior and
   unrelated device debug bytes mixed into the serial stream.
3. Validate Android and mtjk clients on the Bless-advertised service with BlueZ,
   then macOS and Windows where supported; confirm discovery, pairing, MTU,
   long `FROMRADIO` reads, subscriptions, notification wraparound and cleanup.
4. Force link loss, unplug, permission denial, slow readers, extra TCP clients,
   BLE read/write failures, and device reboot during a config download.
5. Exercise Android MQTT client-proxy mode **with a real companion broker** to
   verify radio packet transport, without assuming the gateway publishes MQTT.
6. Confirm that remote users cannot reach a privileged TCP listener by mistake;
   gate experiments using a trusted private network.

The automated test suite uses real loopback sockets and a POSIX PTY, plus a fake
Bless server, but **does not** establish physical BLE interoperability. Run the
normal CI suite and this hardware checklist before treating advertising as
supported on a platform.
