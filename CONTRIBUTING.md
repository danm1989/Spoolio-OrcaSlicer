# Contributing

Thanks for helping out. The whole plugin is one Python file.

## Ideas and bugs

Open an [issue](../../issues/new/choose) using the bug report or feature
request form, or use the feedback icon on the plugin's Settings & About page.
For bugs, include your OrcaSlicer, Spoolio and Spoolman versions, your
operating system, and the end of `spoolio.log` (its path is shown at
the bottom of the Settings & About page).

## Layout

- `plugin/spoolio/spoolio_any.py` - the plugin. The `_any`
  suffix tells Orca Cloud it is OS-independent, so keep it free of
  OS-specific code.
  Network calls run on background `threading.Thread`s so the UI never blocks;
  see `_check_connection`, `_push_data` and the `check_update` handler.
- `scripts/check_version.py` - checks the plugin's version numbers agree.
- `assets/` - logo, banner and screenshot used in the README.

## Workflow

1. Branch from `main` and make your change. Keep Python code within 100 columns
   and follow PEP 8 (CI runs `ruff check`).
2. To try it in OrcaSlicer, copy the `plugin/spoolio/` folder into
   OrcaSlicer's plugin directory (or symlink it) and restart OrcaSlicer.
3. Add a line under **Unreleased** in `CHANGELOG.md`.
4. Open a pull request. CI checks the version numbers agree on Windows, Linux
   (x86_64 and arm64) and macOS, and lints the code.

## Versioning

The version is in **two places** in the plugin file: the `# version = "..."`
header and `PLUGIN_VERSION`. `scripts/check_version.py` fails if they differ.

## License

By contributing you agree that your work is licensed under the project's
[GNU General Public License v3.0](LICENSE).
