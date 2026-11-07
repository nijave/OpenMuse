#!/usr/bin/env bash
# Build the openmuse-capture image for linux/amd64 and linux/arm64 as one
# manifest list, tag it :<tag> and :latest. Push only with --push.
#
#   ./build.sh                 # build, tag = short git sha (-dirty if needed)
#   ./build.sh --tag 1.0.0     # build with an explicit tag
#   ./build.sh --push          # build, then push :<tag> and :latest
#
# Needs podman, and qemu-user-static (binfmt) for the foreign architecture.
# registry.apps.nickv.me takes anonymous pushes from the LAN; no login.
#
# Run from anywhere: the build context is the repository root (two levels
# above this directory) and the Containerfile lives next to this script.
set -euo pipefail

REPO="${OPENMUSE_IMAGE_REPO:-registry.apps.nickv.me/nijave/openmuse}"
PLATFORMS="${OPENMUSE_PLATFORMS:-linux/amd64,linux/arm64}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
context="$(cd "$here/../.." && pwd)"
tag=""
push=0

usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --push) push=1 ;;
        --tag) tag="${2:?--tag needs a value}"; shift ;;
        --tag=*) tag="${1#--tag=}" ;;
        -h | --help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if [[ -z "$tag" ]]; then
    tag="$(git -C "$context" rev-parse --short HEAD)"
    if [[ -n "$(git -C "$context" status --porcelain -- "$context")" ]]; then
        tag="$tag-dirty"
    fi
fi
if [[ "$tag" == latest ]]; then
    echo "use a version tag; :latest is added automatically" >&2
    exit 2
fi

image="$REPO:$tag"
latest="$REPO:latest"

# `podman build --manifest` adds to an existing list, so start from an empty one.
for ref in "$image" "$latest"; do
    if podman manifest exists "$ref" 2>/dev/null; then
        podman manifest rm "$ref" >/dev/null
    elif podman image exists "$ref" 2>/dev/null; then
        podman rmi "$ref" >/dev/null
    fi
done

echo ">> building $image for $PLATFORMS (context: $context)"
podman build \
    --platform "$PLATFORMS" \
    --manifest "$image" \
    --build-arg "VERSION=$tag" \
    -f "$here/Containerfile" \
    "$context"

podman tag "$image" "$latest"

echo ">> manifest $image"
podman manifest inspect "$image" \
    | python3 -c 'import json,sys; [print("   ", m["platform"]["os"]+"/"+m["platform"]["architecture"], m["digest"]) for m in json.load(sys.stdin)["manifests"]]'

if [[ $push -eq 1 ]]; then
    for ref in "$image" "$latest"; do
        echo ">> pushing $ref"
        podman manifest push --all "$ref" "docker://$ref"
    done
else
    echo ">> not pushed (rerun with --push)"
fi
