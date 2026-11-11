"""Isolated web access for Misty research tools.

The implementation lives in :mod:`app.web.fetch`; import it directly::

    from app.web.fetch import fetch

Keeping this package initializer minimal avoids shadowing the ``fetch``
submodule with the ``fetch`` function.  Importing the package performs no
network I/O and does not touch Misty's database, models, or configuration.
"""
