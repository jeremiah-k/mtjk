# Select local transport interfaces

Run `mtjk --list-interfaces` to list local NIC names, assigned addresses, and
Linux BlueZ controllers. Inventory does not scan for or connect to radios.

Select a Bluetooth controller for ordinary BLE commands or scans:

```bash
mtjk --ble-adapter hci0 --ble AA:BB:CC:DD:EE:FF --info
mtjk --ble-adapter hci1 --ble-scan
```

The Python equivalent is `BLEInterface(address, bleAdapter="hci0")` or
`BLEInterface.scan(adapter="hci0")`. Selection is retained across discovery
and reconnects. Explicit BLE controller selection requires Linux/BlueZ;
omitting it preserves the backend's normal default behavior.

For TCP, select a local NIC or one of its IP addresses:

```bash
mtjk --host radio.example:4403 --tcp-interface wlan0 --info
mtjk --host 100.100.100.100 --tcp-interface tailscale0 --info
mtjk --host 127.0.0.1:4403 --tcp-interface 127.0.0.1 --info
```

`TCPInterface(host, networkInterface="wlan0")` provides the same selection in
Python. Each connection attempt binds an address from that interface, matching
the destination's IP family. Names and assigned addresses depend on the host;
use the inventory instead of assuming the example names exist. Source binding
does not override the operating system's routes or enforce a VPN kill switch.

Proxy input and output can use different interfaces:

```bash
mtjk --interface-proxy-in tcp:127.0.0.1:4403 --tcp-interface lo \
  --interface-proxy-out ble-peripheral --interface-proxy-ble-adapter hci1 \
  --interface-proxy-ble-name MT_037b --interface-proxy-allow-public

mtjk --interface-proxy-in ble:AA:BB:CC:DD:EE:FF --ble-adapter hci0 \
  --interface-proxy-out tcp:127.0.0.1:4403 --interface-proxy-bind-interface lo
```

`--interface-proxy-bind-interface` overrides the host in the output URI with
one address from the selected NIC. It prefers the URI's IPv4/IPv6 family and
otherwise uses an available address. This creates one listener address;
it does not bind all addresses on the interface. Non-loopback binds still
require `--interface-proxy-allow-public`, checked against the selected address.

For BLE-to-BLE relays, give the central and peripheral separate controllers
when the hardware cannot support both roles concurrently. A selected controller
must exist and be powered on; the backend reports unavailable controllers.

Hardware qualification on 2026-10-22: inventory listed both physical Bluetooth
adapters and the host's network interfaces. T1000-E USB serial → TCP with
`--interface-proxy-bind-interface lo`, followed by ordinary `--info` using
`--tcp-interface lo`, completed the real radio's configuration download.
A normal CLI BLE `--info` also completed against Docker meshtasticd with
`--ble-adapter hci1`, using a proxy started with
`--interface-proxy-ble-adapter hci0 --tcp-interface lo`. This test used the
unmodified CLI and explicit adapter flags, without overriding Bleak defaults.
