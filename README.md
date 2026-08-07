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
- Double-click the text object and type — `text` updates in the registry too.
- **Import an SVG** with the file picker. It comes back as a *flat list of ordinary objects*
  (`Path`, `Rect`, `Circle`, …), each individually selectable and editable — not one opaque
  image. Drag a single piece out of the imported drawing to see it.
- **Lock** a selection: the transform handles vanish and dragging stops. The lock flags are
  ordinary props, so you can watch them appear in the registry panel (🔒 in the object list).
- Rubber-band select several shapes and press <kbd>Delete</kbd>.
- Notice `on_added` stays quiet when you press the buttons: it fires only for free-hand strokes.

Two details worth knowing:

- `add_svg` is the one `add_*` that is **async and needs a connected browser** — Fabric's SVG
  parser runs on the browser's `DOMParser`, so it is a genuine round trip. Calling it from a
  page-builder body would only run out its timeout; an upload handler is the right place.
- It imports at **native size** and does not resize the canvas, so the demo scales and centres
  the result itself (`fit_to_canvas`, using `canvas.last_svg_size`). Fabric bakes `<g>`
  transforms into each object, so grouping is not preserved — scaling the drawing is just a
  multiply on every object's centre and scale.

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
