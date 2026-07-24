"""Gmail Factory vendor package marker.

The vendor directory is added to sys.path by ``hermes_runner.py`` and by
``scripts/install_gmail_factory.sh`` (which drops a ``.pth`` file into
the dedicated venv's site-packages). This file exists so that tooling
can recognise ``vendor/gmail_factory`` as a package and emit nicer
tracebacks.
"""
