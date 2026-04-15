PYTHON ?= python3

.PHONY: check test

check:
	$(PYTHON) -m py_compile android_motion_emulator.py

test:
	$(PYTHON) -m unittest discover -s tests -p "test_*.py"
