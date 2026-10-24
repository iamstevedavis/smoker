# SettingsPanel

The settings dialog: Linear's section list and setting rows, built from the Vercel-style tokens.

- The section list is `nav-docs-item` rows on `color-surface-1`. Each row shows its section's state on the right in mono ("WiFi", "On", "Off").
- A setting row puts the label (and an optional hint in `color-label-secondary`) on the left and the control on the right, separated by `border-subtle`. Group rows in a `border-default` box under a `section-label`.
- Choices between a few values use choice cards (the source) or the segmented control. On/off uses `toggle`. Numbers use a NumberStepper with the unit beside it.
- Badge only values that differ from the default: `badge` "Custom" with **Reset**, or "From .env" as the hint.
- **Save as you go.** There is no Save button and no unsaved count. Text and number fields save when they lose focus, if the value changed; switches, choice cards and selects save at once. Show **Saved** next to a saved field for a moment, and the bridge's error under a rejected one, keeping what was typed. The footer says "Changes save when you leave a field. Changing the connection reconnects to the grill."
- On a phone (about 390 px) show the section list first, then one section full screen with **Back**.
- See issues #72 and #73 for what goes in each section.
