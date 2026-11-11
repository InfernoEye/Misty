# Misty provisioning helpers. Every target is non-interactive.
#
#   make deps-check   verify requirements.txt pins, binaries, vendored builds, models
#   make lock         regenerate MANIFEST.lock from this machine
#   make test         run the isolated unit-test suite (bwrap, offscreen, no network)
#   make run          start Misty (desktop session required)
#   make run-popup    preview the popup UI only (no voice, no engine)
#
# Paths are relative to this directory; nothing here installs or downloads.

PYTHON ?= .venv/bin/python
PYFLAGS ?= -B

# No .pyc files anywhere, including child processes the app spawns.
export PYTHONDONTWRITEBYTECODE := 1

# Sandbox used by the test suite: read-only host, private /tmp, no network,
# no desktop/D-Bus session. Mirrors the recipe in MISTY_INTEGRATION_LEDGER.md.
BWRAP ?= bwrap
BWRAP_FLAGS = --ro-bind / / --dev /dev --proc /proc --tmpfs /tmp --unshare-net \
	--die-with-parent \
	--unsetenv DBUS_SESSION_BUS_ADDRESS --unsetenv DISPLAY --unsetenv WAYLAND_DISPLAY \
	--unsetenv MISTY_MEDIA_LIVE_TEST \
	--setenv QT_QPA_PLATFORM offscreen --setenv PYTHONDONTWRITEBYTECODE 1

# Refuse to run from anywhere else: the app resolves models and config
# relative to the project root.
ROOT := $(CURDIR)

.PHONY: all deps-check lock test run run-popup

all: deps-check

deps-check:
	@test -f $(ROOT)/requirements.txt || { echo 'run make from the project root'; exit 2; }
	$(PYTHON) $(PYFLAGS) $(ROOT)/scripts/check_deps.py --root $(ROOT)

lock:
	$(PYTHON) $(PYFLAGS) $(ROOT)/scripts/check_deps.py --root $(ROOT) --write-lock

test:
	cd $(ROOT) && $(BWRAP) $(BWRAP_FLAGS) $(PYTHON) $(PYFLAGS) \
		-m unittest discover -s tests -p 'test_*.py'

run:
	$(PYTHON) $(PYFLAGS) $(ROOT)/misty.py

run-popup:
	$(PYTHON) $(PYFLAGS) $(ROOT)/popup_preview.py
