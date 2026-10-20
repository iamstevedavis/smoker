# Agent instructions

Smokestack is a self-hosted dashboard and alert bridge for Pit Boss pellet
grills. It reads the grill over WiFi (local HTTP RPC) or Bluetooth through
[pytboss](https://github.com/dknowles2/pytboss) and serves a FastAPI API, a
plain HTML/JS dashboard, MQTT and ntfy alerts. See the [README](README.md).

## Setup and checks

```bash
python -m venv .venv && . .venv/bin/activate
python -m pip install -e ".[test]"
pytest
PITBOSS_SOURCE=fake uvicorn smokestack.api:create_app --factory --reload
```

- Python 3.12 or 3.13, the versions CI tests.
- Use the `fake` source for anything that needs a running app. CI never talks
  to a real grill.
- The dashboard in `src/smokestack/static/` has no build step.
- CI also builds the Docker image: amd64 on PRs, amd64 and arm64 on `master`.

## Rules

- **The bridge is read-only towards the grill.** Never add anything that
  sends commands to it (power, temperature, probe targets). Grill control is
  only designed so far, in
  [`docs/specs/2026-10-19-grill-control.md`](docs/specs/2026-10-19-grill-control.md),
  and is not to be built until the owner approves it on #17.
- Don't push tags, create releases or bump the version unless asked. Releases
  follow [`docs/releasing.md`](docs/releasing.md).
- Never commit secrets, `.env` files or grill passwords.

## Code

- Match the surrounding code: type hints, `from __future__ import annotations`
  at the top of every module, small modules.
- Add or update tests in `tests/` for behaviour changes.
- Settings are env vars read in `src/smokestack/config.py`. Settings that
  describe the grill use the `PITBOSS_` prefix (`GRILL_FIELDS`); everything
  else uses `SMOKESTACK_`. Document each one in
  [`docs/integrations.md`](docs/integrations.md#settings). A setting editable
  from the dashboard also needs:
  - its group in `EDITABLE_GROUPS` (and `SECRET_FIELDS` if it's a secret);
  - a field with its validation in `overrides.SettingsUpdate`, which rejects
    unknown fields, so leaving it out makes the setting impossible to save;
  - its label and input type in the settings field list in
    `src/smokestack/static/app.js`;
  - a save round-trip test in `tests/test_settings.py`.
- `pyproject.toml` is the only place the version is written.

## Pull requests

- Branch from `master`, one issue per PR. Put `Closes #<n>` in the body when
  the PR finishes the issue, or `Part of #<n>` when it doesn't.
- Update the docs in the same PR when behaviour, settings or deployment
  change.
- **Label every PR.** The labels build the release notes
  ([`.github/release.yml`](.github/release.yml)):

  | Label | Use for | Notes section |
  | --- | --- | --- |
  | `breaking` | Anything that needs action when upgrading: renamed settings, moved data, changed compose files | Breaking changes (listed first) |
  | `enhancement` | New features and user-visible improvements | Features |
  | `bug` | Fixes | Fixes |
  | `documentation` | Docs-only changes | Documentation |
  | none | CI, tooling, refactors, chores | Other changes |

  A PR is listed once, under the first section it matches, so a breaking
  feature needs both `breaking` and `enhancement` but appears only under
  Breaking changes.
- **Write the PR title for someone running Smokestack.** It becomes the
  release-notes line.
- PRs are rebase-merged (the only method the repo allows), so every commit
  lands on `master` as-is. Keep each commit self-contained, with a clear
  subject. `feat:`/`fix:`/`docs:` prefixes are fine but optional. Mark a
  breaking change with `!` (`refactor!: ...`) as well as the label.
- Changes that need the real grill to verify get the `needs-hardware` label
  on the issue, with what to check written down.
