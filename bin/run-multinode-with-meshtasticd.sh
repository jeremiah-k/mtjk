#!/usr/bin/env bash

set -euo pipefail

# Pinned meshtasticd 2.7.20 line (Docker Hub tag: 2.7.20-alpha-debian)
MESHTASTICD_IMAGE="${MESHTASTICD_IMAGE:-meshtastic/meshtasticd:2.7.26-alpha-debian@sha256:80aad2b748f9a28fc70ec4ffa39901617a20d620a815b4c7e9a7d9556ecae1d8}"
MESHTASTICD_CONTAINER_A="${MESHTASTICD_CONTAINER_A:-meshtasticd-multinode-a}"
MESHTASTICD_CONTAINER_B="${MESHTASTICD_CONTAINER_B:-meshtasticd-multinode-b}"
MESHTASTICD_HOST_A="${MESHTASTICD_HOST_A:-localhost:4401}"
MESHTASTICD_HOST_B="${MESHTASTICD_HOST_B:-localhost:4402}"
MESHTASTICD_PORT_A="${MESHTASTICD_PORT_A:-4401}"
MESHTASTICD_PORT_B="${MESHTASTICD_PORT_B:-4402}"
MESHTASTICD_HWID_A="${MESHTASTICD_HWID_A:-11}"
MESHTASTICD_HWID_B="${MESHTASTICD_HWID_B:-22}"
MESHTASTICD_READY_TIMEOUT_SECONDS="${MESHTASTICD_READY_TIMEOUT_SECONDS:-180}"
READY_LOG_A="${READY_LOG_A-}"
READY_LOG_B="${READY_LOG_B-}"
MESHTASTICD_LOG_DIR="${MESHTASTICD_LOG_DIR-}"
MESHTASTICD_LOG_ON_SUCCESS="${MESHTASTICD_LOG_ON_SUCCESS:-false}"
SMOKEVIRT_PYTEST_ARGS="${SMOKEVIRT_PYTEST_ARGS-}"
MESHTASTICD_PYTEST_TARGETS="${MESHTASTICD_PYTEST_TARGETS:-meshtastic/tests/test_meshtasticd_multinode_ci.py}"
MESHTASTICD_PYTEST_MARK_EXPR="${MESHTASTICD_PYTEST_MARK_EXPR:-int}"
EXTRA_PYTEST_ARGS=()
PYTEST_TARGETS=()
READY_LOG_A_IS_TEMP=false
READY_LOG_B_IS_TEMP=false
LOGS_PRINTED_MARKER="$(mktemp /tmp/meshtasticd-multinode-logs-printed.XXXXXX)"
rm -f "${LOGS_PRINTED_MARKER}"

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

mark_logs_printed() {
	: >"${LOGS_PRINTED_MARKER}"
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
	for container in "${MESHTASTICD_CONTAINER_A}" "${MESHTASTICD_CONTAINER_B}"; do
		if docker ps -a --format '{{.Names}}' | grep -Fxq "${container}"; then
			local log_file=""
			local already_printed=false
			if [[ -n ${MESHTASTICD_LOG_DIR} ]]; then
				log_file="${MESHTASTICD_LOG_DIR}/${container}.log"
				docker logs "${container}" >"${log_file}" 2>&1 || true
			fi
			if [[ -f ${LOGS_PRINTED_MARKER} ]]; then
				already_printed=true
			fi
			if [[ ${print_logs} == true && ${already_printed} == false ]]; then
				echo "===== meshtasticd logs (${container}, full) ====="
				if [[ -n ${log_file} && -f ${log_file} ]]; then
					cat "${log_file}" || true
				else
					docker logs "${container}" || true
				fi
			fi
			docker rm -f "${container}" >/dev/null || true
		fi
	done
	if [[ ${READY_LOG_A_IS_TEMP} == true ]]; then
		rm -f "${READY_LOG_A}" || true
	fi
	if [[ ${READY_LOG_B_IS_TEMP} == true ]]; then
		rm -f "${READY_LOG_B}" || true
	fi
	rm -f "${LOGS_PRINTED_MARKER}" || true
	exit "${exit_code}"
}

trap cleanup EXIT

if ! command -v docker >/dev/null 2>&1; then
	echo "docker is required to run multinode meshtasticd integration checks." >&2
	exit 1
fi

if ! command -v poetry >/dev/null 2>&1; then
	echo "poetry is required to run multinode meshtasticd integration checks." >&2
	exit 1
fi

OS_NAME="$(uname -s)"
if [[ ${OS_NAME} != "Linux" ]]; then
	echo "multinode meshtasticd runner currently requires Linux host networking." >&2
	exit 1
fi

if ((BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 3))); then
	echo "bash 4.3+ is required (wait -n support)." >&2
	exit 1
fi

require_regex "${MESHTASTICD_CONTAINER_A}" '^[A-Za-z0-9][A-Za-z0-9_.-]*$' "MESHTASTICD_CONTAINER_A"
require_regex "${MESHTASTICD_CONTAINER_B}" '^[A-Za-z0-9][A-Za-z0-9_.-]*$' "MESHTASTICD_CONTAINER_B"
require_regex "${MESHTASTICD_IMAGE}" '^[^[:space:]]+$' "MESHTASTICD_IMAGE"
require_regex "${MESHTASTICD_HOST_A}" '^[A-Za-z0-9._:-]+$' "MESHTASTICD_HOST_A"
require_regex "${MESHTASTICD_HOST_B}" '^[A-Za-z0-9._:-]+$' "MESHTASTICD_HOST_B"
require_regex "${MESHTASTICD_PORT_A}" '^[0-9]+$' "MESHTASTICD_PORT_A"
require_regex "${MESHTASTICD_PORT_B}" '^[0-9]+$' "MESHTASTICD_PORT_B"
require_regex "${MESHTASTICD_HWID_A}" '^[0-9]+$' "MESHTASTICD_HWID_A"
require_regex "${MESHTASTICD_HWID_B}" '^[0-9]+$' "MESHTASTICD_HWID_B"
require_regex "${MESHTASTICD_READY_TIMEOUT_SECONDS}" '^[0-9]+$' "MESHTASTICD_READY_TIMEOUT_SECONDS"
MESHTASTICD_PORT_A_DEC=$((10#${MESHTASTICD_PORT_A}))
MESHTASTICD_PORT_B_DEC=$((10#${MESHTASTICD_PORT_B}))
MESHTASTICD_READY_TIMEOUT_SECONDS_DEC=$((10#${MESHTASTICD_READY_TIMEOUT_SECONDS}))
if ((MESHTASTICD_PORT_A_DEC < 1 || MESHTASTICD_PORT_A_DEC > 65535)); then
	echo "MESHTASTICD_PORT_A must be between 1 and 65535." >&2
	exit 1
fi
if ((MESHTASTICD_PORT_B_DEC < 1 || MESHTASTICD_PORT_B_DEC > 65535)); then
	echo "MESHTASTICD_PORT_B must be between 1 and 65535." >&2
	exit 1
fi
if ((MESHTASTICD_PORT_A_DEC == MESHTASTICD_PORT_B_DEC)); then
	echo "MESHTASTICD_PORT_A and MESHTASTICD_PORT_B must not be the same." >&2
	exit 1
fi
if [[ -z ${READY_LOG_A} ]]; then
	if [[ -n ${MESHTASTICD_LOG_DIR} ]]; then
		READY_LOG_A="${MESHTASTICD_LOG_DIR}/meshtasticd-multinode-a-ready.log"
	else
		READY_LOG_A="$(mktemp /tmp/meshtasticd-multinode-a-ready.XXXXXX.log)"
		READY_LOG_A_IS_TEMP=true
	fi
fi
if [[ -z ${READY_LOG_B} ]]; then
	if [[ -n ${MESHTASTICD_LOG_DIR} ]]; then
		READY_LOG_B="${MESHTASTICD_LOG_DIR}/meshtasticd-multinode-b-ready.log"
	else
		READY_LOG_B="$(mktemp /tmp/meshtasticd-multinode-b-ready.XXXXXX.log)"
		READY_LOG_B_IS_TEMP=true
	fi
fi
if [[ ${READY_LOG_A} == *$'\n'* ]]; then
	echo "Invalid READY_LOG_A path." >&2
	exit 1
fi
if [[ ${READY_LOG_B} == *$'\n'* ]]; then
	echo "Invalid READY_LOG_B path." >&2
	exit 1
fi
if [[ -n ${MESHTASTICD_LOG_DIR} ]] && [[ ${MESHTASTICD_LOG_DIR} == *$'\n'* ]]; then
	echo "Invalid MESHTASTICD_LOG_DIR path." >&2
	exit 1
fi
if ((MESHTASTICD_READY_TIMEOUT_SECONDS_DEC <= 0)); then
	echo "MESHTASTICD_READY_TIMEOUT_SECONDS must be greater than zero." >&2
	exit 1
fi
if [[ -n ${MESHTASTICD_LOG_DIR} ]]; then
	mkdir -p "${MESHTASTICD_LOG_DIR}"
fi

: >"${READY_LOG_A}"
: >"${READY_LOG_B}"
docker rm -f "${MESHTASTICD_CONTAINER_A}" "${MESHTASTICD_CONTAINER_B}" >/dev/null 2>&1 || true

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
	--name "${MESHTASTICD_CONTAINER_A}" \
	--network host \
	"${MESHTASTICD_IMAGE}" \
	bash -c "while true; do meshtasticd -s --fsdir=/var/lib/meshtasticd-a -p ${MESHTASTICD_PORT_A_DEC} -h ${MESHTASTICD_HWID_A}; echo \"meshtasticd exited with code \$?, restarting in 2s...\"; sleep 2; done" >/dev/null
docker run -d \
	--name "${MESHTASTICD_CONTAINER_B}" \
	--network host \
	"${MESHTASTICD_IMAGE}" \
	bash -c "while true; do meshtasticd -s --fsdir=/var/lib/meshtasticd-b -p ${MESHTASTICD_PORT_B_DEC} -h ${MESHTASTICD_HWID_B}; echo \"meshtasticd exited with code \$?, restarting in 2s...\"; sleep 2; done" >/dev/null

wait_for_ready() {
	local host=$1
	local container=$2
	local ready_log_file=$3
	local deadline=$((SECONDS + MESHTASTICD_READY_TIMEOUT_SECONDS_DEC))

	until poetry run meshtastic --timeout 5 --host "${host}" --info >>"${ready_log_file}" 2>&1; do
		if ! docker ps --format '{{.Names}}' | grep -Fxq "${container}"; then
			echo "${container} exited before becoming ready." >&2
			mark_logs_printed
			docker logs "${container}" >&2 || true
			return 1
		fi
		if ((SECONDS >= deadline)); then
			echo "${container} did not become ready within ${MESHTASTICD_READY_TIMEOUT_SECONDS}s." >&2
			echo "===== readiness output (${host}) =====" >&2
			cat "${ready_log_file}" >&2 || true
			mark_logs_printed
			docker logs "${container}" >&2 || true
			return 1
		fi
		sleep 2
	done
}

wait_for_parallel_failfast() {
	# wait -n reaps any background job. This function assumes only pid_a and pid_b
	# are active for this script section.
	local pid_a=$1
	local pid_b=$2
	local first_status=0
	local status_a=127
	local status_b=127

	if wait -n; then
		first_status=0
	else
		first_status=$?
	fi

	if ((first_status != 0)); then
		kill "${pid_a}" "${pid_b}" 2>/dev/null || true
	fi

	if wait "${pid_a}" 2>/dev/null; then
		status_a=0
	else
		status_a=$?
	fi
	if wait "${pid_b}" 2>/dev/null; then
		status_b=0
	else
		status_b=$?
	fi

	if ((first_status != 0)); then
		return "${first_status}"
	fi
	if ((status_a != 0 && status_a != 127)); then
		return "${status_a}"
	fi
	if ((status_b != 0 && status_b != 127)); then
		return "${status_b}"
	fi
	return 0
}

wait_for_ready "${MESHTASTICD_HOST_A}" "${MESHTASTICD_CONTAINER_A}" "${READY_LOG_A}" &
pid_ready_a=$!
wait_for_ready "${MESHTASTICD_HOST_B}" "${MESHTASTICD_CONTAINER_B}" "${READY_LOG_B}" &
pid_ready_b=$!

wait_for_parallel_failfast "${pid_ready_a}" "${pid_ready_b}"

wait_for_log_pattern() {
	local container=$1
	local pattern=$2
	local timeout_seconds=${3:-30}
	local deadline=$((SECONDS + timeout_seconds))
	local last_log_error=""
	local log_output=""

	while ((SECONDS < deadline)); do
		log_output=""
		if ! log_output="$(docker logs "${container}" 2>&1)"; then
			last_log_error="${log_output}"
			if ! docker ps --format '{{.Names}}' | grep -Fxq "${container}"; then
				echo "${container} exited while waiting for log pattern '${pattern}'." >&2
				if [[ -n ${last_log_error} ]]; then
					printf '%s\n' "${last_log_error}" >&2
				fi
				mark_logs_printed
				# Retry once to capture complete logs from the now-stopped container.
				docker logs "${container}" >&2 || true
				return 1
			fi
			sleep 1
			continue
		fi
		last_log_error=""
		if grep -Fq "${pattern}" <<<"${log_output}"; then
			return 0
		fi
		if ! docker ps --format '{{.Names}}' | grep -Fxq "${container}"; then
			echo "${container} exited while waiting for log pattern '${pattern}'." >&2
			mark_logs_printed
			docker logs "${container}" >&2 || true
			return 1
		fi
		sleep 1
	done

	if [[ -n ${last_log_error} ]]; then
		echo "Failed to read logs from ${container} before timeout." >&2
		printf '%s\n' "${last_log_error}" >&2
	fi
	echo "${container} did not emit expected log pattern '${pattern}' within ${timeout_seconds}s." >&2
	mark_logs_printed
	docker logs "${container}" >&2 || true
	return 1
}

wait_for_log_pattern "${MESHTASTICD_CONTAINER_A}" "Start multicast thread" 30 &
pid_log_a=$!
wait_for_log_pattern "${MESHTASTICD_CONTAINER_B}" "Start multicast thread" 30 &
pid_log_b=$!

wait_for_parallel_failfast "${pid_log_a}" "${pid_log_b}"

if [[ -n ${SMOKEVIRT_PYTEST_ARGS} ]]; then
	# Intentionally whitespace-split; keep args as simple tokens.
	read -r -a EXTRA_PYTEST_ARGS <<<"${SMOKEVIRT_PYTEST_ARGS}"
fi

read -r -a PYTEST_TARGETS <<<"${MESHTASTICD_PYTEST_TARGETS}"
if [[ ${#PYTEST_TARGETS[@]} -eq 0 ]]; then
	echo "MESHTASTICD_PYTEST_TARGETS must not be empty." >&2
	exit 1
fi

PYTEST_CMD=(poetry run pytest -m "${MESHTASTICD_PYTEST_MARK_EXPR}")
PYTEST_CMD+=("${PYTEST_TARGETS[@]}")
if [[ ${#EXTRA_PYTEST_ARGS[@]} -gt 0 ]]; then
	PYTEST_CMD+=("${EXTRA_PYTEST_ARGS[@]}")
fi
MESHTASTICD_HOST_A="${MESHTASTICD_HOST_A}" MESHTASTICD_HOST_B="${MESHTASTICD_HOST_B}" "${PYTEST_CMD[@]}"
