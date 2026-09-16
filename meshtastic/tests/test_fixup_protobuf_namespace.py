"""Regression tests for the protobuf namespace staging transformation."""

import importlib.util
import re
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
def test_rewrite_proto_source_handles_single_quoted_imports() -> None:
    source = textwrap.dedent("""\
        import 'meshtastic/config.proto';
        import public 'meshtastic/field_metadata.proto';
        import weak 'nanopb.proto';
        import 'meshtastic/' 'device_ui.proto';
        """)

    assert _rewrite_proto_source(source) == textwrap.dedent("""\
        import 'meshtastic/protobuf/config.proto';
        import public 'meshtastic/protobuf/field_metadata.proto';
        import weak 'meshtastic/protobuf/nanopb.proto';
        import 'meshtastic/protobuf/' 'device_ui.proto';
        """)


@pytest.mark.unit
def test_rewrite_proto_source_handles_spaced_qualified_symbols() -> None:
    source = textwrap.dedent("""\
        meshtastic . Config spaced = 1;
        meshtastic /* qualifier comment */ . Config commented = 2;
        . meshtastic . Config rooted = 3;
        vendor.meshtastic . Config foreign = 4;
        vendor /* foreign qualifier */ . meshtastic . Config separated_foreign = 5;
        meshtastic.protobuf . Config already_rewritten = 6;
        """)

    rewritten = _rewrite_proto_source(source)

    assert rewritten == textwrap.dedent("""\
        meshtastic.protobuf . Config spaced = 1;
        meshtastic.protobuf /* qualifier comment */ . Config commented = 2;
        . meshtastic.protobuf . Config rooted = 3;
        vendor.meshtastic . Config foreign = 4;
        vendor /* foreign qualifier */ . meshtastic . Config separated_foreign = 5;
        meshtastic.protobuf . Config already_rewritten = 6;
        """)
    assert _rewrite_proto_source(rewritten) == rewritten


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


def _comment_regions(source: str) -> list[str]:
    """Return every comment region byte-for-byte, in order of appearance."""
    return re.findall(r"//[^\n]*|/\*.*?\*/", source, flags=re.DOTALL)


@pytest.mark.unit
def test_rewrite_proto_source_preserves_inline_comments_between_tokens() -> None:
    source = textwrap.dedent("""\
        package /* package comment */ meshtastic;
        import /* import comment */ "meshtastic/config.proto";
        import public /* public comment */ "meshtastic/field_metadata.proto";
        import weak /* weak comment */ "nanopb.proto";
        """)

    rewritten = _rewrite_proto_source(source)

    assert rewritten == textwrap.dedent("""\
        package /* package comment */ meshtastic.protobuf;
        import /* import comment */ "meshtastic/protobuf/config.proto";
        import public /* public comment */ "meshtastic/protobuf/field_metadata.proto";
        import weak /* weak comment */ "meshtastic/protobuf/nanopb.proto";
        """)


@pytest.mark.unit
def test_rewrite_proto_source_preserves_line_comments_between_tokens() -> None:
    source = (
        "package // package comment\n"
        "meshtastic;\n"
        "import // import comment\n"
        '"meshtastic/config.proto";\n'
    )

    rewritten = _rewrite_proto_source(source)

    assert rewritten == (
        "package // package comment\n"
        "meshtastic.protobuf;\n"
        "import // import comment\n"
        '"meshtastic/protobuf/config.proto";\n'
    )


@pytest.mark.unit
def test_rewrite_proto_source_preserves_multiline_block_comments() -> None:
    source = (
        "package /* first line\n"
        "second line */ meshtastic;\n"
        "import /* first line\n"
        'second line */ "meshtastic/config.proto";\n'
    )

    rewritten = _rewrite_proto_source(source)

    assert rewritten == (
        "package /* first line\n"
        "second line */ meshtastic.protobuf;\n"
        "import /* first line\n"
        'second line */ "meshtastic/protobuf/config.proto";\n'
    )


@pytest.mark.unit
def test_rewrite_proto_source_preserves_comment_after_nanopb_import() -> None:
    source = 'import "nanopb.proto" /* staged beside the schemas */;\n'

    rewritten = _rewrite_proto_source(source)

    assert rewritten == (
        'import "meshtastic/protobuf/nanopb.proto"'
        " /* staged beside the schemas */;\n"
    )


@pytest.mark.unit
def test_rewrite_proto_source_preserves_comments_around_qualified_symbols() -> None:
    source = textwrap.dedent("""\
        /* leading */ .meshtastic.Type rooted = 1;
        // own line
        meshtastic.Type plain = 2;
        """)

    rewritten = _rewrite_proto_source(source)

    assert rewritten == textwrap.dedent("""\
        /* leading */ .meshtastic.protobuf.Type rooted = 1;
        // own line
        meshtastic.protobuf.Type plain = 2;
        """)


@pytest.mark.unit
def test_rewrite_proto_source_does_not_match_non_statement_lines() -> None:
    source = textwrap.dedent("""\
        option java_package = "org.meshtastic.proto";
        packagex meshtastic;
        message package_info {
          string label = 1;
        }
        """)

    assert _rewrite_proto_source(source) == source


def _build_comment_preserving_variants() -> list[tuple[str, str, str]]:
    """Build ``(name, source, expected)`` pairs over separator matrices.

    Each variant interleaves legal comment/whitespace separators around the
    namespace tokens; the expected output keeps every non-token byte intact.
    """
    variants: list[tuple[str, str, str]] = []

    package_leads = ["", "  ", "\t", "// lead\n", "/* lead */ "]
    package_inners = [
        " ",
        "\t",
        " /* inner */ ",
        " // inner\n",
        " /* multi\n   line */ ",
    ]
    package_tails = ["", " ", " /* tail */ ", " // tail\n"]
    for lead_index, lead in enumerate(package_leads):
        for inner_index, inner in enumerate(package_inners):
            for tail_index, tail in enumerate(package_tails):
                name = f"package-lead{lead_index}-inner{inner_index}-tail{tail_index}"
                source = f"{lead}package{inner}meshtastic{tail};\n"
                expected = f"{lead}package{inner}meshtastic.protobuf{tail};\n"
                variants.append((name, source, expected))

    import_verbs = ["import", "import public", "import weak"]
    import_inners = [" ", " /* inner */ ", " // inner\n"]
    import_leads = ["", "// lead\n"]
    for verb_index, verb in enumerate(import_verbs):
        for inner_index, inner in enumerate(import_inners):
            for lead_index, lead in enumerate(import_leads):
                name = f"import-verb{verb_index}-inner{inner_index}-lead{lead_index}"
                source = f'{lead}{verb}{inner}"meshtastic/config.proto";\n'
                expected = f'{lead}{verb}{inner}"meshtastic/protobuf/config.proto";\n'
                variants.append((name, source, expected))

    nanopb_verbs = ["import", "import weak"]
    nanopb_inners = [" ", " /* inner */ "]
    nanopb_tails = ["", " /* tail */ ", " // tail\n"]
    for verb_index, verb in enumerate(nanopb_verbs):
        for inner_index, inner in enumerate(nanopb_inners):
            for tail_index, tail in enumerate(nanopb_tails):
                name = f"nanopb-verb{verb_index}-inner{inner_index}-tail{tail_index}"
                source = f'{verb}{inner}"nanopb.proto"{tail};\n'
                expected = f'{verb}{inner}"meshtastic/protobuf/nanopb.proto"{tail};\n'
                variants.append((name, source, expected))

    qualified_placements = ["", " ", " /* pre */ ", "// pre\n", " /* pre\n   */ "]
    qualified_forms = [
        (
            "plain",
            "meshtastic.Type plain = 1;",
            "meshtastic.protobuf.Type plain = 1;",
        ),
        (
            "rooted",
            ".meshtastic.Type rooted = 2;",
            ".meshtastic.protobuf.Type rooted = 2;",
        ),
    ]
    for form_name, before, after in qualified_forms:
        for placement_index, placement in enumerate(qualified_placements):
            name = f"qualified-{form_name}-placement{placement_index}"
            variants.append((name, f"{placement}{before}\n", f"{placement}{after}\n"))

    variants.append(
        (
            "qualified-vendor-unchanged",
            "vendor.meshtastic.Type foreign = 3;\n",
            "vendor.meshtastic.Type foreign = 3;\n",
        )
    )
    variants.append(
        (
            "qualified-prefixed-unchanged",
            "_meshtastic.Type prefixed = 4;\n",
            "_meshtastic.Type prefixed = 4;\n",
        )
    )

    variants.append(
        (
            "combined-block-comments",
            textwrap.dedent("""\
                // header comment
                syntax = "proto3";

                /* lead */ package /* inner
                   line */ meshtastic /* tail */;

                import // lead
                "meshtastic/device_ui.proto";
                import public /* inner */ "meshtastic/field_metadata.proto";
                import weak "nanopb.proto" /* tail */;

                message Example {
                  .meshtastic.Config config = 1;
                  uint32 value = 2 [(meshtastic.field_metadata) = {diy_only: true}];
                }
                """),
            "// header comment\n"
            'syntax = "proto3";\n'
            "\n"
            "/* lead */ package /* inner\n"
            "   line */ meshtastic.protobuf /* tail */;\n"
            "\n"
            "import // lead\n"
            '"meshtastic/protobuf/device_ui.proto";\n'
            'import public /* inner */ "meshtastic/protobuf/'
            'field_metadata.proto";\n'
            'import weak "meshtastic/protobuf/nanopb.proto" /* tail */;\n'
            "\n"
            "message Example {\n"
            "  .meshtastic.protobuf.Config config = 1;\n"
            "  uint32 value = 2 [(meshtastic.protobuf.field_metadata)"
            " = {diy_only: true}];\n"
            "}\n",
        )
    )
    variants.append(
        (
            "combined-line-comments",
            textwrap.dedent("""\
                // header comment
                syntax = "proto3";

                package // package comment
                meshtastic;

                import // import comment
                "meshtastic/config.proto";
                import public // public comment
                "meshtastic/field_metadata.proto";
                import weak // weak comment
                "nanopb.proto";

                message Example {
                  .meshtastic.Config config = 1;
                  uint32 value = 2 [(meshtastic.field_metadata) = {diy_only: true}];
                }
                """),
            "// header comment\n"
            'syntax = "proto3";\n'
            "\n"
            "package // package comment\n"
            "meshtastic.protobuf;\n"
            "\n"
            "import // import comment\n"
            '"meshtastic/protobuf/config.proto";\n'
            "import public // public comment\n"
            '"meshtastic/protobuf/field_metadata.proto";\n'
            "import weak // weak comment\n"
            '"meshtastic/protobuf/nanopb.proto";\n'
            "\n"
            "message Example {\n"
            "  .meshtastic.protobuf.Config config = 1;\n"
            "  uint32 value = 2 [(meshtastic.protobuf.field_metadata)"
            " = {diy_only: true}];\n"
            "}\n",
        )
    )

    return variants


_VARIANTS = _build_comment_preserving_variants()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("source", "expected"),
    [(source, expected) for _, source, expected in _VARIANTS],
    ids=[name for name, _, _ in _VARIANTS],
)
def test_rewrite_proto_source_preserves_comments_across_variants(
    source: str, expected: str
) -> None:
    rewritten = _rewrite_proto_source(source)

    assert rewritten == expected
    assert _comment_regions(source) == _comment_regions(rewritten)
    assert _rewrite_proto_source(rewritten) == rewritten
