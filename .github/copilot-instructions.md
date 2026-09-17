# Copilot Instructions for Meshtastic Python

## Project Overview

This is the Meshtastic Python library and CLI - a Python API for interacting with Meshtastic mesh radio devices. It supports communication via Serial, TCP, and BLE interfaces.

## Technology Stack

- **Language**: Python 3.10+
- **Package Manager**: Poetry
- **Testing**: pytest with hypothesis for property-based testing
- **Linting**: pylint, Ruff
- **Type Checking**: mypy (working toward strict mode)
- **Documentation**: pdoc3
- **License**: GPL-3.0

## Project Structure

```text
meshtastic/           # Main library package
├── __init__.py       # Core interface classes and pub/sub topics
├── __main__.py       # CLI entry point
├── mesh_interface.py # Base interface class for all connection types
├── serial_interface.py
├── tcp_interface.py
├── ble_interface.py  # BLE compatibility shim (imports from interfaces/ble/)
├── interfaces/       # Modular interface implementations
│   └── ble/          # BLE subsystem (refactored)
│       ├── __init__.py
│       ├── client.py        # BLEClient wrapper for Bleak
│       ├── connection.py    # Connection lifecycle management
│       ├── constants.py     # Configuration and timeouts
│       ├── coordination.py  # Thread coordination utilities
│       ├── discovery.py     # Device discovery strategies
│       ├── errors.py        # Error handling utilities
│       ├── gating.py        # Process-wide connection gating
│       ├── interface.py     # Main BLEInterface implementation
│       ├── notifications.py # Notification subscription management
│       ├── policies.py      # Retry policies
│       ├── reconnection.py  # Auto-reconnect scheduling
│       ├── runner.py        # Singleton asyncio runner
│       ├── state.py         # Connection state machine
│       └── utils.py         # BLE utility functions
├── node.py           # Node representation and configuration
├── protobuf/         # Generated Protocol Buffer files (*_pb2.py, *_pb2.pyi)
├── tests/            # Unit and integration tests
├── powermon/         # Power monitoring tools
└── analysis/         # Data analysis tools
examples/             # Usage examples
protobufs/            # Protocol Buffer source definitions
```

## Coding Standards

### Style Guidelines

- Follow PEP 8 style conventions
- Use type hints for function parameters and return values
- Document public functions and classes with docstrings
- Use NumPy-style docstrings for new and edited docstrings (Ruff pydocstyle convention)
- Prefer explicit imports over wildcard imports
- Use `logging` module instead of print statements for debug output

### Type Annotations

- Add type hints to all new code
- Project typing baseline is Python 3.10+.
- Use PEP 604 unions (`X | None`, `A | B`) and built-in generics (`dict[K, V]`, `list[T]`, `tuple[T, ...]`) in new and edited annotations.
- Do not convert `|` unions to `Optional`/`Union` for compatibility with Python 3.9 (that is out of scope for this project).
- Avoid mass formatting-only annotation churn; normalize types in the area you are already changing.
- If LSP/type-checking appears to reject PEP 604 syntax, fix the interpreter/version configuration (project `venv` / Poetry environment) before editing code.
- Protobuf types are in `meshtastic.protobuf.*_pb2` modules

### Naming Conventions

- Classes: `PascalCase` (e.g., `MeshInterface`, `SerialInterface`)
- Functions/methods: `camelCase` for public API (e.g., `sendText`, `sendData`)
- Internal functions: `snake_case` with leading underscore (e.g., `_send_packet`)
- Canonical compatibility/deprecation inventory is `COMPATIBILITY.md`; keep
  alias/warning behavior aligned with that document.
- BLE compatibility exception: keep the historical BLE public method names from
  the pre-refactor `meshtastic.ble_interface` surface (snake_case such as
  `find_device`, `read_gatt_char`, `start_notify`) to preserve existing project
  compatibility.
- BLE camelCase promotions are intentionally narrow: keep only `findDevice`,
  `isConnected`, and `stopNotify` as promoted BLE camelCase names unless
  maintainers explicitly request additional promotions.
- When adding approved camelCase BLE names, keep legacy snake_case methods as
  compatibility wrappers and route both names to one implementation.
- Avoid mass-generating aliases for internal orchestration methods; aliases are
  strictly for historically public APIs.
- Constants: `UPPER_SNAKE_CASE` (e.g., `BROADCAST_ADDR`, `LOCAL_ADDR`)

### Error Handling

- Use custom exception classes when appropriate (e.g., `MeshInterface.MeshInterfaceError`)
- Provide meaningful error messages
- `**/__main__.py`: Use `our_exit()` from `meshtastic.util` for CLI exits with error codes.
- Treat local `_cli_exit()` helpers as internal/local wrappers only (for module-specific formatting or context).

## Testing

### Test Organization

Tests are in `meshtastic/tests/` and use pytest markers:

- `@pytest.mark.unit` - Fast unit tests (default)
- `@pytest.mark.unitslow` - Slower unit tests
- `@pytest.mark.int` - Integration tests
- `@pytest.mark.smoke1` - Single device smoke tests
- `@pytest.mark.smoke2` - Two device smoke tests
- `@pytest.mark.smokevirt` - Virtual device smoke tests
- `@pytest.mark.examples` - Example validation tests

### Running Tests

```bash
# Run unit tests only (default)
make test
# or
pytest -m unit

# Run all tests
pytest

# Run with coverage
make cov
```

### Writing Tests

- Use `pytest` fixtures from `conftest.py`
- Use `hypothesis` for property-based testing where appropriate
- Mock external dependencies (serial ports, network connections)
- Test file naming: `test_<module_name>.py`

## Pub/Sub Events

The library uses pypubsub for event handling. Key topics:

- `meshtastic.connection.established` - Connection successful
- `meshtastic.connection.lost` - Connection lost
- `meshtastic.receive.text(packet)` - Text message received
- `meshtastic.receive.position(packet)` - Position update received
- `meshtastic.receive.data.portnum(packet)` - Data packet by port number
- `meshtastic.node.updated(node)` - Node database changed
- `meshtastic.log.line(line)` - Raw log line from device

## Protocol Buffers

- Protobuf definitions are in `protobufs/meshtastic/`
- Generated Python files are in `meshtastic/protobuf/`
- Never edit `*_pb2.py` or `*_pb2.pyi` files directly
- Regenerate with: `make protobufs` or `./bin/regen-protobufs.sh`

## Common Patterns

### Creating an Interface

```python
import meshtastic.serial_interface

# Auto-detect device
iface = meshtastic.serial_interface.SerialInterface()

# Specific device
iface = meshtastic.serial_interface.SerialInterface(devPath="/dev/ttyUSB0")

# Always close when done
iface.close()

# Or use context manager
with meshtastic.serial_interface.SerialInterface() as iface:
    iface.sendText("Hello mesh")
```

### Sending Messages

```python
# Text message (broadcast)
iface.sendText("Hello")

# Text message to specific node
iface.sendText("Hello", destinationId="!abcd1234")

# Binary data
iface.sendData(data, portNum=portnums_pb2.PRIVATE_APP)
```

### Subscribing to Events

```python
from pubsub import pub

def on_receive(packet, interface):
    print(f"Received: {packet}")

pub.subscribe(on_receive, "meshtastic.receive")
```

## Development Workflow

Trunk is the repository's linter orchestrator. It pins standalone tools such as
Ruff, while Poetry pins project-aware Python tools such as Pylint and Mypy;
Trunk invokes the latter through the `pylint-poetry` and `mypy-poetry`
definitions. Mypy is the canonical Python type checker.

1. Install dependencies: `poetry install --all-extras --with dev`
2. Make changes
3. Run unified lint/type checks: `TRUNK_INTERACTIVE=0 .trunk/trunk check --fix --show-existing`
4. Run tests: `poetry run pytest -m unit`
5. Update documentation if needed

## CLI Development

The CLI is in `meshtastic/__main__.py`. When adding new CLI commands:

- Use argparse for argument parsing
- Support `--dest` for specifying target node
- Provide `--help` documentation
- Handle errors gracefully with meaningful messages

## Dependencies

### Required

- `pyserial` - Serial port communication
- `protobuf` - Protocol Buffers
- `pypubsub` - Pub/sub messaging
- `bleak` - BLE communication
- `tabulate` - Table formatting
- `pyyaml` - YAML config support
- `requests` - HTTP requests
- `packaging` - Version parsing and comparison helpers

### Optional (extras)

- `cli` extra: `segno`, `print-color`, `argcomplete`, `wcwidth`
- `analysis` extra: `dash`, `dash-bootstrap-components`, `pandas`, `pandas-stubs`
- tunnel support is built in (in-tree `LinuxTunDevice`, stdlib only)
- `powermon` extra: `platformdirs` for platform-specific user data/cache directory resolution

## Important Notes

- Always test with actual Meshtastic hardware when possible
- Be mindful of radio regulations in your region
- The nodedb (`interface.nodes`) is read-only
- Packet IDs are random 32-bit integers
- Default timeout is 300 seconds for operations
