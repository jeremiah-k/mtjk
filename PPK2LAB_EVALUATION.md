# PPK2 Backend Evaluation: ppk2lab (experiment)

Status: **experiment — no production switch.** This branch only records an
evaluation plan. The production powermon backend remains `ppk2-api` 0.9.2 from
PyPI (see `maint/ppk2-backend-compatibility` for adapter hardening).

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

1. Requires Python >= 3.11; mtjk supports 3.10–3.14. Until mtjk raises its
   floor or ppk2lab widens support, ppk2lab cannot be an unconditional
   powermon dependency.
2. Alpha classification: measurement fidelity and device coverage are not yet
   proven to the standard required for a hardware backend.

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
- A migration path exists for Python 3.10 users (keep ppk2-api behind a
  selection flag, or mtjk floor rises to 3.11 first).

## Exit conditions

- **Adopt** when success criteria are met and ppk2lab is no longer Alpha
  (or the hardware validation above is deemed sufficient by a maintainer).
- **Stay on ppk2-api** if evaluation fails or stalls; revisit when ppk2-api
  publishes a new release or mtjk's Python floor changes.
- **Fork ppk2-api** only for a concrete upstream-blocked hardware fix: carry
  the smallest patch, pin an exact commit, submit upstream, and record an
  exit condition. Note ppk2-api is GPLv2 — any vendoring/copying of source
  requires a separate license review.

## Results

| Date | PPK2 firmware | Procedures passed | Notes |
| ---- | ------------- | ----------------- | ----- |
| —    | —             | —                 | Not yet run |
