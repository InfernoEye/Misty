"""Declarative plugin manifest support for Misty.

This package is intentionally isolated from the rest of Misty. It contains no
Qt, model, speech, desktop, network, database or tool-registry imports, and it
never imports, evaluates or executes plugin code. Manifests are inert data.
"""

from app.plugins.manifest import (
    ALLOWED_EFFECTS,
    ALLOWED_PERMISSIONS,
    MAX_MANIFEST_BYTES,
    PluginManifest,
    PluginTool,
    discover_manifests,
    load_manifest,
    validate_manifest,
)

__all__ = [
    "ALLOWED_EFFECTS",
    "ALLOWED_PERMISSIONS",
    "MAX_MANIFEST_BYTES",
    "PluginManifest",
    "PluginTool",
    "discover_manifests",
    "load_manifest",
    "validate_manifest",
]
