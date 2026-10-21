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

- **Only the approved grill commands.** The bridge may send exactly the
  commands in
  [`docs/specs/2026-10-19-grill-control.md`](docs/specs/2026-10-19-grill-control.md)
  (turn off, set temperature, reboot the WiFi module, and switching the
  unit while the grill is off, approved for #58), built as that spec says
  (#59). Never add any other command that writes to the grill, above all
  power-on/ignition. Password/WiFi changes (#54) need their own approval
  first.
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

### Grill commands

Adding or changing one needs the owner's approval first (see Rules), then:

- the command in `sources/commands.py` (`Command`, `COMMANDS`, and
  `MCU_COMMANDS` if it goes to the control board), its pytboss call in
  `sources/_pytboss.py` `run_command`, and its behaviour in the fake source;
- a route under `/api/control/<command>` that goes through `GrillControl.run`,
  which supplies the lock, the session and enabled checks, the
  `X-Smokestack-Control` header and `Origin` check, the confirmation and the
  audit row. Nothing else lives under `/api/control/`: routes that don't
  write to the grill go elsewhere (the command log is `/api/control-log`);
- the tests that pin the list: `test_only_the_approved_commands_exist` and the
  forbidden calls in `tests/test_source_commands.py`, and the
  `/api/control/*` set in `tests/test_web.py`;
- the spec in `docs/specs/` and the approved list under Rules above.

Test control with the fake source's failure injection: add names from
`FAULTS` in `sources/fake.py` to `source.faults` (an RPC error, an ignored
command, a blank frame, `Unauthorized` once, a reboot that never comes back).

### Dashboard changes

CI only checks the dashboard's HTML and JS as text. After changing
`src/smokestack/static/`, run the app with the `fake` source and use it in a
browser (headless Chromium with Playwright is fine): the changed flow, at
desktop and phone (about 390 px) widths, with no errors in the console.

## Working on an issue

1. **Always finish with a PR.** Local commits alone aren't done: push the
   branch and open the PR (see below). Never merge it yourself.
2. **Address review comments.** For each one: fix it if it's valid and run
   the tests, push, and reply on that comment with what changed (and the
   commit) or why no change is needed. Reviews run again on each push, so
   check for new comments after pushing.
3. **Keep the PR mergeable.** If `master` moves and the PR conflicts, rebase
   onto `origin/master`, resolve, rerun the tests and push with
   `--force-with-lease`.

## Pull requests

- Branch from `master`, one issue per PR. Put `Closes #<n>` in the body when
  the PR finishes the issue, or `Part of #<n>` when it doesn't. An issue
  labelled `needs-hardware` stays open until the owner has checked it at the
  grill, so its PR says `Part of #<n>` and lists the hardware check.
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
