# NumberStepper

A number input between − and + buttons, for every number the user changes: the grill's set temperature, poll and stale seconds, alert thresholds.

The Vercel-style base has no stepper, and the browser's own spin arrows ignore the system's styling and don't appear on phones. So `.input[type="number"]` hides them, and up and down come from this component.

## Markup

```html
<div class="stepper" role="group" aria-label="Set grill to">
  <button type="button" class="stepper-btn" aria-label="Lower by 5°">−</button>
  <input id="set-to" class="input" type="number" inputmode="numeric" value="225">
  <button type="button" class="stepper-btn" aria-label="Raise by 5°">+</button>
</div>
```

Add `stepper--touch` for 44px buttons on phones. Keep a visible `<label for>` next to it; the group's `aria-label` repeats it for screen readers.

## Behaviour (the consumer provides it)

- **One valid step per tap.** For the set temperature, move to the previous or next value in the grill's accepted list (the app already has it, and it depends on the model and on °F or °C), so a tap never lands on a temperature the grill would reject. Other fields step by a fixed amount (1 for seconds).
- **Hold to repeat:** after about 400 ms, then every 90 ms, stopping on release, on leaving the button, or at a limit.
- **Keyboard:** the buttons are real buttons, so Enter and Space step once. A keyboard click has `detail === 0`; step on that, and on `pointerdown` for pointers, so a tap doesn't count twice.
- **Typing still works:** `inputmode="numeric"` opens the number pad on phones, and a typed value snaps to the nearest step on blur.
- **Limits:** clamp to min and max, and disable the − or + button there.
