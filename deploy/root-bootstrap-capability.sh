#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'
umask 077

if [[ $# -ne 1 || "$1" != /* ]]; then
  printf 'bootstrap capability: expected one absolute state directory\n' >&2
  exit 2
fi

state="$1"
capability_file="$state/deploy-capability"
install -d -o root -g root -m 0700 "$state"

validate_capability() {
  local candidate="$1"
  local metadata
  local size
  [[ -f "$candidate" && ! -L "$candidate" ]] || return 1
  metadata="$(stat -c '%u:%g:%a' -- "$candidate")" || return 1
  [[ "$metadata" == '0:0:600' ]] || return 1
  size="$(wc -c <"$candidate")" || return 1
  [[ "$size" == 65 ]] || return 1
  grep -Eq '^[0-9a-f]{64}$' "$candidate"
}

if [[ -e "$capability_file" || -L "$capability_file" ]]; then
  if ! validate_capability "$capability_file"; then
    printf 'bootstrap capability: existing capability has unsafe type, owner, mode, size, or format\n' >&2
    exit 1
  fi
else
  # The state directory is root-only, so this creation cannot be raced by the
  # unprivileged runner. Never print the capability or place it in argv.
  temporary="$(mktemp "$state/.deploy-capability.XXXXXX")"
  trap 'rm -f -- "$temporary"' EXIT
  openssl rand -hex 32 >"$temporary"
  chown root:root "$temporary"
  chmod 0600 "$temporary"
  if ! validate_capability "$temporary"; then
    printf 'bootstrap capability: generated capability failed validation\n' >&2
    exit 1
  fi
  ln -- "$temporary" "$capability_file"
  rm -f -- "$temporary"
fi

printf 'bootstrap capability: ready (value withheld)\n'
