#!/usr/bin/env bash

set -euo pipefail

echo "Building Ubuntu binary"
if [[ -f uv.lock ]]; then
	uv sync --locked --extra cli
	PROJECT_RUN=(uv run --locked)
elif [[ -f poetry.lock ]]; then
	# Manual asset recovery can target releases with only a Poetry lockfile.
	# Keep their locked build tooling isolated from the uv project environment.
	uv tool run --from poetry==2.5.1 poetry install --extras cli --with dev
	PROJECT_RUN=(uv tool run --from poetry==2.5.1 poetry run)
else
	echo "A committed uv.lock or historical poetry.lock is required to build standalone assets." >&2
	exit 1
fi

distribution_name="$("${PROJECT_RUN[@]}" python -c 'from meshtastic._branding import DISTRIBUTION_NAME; print(DISTRIBUTION_NAME)')"
primary_cli="$("${PROJECT_RUN[@]}" python -c 'from meshtastic._branding import PRIMARY_CLI_NAME; print(PRIMARY_CLI_NAME)')"

# Preserve product package data without collecting the test tree. Historical
# release tags may predate the compatibility manifest.
product_data=()
for member in py.typed _runtime_compatibility.json; do
	if [[ -f "meshtastic/${member}" ]]; then
		product_data+=(--add-data "meshtastic/${member}:meshtastic")
	fi
done

# Filter the package walk itself. An analysis exclusion alone still hands
# every discovered test module to PyInstaller as a hidden import.
hook_dir="$(mktemp -d)"
trap 'rm -rf -- "${hook_dir}"' EXIT
cat >"${hook_dir}/hook-meshtastic.py" <<'PY'
from PyInstaller.utils.hooks import collect_submodules, is_module_or_submodule

hiddenimports = collect_submodules(
    "meshtastic",
    filter=lambda name: not is_module_or_submodule(name, "meshtastic.tests"),
)
# pkgutil's walk from meshtastic skips this implicit namespace package.
hiddenimports += collect_submodules("meshtastic.interfaces")
PY

"${PROJECT_RUN[@]}" pyinstaller \
	--clean \
	--noconfirm \
	-F \
	-n "${primary_cli}" \
	--copy-metadata "${distribution_name}" \
	--additional-hooks-dir "${hook_dir}" \
	--exclude-module meshtastic.tests \
	"${product_data[@]}" \
	meshtastic/__main__.py

# Inspect the actual frozen archive, not just the collection arguments. The
# recursive listing includes embedded PYZ modules as well as packaged data.
archive_members="$("${PROJECT_RUN[@]}" pyi-archive_viewer --recursive --brief "dist/${primary_cli}")"
if grep -Eq '(^|[[:space:]])meshtastic([./\\])tests([./\\]|$)' <<<"${archive_members}"; then
	echo "Standalone archive contains meshtastic.tests; refusing product-only build." >&2
	exit 1
fi

if [[ -n ${GITHUB_OUTPUT:-} ]]; then
	printf 'primary=%s\n' "${primary_cli}" >>"${GITHUB_OUTPUT}"
fi
