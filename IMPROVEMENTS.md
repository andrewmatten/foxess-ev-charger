# Improvements backlog

Ranked. Nothing here is done in 3.1.3. Sizes: S (hours), M (a day or two), L (more).

1. **Firmware 1.8: a protective zero on a lossy link ends the session.** (M, safety path) With no acknowledged power-limit write for 45 s the integration writes zero; on firmware 1.8 that ends the session and charging stays off until something turns it on. Simulation: about 1 hour in 5 at 10% frame loss. Options: reuse a recent poll frame when the refresh's own read is lost (5% loss becomes clean, 10% improves by about a third); a longer telemetry grace; or one bounded automatic re-enable after the integration's own protective zero.
2. **A crash between the two state-file writes can restore a `hard` latch as a plain Stop fallback.** (S, `persistence.py`) Needs a crash within milliseconds, or the state write failing while the legacy write succeeds. Fix: import that case as a latch with no cause, which loads as `hard`.
3. **Unplugging a paused car stops the zero refresh.** (M, safety path) The charger returns to its own maximum, so a replug can run at full power for a few seconds until the saved limit is applied. Present before 3.1.3.
4. **One `hold` state instead of four flags.** (L, safety path, after real-charger testing) `safety_latched`, `latch_cause`, `stop_fallback` and `enabled` allow combinations that should not exist.
5. **Derive the soft latches from live evidence.** (L, design only) Store only `hard` and the release times; compute `comms`, `fault` and `stop_failed` from a rolling window of readings. Removes most saved-state and restart cases.
6. **`auto_retry_in` is empty whenever the healthy timer is not running.** (S) Show the full wait instead.
7. **An unconfirmed safety configuration reads as a fault.** (S) The blueprint then errors every minute until it confirms.
8. **A person's turn-on is refused for up to one poll after a hard latch lands.** (S) The latch is not yet published. Clicking again works.
9. **Command Time Validity range.** (S) The controller accepts 1-3600 s, the number entity 10-255, the FoxESS document says 10-60.
10. **Pin GitHub Actions to commit SHAs.** (S)
11. **Run the property tests under Hypothesis.** (S) They use fixed seeds with the standard library today.
12. **Historical replay tests skip in a normal checkout.** (S) They look for a history repository three directories up.
13. **Reconfigure step for host and port.** (M)
14. **Licence note for releases before 3.1.0.** (S, needs a decision) Those tags contain fork-derived code.
