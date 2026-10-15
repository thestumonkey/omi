#!/usr/bin/env bash
# [fork-only] Retired. Deploys go through backend/Makefile:
#
#   make -C backend release              # full build + deploy of the current commit
#   make -C backend overlay REF=<sha>    # ship only changed backend files
#   make -C backend status               # what runs now
#
# This script used to build backend-listen:local and pin omi-backend to the
# node anubis; the backends now run on erebus with commit-tagged images.
echo "k8s-deploy.sh is retired; use: make -C backend help" >&2
make -C "$(dirname "$0")/backend" help
exit 1
