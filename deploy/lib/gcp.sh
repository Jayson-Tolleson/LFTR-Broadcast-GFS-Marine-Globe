#!/usr/bin/env bash
set -euo pipefail

maybe_detect_gcp() {
  local md="http://metadata.google.internal/computeMetadata/v1"
  if curl -fsS -H 'Metadata-Flavor: Google' "${md}/instance/id" >/dev/null 2>&1; then
    log_info "GCP metadata detected"
    run_optional "show GCP instance" curl -fsS -H 'Metadata-Flavor: Google' "${md}/instance/name"
    run_optional "show GCP zone" curl -fsS -H 'Metadata-Flavor: Google' "${md}/instance/zone"
  else
    log_info "Non-GCP environment (or metadata unavailable)"
  fi
}
