"""Regression tests for the protobuf namespace staging transformation."""

import importlib.util
import sys
import textwrap
import types
from pathlib import Path
from unittest.mock import patch

import pytest

_SCRIPT_PATH = (
    Path(__file__).parent.parent.parent / "bin" / "fixup_protobuf_namespace.py"
)


def _load_fixup_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "fixup_protobuf_namespace", _SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with patch.object(sys, "argv", ["fixup_protobuf_namespace.py"]):
        spec.loader.exec_module(module)
    return module


_fixup = _load_fixup_module()
_rewrite_proto_source = _fixup._rewrite_proto_source
_rewrite_proto_file = _fixup._rewrite_proto_file
_rewrite_proto_directory = _fixup._rewrite_proto_directory


@pytest.mark.unit
def test_rewrite_proto_source_moves_package_imports_and_qualified_symbols() -> None:
    source = textwrap.dedent("""\
        syntax = "proto3";

        package meshtastic;

        import "meshtastic/device_ui.proto";
        /* trunk-ignore(buf-lint/COMPILE) */
        import public "meshtastic/field_metadata.proto";
        // nanopb is staged beside the Meshtastic schemas.
        import weak "nanopb.proto";

        option java_package = "org.meshtastic.proto";

        message Example {
          uint32 value = 1 [(meshtastic.field_metadata) = {diy_only: true}];
          .meshtastic.Config config = 2;
        }
        """)

    rewritten = _rewrite_proto_source(source)

    assert "package meshtastic.protobuf;" in rewritten
    assert 'import "meshtastic/protobuf/device_ui.proto";' in rewritten
    assert "/* trunk-ignore(buf-lint/COMPILE) */" in rewritten
    assert 'import public "meshtastic/protobuf/field_metadata.proto";' in rewritten
    assert "// nanopb is staged beside the Meshtastic schemas." in rewritten
    assert 'import weak "meshtastic/protobuf/nanopb.proto";' in rewritten
    assert "(meshtastic.protobuf.field_metadata)" in rewritten
    assert ".meshtastic.protobuf.Config config = 2;" in rewritten
    assert 'option java_package = "org.meshtastic.proto";' in rewritten


@pytest.mark.unit
def test_rewrite_proto_source_handles_field_and_enum_metadata_from_680d829() -> None:
    source = textwrap.dedent("""\
        syntax = "proto3";
        package meshtastic;
        import "meshtastic/field_metadata.proto";

        message Config {
          message PositionConfig {
            enum PositionFlags {
              UNSET = 0;
              ALTITUDE = 1 [(meshtastic.enum_value_metadata) = {
                label: "Altitude"
              }];
            }
            uint32 rx_gpio = 8 [(meshtastic.field_metadata) = {diy_only: true}];
          }
        }
        """)

    rewritten = _rewrite_proto_source(source)

    assert 'import "meshtastic/protobuf/field_metadata.proto";' in rewritten
    assert "(meshtastic.protobuf.enum_value_metadata)" in rewritten
    assert "(meshtastic.protobuf.field_metadata)" in rewritten
    assert "(meshtastic.enum_value_metadata)" not in rewritten
    assert "(meshtastic.field_metadata)" not in rewritten


@pytest.mark.unit
def test_rewrite_proto_source_is_generic_for_future_qualified_symbols() -> None:
    source = textwrap.dedent("""\
        uint32 value = 1 [(meshtastic.future_option) = true];
        .meshtastic.FutureMessage nested = 2;
        """)

    assert _rewrite_proto_source(source) == textwrap.dedent("""\
        uint32 value = 1 [(meshtastic.protobuf.future_option) = true];
        .meshtastic.protobuf.FutureMessage nested = 2;
        """)


@pytest.mark.unit
def test_rewrite_proto_source_rewrites_only_root_qualified_symbols() -> None:
    source = textwrap.dedent("""\
        meshtastic.Type plain = 1;
        .meshtastic.Type rooted = 2;
        vendor.meshtastic.Type foreign = 3;
        _meshtastic.Type prefixed = 4;
        """)

    assert _rewrite_proto_source(source) == textwrap.dedent("""\
        meshtastic.protobuf.Type plain = 1;
        .meshtastic.protobuf.Type rooted = 2;
        vendor.meshtastic.Type foreign = 3;
        _meshtastic.Type prefixed = 4;
        """)


@pytest.mark.unit
def test_rewrite_proto_source_preserves_strings_and_comments() -> None:
    source = textwrap.dedent("""\
        // (meshtastic.comment_option) must stay documentation.
        /*
         * package meshtastic;
         * import "meshtastic/config.proto";
         * import "nanopb.proto";
         * (meshtastic.block_option) must also stay documentation.
         */
        option java_package = "org.meshtastic.proto";
        option csharp_namespace = '(meshtastic.single_quoted_option)';
        option documentation = "https://example.invalid/meshtastic.config";
        uint32 value = 1 [(meshtastic.real_option) = true];
        """)

    rewritten = _rewrite_proto_source(source)

    assert "// (meshtastic.comment_option) must stay documentation." in rewritten
    assert " * package meshtastic;" in rewritten
    assert ' * import "meshtastic/config.proto";' in rewritten
    assert ' * import "nanopb.proto";' in rewritten
    assert " * (meshtastic.block_option) must also stay documentation." in rewritten
    assert 'option java_package = "org.meshtastic.proto";' in rewritten
    assert "'(meshtastic.single_quoted_option)'" in rewritten
    assert '"https://example.invalid/meshtastic.config"' in rewritten
    assert "(meshtastic.protobuf.real_option)" in rewritten


@pytest.mark.unit
def test_rewrite_proto_source_is_idempotent() -> None:
    source = textwrap.dedent("""\
        package meshtastic;
        import "meshtastic/config.proto";
        message Example {
          uint32 value = 1 [(meshtastic.field_metadata) = {diy_only: true}];
          .meshtastic.Config config = 2;
        }
        """)

    once = _rewrite_proto_source(source)

    assert _rewrite_proto_source(once) == once
    assert "meshtastic.protobuf.protobuf" not in once
    assert "meshtastic/protobuf/protobuf" not in once


@pytest.mark.unit
def test_rewrite_proto_file_reports_whether_content_changed(tmp_path: Path) -> None:
    proto = tmp_path / "config.proto"
    proto.write_text("package meshtastic;\n", encoding="utf-8")

    assert _rewrite_proto_file(proto) is True
    assert _rewrite_proto_file(proto) is False
    assert proto.read_text(encoding="utf-8") == "package meshtastic.protobuf;\n"


@pytest.mark.unit
def test_rewrite_proto_directory_only_changes_proto_files(tmp_path: Path) -> None:
    proto = tmp_path / "config.proto"
    options = tmp_path / "config.options"
    proto.write_text('package meshtastic;\nimport "nanopb.proto";\n', encoding="utf-8")
    options.write_text("*Config.value max_size:8\n", encoding="utf-8")

    assert _rewrite_proto_directory(tmp_path) == 1
    assert "package meshtastic.protobuf;" in proto.read_text(encoding="utf-8")
    assert options.read_text(encoding="utf-8") == "*Config.value max_size:8\n"


@pytest.mark.unit
def test_rewrite_proto_directory_rejects_missing_directory(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(NotADirectoryError, match="missing"):
        _rewrite_proto_directory(missing)
