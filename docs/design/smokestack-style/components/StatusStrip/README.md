# StatusStrip

The connection line, the grill's component states and its error flags.

- The connection line (`role="status"`) names the source and the age of the last reading, from `/api/health`.
- Components that are on use `status-badge--ready` and a `border-default` tile. Components that are off use `status-badge--queued` with secondary text.
- Error flags use `badge--success` when there are none, `badge--warning` for stale data and `badge--error` for grill errors. Always say what the flag is in words; never rely on colour alone.
