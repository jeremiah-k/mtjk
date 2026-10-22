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
read backpressure when downstream delivery is slow. The experimental BLE
peripheral applies the same backpressure on its 128-packet `FROMRADIO` queue
when the connected central reads slowly. Backpressure bounds host
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
the proxy exits, and is not a Windows COM-device emulation. Once a PTY client
has sent data, closing the slave ends the session and closes the upstream;
restart the proxy for a new client. Windows virtual COM
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

## Recorded local qualification

On 2026-10-16, the following read-only connection and configuration checks ran
on Linux with Python 3.13.15. The physical T1000-E reported firmware
`2.8.2.34c9457`; the simulator used the cached
`meshtastic/meshtasticd:2.8.0-alpha-debian` image
(`sha256:099b99e965a4ce5d5220572d3f3160ad0a8c541eccf335d941355d6d5cb35465`).

| Source              | Output         | Observed result                                                                                   |
| ------------------- | -------------- | ------------------------------------------------------------------------------------------------- |
| T1000-E USB serial  | TCP            | Two full configuration downloads across reconnect; normal CLI `--get device.role`; Ctrl+C cleanup |
| T1000-E USB serial  | PTY            | Normal CLI `--get device.role`; proxy exits after client closes                                   |
| T1000-E BLE central | TCP            | Full configuration download using the existing Bluetooth bond                                     |
| T1000-E BLE central | PTY            | Normal CLI `--get device.role`; proxy exits after client closes                                   |
| meshtasticd TCP     | TCP            | Normal CLI `--get device.role`; Ctrl+C cleanup                                                    |
| meshtasticd TCP     | PTY            | Normal CLI `--get device.role`; proxy exits after client closes                                   |
| meshtasticd TCP     | BLE peripheral | GATT setup reached BlueZ, but advertisement registration failed                                   |

The BLE peripheral attempt used unmodified Bless 0.3.0 and BlueZ 5.72. The host
controller rejected advertising with `Invalid Parameters (0x0d)`, including
diagnostic retries with a shorter name and without experimental advertisement
properties. No phone BLE session was established. The successful BLE-central
checks used an existing bond, so PIN entry and fresh pairing were not qualified.

Follow-up adapter-level diagnosis isolated the failure below mtjk and Bless:
`bluetoothctl advertise on` and a minimal raw-D-Bus `LEAdvertisement1`
registration fail identically at the kernel MGMT layer, independent of
advertisement content. A second, unrelated controller (a Broadcom BCM20702A0
USB dongle) rejects advertisement registration with the same
`Invalid Parameters (0x0d)` from its first attempt — across clean bluetoothd
restarts, rfkill controller power-cycles, USB re-enumeration, and empty
advertising instances — although exactly one registration succeeded on it
immediately after plug-in and could not be reproduced. Two controllers from
different vendors failing identically points at the host stack rather than
the adapters: kernel regressions that make MGMT Add Advertising return 0x0d
are a documented, recurring problem class in 2026-era kernels (BlueZ issue
875 and its backported fixes; Raspberry Pi issues 7473 and the
RPi-Distro 407 6.12-series regression). These observations apply to the host
stack tested on October 16; advertising was retested after a kernel update
as recorded below.

Regardless of the eventual root cause, the qualification procedure stands:
confirm the adapter-level canary `bluetoothctl advertise on` succeeds, then a
Bless smoke advertisement, then phone connectivity; keep BLE central sessions
off the same adapter while advertising (BT 4.0-class controllers reject the
concurrent role), and note that an uncleanly terminated advertisement can
leave the adapter unable to re-register until the controller or bluetoothd
is reset.

The gateway now translates backend startup/shutdown failures into CLI errors,
releases its owned BlueZ D-Bus connection after cleanup failures, and restores
the host adapter alias that Bless changes during advertising.

On 2026-10-22, with kernel `7.0.0-38-generic` and BlueZ 5.72, the Broadcom
controller successfully registered repeated `bluetoothctl` advertisements and
an unmodified Bless 0.3.0 Meshtastic GATT advertisement. Advertising therefore
no longer requires the proposed older-kernel comparison on this host.

A second local Bluetooth controller, acting as a paired central, completed
a T1000-E USB serial → BLE peripheral configuration download: 51 protobuf
messages, including the matching configuration-complete token, with a maximum
payload of 243 bytes. The proxy exited when that central disconnected. This
download used a temporary Bless advertisement with `Discoverable=true`; that
change alone did not resolve the Android failure. The gateway now supplies
that property for its own BlueZ advertisement, without changing desktop
discoverability. With desktop discoverability off, Android observed firmware-style
flags `0x06` and classified the proxy as an LE device.
A second download passed with unmodified Bless on the Atheros controller,
using a fresh, unpaired central connection from the Broadcom controller.
An LE-only scan discovered the server and the controller connection list
confirmed LE transport. This qualifies those connections' data paths, rather
than all ATT MTUs or phones.

The Android app could discover the proxy but failed in its bonding step,
before GATT connection or configuration download. Shortening the advertised
name to the firmware-style `MT_fcc6` and enabling advertisement discoverability
did not resolve it. The failure also occurred with the second controller's
distinct address and with a second phone. A successful explicit pairing between
the two local Linux controllers does not establish Android pairing
compatibility. Temporarily enabling desktop discoverability on the Atheros
adapter allowed a pairing-code dialog to appear, but the phone log showed
Meshtastic's 30-second bonding timeout, followed by Android returning from
`BONDING` to `NONE` after about 44 seconds. No completed bond or subsequent
GATT connection to the proxy appeared in that log. Confirming a code therefore
did not qualify phone pairing. The original desktop discoverability settings
were restored after the experiment.

The gateway's characteristics are unencrypted and it does not implement
firmware's fixed PIN pairing mode; `123456` is not configured as a proxy PIN.
Android bonding depends on the host pairing agent; see the qualified setup below.

Further TCP → BLE tests used the Docker meshtasticd simulator on loopback,
with the Atheros controller hosting the peripheral and the Broadcom controller
as the central. A complete configuration download passed with ATT MTU 517,
including notification subscription and the matching configuration-complete
token. The normal `mtjk --info` path also completed with the client explicitly
selected to use the Broadcom controller. Without that selection, a failed CLI
run scanned on the same controller that hosted the peripheral. General BLE
adapter selection is a separate transport-selection concern.

A targeted backend reproduction found that Bless 0.3.0 discards BlueZ read
offsets: a continuation read consumed the next FROMRADIO packet. The gateway
now handles BlueZ continuation reads on its owned D-Bus connection, retaining
the original packet and returning its suffix, as the firmware does. The
offset reproduction and another real TCP → BLE configuration download passed
after the fix. Physical connections at smaller MTUs remain unqualified.

Pairing before GATT timed out with the desktop pairing agent. A temporary
`NoInputNoOutput` test agent restricted to the two local controllers allowed
both sides to record a completed bond and the same configuration download to
finish. That agent was removed and the desktop agent restored after the test;
it is not installed by the gateway. This separates local Bluetooth pairing
authorization from the Meshtastic protocol bytes forwarded over TCP or serial.


Android 2.8.3 qualification used an ADB-connected TCL T768S running Android 11,
Docker meshtasticd TCP input, and the Broadcom controller as the peripheral.
After pairing with a temporary, controller-restricted `NoInputNoOutput` agent,
the app completed both configuration requests (tokens 69420 and 69421), showed
the simulator node and firmware version, and exchanged heartbeats. An earlier
failure dialog remained visible over the connected UI; the matching completion
tokens and subsequent heartbeat traffic establish the successful data path.
The temporary agent is test infrastructure, not installed by the proxy.
Android reported MTU 512; BlueZ supplied 517 in its read options. A separate
Android diagnostic client downloaded all 47 configuration messages at requested
MTU 517. Requesting MTU 23 failed on the first nonempty read, so small-MTU
interoperability is still unqualified despite the continuation-read regression
fix. The Atheros controller also failed the phone's LE link establishment
(status 133, native reason 0x3e) before any GATT request.
