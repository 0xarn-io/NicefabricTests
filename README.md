# NicefabricTests

A series of small, runnable demos for [NiceFabric](https://github.com/0xarn-io/NiceFabric) —
the Fabric.js canvas element for NiceGUI. Each one is a standalone app you start, open in a
browser, and play with. They are built one at a time, each focused on a single idea.

## Setup

`nicefabric` is not on PyPI yet, so install it from a sibling checkout:

```sh
git clone https://github.com/0xarn-io/NiceFabric ../NiceFabric
pip install -e ../NiceFabric
pip install -r requirements.txt
```

## Running

Each demo owns a port, so several can run side by side.

```sh
python demos/01_shapes.py    # -> http://localhost:8081
```

Stop with <kbd>Ctrl</kbd>+<kbd>C</kbd>.

## The demos

| #  | Demo                                 | Port | What it shows                                            |
| -- | ------------------------------------ | ---- | -------------------------------------------------------- |
| 01 | [`01_shapes.py`](demos/01_shapes.py) | 8081 | Every `add_*` shape helper, selection, and the live server-side registry |

### 01 — shapes, selection, and the registry

The point is that **Python is authoritative**. A button press adds a shape to a server-side
registry and the browser renders it; dragging that shape in the browser reports its new geometry
back into the same registry. The panel on the right is `canvas.to_dict()` itself, polled twice a
second — not a hand-maintained copy — so what you see is what Python actually holds.

Worth trying:

- Add shapes, drag one, and watch `left`/`top` change in the registry panel.
- Double-click the text object and type — `text` updates in the registry too.
- Rubber-band select several shapes and press <kbd>Delete</kbd>.
- Notice `on_added` stays quiet when you press the buttons: it fires only for free-hand strokes.

## Notes

Two things from the NiceFabric README that these demos are careful about, and that are easy to
get wrong when writing your own:

- **`left`/`top` are the object's centre**, not its top-left corner (changed in Fabric 6).
- **`clear_objects()` is not `clear()`.** `clear()` is NiceGUI's own method for removing child
  elements; on a canvas it silently does nothing to your shapes.
