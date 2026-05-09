#!/usr/bin/env bash

set -euo pipefail

# Pinned meshtasticd 2.7.20 line (Docker Hub tag: 2.7.20-alpha-debian)
MESHTASTICD_IMAGE="${MESHTASTICD_IMAGE:-meshtastic/meshtasticd:2.7.23-alpha-debian@sha256:6ed5069979f2301d6863a3fc13360c2b59e9722f9d11f4673637d8cc337acf89}"
MESHTASTICD_CONTAINER="${MESHTASTICD_CONTAINER:-meshtasticd-smokevirt}"
MESHTASTICD_HOST="${MESHTASTICD_HOST:-localhost:4401}"
MESHTASTICD_PORT="${MESHTASTICD_PORT:-4401}"
MESHTASTICD_READY_TIMEOUT_SECONDS="${MESHTASTICD_READY_TIMEOUT_SECONDS:-120}"
READY_LOG_FILE="${READY_LOG_FILE-}"
MESHTASTICD_LOG_DIR="${MESHTASTICD_LOG_DIR-}"
MESHTASTICD_LOG_ON_SUCCESS="${MESHTASTICD_LOG_ON_SUCCESS:-false}"
SMOKEVIRT_PYTEST_ARGS="${SMOKEVIRT_PYTEST_ARGS-}"
MESHTASTICD_DEFAULT_PYTEST_TARGETS="meshtastic/tests/test_meshtasticd_ci.py meshtastic/tests/test_meshtasticd_tcp_interface_ci.py"
MESHTASTICD_PYTEST_TARGETS="${MESHTASTICD_PYTEST_TARGETS:-${MESHTASTICD_DEFAULT_PYTEST_TARGETS}}"
MESHTASTICD_AUTO_INT_TARGETS="${MESHTASTICD_AUTO_INT_TARGETS:-meshtastic/tests/test_meshtasticd_ci.py}"
MESHTASTICD_PYTEST_MARK_EXPR="${MESHTASTICD_PYTEST_MARK_EXPR-}"
EXTRA_PYTEST_ARGS=()
PYTEST_TARGETS=()
AUTO_INT_TARGETS=()
MESHTASTICD_CI_TARGETS=()
SMOKEVIRT_TARGETS=()
LOGS_PRINTED=false
READY_LOG_FILE_IS_TEMP=false

# Keep this helper local in each runner script so each entrypoint stays standalone.
# Usage: require_regex "<value>" "<regex>" "<env-name>"
require_regex() {
	local value=$1
	local pattern=$2
	local name=$3
	if [[ ! ${value} =~ ${pattern} ]]; then
		echo "Invalid ${name}: ${value}" >&2
		exit 1
	fi
}

cleanup() {
	local exit_code=$?
	local print_logs=false
	if ((exit_code != 0)); then
		print_logs=true
	else
		case "${MESHTASTICD_LOG_ON_SUCCESS,,}" in
		1 | true | yes | on)
			print_logs=true
			;;
		*) ;;
		esac
	fi
	if docker ps -a --format '{{.Names}}' | grep -Fxq "${MESHTASTICD_CONTAINER}"; then
		local log_file=""
		if [[ -n ${MESHTASTICD_LOG_DIR} ]]; then
			log_file="${MESHTASTICD_LOG_DIR}/${MESHTASTICD_CONTAINER}.log"
			docker logs "${MESHTASTICD_CONTAINER}" >"${log_file}" 2>&1 || true
		fi
		if [[ ${print_logs} == true && ${LOGS_PRINTED} == false ]]; then
			echo "===== meshtasticd logs (${MESHTASTICD_CONTAINER}, full) ====="
			if [[ -n ${log_file} && -f ${log_file} ]]; then
				cat "${log_file}" || true
			else
				docker logs "${MESHTASTICD_CONTAINER}" || true
			fi
		fi
		docker rm -f "${MESHTASTICD_CONTAINER}" >/dev/null || true
	fi
	if [[ ${READY_LOG_FILE_IS_TEMP} == true ]]; then
		rm -f "${READY_LOG_FILE}" || true
	fi
	exit "${exit_code}"
}

trap cleanup EXIT

if ! command -v docker >/dev/null 2>&1; then
	echo "docker is required to run smokevirt against meshtasticd." >&2
	exit 1
fi

if ! command -v poetry >/dev/null 2>&1; then
	echo "poetry is required to run smokevirt against meshtasticd." >&2
	exit 1
fi

require_regex "${MESHTASTICD_CONTAINER}" '^[A-Za-z0-9][A-Za-z0-9_.-]*$' "MESHTASTICD_CONTAINER"
require_regex "${MESHTASTICD_IMAGE}" '^[^[:space:]]+$' "MESHTASTICD_IMAGE"
require_regex "${MESHTASTICD_PORT}" '^[0-9]+$' "MESHTASTICD_PORT"
require_regex "${MESHTASTICD_READY_TIMEOUT_SECONDS}" '^[0-9]+$' "MESHTASTICD_READY_TIMEOUT_SECONDS"
MESHTASTICD_PORT_DEC=$((10#${MESHTASTICD_PORT}))
if ((MESHTASTICD_PORT_DEC < 1 || MESHTASTICD_PORT_DEC > 65535)); then
	echo "MESHTASTICD_PORT must be between 1 and 65535." >&2
	exit 1
fi

MESHTASTICD_PARSED_HOST_AND_PORT="$(
	poetry run python - "${MESHTASTICD_HOST}" "${MESHTASTICD_PORT_DEC}" <<'PY'
import sys

from meshtastic.host_port import parseHostAndPort

host = sys.argv[1]
default_port = int(sys.argv[2])
try:
    parsed_host, parsed_port = parseHostAndPort(
        host,
        default_port=default_port,
        env_var="MESHTASTICD_HOST",
    )
except ValueError as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(1) from exc

print(f"{parsed_host}\t{parsed_port}")
PY
)"
IFS=$'\t' read -r MESHTASTICD_PARSED_HOST MESHTASTICD_HOST_PORT_DEC <<<"${MESHTASTICD_PARSED_HOST_AND_PORT}"
if [[ -z ${MESHTASTICD_PARSED_HOST-} || -z ${MESHTASTICD_HOST_PORT_DEC-} ]]; then
	echo "Invalid MESHTASTICD_HOST: ${MESHTASTICD_HOST}" >&2
	exit 1
fi
if ((MESHTASTICD_HOST_PORT_DEC != MESHTASTICD_PORT_DEC)); then
	echo "MESHTASTICD_HOST port must match MESHTASTICD_PORT, or omit the inline port." >&2
	exit 1
fi
if [[ ${MESHTASTICD_PARSED_HOST} == *:* ]]; then
	MESHTASTICD_HOST="[${MESHTASTICD_PARSED_HOST}]:${MESHTASTICD_HOST_PORT_DEC}"
else
	MESHTASTICD_HOST="${MESHTASTICD_PARSED_HOST}:${MESHTASTICD_HOST_PORT_DEC}"
fi
if [[ -z ${READY_LOG_FILE} ]]; then
	if [[ -n ${MESHTASTICD_LOG_DIR} ]]; then
		READY_LOG_FILE="${MESHTASTICD_LOG_DIR}/meshtasticd-smokevirt-ready.log"
	else
		READY_LOG_FILE="$(mktemp /tmp/meshtasticd-smokevirt-ready.XXXXXX.log)"
		READY_LOG_FILE_IS_TEMP=true
	fi
fi
if [[ ${READY_LOG_FILE} == *$'\n'* ]]; then
	echo "Invalid READY_LOG_FILE path." >&2
	exit 1
fi
if [[ -n ${MESHTASTICD_LOG_DIR} ]] && [[ ${MESHTASTICD_LOG_DIR} == *$'\n'* ]]; then
	echo "Invalid MESHTASTICD_LOG_DIR path." >&2
	exit 1
fi
if ((10#${MESHTASTICD_READY_TIMEOUT_SECONDS} <= 0)); then
	echo "MESHTASTICD_READY_TIMEOUT_SECONDS must be greater than zero." >&2
	exit 1
fi
if [[ -n ${MESHTASTICD_LOG_DIR} ]]; then
	mkdir -p "${MESHTASTICD_LOG_DIR}"
fi

: >"${READY_LOG_FILE}"
docker rm -f "${MESHTASTICD_CONTAINER}" >/dev/null 2>&1 || true

if ! docker pull "${MESHTASTICD_IMAGE}"; then
	if [[ ${MESHTASTICD_IMAGE} == "meshtastic/meshtasticd:latest" || ${MESHTASTICD_IMAGE} == "meshtastic/meshtasticd" ]]; then
		echo "##[warning]Failed to pull ${MESHTASTICD_IMAGE}, falling back to meshtastic/meshtasticd:beta" >&2
		MESHTASTICD_IMAGE="meshtastic/meshtasticd:beta"
		docker pull "${MESHTASTICD_IMAGE}"
	else
		echo "Failed to pull ${MESHTASTICD_IMAGE}" >&2
		exit 1
	fi
fi

docker run -d \
	--name "${MESHTASTICD_CONTAINER}" \
	-p "${MESHTASTICD_PORT_DEC}":4403 \
	"${MESHTASTICD_IMAGE}" \
	bash -c 'while true; do meshtasticd -s --fsdir=/var/lib/meshtasticd; echo "meshtasticd exited with code $?, restarting in 2s..."; sleep 2; done' >/dev/null

deadline=$((SECONDS + 10#${MESHTASTICD_READY_TIMEOUT_SECONDS}))
until poetry run meshtastic --timeout 5 --host "${MESHTASTICD_HOST}" --info >>"${READY_LOG_FILE}" 2>&1; do
	if ! docker ps --format '{{.Names}}' | grep -Fxq "${MESHTASTICD_CONTAINER}"; then
		echo "${MESHTASTICD_CONTAINER} exited before becoming ready." >&2
		if [[ -f ${READY_LOG_FILE} ]]; then
			echo "===== meshtastic readiness output =====" >&2
			cat "${READY_LOG_FILE}" >&2
		fi
		LOGS_PRINTED=true
		docker logs "${MESHTASTICD_CONTAINER}" >&2 || true
		exit 1
	fi
	if ((SECONDS >= deadline)); then
		echo "meshtasticd did not become ready within ${MESHTASTICD_READY_TIMEOUT_SECONDS}s." >&2
		if [[ -f ${READY_LOG_FILE} ]]; then
			echo "===== meshtastic readiness output =====" >&2
			cat "${READY_LOG_FILE}" >&2
		fi
		LOGS_PRINTED=true
		docker logs "${MESHTASTICD_CONTAINER}" >&2 || true
		exit 1
	fi
	sleep 2
done

if [[ -n ${SMOKEVIRT_PYTEST_ARGS} ]]; then
	# Intentionally whitespace-split; keep args as simple tokens.
	read -r -a EXTRA_PYTEST_ARGS <<<"${SMOKEVIRT_PYTEST_ARGS}"
fi

read -r -a PYTEST_TARGETS <<<"${MESHTASTICD_PYTEST_TARGETS}"
read -r -a AUTO_INT_TARGETS <<<"${MESHTASTICD_AUTO_INT_TARGETS}"

if [[ ${#PYTEST_TARGETS[@]} -eq 0 ]]; then
	echo "MESHTASTICD_PYTEST_TARGETS must not be empty." >&2
	exit 1
fi

HAS_SMOKEVIRT_TARGET=false
HAS_EXPLICIT_SELECTOR=false
for target in "${PYTEST_TARGETS[@]}"; do
	is_explicit_selector=false
	if [[ ${target} == *"::"* ]]; then
		is_explicit_selector=true
	fi
	normalized_target="${target%%::*}"
	normalized_target="${normalized_target#./}"
	normalized_basename="${normalized_target##*/}"
	if [[ ${is_explicit_selector} == true ]] && [[ ${normalized_basename} != "test_smokevirt.py" ]]; then
		HAS_EXPLICIT_SELECTOR=true
	fi
	if [[ ${normalized_basename} == "test_smokevirt.py" ]]; then
		HAS_SMOKEVIRT_TARGET=true
		SMOKEVIRT_TARGETS+=("${target}")
	fi
	for default_target in "${AUTO_INT_TARGETS[@]}"; do
		normalized_default_target="${default_target#./}"
		normalized_default_basename="${normalized_default_target##*/}"
		if [[ ${normalized_target} == "${normalized_default_target}" || ${normalized_basename} == "${normalized_default_basename}" ]]; then
			MESHTASTICD_CI_TARGETS+=("${target}")
			break
		fi
	done
done

if [[ -z ${MESHTASTICD_PYTEST_MARK_EXPR} ]]; then
	if [[ ${HAS_EXPLICIT_SELECTOR} == true ]] && [[ ${HAS_SMOKEVIRT_TARGET} == true ]]; then
		echo "MESHTASTICD_PYTEST_TARGETS mixes bare smokevirt targets with explicit selectors; set MESHTASTICD_PYTEST_MARK_EXPR explicitly." >&2
		exit 1
	elif [[ ${HAS_EXPLICIT_SELECTOR} == false ]] && [[ ${HAS_SMOKEVIRT_TARGET} == true ]] && [[ ${#MESHTASTICD_CI_TARGETS[@]} -gt 0 ]]; then
		echo "MESHTASTICD_PYTEST_TARGETS includes both smokevirt and meshtasticd-ci targets; set MESHTASTICD_PYTEST_MARK_EXPR explicitly." >&2
		exit 1
	elif [[ ${HAS_EXPLICIT_SELECTOR} == false ]] && [[ ${HAS_SMOKEVIRT_TARGET} == true ]] && [[ ${#SMOKEVIRT_TARGETS[@]} -eq ${#PYTEST_TARGETS[@]} ]]; then
		MESHTASTICD_PYTEST_MARK_EXPR="smokevirt and not smoke1_destructive"
	elif [[ ${HAS_EXPLICIT_SELECTOR} == false ]] && [[ ${HAS_SMOKEVIRT_TARGET} == true ]]; then
		echo "MESHTASTICD_PYTEST_TARGETS mixes smokevirt with non-smokevirt targets; set MESHTASTICD_PYTEST_MARK_EXPR explicitly." >&2
		exit 1
	# Auto-apply "int" when every selected target is one of the dedicated
	# auto-int eligible files.
	elif [[ ${HAS_EXPLICIT_SELECTOR} == false ]] && [[ ${#MESHTASTICD_CI_TARGETS[@]} -eq ${#PYTEST_TARGETS[@]} ]]; then
		MESHTASTICD_PYTEST_MARK_EXPR="int"
	fi
fi

PYTEST_CMD=(poetry run pytest)
if [[ -n ${MESHTASTICD_PYTEST_MARK_EXPR} ]]; then
	PYTEST_CMD+=(-m "${MESHTASTICD_PYTEST_MARK_EXPR}")
fi
PYTEST_CMD+=("${PYTEST_TARGETS[@]}")
if [[ ${#EXTRA_PYTEST_ARGS[@]} -gt 0 ]]; then
	PYTEST_CMD+=("${EXTRA_PYTEST_ARGS[@]}")
fi
MESHTASTICD_HOST="${MESHTASTICD_HOST}" MESH_HOST_READY_TIMEOUT="${MESH_HOST_READY_TIMEOUT:-${MESHTASTICD_READY_TIMEOUT_SECONDS}}" "${PYTEST_CMD[@]}"
