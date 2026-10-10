# Publishing releases

- Versions follow the upstream version with a `.postN` suffix, for example
  `2.7.11.post8`.
- Publish a GitHub release with tag `vX.Y.Z[.postN]` (or the same version without
  the leading `v`).
- The PyPI workflow verifies that the release tag matches `pyproject.toml`, runs
  the standard `python -m build`, and publishes the generated source and wheel
  distributions with PyPI Trusted Publishing.
- The PyPI Trusted Publisher is configured for
  `jeremiah-k/mtjk` + `.github/workflows/pypi-publish.yml` + `pypi-release`.
