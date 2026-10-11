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

"${PROJECT_RUN[@]}" pyinstaller \
	--clean \
	--noconfirm \
	-F \
	-n "${primary_cli}" \
	--copy-metadata "${distribution_name}" \
	--collect-all meshtastic \
	meshtastic/__main__.py

if [[ -n ${GITHUB_OUTPUT:-} ]]; then
	printf 'primary=%s\n' "${primary_cli}" >>"${GITHUB_OUTPUT}"
fi
