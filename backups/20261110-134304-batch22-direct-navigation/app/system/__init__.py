"""Read-only system telemetry for Misty.

Only :class:`app.system.telemetry.SystemTelemetry` is exported.  The package
does not import Qt, models, tools, or configuration.
"""

from app.system.telemetry import SystemTelemetry

__all__ = ["SystemTelemetry"]
