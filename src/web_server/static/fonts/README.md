# Self-hosted webfonts

Both families are SIL Open Font License 1.1, downloaded from Google Fonts and
served from this directory so the dashboard makes no third-party request at
page load.

| File | Family | Axes | Upstream |
|---|---|---|---|
| `archivo-var-*.woff2` | Archivo (Omnibus-Type) | `wght` 100–900, `wdth` 62–125 | fonts.gstatic.com/s/archivo/v25 |
| `publicsans-var-*.woff2` | Public Sans (USWDS) | `wght` 100–900 | fonts.gstatic.com/s/publicsans/v21 |

The `wdth` axis on Archivo is load-bearing: the nav rail signals the active
section by setting its header wider, not by colouring it. Replacing Archivo
with a non-variable face would silently drop that signal.

Each family ships `latin` and `latin-ext` subsets with the `unicode-range`
descriptors Google generated, so `latin-ext` is only fetched when a member's
display name actually needs it.
