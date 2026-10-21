# Publishing ScreenControl

Everything the [plugin manifest](https://github.com/Flow-Launcher/Flow.Launcher.PluginsManifest)
requires is prepared; only the GitHub side is left. Checked against the manifest's
own CI (`ci/src/validator.py`, all checks pass on a generated entry).

1. Create a **public** GitHub repository, here `aalbertinib/flow-launcher-screen-control`,
   default branch `main`, and add a `LICENSE` of your choice.
2. Generate the store entry and set the plugin's website:

   ```bash
   python build_plugin.py manifest --owner <github-user> --set-website
   ```

   This writes `release/ScreenControl-<ID>.json` (UrlDownload, UrlSourceCode and a
   jsDelivr IcoPath, as the store requires) and validates it. Use `--repo` / `--branch`
   if you pick other names.
3. Push. `.github/workflows/publish-release.yml` runs the tests, builds
   `Flow.Launcher.Plugin.ScreenControl.zip` and publishes release `v<Version>`
   (only when that version has no release yet).
4. Try the published release in Flow (remove the local install first, same plugin ID):
   `pm install https://github.com/<github-user>/<repo>/releases/download/v<Version>/Flow.Launcher.Plugin.ScreenControl.zip`
5. Fork the manifest repository, copy the generated JSON into its `plugins/` folder
   and open a pull request. After approval it can take a few days to show up in the store.

**Updates:** bump `Version` in `plugin.json` (and `CHANGELOG.txt`) and push. The
workflow publishes the release and the store picks it up within ~3 hours, no new
pull request needed. The store re-reads Description, Author, Language and Website
from the `plugin.json` inside the release zip.

## Recording the showcase

`README.md` shows `docs/demo.gif`, recorded with `tools/record_demo.py`. It opens Flow
with the plugin's shortcut, types a short tour (screens, brightness, contrast, quick
actions, HDR, profiles, help) and captures **only Flow's own window** (`PrintWindow`,
never the desktop). It only browses: no setting is changed.

```bash
pip install pillow
```

```bash
python tools/record_demo.py record docs/demo.gif
```

The README screenshots come from the same tool (`stills` mode). The identify screenshot
is taken within the overlay's rectangle and its flat colours are flattened, so nothing of
the desktop behind the 98%-opaque overlay can show through:

```bash
python tools/record_demo.py stills docs/images
```

Keep the plugin installed in Flow and don't touch the keyboard or mouse while it runs
(about 20 seconds); it stops if another window comes to the front.
