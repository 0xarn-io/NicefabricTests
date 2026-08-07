"""Demo 01 — shapes, SVG import, locking, and the Python-side registry.

What this demo is for: seeing that **Python is authoritative**. Every shape you add from a
button lands in a server-side registry; every shape you drag, scale or rotate in the browser
reports its new geometry back into that same registry. The panel on the right is
``canvas.to_dict()`` — the real thing, polled twice a second, not a copy kept in sync by hand.

Things worth trying:

* Add a few shapes, then drag one — watch ``left``/``top`` change in the registry panel.
* Double-click the text object and type — ``text`` updates in the registry too.
* Import an SVG. It arrives as a *flat list of ordinary objects* (``Path``, ``Rect``, …), each
  one selectable and editable on its own — not a single opaque image. Drag one piece out of it.
* Select something and hit Lock: the transform handles disappear and dragging stops. The lock
  flags are ordinary props, so you can watch them land in the registry panel.
* Rubber-band select several shapes, then hit Delete (keyboard_delete is on).
* Note that ``on_added`` stays quiet when you press the buttons: it only fires for free-hand
  strokes, which is demo 02's territory.

Run with::

    python demos/01_shapes.py
"""
import asyncio
import json
import random

from nicegui import ui

from nicefabric import FabricCanvas

PORT = 9090
CANVAS_W, CANVAS_H = 800, 450

# Fabric props are camelCase and passed straight through to Fabric.js.
# NOTE: `left`/`top` are the object's CENTRE in Fabric 7, not its top-left corner.
PALETTE = ['#3b82f6', '#ef4444', '#10b981', '#f59e0b', '#8b5cf6', '#ec4899']

# Fabric's own lock flags. They stop the transform — they are not a permission system: the
# object stays selectable (deliberately, or you could never unlock it) and, because keyboard
# delete is handled inside the library, Delete still removes a locked object.
LOCK_PROPS = ('lockMovementX', 'lockMovementY', 'lockRotation', 'lockScalingX', 'lockScalingY')

SVG_MAX_BYTES = 1_000_000   # the same cap add_svg puts on its own source


def fit_to_canvas(objects: list, size: tuple[float, float] | None) -> None:
    """Scale and centre freshly imported shapes — ``add_svg`` imports at *native* size.

    Fabric has already baked each element's accumulated parent transforms into absolute
    ``left``/``top``/``scaleX``/``scaleY`` values and thrown the ``<g>`` structure away, so
    scaling the whole drawing about the document origin is just a multiply on every object's
    centre and scale, then one shared offset to centre it.
    """
    if not size:
        return
    doc_w, doc_h = size
    if not (doc_w and doc_h):
        return
    k = min(CANVAS_W / doc_w, CANVAS_H / doc_h, 1.0)
    off_x, off_y = (CANVAS_W - doc_w * k) / 2, (CANVAS_H - doc_h * k) / 2
    for obj in objects:
        props = obj.props
        obj.update(left=off_x + props.get('left', 0) * k,
                   top=off_y + props.get('top', 0) * k,
                   scaleX=props.get('scaleX', 1) * k,
                   scaleY=props.get('scaleY', 1) * k)


def abbreviated(state: dict) -> dict:
    """Shorten bulky prop values so the JSON panel stays readable.

    An imported ``Path`` carries hundreds of segments and an ``Image`` a base64 ``src``; either
    one buries the interesting props. ``to_dict()`` already handed back a deep copy, so editing
    it here cannot reach the registry.
    """
    for obj in state['objects']:
        for key, value in obj.items():
            if isinstance(value, str) and len(value) > 80:
                obj[key] = f'{value[:60]}… (+{len(value) - 60} chars)'
            elif isinstance(value, list) and len(value) > 8:
                obj[key] = [f'… {len(value)} entries, elided for display']
    return state


@ui.page('/')  # per-visit page: a module-level canvas would be shared by ALL tabs and users
def index() -> None:
    def spot() -> dict:
        """A random-ish centre point that keeps the shape well inside the canvas."""
        return {'left': random.randint(120, 680), 'top': random.randint(90, 360)}

    def color() -> str:
        return random.choice(PALETTE)

    ui.label('01 — shapes, SVG import, locking, and the registry').classes('text-2xl font-bold')
    ui.label('Add or import shapes, then drag them around and watch the Python registry update.') \
        .classes('text-gray-600')

    with ui.row().classes('w-full items-start gap-4 no-wrap'):
        with ui.column().classes('gap-2'):
            # --- one button per add_* helper, so the whole shape API is reachable ------------
            with ui.row().classes('gap-2 flex-wrap'):
                ui.button('Rect', on_click=lambda: canvas.add_rect(
                    width=110, height=75, fill=color(), rx=6, **spot()))
                ui.button('Circle', on_click=lambda: canvas.add_circle(
                    radius=45, fill=color(), **spot()))
                ui.button('Ellipse', on_click=lambda: canvas.add_ellipse(
                    rx=70, ry=40, fill=color(), **spot()))
                ui.button('Line', on_click=lambda: canvas.add_line(
                    0, 0, 140, 90, stroke=color(), strokeWidth=5, **spot()))
                ui.button('Triangle', on_click=lambda: canvas.add_polygon(
                    [{'x': 0, 'y': 70}, {'x': 60, 'y': 0}, {'x': 120, 'y': 70}],
                    fill=color(), **spot()))
                ui.button('Zigzag', on_click=lambda: canvas.add_polyline(
                    [{'x': 0, 'y': 0}, {'x': 40, 'y': 60}, {'x': 80, 'y': 0},
                     {'x': 120, 'y': 60}],
                    stroke=color(), strokeWidth=4, fill='', **spot()))
                ui.button('Heart', on_click=lambda: canvas.add_path(
                    'M 0 0 C 0 -30 40 -30 40 0 C 40 30 0 50 0 70 C 0 50 -40 30 -40 0 '
                    'C -40 -30 0 -30 0 0 z',
                    fill=color(), **spot()))
                ui.button('Text', on_click=lambda: canvas.add_text(
                    'double-click me', fontSize=26, fill=color(), **spot()))

            canvas = FabricCanvas(
                width=CANVAS_W, height=CANVAS_H, background='#f8fafc', keyboard_delete=True,
                on_selection=lambda e: log.push(
                    f'selection {e.args["kind"]}: {len(e.args["ids"])} object(s)'),
                on_modified=lambda e: log.push(
                    f'modified {e.args["id"][:6]} -> '
                    f'left={e.args["props"].get("left", 0):.0f} '
                    f'top={e.args["props"].get("top", 0):.0f} '
                    f'angle={e.args["props"].get("angle", 0):.0f}'),
                on_text_changed=lambda e: log.push(
                    f'text {e.args["id"][:6]} -> {e.args["text"]!r}'),
                on_added=lambda e: log.push(f'added (free-hand) {e.args["id"][:6]}'),
                on_error=lambda e: log.push(f'ERROR {e.args}'))

            def toggle_lock() -> None:
                """Lock or unlock the current selection using Fabric's own lock flags.

                Locking hides the transform controls too, so it is visible without selecting.
                """
                selected = canvas.get_selected()
                if not selected:
                    ui.notify('select something first')
                    return
                lock = not all(o.props.get('lockMovementX') for o in selected)
                for obj in selected:
                    obj.update(hasControls=not lock, **{prop: lock for prop in LOCK_PROPS})
                log.push(f'{"locked" if lock else "unlocked"} {len(selected)} object(s)')

            async def insert_svg(e) -> None:
                """Import an uploaded SVG through Fabric's parser, in the browser.

                ``add_svg`` is a real round trip — Fabric's parser needs the browser's
                ``DOMParser`` — so it is async and needs a connected client. An upload handler
                is exactly the right place; a page-builder body would just time out.
                """
                source = (await e.file.read()).decode('utf-8', errors='replace')
                uploader.reset()  # drop the finished file so the picker is ready for the next
                try:
                    objects = await canvas.add_svg(source)
                except (ValueError, RuntimeError, asyncio.TimeoutError) as err:
                    ui.notify(f'{e.file.name}: {err}', type='negative')
                    log.push(f'SVG import failed: {err}')
                    return
                if not objects:
                    # Fabric cannot tell a rejected document from a genuinely empty one:
                    # both parse to zero objects, so both land here
                    ui.notify(f'{e.file.name}: no shapes found (empty or not valid SVG)',
                              type='warning')
                    log.push(f'SVG import: {e.file.name} produced no objects')
                    return
                size = canvas.last_svg_size
                fit_to_canvas(objects, size)
                shown = f'{size[0]:.0f}x{size[1]:.0f}' if size else 'unknown size'
                log.push(f'imported {e.file.name}: {len(objects)} object(s), document {shown}')

            with ui.row().classes('gap-2 items-center'):
                ui.button('Lock / Unlock', on_click=toggle_lock).props('outline')
                ui.button('Delete selected', on_click=canvas.remove_selected).props('outline')
                # clear_objects(), NOT clear() — clear() is NiceGUI's and silently does nothing here
                ui.button('Clear all', on_click=canvas.clear_objects).props('outline')
                ui.button('Deselect', on_click=canvas.discard_selection).props('outline')
                ui.space()
                ui.label('Delete/Backspace also works').classes('text-xs text-gray-500')

            # no-thumbnails: the uploader's own image preview of an SVG is large enough to
            # push the canvas off screen, and the canvas is already the preview
            uploader = ui.upload(
                label='Insert SVG', auto_upload=True, max_file_size=SVG_MAX_BYTES,
                on_upload=insert_svg,
                on_rejected=lambda: ui.notify(
                    f'SVG too large (max {SVG_MAX_BYTES // 1000} KB)', type='negative')) \
                .props('accept=".svg,image/svg+xml" flat dense no-thumbnails') \
                .classes('w-full')

            log = ui.log(max_lines=14).classes('w-full h-40 text-xs')

        # --- the point of the demo: the server-side registry, live ------------------------
        with ui.column().classes('gap-2 min-w-[380px]'):
            ui.label('canvas.to_dict() — server-side registry').classes('font-bold')
            count = ui.label().classes('text-sm text-gray-600')
            table = ui.column().classes('gap-1 w-full max-h-72 overflow-auto')
            ui.label('raw JSON').classes('font-bold mt-2')
            ui.label('long strings and point lists are elided for display only') \
                .classes('text-xs text-gray-500')
            raw = ui.code('{}', language='json').classes('w-full max-h-64 overflow-auto text-xs')

            def refresh_registry() -> None:
                state = canvas.to_dict()
                objects = state['objects']
                count.text = f'{len(objects)} object(s), fabric version {state["version"]}'
                table.clear()
                with table:
                    for obj in objects:
                        left, top = obj.get('left', 0), obj.get('top', 0)
                        with ui.row().classes('gap-2 items-center text-xs font-mono'):
                            ui.label(obj['type']).classes('w-20 font-bold')
                            ui.label(obj['id'][:6]).classes('w-14 text-gray-500')
                            ui.label(f'({left:.0f}, {top:.0f})').classes('w-24')
                            ui.label('🔒' if obj.get('lockMovementX') else '').classes('w-4')
                            swatch = obj.get('fill') or obj.get('stroke') or 'transparent'
                            if isinstance(swatch, str) and swatch.startswith('#'):
                                ui.html(f'<div style="width:14px;height:14px;border-radius:3px;'
                                        f'background:{swatch};border:1px solid #cbd5e1"></div>')
                raw.content = json.dumps(abbreviated(state), indent=2)

            # Polling keeps the panel honest: it reflects the registry itself, so there is no
            # way for it to drift from what to_dict() would actually return.
            ui.timer(0.5, refresh_registry)


if __name__ in {'__main__', '__mp_main__'}:
    ui.run(port=PORT, title='NiceFabric 01 — shapes', show=False, reload=True)
