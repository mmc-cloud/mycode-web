#!/usr/bin/env sh
set -eu

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
    echo "usage: $0 <mycode-source-dir> [image-tag]" >&2
    exit 2
fi

MYCODE_SOURCE_DIR=$1
IMAGE_TAG=${2:-mycode-sandbox:dev}
WEB_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

for required_path in README.md pyproject.toml uv.lock mycode; do
    if [ ! -e "$MYCODE_SOURCE_DIR/$required_path" ]; then
        echo "MyCode source is missing required path: $required_path" >&2
        exit 2
    fi
done

docker buildx build \
    --load \
    --build-context "mycode=$MYCODE_SOURCE_DIR" \
    --file "$WEB_ROOT/docker/Dockerfile.sandbox" \
    --tag "$IMAGE_TAG" \
    "$WEB_ROOT"
