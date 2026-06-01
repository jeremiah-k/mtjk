"""Version lookup utilities, isolated for cleanliness."""

from importlib.metadata import PackageNotFoundError, version

# Primary distribution name (used in pyproject.toml [tool.poetry] name).
# Swap to "meshtastic" when upstreaming.
PACKAGE_NAME: str = "mtjk"

# Ordered candidates for installed distribution metadata resolution.
# Fork builds can publish under an alternate package name while keeping
# the import package as `meshtastic`.
DISTRIBUTION_NAME_CANDIDATES: tuple[str, ...] = (PACKAGE_NAME, "meshtastic")

# Human-readable project name shown in CLI output.
PROJECT_DISPLAY_NAME: str = "Meshtastic (mtjk fork)"

# Recommended one-liner for upgrading the package.
# Uses pipx (recommended for CLI tools) with pip as fallback.
INSTALL_UPGRADE_HINT: str = (
    f"pipx upgrade {PACKAGE_NAME}"
)


def getActiveVersion() -> str:
    """Retrieve the active installed package version.

    The lookup tries each candidate distribution name in
    ``DISTRIBUTION_NAME_CANDIDATES`` and returns the first installed version.

    Returns
    -------
    str
        The package version string, or "unknown" if the distribution metadata cannot be found.
    """
    for distribution_name in DISTRIBUTION_NAME_CANDIDATES:
        try:
            return version(distribution_name)
        except PackageNotFoundError:
            continue
    return "unknown"


# COMPAT_STABLE_SHIM: historical snake_case alias.
def get_active_version() -> str:
    """Compatibility alias for `getActiveVersion`.

    Returns
    -------
    str
        Active version string resolved by getActiveVersion().
    """
    return getActiveVersion()
