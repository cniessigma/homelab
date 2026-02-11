#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TALOS_DIR="${ROOT_DIR}/talos"
RENDERED_DIR="${TALOS_DIR}/.rendered"
TALCONFIG_FILE="${TALOS_DIR}/talconfig.yaml"
TALOSCONFIG_FILE="${RENDERED_DIR}/talosconfig"
TALSECRET_FILE="${TALOS_DIR}/talsecret.enc.yaml"

DOMAIN_SUFFIX="${DOMAIN_SUFFIX:-chardalyn.nies.io}"
MODE="${MODE:-auto}"

if [[ ! -f "${TALCONFIG_FILE}" ]]; then
  echo "Missing talconfig: ${TALCONFIG_FILE}" >&2
  exit 1
fi

if ! command -v talosctl >/dev/null 2>&1; then
  echo "talosctl is not installed or not in PATH." >&2
  exit 1
fi

if ! command -v talhelper >/dev/null 2>&1; then
  echo "talhelper is not installed or not in PATH." >&2
  exit 1
fi

if ! command -v dig >/dev/null 2>&1; then
  echo "dig is not installed or not in PATH." >&2
  exit 1
fi

if [[ ! -f "${TALSECRET_FILE}" ]]; then
  echo "Missing Talos secret file: ${TALSECRET_FILE}" >&2
  exit 1
fi

echo "Generating Talos configs with talhelper"
talhelper genconfig \
  --config-file "${TALCONFIG_FILE}" \
  --no-gitignore \
  --out-dir "${RENDERED_DIR}" \
  --secret-file "${TALSECRET_FILE}"

if [[ ! -f "${TALOSCONFIG_FILE}" ]]; then
  echo "Missing talosconfig after generation: ${TALOSCONFIG_FILE}" >&2
  exit 1
fi

cluster_name="$(awk -F': ' '/^clusterName:/ {print $2; exit}' "${TALCONFIG_FILE}")"
if [[ -z "${cluster_name}" ]]; then
  echo "Could not read clusterName from ${TALCONFIG_FILE}" >&2
  exit 1
fi

nodes_text="$(awk '/^[[:space:]]*- hostname:/ {print $3}' "${TALCONFIG_FILE}")"
if [[ -z "${nodes_text}" ]]; then
  echo "No nodes found in ${TALCONFIG_FILE}" >&2
  exit 1
fi

node_ip_pairs="$(awk '
  /^[[:space:]]*- hostname:/ {host=$3}
  /^[[:space:]]*ipAddress:/ {print host " " $2}
' "${TALCONFIG_FILE}")"

export TALOSCONFIG="${TALOSCONFIG_FILE}"

while IFS= read -r node; do
  fqdn="${node}.${DOMAIN_SUFFIX}"
  node_ip="$(dig +short "${fqdn}" A | head -n 1)"

  if [[ -z "${node_ip}" ]]; then
    node_ip="$(awk -v n="${node}" '$1 == n {print $2; exit}' <<< "${node_ip_pairs}")"
    if [[ -n "${node_ip}" ]]; then
      echo "DNS lookup failed for ${fqdn}; falling back to talconfig IP ${node_ip}" >&2
    fi
  fi

  if [[ -z "${node_ip}" ]]; then
    echo "Skipping ${fqdn}: DNS and talconfig IP lookup failed" >&2
    continue
  fi

  node_config="${RENDERED_DIR}/${cluster_name}-${node}.yaml"

  if [[ ! -f "${node_config}" ]]; then
    echo "Skipping ${fqdn}: missing config ${node_config}" >&2
    continue
  fi

  echo "Applying ${node_config} to ${fqdn} via ${node_ip} (mode=${MODE})"
  talosctl --nodes "${node_ip}" apply-config --file "${node_config}" --mode "${MODE}"
done <<< "${nodes_text}"
