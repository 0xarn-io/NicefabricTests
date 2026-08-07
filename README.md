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

## Notes

Two things from the NiceFabric README that these demos are careful about, and that are easy to
get wrong when writing your own:

- **`left`/`top` are the object's centre**, not its top-left corner (changed in Fabric 6).
- **`clear_objects()` is not `clear()`.** `clear()` is NiceGUI's own method for removing child
  elements; on a canvas it silently does nothing to your shapes.
