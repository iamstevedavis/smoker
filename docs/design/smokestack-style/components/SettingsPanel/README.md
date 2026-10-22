# SettingsPanel

The settings dialog: Linear's section list and setting rows, built from the Vercel-style tokens.

- The section list is `nav-docs-item` rows on `color-surface-1`. Each row shows its section's state on the right in mono ("WiFi", "On", "Off").
- A setting row puts the label (and an optional hint in `color-label-secondary`) on the left and the control on the right, separated by `border-subtle`. Group rows in a `border-default` box under a `section-label`.
- Choices between a few values use the segmented control. On/off uses `toggle`. Numbers use a NumberStepper with the unit beside it.
- Badge only values that differ from the default: `badge` "Custom" with **Reset**, or "From .env" as the hint.
- The footer counts unsaved changes in `color-warning`, with **Discard** (`btn-secondary`) and **Save & apply** (`btn-primary`).
- See issues #72 and #73 for what goes in each section.
