"""Pythonator: a desktop supervisor for long-running Python and Node.js scripts, web servers and bots."""

# The one place the version is written. pyproject.toml, the app (title bar, --version), the
# PyInstaller builds and the release workflow all read it from here. Changing it and pushing to main
# publishes a release (.github/workflows/release.yml). A dash, like "0.1.0-beta.1", makes a pre-release.
__version__ = "0.0.5"

__all__ = ["__version__"]
