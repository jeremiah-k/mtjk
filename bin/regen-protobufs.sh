#!/bin/bash

set -e

NANOPB_VERSION="${NANOPB_VERSION:-0.4.9.2}"
NANOPB_DIR="${NANOPB_DIR:-./nanopb-${NANOPB_VERSION}}"
NANOPB_LINUX_DIR="${NANOPB_LINUX_DIR:-./nanopb-${NANOPB_VERSION}-linux-x86}"
NANOPB_DOWNLOAD_URL="${NANOPB_DOWNLOAD_URL:-https://github.com/nanopb/nanopb/releases/download/nanopb-${NANOPB_VERSION}/nanopb-${NANOPB_VERSION}-linux-x86.tar.gz}"
NANOPB_DOWNLOAD_FALLBACK_URL="${NANOPB_DOWNLOAD_FALLBACK_URL:-https://jpa.kapsi.fi/nanopb/download/nanopb-${NANOPB_VERSION}-linux-x86.tar.gz}"

#Uncomment to run hack
#gsed -i 's/import "\//import ".\//g' ./protobufs/meshtastic/*
#gsed -i 's/package meshtastic;//g' ./protobufs/meshtastic/*

# uv run exposes the locked mypy plugin on PATH for protoc.
uv sync --locked

if [[ -z ${PROTOC-} ]]; then
	for PROTOC_CANDIDATE in \
		"${NANOPB_DIR}/generator-bin/protoc" \
		"${NANOPB_LINUX_DIR}/generator-bin/protoc"; do
		if [[ -x ${PROTOC_CANDIDATE} ]]; then
			PROTOC="${PROTOC_CANDIDATE}"
			break
		fi
	done
fi

if [[ -z ${PROTOC-} && ${ALLOW_SYSTEM_PROTOC:-0} == 1 ]] && command -v protoc >/dev/null 2>&1; then
	PROTOC="$(command -v protoc)"
fi

if [[ -z ${PROTOC-} || ! -x ${PROTOC} ]]; then
	cat >&2 <<EOF
Unable to find a protoc compiler.

Set PROTOC=/path/to/protoc, set ALLOW_SYSTEM_PROTOC=1 to use protoc from PATH, or download nanopb:
  curl -fsSL -o nanopb-${NANOPB_VERSION}-linux-x86.tar.gz ${NANOPB_DOWNLOAD_URL}
  curl -fsSL -o nanopb-${NANOPB_VERSION}-linux-x86.tar.gz ${NANOPB_DOWNLOAD_FALLBACK_URL}
  tar xzf nanopb-${NANOPB_VERSION}-linux-x86.tar.gz
  mv nanopb-${NANOPB_VERSION}-linux-x86 nanopb-${NANOPB_VERSION}

The nanopb directory is intentionally ignored by git.
EOF
	exit 1
fi

echo "Using protoc: ${PROTOC}"
"${PROTOC}" --version

# Put generated files in the project's build directory.
PROTO_WORK_DIR=./build/meshtastic/protofixup
echo "Fixing up protobuf paths in ${PROTO_WORK_DIR} temp directory"

# Ensure a clean build
[[ -e ${PROTO_WORK_DIR} ]] && rm -r "${PROTO_WORK_DIR}"

INDIR=${PROTO_WORK_DIR}/in/meshtastic/protobuf
OUTDIR=${PROTO_WORK_DIR}/out
PYIDIR=${PROTO_WORK_DIR}/out
mkdir -p "${OUTDIR}" "${INDIR}" "${PYIDIR}"
cp ./protobufs/meshtastic/*.proto "${INDIR}"
cp ./protobufs/nanopb.proto "${INDIR}"
cp ./protobufs/meshtastic/*.options "${INDIR}"

# Rewrite the upstream protobuf namespace consistently before generation,
# including package declarations, import paths, and code-level qualified
# references such as (meshtastic.field_metadata).
uv run --locked python ./bin/fixup_protobuf_namespace.py "${INDIR}"

# OS-X sed is apparently a little different and expects an arg for -i
if [[ ${OSTYPE-} == darwin* ]]; then
	SEDCMD=(sed -i '' -E)
else
	SEDCMD=(sed -i -E)
fi

# Inject nanopb .options constraints as inline proto field options so that
# protoc --python_out embeds them in the generated descriptors.  Python code
# can then read them via:
#   field.GetOptions().Extensions[nanopb_pb2.nanopb].max_size
echo "Injecting nanopb options into proto files..."
for OPTS_FILE in "${INDIR}"/*.options; do
	BASENAME=$(basename "${OPTS_FILE}" .options)
	PROTO_FILE="${INDIR}/${BASENAME}.proto"
	if [[ -f ${PROTO_FILE} ]]; then
		uv run --locked python ./bin/inject_nanopb_options.py "${OPTS_FILE}" "${PROTO_FILE}"
	fi
done

# Generate the python files
uv run --locked "${PROTOC}" -I="${PROTO_WORK_DIR}/in" --python_out "${OUTDIR}" "--mypy_out=${PYIDIR}" "${INDIR}"/*.proto

# Change "from meshtastic.protobuf import" to "from . import"
"${SEDCMD[@]}" 's/^from meshtastic.protobuf import/from . import/' "${OUTDIR}"/meshtastic/protobuf/*pb2*.py
"${SEDCMD[@]}" 's/^from meshtastic.protobuf import/from . import/' "${OUTDIR}"/meshtastic/protobuf/*pb2*.pyi

# Create a __init__.py in the out directory
touch "${OUTDIR}/meshtastic/protobuf/__init__.py"

# Copy to the source controlled tree
mkdir -p meshtastic/protobuf
rm -rf meshtastic/protobuf/*pb2*.py
cp "${OUTDIR}/meshtastic/protobuf"/* meshtastic/protobuf

exit 0
