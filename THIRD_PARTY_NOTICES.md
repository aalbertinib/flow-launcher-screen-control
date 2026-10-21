# Third-party notices

The release archive bundles one third-party package in `lib/`:

| Package | Version | License | Source |
| --- | --- | --- | --- |
| flogin | 2.0.0a579+g4782113 (commit 4782113) | MIT (declared in its package metadata) | https://github.com/cibere/flogin |

flogin is by cibere. Its repository does not include a license file; its package
metadata declares the MIT License.

flogin's optional dependencies are not bundled: its pip helper (and therefore
`requests`) is replaced by an empty stand-in, and its `aioconsole`-based input
loop is replaced by the plugin's own standard-library code.
