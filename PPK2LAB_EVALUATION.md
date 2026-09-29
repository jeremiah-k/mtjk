# PPK2 Backend Evaluation: ppk2lab (experiment)

Status: **parked (2026-09-29) — no PPK2 hardware is available for the
evaluation.** The production powermon backend remains `ppk2-api` 0.9.2 from
PyPI (see https://github.com/jeremiah-k/mtjk/pull/526 for adapter hardening).
This document is kept as the standing evaluation plan; see "Exit conditions"
for what would reopen the decision.

Context: dependency health audit, 2026-09-16. `ppk2-api`'s PyPI release has been
stuck at 0.9.2 (June 2023) while upstream master carries unreleased behavior
changes (including `list_devices()` tuple entries) and active hardware-related
reports for newer PPK2 firmware. Before any backend decision, evaluate the
modern alternative against real hardware.

## Candidate: ppk2lab 0.5.1

- Released 2026-08-25; MIT licensed; typed; Trusted Publishing with provenance.
- Supports Python 3.11–3.14.
- Classified **Alpha**; real-hardware validation still young.

## Blockers (why it cannot become the default today)

1. ~~Requires Python >= 3.11; mtjk supports 3.10–3.14.~~ Resolved: the mtjk
   floor is now 3.11. The version gate no longer blocks adoption.
2. Alpha classification: measurement fidelity and device coverage are not yet
   proven to the standard required for a hardware backend.
3. No PPK2 hardware is available to run the real-hardware evaluation below;
   measurement fidelity cannot be validated without it.

## Evaluation procedure (requires real PPK2 hardware)

Run on an experiment branch only (`--with powermon` plus ppk2lab installed
ad hoc). Record firmware revision of the PPK2 unit under test.

1. **Discovery parity**: enumerate devices via ppk2lab; compare against
   `PPK2_API.list_devices()` output (both the 0.9.2 string shape and the
   master tuple shape). Multiple-device ambiguity handling must be possible.
2. **Measurement parity**: for a fixed DUT load (e.g. 3.3 V rail, known
   resistor), compare average/min/max current over 60 s windows between
   ppk2-api 0.9.2 and ppk2lab. Tolerance: within 1% or 2 µA, whichever is
   larger.
3. **Mode behavior**: source-meter vs amp-meter switching, source voltage
   set, DUT power toggle on/off; verify each takes effect on hardware.
4. **Sample-stream stability**: 30-minute continuous capture without read
   errors, stalls, or memory growth; verify reconnect after USB re-enumeration.
5. **Firmware matrix**: repeat 1–4 against each PPK2 firmware revision we
   intend to support; record results below.

## Success criteria

- All five procedures pass on every tested firmware revision.
- (The former Python 3.10 migration-path criterion is moot: the mtjk floor
  is 3.11 as of 2026-09-29.)

## Exit conditions

- **Parked (2026-09-29):** stay on `ppk2-api` 0.9.2. The decision reopens when
  any of these changes: a PPK2 unit becomes available to run the evaluation,
  `ppk2-api` publishes a new PyPI release, or `ppk2lab` leaves Alpha with
  real-hardware validation documented by its maintainers.
- **Adopt** when the parked decision is reopened and the success criteria are
  met (ppk2lab no longer Alpha, or the hardware validation above is deemed
  sufficient by a maintainer).
- **Fork ppk2-api** only for a concrete upstream-blocked hardware fix: carry
  the smallest patch, pin an exact commit, submit upstream, and record an
  exit condition. Note ppk2-api is GPLv2 — any vendoring/copying of source
  requires a separate license review.

## Results

| Date | PPK2 firmware | Procedures passed | Notes       |
| ---- | ------------- | ----------------- | ----------- |
| —    | —             | —                 | Not yet run |
