# Smokestack-style

High-contrast and monochrome with one ember accent, so the grill reads at a glance from across the yard.

The design system for the Smokestack dashboard, used from the #71 restyle on: a self-hosted dashboard and alert bridge for Pit Boss pellet grills. It's built on the **Vercel-style** system (imported from [iamstevedavis/ClaudeDesignSystems](https://github.com/iamstevedavis/ClaudeDesignSystems/tree/main/Vercel) at `1127bb8`), with **Linear's app patterns** for settings and lists. The base's values are used as they are. The app changes to match the system, not the other way round. The plan and open decisions are in issue #71. The dashboard's look before the restyle is kept as **Smokestack-style-old**: https://claude.ai/artifact/RjxxB7wbxQutqmAjoMWd2X

## Principles

- **Readable outdoors.** People check the grill on a phone in daylight from a few feet away. Readings are large, mono and `color-label-primary` on `color-bg`. Never put a reading or a label in `color-label-tertiary`.
- **Colour means something.** The base is black, white and grey. Ember marks the grill; otherwise colour appears only for a probe's series, a warning, an error or success. If something is coloured, it should tell the cook something.
- **Say it in words too.** Every coloured state also has a label ("Off target 12 min", "Stale: no reading for 45 s"), so colour is never the only signal.
- **Calm and dense.** From Linear: compact rows, a side list of sections, the control on the right of its label, hairline borders rather than shadows inside panels.
- **Careful with the grill.** Every command to the grill is confirmed first (ControlConfirm). Turning off uses the danger button. Nothing can turn the grill on.

## Colour

Both themes come from the base; **dark is the primary theme**, light is the alternative.

| Role | Tokens | Use |
| --- | --- | --- |
| Ground | `color-bg`, `color-surface-1`, `color-surface-2`, `color-surface-3` | Page, cards, inset controls, bar tracks |
| Text | `color-label-primary`, `color-label-secondary` | Readings and labels; hints, units and timestamps |
| Lines | `color-border-subtle`, `color-border-default`, `color-border-strong` | Row dividers, card and panel edges, focused or hovered edges |
| Actions | `color-label-primary` fill (`btn-primary`), `color-accent-blue` | The main action in a view; links and focus |
| States | `color-success`, `color-warning`, `color-error`, each with `-bg` and `-border` | Probe done and connected; off target and stale; grill errors and failed commands |
| Accent | `color-ember` | The grill: its chart line, legend, progress bar, and the brand dot. Never buttons or text |
| Chart | `series-grill`, `series-set`, `series-probe-1`, `series-probe-2` | Lines, legend dots and progress bars |

Additions to the base's colours: `color-ember` (#e8703f in dark, #c2410c in light), the chart series (the grill is `color-ember`, the set point a dashed `color-label-secondary`, probe 1 `color-accent-blue`, probe 2 `color-accent-teal`), and darker light-theme values for `color-warning` and `color-building` (#b45309) and `color-success` (#15803d). The dark theme keeps the base's status colours.

### Contrast to watch

- `color-label-tertiary` is about 2.8:1 on `color-bg` in both themes. Use it for decoration only.
- The base's light-theme warning (2.0:1) and success (2.6:1) failed on white, so they're replaced with #b45309 and #15803d, both 5.0:1. Their `-bg` and `-border` tints are still the base's.
- `color-accent-teal` is 3.2:1 on white in light: fine for probe 2's line, not for text.
- `color-accent-blue` passes for text in both themes (4.6:1 on black, 4.6:1 on white).

## Type

- `sans`: Geist, falling back to Inter and the system stack. `mono`: Geist Mono, falling back to JetBrains Mono and the system mono stack.
- Readings, set points, ages and the command log are always `--font-mono`, so digits don't jump as they change.
- The scale is the base's `text-11` to `text-48`. The grill's hero reading goes up to 96px.
- Geist and Geist Mono (free, OFL) are **self-hosted**: the app ships their woff2 files and serves them itself, with no font CDN. The files are in the app (`src/smokestack/static/fonts/`), added with the restyle (#71).

## Space and shape

- Spacing on the base's 4px grid, `space-1` (4px) to `space-24` (96px). Card padding is `space-5`; rows are `space-3` by `space-4`.
- Radius: `radius-md` (8px) for cards and row groups, `radius-lg` (12px) for dialogs, `radius-sm` for buttons and inputs, `radius-pill` for badges and switches.

## Components

**Smokestack:** GrillReadout, ProbeCard, StatusStrip, SettingsPanel, ControlConfirm and NumberStepper, built from the base's classes in `components/bundle.css`.

The base has no stepper, so the end of `components/bundle.css` adds one (`.stepper`, `.stepper-btn`, `.stepper--touch`) and hides the browser's spin arrows on `.input[type="number"]`. Those are the only styles added to the base.

**Base:** Buttons, Cards, Forms, Modals and Navigation from the Vercel-style system, kept for reference. Their sample content is the base's own (deployments, projects), not Smokestack's.

All previews are static HTML and CSS: copy the markup and classes. NumberStepper's preview includes a small script showing the stepping behaviour its guidelines describe.

## Not synced

- Variables the token format can't hold (motion, z-index, sizes, gradients, values built from other variables) are kept as CSS variables at the top of `components/bundle.css`, so the component styles work. They don't change with the theme switch.
