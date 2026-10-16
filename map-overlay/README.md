# Map overlay: compare scale

Stack two OpenStreetMap maps at the same real-world scale to compare an unfamiliar
place (bottom, full opacity) with one you know (top, translucent).

## Run

Serve the folder over HTTP (OSM's tile servers expect a normal `Referer`, so opening
the file directly via `file://` may get tiles blocked):

```sh
cd map-overlay
python3 -m http.server 8000
# open http://localhost:8000
```

## Controls

| Action | Effect |
| --- | --- |
| Pinch (trackpad) / Ctrl+scroll | Zoom both maps, kept at matched scale |
| Drag / two-finger scroll | Pan both maps together |
| Shift or Option(⌥) + drag / scroll | Move only the top map (change alignment) |
| "Dragging moves the top map only" checkbox | Same, without holding a key |
| Hold Space | Hide the top map to peek underneath |
| Double-click / Shift+double-click | Zoom in / out |
| Arrows, `+` / `−` | Pan / zoom from the keyboard |
| Esc | Show / hide the search panel |

The view (both locations, zoom, opacity) is kept in the URL hash, so you can bookmark
or share a comparison.

## How the scale matching works

Web Mercator stretches everything by `1 / cos(latitude)`, so the same zoom level shows
different real distances at different latitudes. The top map's zoom is therefore set to
`bottomZoom + log2(cos(topLat) / cos(bottomLat))` (fractional zoom), recomputed on every
pan and zoom, so one screen pixel covers the same number of metres at the centre of
both maps. Scale still varies slightly across a very zoomed-out view, as it does on any
Mercator map.

Maps are rendered with [MapLibre GL JS](https://maplibre.org/) using OSM raster tiles.
Each map extends one tile past the screen edges so nearby tiles are preloaded, and a
tile cache keeps recently seen areas so panning back does not re-download them.

Search uses the public [Nominatim](https://nominatim.org/) API; tiles and data are
© OpenStreetMap contributors.
