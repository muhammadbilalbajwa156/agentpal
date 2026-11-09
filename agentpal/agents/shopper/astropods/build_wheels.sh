#!/bin/sh
# Download the image's pinned dependencies as wheels (run before `ast blueprint push`).
set -eu
cd "$(dirname "$0")"
rm -rf wheels
pip download -q -r requirements-image.txt -d wheels --only-binary=:all: \
  --python-version 3.12 --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 --implementation cp
(cd wheels && sha256sum *.whl) > wheels.sha256
