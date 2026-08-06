#!/usr/bin/env bash
# Verifies that images.json — the inventory `make mirror-images` copies from —
# still describes the images an install actually pulls. Two ways it can be
# wrong, and both have happened:
#
#   1. A pin drifts. The chart sat on LiteLLM v1.92.0 for a release while the
#      kustomize base it claimed to mirror was on v1.95.0. A mirror populated
#      from the inventory then lacks the tag the install asks for.
#   2. A new image appears with no inventory entry. Nothing copies it, and the
#      air-gapped install fails at pull time on a registry nobody approved.
#
# Run via `make images-check`; CI runs it in validate.yml.
set -euo pipefail
cd "$(dirname "$0")/.."

INVENTORY=images.json
MIRROR=registry.example.invalid/mirror
status=0

fail() {
  echo "ERROR: $1" >&2
  status=1
}

for tool in jq helm; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "ERROR: $tool is required to check the image inventory." >&2
    exit 1
  }
done

# Docker Hub official images are pullable by bare name; the inventory spells
# out the registry so mirroring has an unambiguous source. Compare on the
# normalised form so both spellings agree.
normalise() {
  local ref=$1
  ref=${ref#docker.io/library/}
  ref=${ref#docker.io/}
  echo "$ref"
}

pin_of() {
  jq -r --arg n "$1" '.images[] | select(.name == $n) | .tag' "$INVENTORY"
}

repo_of() {
  jq -r --arg n "$1" '.images[] | select(.name == $n) | .repository' "$INVENTORY"
}

# A Dockerfile's `ARG FOO=bar` default, or empty if the arg has no default.
arg_default() {
  sed -n "s/^ARG $2=\(.*\)$/\1/p" "$1" | head -n1
}

# ---------------------------------------------------------------------------
# 0. Origins. Both consumers filter on this field and neither complains about a
#    value it does not recognise: mirror_images.sh folds an unknown origin into
#    its "skipped" line, and generate_docs.py's section loop drops it. A typo
#    like "third_party" therefore removes an image from the mirror AND from the
#    docs while everything still reports success — failure mode #2 above,
#    arriving quietly.
# ---------------------------------------------------------------------------
unknown_origins="$(jq -r '.images[].origin | select(. != "first-party" and . != "third-party" and . != "build-time")' "$INVENTORY" | sort -u)"
[ -z "$unknown_origins" ] ||
  fail "$INVENTORY has unrecognised origin(s): $(tr '\n' ' ' <<<"$unknown_origins")— must be first-party, third-party, or build-time, or the entry is silently neither mirrored nor documented."

# ---------------------------------------------------------------------------
# 1. Build-time base images: the ARG defaults are what a plain `docker build`
#    pulls, so they are the pins the inventory must mirror.
# ---------------------------------------------------------------------------
check_base_image() {
  local name=$1 dockerfile=$2 image_arg=$3 version_arg=$4
  local want_repo want_tag got_repo got_tag
  want_repo="$(normalise "$(repo_of "$name")")"
  want_tag="$(pin_of "$name")"
  got_repo="$(normalise "$(arg_default "$dockerfile" "$image_arg")")"
  got_tag="$(arg_default "$dockerfile" "$version_arg")"

  [ "$got_repo" = "$want_repo" ] ||
    fail "$dockerfile: ARG $image_arg defaults to '${got_repo:-<unset>}', but $INVENTORY has '$want_repo' for '$name'."
  [ "$got_tag" = "$want_tag" ] ||
    fail "$dockerfile: ARG $version_arg defaults to '${got_tag:-<unset>}', but $INVENTORY pins '$name' at '$want_tag'."
}

check_base_image envoy deploy/docker/Dockerfile ENVOY_IMAGE ENVOY_VERSION
check_base_image golang deploy/docker/Dockerfile GOLANG_IMAGE GOLANG_VERSION
check_base_image golang k8s-operator/Dockerfile GOLANG_IMAGE GOLANG_VERSION
check_base_image distroless-static k8s-operator/Dockerfile DISTROLESS_IMAGE DISTROLESS_VERSION
check_base_image python examples/inference-replay/replay-proxy/Dockerfile PYTHON_IMAGE PYTHON_VERSION

# hermes-agent is the one base image whose tag lives outside the Dockerfile —
# the release workflows read tags.env — so the inventory points at that file
# rather than copying the value. Check the pointer still resolves.
hermes_repo="$(normalise "$(arg_default deploy/docker/Dockerfile HERMES_AGENT_IMAGE)")"
[ "$hermes_repo" = "$(normalise "$(repo_of hermes-agent)")" ] ||
  fail "deploy/docker/Dockerfile: ARG HERMES_AGENT_IMAGE defaults to '$hermes_repo', but $INVENTORY has '$(repo_of hermes-agent)'."

jq -r '.images[] | select(.tagFrom) | "\(.name)\t\(.tagFrom.file)\t\(.tagFrom.key)"' "$INVENTORY" |
  while IFS=$'\t' read -r name file key; do
    [ -f "$file" ] || {
      echo "ERROR: $INVENTORY: '$name' takes its tag from '$file', which does not exist." >&2
      exit 1
    }
    grep -q "^${key}=" "$file" || {
      echo "ERROR: $INVENTORY: '$name' takes its tag from ${file}:${key}, which is not set there." >&2
      exit 1
    }
  done || status=1

# ---------------------------------------------------------------------------
# 2. The fluent-bit pin compiled into the operator. It is the only image the
#    operator falls back to without an env var, so a drift here mirrors the
#    wrong tag with nothing to catch it at render time.
# ---------------------------------------------------------------------------
want_fluent="$(normalise "$(repo_of fluent-bit)"):$(pin_of fluent-bit)"
got_fluent="$(sed -n 's/.*fallbackFluentBitImage = "\(.*\)".*/\1/p' k8s-operator/internal/controller/manifest_helpers.go)"
[ "$(normalise "$got_fluent")" = "$want_fluent" ] ||
  fail "k8s-operator/internal/controller/manifest_helpers.go: fallbackFluentBitImage is '$got_fluent', but $INVENTORY has '$want_fluent'."

# ---------------------------------------------------------------------------
# 3. The chart. Rendering it is the only way to see what it actually pulls:
#    the operator's image env vars are assembled from several values, and a
#    grep over values.yaml would miss exactly the composition bugs that matter.
# ---------------------------------------------------------------------------
REQUIRED_VALUES=(
  --set platformAgent.harness.clusterName=ci-cluster
  --set platformAgent.harness.location=us-central1
  --set platformAgent.harness.projectId=ci-project
)

# Every image the chart renders, from `image:` fields and from the operator's
# *_IMAGE env vars — the latter are what the operator later stamps onto agent
# pods, so leaving them public half-mirrors the install.
chart_images() {
  helm template test-release charts/kube-agents "${REQUIRED_VALUES[@]}" "$@" |
    sed -n -e 's/^[[:space:]]*image:[[:space:]]*"\?\([^"]*\)"\?[[:space:]]*$/\1/p' \
      -e 's/^[[:space:]]*value:[[:space:]]*"\([^"]*\/[^"]*:[^"]*\)"[[:space:]]*$/\1/p' |
    sort -u
}

# 3a. Default install: every rendered image must be in the inventory, so the
#     mirror built from it is complete.
inventory_repos="$(jq -r '.images[].repository' "$INVENTORY" | sort -u)"
while read -r image; do
  [ -n "$image" ] || continue
  repo="${image%%:*}"
  repo="${repo%%@*}"
  grep -qxF "$repo" <<<"$inventory_repos" ||
    fail "the chart renders '$image', which has no entry in $INVENTORY — 'make mirror-images' would not copy it."
done < <(chart_images)

# 3b. Mirrored install: nothing may be left on a public registry. This is the
#     chart-side equivalent of TestNoPublicRegistryWhenMirrored in the
#     operator, and it covers the env vars a Go test cannot see.
while read -r image; do
  [ -n "$image" ] || continue
  case "$image" in
  "$MIRROR"/*) ;;
  *) fail "with global.imageRegistry set, the chart still renders '$image' outside the mirror." ;;
  esac
done < <(chart_images --set "global.imageRegistry=$MIRROR")

if [ "$status" -eq 0 ]; then
  echo "Image inventory check passed: $INVENTORY matches every pin, and the chart mirrors cleanly."
fi
exit "$status"
