#!/usr/bin/env bash
set -euo pipefail

# Generate a baseline from a git ref (default: upstream/master) using the
# current extractor script, without checking out that ref in the working tree.

MASTER_REF="${1:-upstream/master}"

REPO_ROOT="$(git rev-parse --show-toplevel)"
OUT_FILE="${REPO_ROOT}/meshtastic/tests/api_baselines/api_baseline_master.json"

if ! git -C "${REPO_ROOT}" rev-parse --verify "${MASTER_REF}" >/dev/null 2>&1; then
	echo "error: git ref '${MASTER_REF}' not found" >&2
	echo "hint: if missing, add the upstream remote:" >&2
	echo "  git remote add upstream https://github.com/meshtastic/python.git && git fetch upstream master" >&2
	exit 1
fi

tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/meshtastic-master-baseline.XXXXXX")"
cleanup() {
	rm -rf "${tmpdir}"
}
trap cleanup EXIT

# Resolve the owning ref to a full commit SHA so the committed baseline records
# exactly which upstream commit was extracted (a ref name alone could drift).
MASTER_SHA="$(git -C "${REPO_ROOT}" rev-parse "${MASTER_REF}^{commit}")"

git -C "${REPO_ROOT}" archive "${MASTER_SHA}" meshtastic | tar -x -C "${tmpdir}"

(
	cd "${REPO_ROOT}"
	poetry run python bin/extract_api_surface.py "${tmpdir}/meshtastic" \
		--provenance-ref "${MASTER_REF}" \
		--provenance-sha "${MASTER_SHA}" \
		>"${OUT_FILE}"
)

echo "Generated ${OUT_FILE} from ${MASTER_REF}@${MASTER_SHA}"
