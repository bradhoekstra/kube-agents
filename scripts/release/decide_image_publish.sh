#!/usr/bin/env bash
# Decides whether a push should build and publish the SHA-tagged images, for
# docker-publish-ghcr.yml. Writes `build` (true|false) and `reason` to
# GITHUB_OUTPUT and prints them.
#
# `main` always builds, as it always has. A release branch builds only when all
# of these hold, each one closing a way the publish could do harm:
#
#   - the push came from the merger (Tide), so a collaborator's push of an
#     unreviewed commit under a release-branch name mints no signed images;
#   - the head commit is not the GA tagger's stamped release commit, whose
#     images nothing deploys;
#   - the commit has no published images yet. Image tags are mutable, and a
#     line opened at, or fast-forwarded to, a commit `main` already built would
#     otherwise rebuild it under the same `:<sha>` tags, replacing the manifests
#     its validation tags were earned against with a build no gate has seen.
#
# Inputs (environment): GITHUB_REF, GITHUB_SHA, GITHUB_ACTOR, HEAD_COMMIT_MESSAGE.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

# The identity that pushes merges: Tide, through the google-oss-prow GitHub App.
readonly RELEASE_MERGE_ACTOR="google-oss-prow[bot]"
readonly MAIN_REF="${GIT_BRANCH_REF_PREFIX}${RELEASE_MAIN_BRANCH}"

REF="${GITHUB_REF:-}"
SHA="${GITHUB_SHA:-}"
ACTOR="${GITHUB_ACTOR:-}"
HEAD_SUBJECT="${HEAD_COMMIT_MESSAGE:-}"
HEAD_SUBJECT="${HEAD_SUBJECT%%$'\n'*}"

if [ -z "${REF}" ] || [ -z "${SHA}" ]; then
  echo "❌ ERROR: GITHUB_REF and GITHUB_SHA are required." >&2
  exit 1
fi

decide() {
  if [ "${REF}" = "${MAIN_REF}" ]; then
    echo "true" "a push to ${RELEASE_MAIN_BRANCH} always builds"
    return
  fi
  if [ "${ACTOR}" != "${RELEASE_MERGE_ACTOR}" ]; then
    echo "false" "only merges pushed by ${RELEASE_MERGE_ACTOR} build on a release branch; this push is by '${ACTOR}'"
    return
  fi
  case "${HEAD_SUBJECT}" in
    "${RELEASE_STAMP_SUBJECT_PREFIX}"*)
      echo "false" "the head commit is the GA tagger's stamped release commit, whose images nothing deploys"
      return
      ;;
  esac
  if check_commit_images_exist "${SHA}" >/dev/null 2>&1; then
    echo "false" "images for ${SHA:0:7} already exist; a rebuild would replace manifests the release ladder validated"
    return
  fi
  echo "true" "a merge onto a release branch whose commit has no images yet"
}

read -r BUILD REASON <<<"$(decide)"

echo "==> build=${BUILD}: ${REASON}"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  {
    echo "build=${BUILD}"
    echo "reason=${REASON}"
  } >>"${GITHUB_OUTPUT}"
fi
