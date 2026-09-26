# Contributing to Momento

Thanks for helping out. You don't need to write code to contribute. Testing on your distro, desktop or handheld is just as useful.

## Reporting a bug

Please include:

- your distro and version, desktop (KDE Plasma / GNOME / Hyprland / Gaming Mode...) and GPU
- the output of `./install.sh --check` and `momento status`
- the log: `journalctl --user -u momento.service -b --no-pager | tail -n 100`
- what you expected and what happened instead

If a package name in the install instructions is wrong for your distro, that's a bug too. Please report it.

## Making changes

1. Read [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for how it works and how to run it from the source tree.
2. Keep changes focused. One fix or feature per pull request.
3. Run `make lint test` before opening the PR. If you touched capture or export, also check `python3 tests/test_pipeline_live.py`.
4. Anything a player would notice (new settings, changed behaviour) also goes in the README, in plain language.

Guidelines:

- Runtime dependencies come from the distro, not pip. Don't add hard pip requirements.
- Nothing may require root. `install.sh` must stay user-level and idempotent.
- Prefer portals (xdg-desktop-portal) over compositor-specific hacks. Where a fallback is unavoidable, keep it isolated and documented.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
