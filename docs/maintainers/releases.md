# Publishing releases

Run `make ci`, `make check-artifacts`, and `make audit-deps` against the release candidate before publishing. Dependency auditing queries live advisories; a green result from an earlier commit or date does not replace the candidate check.

- Versions follow the upstream version with a `.postN` suffix, for example
  `2.7.11.post14`.
- Publish a GitHub release with tag `vX.Y.Z[.postN]` (or the same version without
  the leading `v`).
- The PyPI workflow verifies that the release tag matches `pyproject.toml`, runs
  the standard `python -m build`, and publishes the generated source and wheel
  distributions with PyPI Trusted Publishing.
- The PyPI Trusted Publisher is configured for
  `jeremiah-k/mtjk` + `.github/workflows/pypi-publish.yml` + `pypi-release`.

## Standalone release assets

Publishing a release also triggers `.github/workflows/release-assets.yml`:

- The workflow checks out trusted `develop`, verifies the release tag against
  trusted branding and the develop lineage with `bin/verify_release_source.py`,
  and checks out the validated release commit before building.
- `bin/build-bin.sh` builds a single-file PyInstaller executable named after
  `PRIMARY_CLI_NAME`; `bin/smoke-standalone.sh` smoke-tests the binary's
  `--version` output against the released version.
- The binary is uploaded to the release as `<primary-cli>_ubuntu` together with
  `readme.txt` (staged from `standalone_readme.txt`); a workflow-artifact copy
  is retained for 30 days.
- A `workflow_dispatch` path rebuilds the assets for an existing tag as a
  recovery route; it requires a `release_tag` input and the release must
  already exist.
