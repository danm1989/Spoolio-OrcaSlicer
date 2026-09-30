# Releasing

## One-time Orca Cloud setup

1. On <https://cloud.orcaslicer.com> open **Plugins > Shared Plugins** and
   create the plugin listing (upload `spoolio_any.py`, add the store
   image, description, tags and compatible OrcaSlicer version).
2. **Edit plugin > GitHub publishing**: enter `owner/repository` and click
   **Connect**. This gives Orca Cloud no access to the repo; it only records
   that this repo's release workflows may publish new versions.
3. In the GitHub repo, set the variable `ORCACLOUD_PUBLISH` to `true`
   (Settings > Secrets and variables > Actions > Variables). Until then the
   publish workflow is skipped.

## Cutting a release

1. Move the **Unreleased** notes in `CHANGELOG.md` under a new version heading.
2. Bump the version in **both** places in the plugin file (`# version = "..."`
   and `PLUGIN_VERSION`).
3. Merge to `main`, then tag it:
   ```bash
   git tag v0.1.1
   git push origin v0.1.1
   ```
4. The **Release** workflow checks the tag matches the plugin version, renders
   a 512px store image from the icon, and opens a **draft** GitHub release
   with generated notes.
5. Review the draft and click **Publish release**. That triggers **Publish to
   OrcaCloud**, which uploads the plugin file and uses the release notes as the
   changelog. The tag must be a semantic version higher than the current one.

Tags with a suffix (`v1.0.0-rc1`) are marked as pre-releases. Plain versions, including `0.x`, are not, because the plugin's update check uses GitHub's "latest release" API, which ignores pre-releases.

## Before publishing

CI covers the logic, not the OrcaSlicer UI, so try the plugin in OrcaSlicer
first and confirm the network and browser permission prompts work.
