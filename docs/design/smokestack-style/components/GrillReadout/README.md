# GrillReadout

The grill's hero reading: the current temperature, its set point and how close it is.

- The reading is `--font-mono` at 96px in `color-label-primary`, with the unit in `color-label-secondary`.
- The bar is a `metric-bar-track`. Its fill is `series-grill` (ember) while the grill is holding, and `color-warning` while it's off target long enough to alert.
- The state badge uses `status-badge--ready` for holding and `status-badge--building` (amber) for off target.
- **`metric-bar-track` has `flex: 1`** (it's built for horizontal metric rows). Inside a vertical flex container it stretches to fill the height and turns into a large pill, so give it `flex: none` there.
- The consumer provides the reading, the set point, the unit and the drift state.
