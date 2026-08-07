# NicefabricTests

A series of small, runnable demos for [NiceFabric](https://github.com/0xarn-io/NiceFabric) —
the Fabric.js canvas element for NiceGUI. Each one is a standalone app you start, open in a
browser, and play with. They are built one at a time, each focused on a single idea.

## Setup

`nicefabric` is not on PyPI yet, so install it from a sibling checkout. Demo 01 uses
`add_svg`, so the checkout needs to be recent enough to have it:

```sh
git clone https://github.com/0xarn-io/NiceFabric ../NiceFabric
pip install -e ../NiceFabric
pip install -r requirements.txt
```

## Running

Each demo owns a port, so several can run side by side.

```sh
python demos/01_shapes.py    # -> http://localhost:9090
```

Stop with <kbd>Ctrl</kbd>+<kbd>C</kbd>.

## The demos

| #  | Demo                                 | Port | What it shows                                            |
| -- | ------------------------------------ | ---- | -------------------------------------------------------- |
| 01 | [`01_shapes.py`](demos/01_shapes.py) | 9090 | Every `add_*` shape helper, SVG import, locking, selection, and the live server-side registry |
| 02 | [`02_editor.py`](demos/02_editor.py) | 9091 | The same library shaped like a tool: a full-page 2D editor with drawer-based tools, a properties panel, and the whole file suite |
| 03 | [`03_agv_tracks.py`](demos/03_agv_tracks.py) | 9092 | A domain editor: drag AGV track pieces from a palette onto a snapping grid, and get told which ends are still open |

### 01 — shapes, SVG import, locking, and the registry

The point is that **Python is authoritative**. A button press adds a shape to a server-side
registry and the browser renders it; dragging that shape in the browser reports its new geometry
back into the same registry. The panel on the right is `canvas.to_dict()` itself, polled twice a
second — not a hand-maintained copy — so what you see is what Python actually holds.

Worth trying:

- Add shapes, drag one, and watch `left`/`top` change in the registry panel.
- **Pick a fill and corner radius for Rect**, then add a couple — the chosen values arrive in
  the registry as `fill`/`rx`/`ry`. They apply to newly added rectangles only; existing ones are
  left alone, and the other shapes keep drawing random palette colours. (`rx` and `ry` are
  Fabric's two separate corner radii — setting only `rx` leaves `ry` at 0 and gives a lopsided
  corner, so the one slider drives both.)
- Double-click the text object and type — `text` updates in the registry too.
- **Import an SVG** with the file picker. It is flattened into a **single** canvas object, so
  the whole drawing drags, scales and rotates as one piece.
- **Lock** a selection: the transform handles vanish and dragging stops. The lock flags are
  ordinary props, so you can watch them appear in the registry panel (🔒 in the object list).
- Rubber-band select several shapes and press <kbd>Delete</kbd>.
- Notice `on_added` stays quiet when you press the buttons: it fires only for free-hand strokes.

#### Why the SVG is flattened rather than grouped

The demo inserts the SVG as one `Image` whose `src` is a `data:image/svg+xml;base64,…` URL,
instead of using the library's `add_svg`. That is a deliberate trade, and the reason is
`load_json`:

| Representation                                 | Registry after import | After `to_dict()` → `load_json()` |
| ---------------------------------------------- | --------------------- | ---------------------------------- |
| `add_svg(...)`                                 | 4 separate objects    | 4 separate objects                 |
| `add_object('Group', objects=[...])`           | 1 `Group`             | **0 — silently dropped**           |
| `add_image('data:image/svg+xml;base64,…')`     | 1 `Image`             | 1 `Image`                          |

`load_json` validates against an allow-list (`Rect`, `Circle`, `Ellipse`, `Line`, `Polygon`,
`Polyline`, `Path`, `Textbox`, `IText`, `Text`, `Image`). `Group` is not on it, so a grouped
import renders correctly and lives in the registry, but vanishes the first time a saved canvas
is reloaded. `Image` *is* on it, and `data:image/` is an accepted `src` scheme, so the
flattened form survives the round trip. The cost is that the drawing arrives flattened — it is
no longer editable shape-by-shape.

If you want the individual shapes instead, `await canvas.add_svg(source)` gives them to you.
Note it is the one `add_*` that is **async and needs a connected browser** (Fabric's parser
runs on the browser's `DOMParser`), and it imports at native size without resizing the canvas —
`canvas.last_svg_size` is what you scale against.

An SVG with only a `viewBox` and no `width`/`height` has no intrinsic size, and a browser
renders it at a 300×150 fallback inside an `<img>`. The demo injects the viewBox's dimensions
before encoding so the result is predictable.

Locking uses Fabric's own `lockMovementX`/`lockRotation`/… flags. They stop the *transform*;
they are not a permission system — a locked object stays selectable (deliberately, or you could
never unlock it) and keyboard <kbd>Delete</kbd> still removes it, since that path is handled
inside the library.

### 02 — a full-page 2D editor

Demo 01 is a lab bench; this is the same library shaped like a tool. The canvas fills the page
and follows the window; everything else lives in NiceGUI layout chrome:

- **Left drawer — creation.** Select/Draw tool toggle (Draw is the free-hand pencil, with brush
  colour and width), a button per shape, the style new shapes are born with (fill, stroke,
  stroke width), SVG insert (flattened, per demo 01's measured trade-off), and image-by-URL
  (validated against the same `https://`/`http://`/`data:image/` allow-list `load_json` uses —
  `add_image` itself passes `src` through unchecked).
- **Right drawer — inspection.** Canvas settings (background, zoom, reset view) and a
  properties panel for the selection: x/y, scale x/y, angle, opacity, fill, stroke, stroke
  width, font size for text, plus lock, duplicate, z-order, delete (the action buttons appear
  once something is selected). A `Selection JSON` expansion shows the registry entries behind
  the selection, and an event log runs below it.
- **Header `File` menu.** Save/load (server-side storage), JSON export/import as files
  (`to_json` is uncapped, so export always succeeds — import goes through `load_json` and its
  1 MB / 1000-object caps), SVG and PNG export (as downloads — exported SVG embeds
  user-controlled URLs and must not be re-inlined into a page), clear.

Machinery worth reading in the source:

- **Full-page sizing.** The canvas draws at a fixed pixel size — CSS stretching would desync
  hit-testing — so the page measures its content area and calls `canvas.resize()`: on connect,
  on debounced window resizes, and after drawer toggles.
- **Placement follows the view.** New shapes land at Fabric's `getVpCenter()` — the centre of
  what you *see*, which matters once you zoom or pan — with the surface centre as fallback
  before init.
- **The properties panel is rebuilt, not synced.** Selection changes and drags rebuild it from
  `FabricObject.props`, so the registry stays the single source of truth and there is no
  update-echo loop to guard. Panel edits refresh only the JSON view — rebuilding widgets on
  every keystroke would destroy the slider mid-drag.
- **Strokes use `strokeUniform`, and it is backfilled.** Scaling a Fabric object multiplies
  `scaleX`/`scaleY` and the stroke scales with it — measured on a 6px stroke scaled 0.5 × 3,
  the sides render at 2px and the top and bottom at 18px, which reads exactly like a stretched
  bitmap. `strokeUniform: true` holds the stroke at 6px on every edge. The shape tools set it
  at creation, but objects arriving by other routes (a canvas saved before this demo set it, a
  brush path, an SVG-parsed shape) do not carry it, so `ensure_uniform_strokes()` backfills it
  after load, after JSON import, and after each free-hand stroke.

### 03 — AGV track editor

A *domain* editor rather than a general one. The canvas is a grid of 80 px cells and every
object on it is one track piece: straight, 90° curve, fork, crossing, charging / load / unload
station, end stop. Drag a piece out of the palette and it lands on the cell you dropped it on;
drag a placed piece and it snaps back to the grid; press <kbd>R</kbd> to rotate the selection.
The right drawer runs a connectivity pass and lists the ends that are still open, so a finished
loop reports *every end is connected*.

Four decisions carry the design:

- **One object per piece.** Each piece is a small generated SVG rendered into a single Fabric
  `Image` through a `data:image/svg+xml` URL — the flattened form demo 01 measured as the only
  single-object representation that survives `to_dict()` → `load_json()`.
- **The kind rides on the object.** `add_image(url, kind='curve')` stores a custom prop, and
  `load_json` deep-copies unknown props, so `kind` comes back after a save/load. This is not a
  stylistic choice: **`load_json` re-ids every object**, so a side table keyed by object id
  would be wiped by the first load. Metadata has to travel *on* the piece.
- **Ports are derived, never stored.** `kind` gives the base ports and Fabric's own `angle`
  gives the rotation; connectivity rotates one by the other. Rotating clockwise moves
  N→E→S→W, which is a single index shift along `'NESW'`, so no per-piece rotation table
  exists to fall out of date. There is one copy of each fact, so nothing can drift.
- **Snapping is server-side.** A drop reports the pointer position and `on_modified` reports
  the position after a drag; both are rounded to the cell grid and to 90° and written back
  with `update_object`. The browser never decides where a piece actually is.

Drag and drop is real HTML5 DnD: the palette tiles are drag sources, and a `drop` listener on
the canvas wrapper hands the pointer position back through NiceGUI's `emitEvent`. The grid
itself is a CSS background on the canvas element, showing through because the Fabric
background is left transparent.

Station arrows point *along* the track stub rather than up the page — the whole tile rotates
with the piece, so an arrow drawn across the stub would point somewhere arbitrary once placed.

## Notes

Two things from the NiceFabric README that these demos are careful about, and that are easy to
get wrong when writing your own:

- **`left`/`top` are the object's centre**, not its top-left corner (changed in Fabric 6).
- **`clear_objects()` is not `clear()`.** `clear()` is NiceGUI's own method for removing child
  elements; on a canvas it silently does nothing to your shapes.
