#!/usr/bin/env bash
# mirror-job.sh -- Option B: Cron job to mirror conda-forge packages to Harbor
#
# Run this on an internet-connected node (or the staging node itself if it
# has internet access). Schedule via cron, e.g.:
#   0 2 * * * /opt/conda-oci-poc/mirror-job.sh >> /var/log/conda-mirror.log 2>&1
#
# Prerequisites:
#   pip install conda-oci-mirror

set -euo pipefail

# --- Configuration -----------------------------------------------------------

# Harbor registry endpoint. Use http:// for insecure registries.
REGISTRY="https://harbor.staging.example.com/conda-mirror"

# Packages to mirror. Add packages your users commonly need.
# Remove the --package flag entirely to mirror ALL packages (very large).
PACKAGES=(
    numpy
    pandas
    scipy
    matplotlib
    scikit-learn
    jupyterlab
    ipykernel
    requests
    h5py
    xarray
    dask
    zarr
)

# Subdirectories (architectures) to mirror.
SUBDIRS="linux-64,noarch"

# Channel to mirror from.
CHANNEL="conda-forge"

# ORAS credentials for pushing to Harbor.
export ORAS_USER="${ORAS_USER:-mirror-bot}"
export ORAS_PASS="${ORAS_PASS:?Error: ORAS_PASS must be set}"

# --- Mirror -------------------------------------------------------------------

echo "$(date -Iseconds) Starting conda-oci mirror job"

for pkg in "${PACKAGES[@]}"; do
    echo "$(date -Iseconds) Mirroring ${pkg}..."
    conda-oci mirror \
        --channel "${CHANNEL}" \
        --package "${pkg}" \
        --subdirs "${SUBDIRS}" \
        --registry "${REGISTRY}" \
        || echo "WARNING: Failed to mirror ${pkg}"
done

echo "$(date -Iseconds) Mirror job complete"
