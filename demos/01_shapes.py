"""Demo 01 — shapes, selection, and the Python-side registry.

What this demo is for: seeing that **Python is authoritative**. Every shape you add from a
button lands in a server-side registry; every shape you drag, scale or rotate in the browser
reports its new geometry back into that same registry. The panel on the right is
``canvas.to_dict()`` — the real thing, polled twice a second, not a copy kept in sync by hand.

Things worth trying:

* Add a few shapes, then drag one — watch ``left``/``top`` change in the registry panel.
* Double-click the text object and type — ``text`` updates in the registry too.
* Rubber-band select several shapes, then hit Delete (keyboard_delete is on).
* Note that ``on_added`` stays quiet when you press the buttons: it only fires for free-hand
  strokes, which is demo 02's territory.

Run with::

    python demos/01_shapes.py
"""
import json
import random

from nicegui import ui

from nicefabric import FabricCanvas

PORT = 8081

# Fabric props are camelCase and passed straight through to Fabric.js.
# NOTE: `left`/`top` are the object's CENTRE in Fabric 7, not its top-left corner.
PALETTE = ['#3b82f6', '#ef4444', '#10b981', '#f59e0b', '#8b5cf6', '#ec4899']


@ui.page('/')  # per-visit page: a module-level canvas would be shared by ALL tabs and users
def index() -> None:
    def spot() -> dict:
        """A random-ish centre point that keeps the shape well inside the canvas."""
        return {'left': random.randint(120, 680), 'top': random.randint(90, 360)}

    def color() -> str:
        return random.choice(PALETTE)

    ui.label('01 — shapes, selection, and the registry').classes('text-2xl font-bold')
    ui.label('Add shapes, then drag them around and watch the Python registry update.') \
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
                width=800, height=450, background='#f8fafc', keyboard_delete=True,
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

            with ui.row().classes('gap-2 items-center'):
                # clear_objects(), NOT clear() — clear() is NiceGUI's and silently does nothing here
                ui.button('Delete selected', on_click=canvas.remove_selected).props('outline')
                ui.button('Clear all', on_click=canvas.clear_objects).props('outline')
                ui.button('Deselect', on_click=canvas.discard_selection).props('outline')
                ui.space()
                ui.label('Delete/Backspace also works').classes('text-xs text-gray-500')

            log = ui.log(max_lines=14).classes('w-full h-40 text-xs')

        # --- the point of the demo: the server-side registry, live ------------------------
        with ui.column().classes('gap-2 min-w-[380px]'):
            ui.label('canvas.to_dict() — server-side registry').classes('font-bold')
            count = ui.label().classes('text-sm text-gray-600')
            table = ui.column().classes('gap-1 w-full')
            ui.label('raw JSON').classes('font-bold mt-2')
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
                            swatch = obj.get('fill') or obj.get('stroke') or 'transparent'
                            if isinstance(swatch, str) and swatch.startswith('#'):
                                ui.html(f'<div style="width:14px;height:14px;border-radius:3px;'
                                        f'background:{swatch};border:1px solid #cbd5e1"></div>')
                raw.content = json.dumps(state, indent=2)

            # Polling keeps the panel honest: it reflects the registry itself, so there is no
            # way for it to drift from what to_dict() would actually return.
            ui.timer(0.5, refresh_registry)


if __name__ in {'__main__', '__mp_main__'}:
    ui.run(port=PORT, title='NiceFabric 01 — shapes', show=False, reload=False)
