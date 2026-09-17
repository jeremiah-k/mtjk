# Dependency Health Policy

How mtjk decides whether a dependency is healthy, how Renovate's release-age
abandonment signal is handled, and the rules for source (Git) dependencies.
Origin: dependency health audit, 2026-09-16.

## Renovate abandonment signal

Renovate's "abandoned dependency" warning is a **release-age heuristic**, not
an upstream-maintenance verdict: it flags packages whose last registry release
exceeded the configured inactivity threshold. The global signal stays enabled
because it caught two real problems in this repository (PyTap2, PyQRCode).

Suppression is the exception, not the rule. A package may be added to the
`abandonmentThreshold: null` rule in `renovate.json` only after a manual
classification has been recorded in the table below (with a revisit date).

### Current exceptions

| Package    | Reviewed   | Classification                                                                                                                                                                          | Revisit                           |
| ---------- | ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------- |
| `pyserial` | 2026-09-16 | PyPI 3.5 (2020) is old, but the source repo is active (2026 pushes, unreleased Python 3.10+ modernization). Too central to replace; no better drop-in.                                  | Next pyserial release, or 2027-03 |
| `segno`    | 2026-09-16 | Latest release 1.6.6 (2025-03) is older than one year, but the source repo is active (2026-07). The same heuristic that flagged it here is exactly why the signal needs interpretation. | Next segno release, or 2027-03    |

### Deliberately left warning

- `ppk2-api`: the source repo is active, so "abandoned" is inaccurate — but the
  PyPI release has been stuck at 0.9.2 (2023-06) while unreleased behavior
  changes accumulate. Keep the dashboard reminder until the backend decision
  (see the ppk2lab evaluation plan on the `experiment/ppk2lab-backend` branch
  and the ppk2 adapter compatibility work) is made.

## Source (Git) dependencies

Rule: **registry releases by default.** If a Git source dependency is
unavoidable:

1. Pin an **immutable commit SHA** via `rev =` — never a moving reference
   (`HEAD`, a branch, or a URL `#fragment`). Fragments are not immutable
   pins: tags can be repointed, and depending on the Poetry version the
   fragment may not resolve as expected (the historical `riden#1.2.1`
   declaration left a stale `reference = "HEAD"` lock entry while the tag
   pointed at a different commit than the one we shipped).
2. Document next to the declaration why the Git source is required.
3. Test the behavior that needs it.
4. Link the upstream issue/PR that would let us return to a registry release.
5. Record an exit condition (e.g. "switch to PyPI when upstream publishes ≥
   X.Y").

Current Git dependencies: `riden` (pinned SHA
`27fd58f069a089676dcaaea2ccb8dc8d24e4c6d9`, geeksville/riden; reconsider
`riden-modbus` when mtjk's Python floor reaches 3.12 — it requires >=3.12).
