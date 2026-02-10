#!/usr/bin/env bash
# pull-cache-job.sh -- Option C: Pull packages from Harbor to local filesystem
#
# Run this on the staging node after mirror-job.sh has pushed packages to Harbor.
# Schedule via cron, e.g.:
#   30 2 * * * /opt/conda-oci-poc/pull-cache-job.sh >> /var/log/conda-pull-cache.log 2>&1
#
# Prerequisites:
#   pip install conda-oci-mirror

set -euo pipefail

# --- Configuration -----------------------------------------------------------

# Harbor registry to pull from.
REGISTRY="https://harbor.staging.example.com/conda-mirror"

# Local cache directory -- this is what nginx serves.
CACHE_DIR="/var/conda-cache"

# Subdirectories to pull.
SUBDIRS=("linux-64" "noarch")

# If Harbor requires authentication for pulls:
# export ORAS_USER="cache-reader"
# export ORAS_PASS="your-token-here"

# --- Pull Cache ---------------------------------------------------------------

echo "$(date -Iseconds) Starting pull-cache job"

for subdir in "${SUBDIRS[@]}"; do
    echo "$(date -Iseconds) Pulling cache for ${subdir}..."
    conda-oci pull-cache \
        --registry "${REGISTRY}" \
        --subdir "${subdir}" \
        --cache-dir "${CACHE_DIR}" \
        || echo "WARNING: Failed to pull cache for ${subdir}"
done

echo "$(date -Iseconds) Pull-cache job complete"
