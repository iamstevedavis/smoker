# ProbeCard

One food probe: its reading, its target and the button that sets the alert.

- The legend dot and bar use that probe's series colour (`series-probe-1`, `series-probe-2`), the same colour as its chart line.
- With no target, show "No target", an empty track and **Set alert**. With a target, show the target and **Edit alert**.
- When the target is reached, the bar turns `color-success`, the card border `color-success-border`, and a **Done** badge appears.
- **`metric-bar-track` has `flex: 1`** (it's built for horizontal metric rows). Inside a vertical flex container it stretches to fill the height and turns into a large pill, so give it `flex: none` there.
- The consumer provides the reading, the target, the unit and when the target was reached.
