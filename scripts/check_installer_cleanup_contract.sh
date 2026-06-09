#!/usr/bin/env bash
set -euo pipefail

pass() { printf 'PASS: %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

check_no_code_after_exec() {
  local file="$1"
  awk '
    /exec bash .*\/deploy\/install\.sh/ { last=NR }
    { lines[NR]=$0 }
    END {
      if (!last) {
        printf "%s: missing final deploy/install.sh exec\n", FILENAME > "/dev/stderr";
        exit 1;
      }
      for (i=last+1; i<=NR; i++) {
        line=lines[i]
        sub(/^[[:space:]]+/, "", line)
        sub(/[[:space:]]+$/, "", line)
        if (line != "" && substr(line, 1, 1) != "#") {
          printf "%s:%d: executable text after final exec: %s\n", FILENAME, i, lines[i] > "/dev/stderr";
          exit 2;
        }
      }
    }
  ' "$file" || fail "$file has executable statements after final deploy/install.sh exec"
  pass "$file ends at final deploy/install.sh exec"
}

count_defs() {
  local name="$1"
  rg -n "^def ${name}\\(" server/gfs/inland/payloads.py | wc -l | tr -d ' '
}

check_no_code_after_exec broadcast.sh
check_no_code_after_exec install.sh

[[ "$(count_defs inland_conditions_payload)" == "1" ]] || fail "inland_conditions_payload must have exactly one definition"
pass "inland_conditions_payload has one definition"

[[ "$(count_defs inland_bait_payload)" == "1" ]] || fail "inland_bait_payload must have exactly one definition"
pass "inland_bait_payload has one definition"

for file in deploy/systemd/broadcast.service deploy/templates/app.service.template deploy/lib/services.sh scripts/repair_broadcast_service.sh; do
  rg -q "ExecStart=.*scripts/run_broadcast_service\.sh|run_broadcast_service\.sh" "$file" || fail "$file does not reference scripts/run_broadcast_service.sh"
  pass "$file uses durable runner contract"
done

if rg -n "ExecStart=.*hypercorn" deploy scripts --glob "!check_installer_cleanup_contract.sh"; then
  fail "found direct hypercorn ExecStart in deploy/scripts service writers or templates"
fi
pass "no direct hypercorn ExecStart remains in deploy/scripts"

bash -n broadcast.sh install.sh deploy/install.sh deploy/lib/services.sh scripts/repair_broadcast_service.sh scripts/run_broadcast_service.sh
pass "installer/service shell syntax checks passed"

python3 -m py_compile server/gfs/inland/payloads.py
pass "inland payload py_compile passed"
