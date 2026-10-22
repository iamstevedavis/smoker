# Design

The dashboard's visual design lives in the **Smokestack-style** design system. The
restyle in [#71](https://github.com/iamstevedavis/smokestack/issues/71) moves the
dashboard onto it, and the settings work in
[#72](https://github.com/iamstevedavis/smokestack/issues/72) and
[#73](https://github.com/iamstevedavis/smokestack/issues/73) builds on it.

## What's here

[`smokestack-style/`](smokestack-style/) is a copy of the design system:

| File | What it is |
| --- | --- |
| [`README.md`](smokestack-style/README.md) | The brand book: principles, colour roles, type, spacing, contrast notes |
| [`tokens.json`](smokestack-style/tokens.json) | Every token: colours in dark and light, type, spacing, radius, shadows |
| [`tokens.css`](smokestack-style/tokens.css) | The tokens as CSS custom properties, generated from `tokens.json` |
| [`components/bundle.css`](smokestack-style/components/bundle.css) | The component styles. The end of the file holds the Smokestack additions (the stepper, hidden number spinners) |
| `components/<Name>/README.md` | Each component's guidelines |
| `components/<Name>/preview.html` | Each component's markup, as a fragment that expects `tokens.css` and `bundle.css` |
| [`gallery.html`](smokestack-style/gallery.html) | Every preview on one page with a theme switch. Open it in a browser from a checkout |

Use these files when working on the dashboard: copy the markup and class names
from a component's preview, and the token names from `tokens.json`.

## Where it came from

- The base is the **Vercel-style** system from
  [iamstevedavis/ClaudeDesignSystems](https://github.com/iamstevedavis/ClaudeDesignSystems/tree/main/Vercel)
  at `1127bb8`, used unchanged.
- Layout patterns (side list of sections, setting rows with the control on the
  right, switches) come from that repo's **Linear-style** system.
- On top: the Smokestack components (GrillReadout, ProbeCard, StatusStrip,
  SettingsPanel, ControlConfirm, NumberStepper) and the decisions below.

The system is also kept in Claude Design, where it can be browsed and edited. Those
links are private to the repo owner:

- Smokestack-style: https://claude.ai/artifact/Fhp5QncT2PFAhZnd33fSKf
- Smokestack-style-old, a snapshot of the dashboard before the restyle: https://claude.ai/artifact/RjxxB7wbxQutqmAjoMWd2X
- A mockup of the dashboard and settings in Smokestack-style: https://claude.ai/artifact/FB46ziwNLGZDUAhsDiKKPR

**This folder is the copy to build from.** If the system changes in Claude Design,
copy it here again in the same PR as the code that uses the change.

## Decisions

Made on #71 and the settings issues:

- **One accent: ember.** `color-ember` (#e8703f in dark, #c2410c in light) marks the
  grill: its chart line, legend, progress bar and the brand dot. The reading itself
  stays `color-label-primary`. Ember isn't used for buttons or text.
- **Darker status colours in the light theme.** The base's light-theme warning and
  success failed contrast on white, so they're #b45309 and #15803d (5.0:1). The dark
  theme keeps the base's.
- **Self-hosted fonts.** Geist and Geist Mono (free, OFL) ship with the app as woff2
  files and are served by it. No font CDN.
- **Dark first**, with light as the alternative, as in the base. The dashboard keeps
  its Auto / Light / Dark theme button.

## Things to watch

- **`.metric-bar-track` has `flex: 1`.** It's built for horizontal rows. Inside a
  `flex-direction: column` container it stretches to fill the height and becomes a
  large pill, so give it `flex: none` there.
- **Number inputs:** the base's `.input` doesn't remove the browser's spin arrows.
  `bundle.css` hides them on `.input[type="number"]`; use the NumberStepper for up and
  down. For the grill's set temperature, step through the setpoints the grill reports
  (the list `snapTemp` uses in `app.js`), not a fixed number of degrees.
- **Contrast:** `color-label-tertiary` is about 2.8:1, so never use it for text.
  `color-accent-teal` (probe 2) is 3.2:1 on white: fine for a line, not for text.
