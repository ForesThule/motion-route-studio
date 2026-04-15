# Contributing

Thanks for helping improve `Motion Route Studio`.

## Before You Start

- use Python `3.9+`
- keep changes focused and easy to review
- avoid mixing unrelated cleanup with functional changes

## Local Checks

Run these before opening a pull request:

```bash
make check
make test
```

If `make` is not available:

```bash
python3 -m py_compile android_motion_emulator.py
python3 -m unittest discover -s tests -p "test_*.py"
```

## Pull Request Guidelines

- explain what changed
- explain why it changed
- mention any behavior changes in the UI or CLI
- include validation steps when the change affects route generation or simulator behavior

## Good First Contributions

- improve route editing ergonomics
- add safe preview or validation features
- improve tests around motion profiles and payload parsing
- refine documentation and examples
