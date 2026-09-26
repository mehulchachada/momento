# Contributing to Momento

Thanks for helping out. You don't need to write code: testing on your distro, desktop or handheld is just as useful. Please be kind to everyone here.

## Reporting a bug

Open an issue with:

- the steps to make it happen, what you expected and what happened instead
- a screenshot if it's something you can see
- your distro, desktop (KDE Plasma, GNOME, Gaming Mode...) and GPU
- the output of `./install.sh --check` and `momento status`

A wrong package name for your distro in the README counts as a bug too.

## Running from a clone

Try it without a game by using the moving test picture. Put this in a file, for example `test.toml`:

```toml
[capture]
source = "test"
```

Then, from the repo folder:

```sh
python3 -m momento -v --config test.toml daemon   # start the recorder
python3 -m momento overlay                        # open the clip bar
make test                                         # run the tests
```

Stop your installed Momento first (`systemctl --user stop momento.service`) so the two don't clash.

## Pull requests

- One fix or feature per pull request.
- Run `make lint test` before opening it.
- If players would notice the change, update the README in plain words.
- Nothing may need root, and `install.sh` must stay user-level.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
