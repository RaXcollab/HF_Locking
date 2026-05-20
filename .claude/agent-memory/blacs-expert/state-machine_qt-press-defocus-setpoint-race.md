---
name: qt-press-defocus-setpoint-race
description: HF_Locking display.py setpoint QLineEdit reverts to old value on Set F click — Qt clicked-on-release + QTimer slow-poll interleave race; root cause confirmed, dirty-flag fix
metadata:
  type: project
---

HF_Locking `display.py` `ChannelControl`: typing a new freq setpoint then clicking "Set F" can revert the field and send the OLD value. CONFIRMED root cause = pure GUI-side Qt event-ordering race.

**Why:** `QAbstractButton` takes focus in `mousePressEvent`; `clicked` emits only in `mouseReleaseEvent`. Press and release are *separate event-loop iterations*, so a slow-poll `QTimer` (`update_slow`) can dispatch between them. At that moment `input_set.hasFocus()` is already False (focus on button) and `_setpoint_pending_until` is still 0.0 (armed only in `_on_setpoint`, which runs on release). Both guards pass → `update_slow` (`display.py:462-463`, the ONLY `setText` path) overwrites the field with stale `self._setpoint`. Release → `_on_setpoint` reads the clobbered text.

**How to apply (Qt semantics for any external-GUI QLineEdit + button pair):**
- A `hasFocus()` + lateness guard armed *on click* cannot cover the press→release window because focus already left on press and the guard arms on release. Use a `textEdited`-driven dirty flag (fires only on user edits, never programmatic `setText`).
- Clear the dirty flag in 3 places only: after successful parse/emit in the submit handler; when the box is blanked; in the channel-disable/reset path (`_on_switcher` Use=off, `display.py:~516`). NEVER clear on focus-out (reintroduces the race).
- Worker contract is sound and uninvolved: `handle_setpoint_write` (`workers.py:329-330`) updates SharedState before signal emit; ZMQ writes don't trip the pending guard.
- `input_volt`/`_on_voltage` is an identical latent twin but currently UNREACHABLE (no timer ever calls `input_volt.setText` — zero grep hits). Apply the same dirty-flag defensively to prevent future regression.
- "Harder to trigger with unused channels off" = hit-rate/timing change ONLY. `_poll_slow` (`workers.py:288`) iterates all 8 ports unconditionally; `update_slow` is not Use-gated. Race window unchanged — confirms genuine race, not state-dependent logic bug. Do NOT treat channel-disabling as a mitigation.
