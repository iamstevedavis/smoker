# Releasing

Pushing a `v*` tag runs [`release.yml`](../.github/workflows/release.yml). It
checks the tag, runs the CI tests and image build, publishes the image to
`ghcr.io/iamstevedavis/smokestack` and creates a GitHub Release with
generated notes. Nothing is published if any step fails.

## Pick a version

Versions are `X.Y.Z`. While the project is `0.x`:

- **`0.Y+1.0`** for new features, or anything that changes how people run it
  (settings, compose files, the database volume).
- **`0.Y.Z+1`** for fixes only.

Pre-releases add a suffix: `0.2.0-rc.1`, `0.2.0-rc.2`.

## Release

1. Make sure the PRs since the last release have labels. The notes group them
   by `enhancement`, `bug` and `documentation`; anything else goes under
   "Other changes" ([`.github/release.yml`](../.github/release.yml)).
2. Bump `version` in `pyproject.toml`, the only place it's written, in a PR
   to `master`. Write pre-releases the way the tag spells them
   (`0.2.0-rc.1`), since the release checks the two match. The installed
   package normalizes it, so `/api/health` reports `0.2.0rc1`.
3. After it merges, tag the merge commit on `master` and push the tag:

   ```bash
   git switch master && git pull
   git tag -a v0.2.0 -m v0.2.0
   git push origin v0.2.0
   ```

## What the workflow checks

- The tag is `v` + the `pyproject.toml` version.
- The tagged commit is on `master`.
- The tests pass and the image builds (the same jobs as CI).

If a check fails, delete the tag (`git push --delete origin v0.2.0 && git tag -d v0.2.0`),
fix it on `master` and tag again.

## Image tags

| Tag pushed      | Image tags                 |
|-----------------|----------------------------|
| `v0.2.0`        | `0.2.0`, `0.2`, `latest`   |
| `v0.2.0-rc.1`   | `0.2.0-rc.1` only          |

Pre-releases never move `latest` or `X.Y`, and their GitHub Release is marked
as a pre-release. Images are amd64 and arm64, carry OCI labels (source,
version, `AGPL-3.0-or-later`) and have SBOM and provenance attestations.

## Verify

- The [Releases](https://github.com/iamstevedavis/smokestack/releases) page
  has the new release and its notes. Edit the notes there if needed.
- The image runs and reports the version:

  ```bash
  docker run --rm -d --name smokestack-check -p 8000:8000 ghcr.io/iamstevedavis/smokestack:0.2.0
  curl -s localhost:8000/api/health    # "version": "0.2.0"
  docker rm -f smokestack-check
  ```

## First release only

GHCR creates the package private. After the first release, open the package
(Your profile → Packages → smokestack) → Package settings → Change visibility
→ **Public**, so `docker compose pull` works without logging in. This can't be
done from the workflow.
