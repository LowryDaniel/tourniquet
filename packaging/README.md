# Packaging

Tourniquet ships via three OS-native package managers. Each subdirectory contains the manifest you'll publish to that ecosystem.

| Manager | OS | File | Status |
|---|---|---|---|
| **Homebrew** | macOS / Linux | [`homebrew/tourniquet.rb`](homebrew/tourniquet.rb) | Scaffolded — needs PyPI release first |
| **Scoop** | Windows | [`scoop/tourniquet.json`](scoop/tourniquet.json) | Scaffolded — needs PyPI release first |
| **winget** | Windows 10+ | [`winget/LowryDaniel.Tourniquet.installer.yaml`](winget/LowryDaniel.Tourniquet.installer.yaml) | Scaffolded — needs prebuilt binary |

## Prerequisite: publish to PyPI

The Homebrew and Scoop manifests both source from PyPI. You need to publish the Python package first:

```bash
cd /path/to/tourniquet
python -m pip install --upgrade build twine
python -m build
python -m twine upload dist/*  # uploads tourniquet-0.1.0.tar.gz + .whl
```

Then capture the sha256 of the source tarball:

```bash
shasum -a 256 dist/tourniquet-0.1.0.tar.gz
```

Replace `REPLACE_WITH_SHA256_AFTER_PYPI_UPLOAD` in `homebrew/tourniquet.rb` and `scoop/tourniquet.json` with that hash.

## Homebrew tap (one-time setup)

1. Create an empty repo: `github.com/LowryDaniel/homebrew-tourniquet`
2. Create the `Formula/` directory
3. Copy `homebrew/tourniquet.rb` → `Formula/tourniquet.rb` in that repo
4. Run `brew update-python-resources tourniquet` to auto-fill the `resource` blocks (run from the tap directory)
5. Push to main

Users then install with:

```bash
brew install LowryDaniel/tourniquet/tourniquet
```

## Scoop bucket (one-time setup)

1. Create a repo `github.com/LowryDaniel/scoop-tourniquet`
2. Copy `scoop/tourniquet.json` to its root
3. Push to main

Users install with:

```powershell
scoop bucket add tourniquet https://github.com/LowryDaniel/scoop-tourniquet
scoop install tourniquet
```

## winget (community submission)

winget requires a PR to the public manifest registry — no self-hosted bucket option (well, you can self-host but no one would discover it).

1. Build a standalone `tourniquet.exe` with PyInstaller (see comments in the YAML)
2. Upload to a GitHub Release on the main repo (`tourniquet/releases/v0.1.0`)
3. Capture the sha256 of the zip
4. Update the YAML
5. Fork `microsoft/winget-pkgs`, add your manifest under `manifests/l/LowryDaniel/Tourniquet/0.1.0/`
6. Submit a PR — Microsoft's bot validates and approves automatically if the manifest is well-formed

Users install with:

```powershell
winget install LowryDaniel.Tourniquet
```

## Recommended publishing order

1. **PyPI first** — unblocks `pip install tourniquet-dev` for everyone
2. **Homebrew tap** — covers macOS / Linux dev machines (5 min after PyPI)
3. **Scoop bucket** — covers Windows dev machines (5 min after PyPI)
4. **winget** — broadest Windows reach (~1 day for review). Do this last.
