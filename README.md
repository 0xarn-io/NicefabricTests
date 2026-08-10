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
| 03 | [`03_agv_tracks.py`](demos/03_agv_tracks.py) | 9092 | A domain editor in the idiom of fleet-commissioning software: laser-scan underlay, drag-and-drop route elements on a metric snapping grid, node/properties/telemetry docks |
| 04 | [`04_pid_bom.py`](demos/04_pid_bom.py) | 9093 | A P&ID editor whose bill of materials builds itself: SKU-tagged ISA symbols, click-to-draw pipe runs billed by length, CSV export |
| 05 | [`05_ethercat_cabinets.py`](demos/05_ethercat_cabinets.py) | 9094 | EtherCAT cabinet planner: DIN rails that pack terminals left-to-right, an E-bus current audit, and a BOM general **and** per location |

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

### 03 — AGV route studio

A *domain* editor rather than a general one, laid out the way fleet-commissioning suites are:
mode tabs across the top, the site field in the middle, a stacked Objects / Properties /
Vehicle sidebar on the right, and a status bar carrying live cursor coordinates and a sync
state. The field follows the same idiom — white site with a metric grid and edge rulers, the
laser scan underneath as red returns, violet route splines, numbered node circles at junctions
and stations, and direction chevrons along each segment.

One grid cell is 1.0 m, so everything reports in metres. Every object on the canvas is one
route element: segment, 90° curve, turnout, crossing, charger, pick / drop station, end stop.
Drag one out of the palette and it lands on the cell you dropped it on; drag a placed element
and it snaps back to the grid; press <kbd>R</kbd> to rotate. The connectivity pass reports
every port still open, so a finished loop reads *network closed — synchronized*.

Node ids are **positional**, not object ids — sorted by position and numbered from 1001 — so
the same layout always yields the same ids even though `load_json` regenerates every
underlying object id on load.

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

- **Node captions are a derived layer.** They are separate `Text` objects held at `angle=0`,
  because a caption baked into the tile art would rotate with the tile and read upside down at
  180°. They carry `kind='label'` so the connectivity pass skips them, and are rebuilt only
  when the layout signature changes, so a plain drag does not churn the registry.
- **Elements cannot be scaled.** `lockScalingX`/`lockScalingY` are set on every placed
  element. Route geometry is fixed: a stretched curve would no longer meet its neighbours.

Drag and drop is real HTML5 DnD: the palette rows are drag sources, and a `drop` listener on
the canvas hands the pointer position back through NiceGUI's `emitEvent`. That position is in
screen pixels, so it is divided back through the zoom before snapping — otherwise a drop made
while zoomed lands on the wrong cell. The cursor readout and the edge rulers are painted
straight from JS: neither needs a server round trip, and rulers as DOM never enter the object
registry.

Two things that bite when styling this element:

- **A `data:` URL cannot go in a NiceGUI `style` attribute.** It contains `;base64,`, and the
  inline-style parser splits on `;`. The laser scan therefore lives in real CSS on its own
  layer behind the transparent canvas, which also lets the scan and grid toggle independently.
- **`element.classes(replace=…)` drops every existing class**, including any hook you are
  using to find the element later.

Station symbols are read *relative to the stub* rather than drawn upright — the whole tile
rotates with the element, so a glyph drawn across the stub would point somewhere arbitrary
once placed.

### 04 — P&ID editor with a live bill of materials

A drafting tool that costs itself. Drag ISA symbols out of a catalogue, draw pipe runs by
clicking two points, and the BOM underneath aggregates by part number: equipment and valves by
count, **pipe and signal cable by drawn length**, each with an extended price and a total that
exports to CSV. Symbols get ISA tags automatically (`HV-101`, `FCV-101`, `TIC-101`, …).

- **The catalogue is the source of truth.** `CATALOG` holds one entry per symbol — artwork,
  ISA tag prefix, SKU, description, unit price. A placed symbol stores only its `kind`;
  everything else is looked up, so a price change is a one-line edit that cannot desync from
  what is on the drawing.
- **Line items are counted, never stored.** Quantities come from walking the registry on every
  refresh, so deleting a valve drops it out of the BOM immediately and nothing can drift.
- **Pipe is drawn with `on_mouse_down`.** In pipe mode the first press records a corner and the
  second draws an orthogonal `Polyline`. The event reports **scene** coordinates, so no
  screen-to-canvas conversion is needed — though the pointer is still divided back through the
  zoom before snapping.
- **A `Polyline`'s `left`/`top` is its bounding-box centre**, with `points` relative to the
  box's top-left corner. Measured against painted pixels, a run asked for at (150,120)→(600,380)
  lands on exactly that.
- **Tags are positional** — sorted and numbered per prefix — so a drawing keeps its tag numbers
  even though `load_json` regenerates every underlying object id on load.
- **A drawing tool has to switch off the objects.** While Pipe or Signal is active, every
  object gets `selectable=False, evented=False` and the cursor becomes a crosshair. Without
  that, a click landing on a symbol selects it and a press-drag *moves* it instead of setting
  a run's corner — which reads as the tool selector doing nothing at all.

The two header tabs switch views: **Diagram** keeps the BOM as a strip under the sheet,
**Bill of materials** hides the sheet and hands the table the whole page.

Measured end to end: a 400 px run bills as 5.00 m at 38.00/m = 190.00, a 160 px signal run as
2.00 m at 6.40/m = 12.80, and the exported CSV carries the same numbers as the on-screen table.

### 05 — EtherCAT cabinet planner

Same shape as the P&ID, but the model is **containment** rather than a free sheet: a terminal
belongs to a rail, a rail belongs to a cabinet, and terminals pack left to right with no gaps.
Drop one between two others and the rest shuffle right, the way real terminals behave. That
structure is what makes a per-location BOM mean anything — the panel switches between a
**General** roll-up and a **By location** breakdown (`+CAB01`, `+CAB02`, …), and the CSV export
carries both.

The drawing is also audited, not just counted:

- **E-bus current budget.** An `EK1100` supplies 2000 mA; every terminal to its right draws it
  down and an `EL9410` refreshes it. The panel walks each rail in order and reports the
  headroom, naming the terminal where it goes negative — measured: 11 × `EL5101` at 200 mA
  against a 2000 mA supply reports *short by 200 mA at EL5101*.
- **Bus end cap.** A segment must finish with an `EL9011`; a rail without one is flagged.
- **Rail fill.** Terminal widths summed in millimetres against the usable rail length.

Containment cannot be stored as object references, because `load_json` **re-ids every object**.
Each terminal instead carries its location designation and rail index as plain custom props,
and its order along the rail is simply its `left` — sorted and re-packed on every layout pass.
Nothing points at an id, so a save/load round trip keeps the whole hierarchy.

Terminal part numbers are printed **vertically inside the terminal artwork**, the way Beckhoff
prints them. That relies on `<text>` rendering inside a `data:` URL SVG loaded as a Fabric
`Image` — verified by pixel-measuring the glyphs before building on it. Only generic font
families are available there; an SVG loaded as an image cannot fetch external fonts.

Two deliberate simplifications: each rail is audited as its own E-bus segment (on real hardware
a segment carries across rails through an `EK1110`/`EK1100` pair), and cabinets are drawn to fit
their rails rather than to enclosure scale.

The catalogue covers infrastructure (`EK1100`, `EK1110`, `EL9410`, `EL9011`, potential
distribution), digital (`EL1008`, `EL1409`, `EL1809`, `EL2008`, `EL2409`, `EL2809`), analog,
comms & special (`EL5101`, `EL6001`, `EL6224` IO-Link master, `EL2574` pixel LED, `EL7031`)
and safety (`EL6910` TwinSAFE Logic, `EK1960` TwinSAFE Compact Controller). The `EK1960` is
126 mm wide and carries its own EtherCAT connectors, so it heads its own segment rather than
drawing from an upstream coupler — which also makes it the part that shows off the packing,
since it swallows a sixth of a rail on its own.

> Part numbers and functions were checked against Beckhoff's product pages, but the widths,
> E-bus figures and prices are **representative values for the demo** — not a datasheet or a
> price list.

## Notes

Two things from the NiceFabric README that these demos are careful about, and that are easy to
get wrong when writing your own:

- **`left`/`top` are the object's centre**, not its top-left corner (changed in Fabric 6).
- **`clear_objects()` is not `clear()`.** `clear()` is NiceGUI's own method for removing child
  elements; on a canvas it silently does nothing to your shapes.
