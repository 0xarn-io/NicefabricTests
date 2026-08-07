"""Demo 02 — a full-page 2D editor.

Demo 01 is a lab bench: canvas on one side, raw registry on the other. This is the same
library shaped like a *tool*. The canvas fills the whole page and resizes with the window;
creation lives in the left drawer (shapes, free drawing, SVG insert, image by URL, and the
style new shapes are born with); the right drawer edits whatever is selected — position,
scale, angle, opacity, colours, z-order, lock, duplicate — plus canvas-wide settings
(background, zoom). File operations sit in the header: save/load to server-side storage,
JSON export/import as files, SVG and PNG export, clear.

The editor-specific machinery worth reading:

* **Full-page sizing.** The canvas draws at a fixed pixel size — CSS stretching would desync
  hit-testing — so the page measures itself and calls ``canvas.resize()``: once on connect,
  again on window resizes (a debounced listener emits ``nf_resize``), and after drawer
  toggles. ``resize`` writes ``_props``, so the size also survives a reconnect.
* **Placement follows the view.** New shapes land at Fabric's ``getVpCenter()`` — the middle
  of what you currently *see* — not the middle of the surface, so adding while zoomed/panned
  puts the shape in front of you. Before init that call returns ``None`` (dropped, not
  queued) and the surface centre is the fallback.
* **The right drawer is rebuilt, not synced.** Every selection change and every drag rebuilds
  the panel from ``FabricObject.props`` — the registry is the single source of truth, so
  there is no widget state to reconcile and no update-echo loop to guard against.
* Everything else — flattened SVG insert, lock semantics, ``load_json``'s 1 MB ceiling that
  ``save`` does not share — is demo 01's territory and keeps its behaviour here.

Run with::

    python demos/02_editor.py
"""
import asyncio
import base64
import json
import random
import re

from nicegui import app, ui

from nicefabric import FabricCanvas

PORT = 9091
SLOT = 'nicefabric-demo-02'

# Fabric's lock flags stop the transform; the object stays selectable and Delete still works
# (that path is handled inside the library) — same semantics demo 01 establishes.
LOCK_PROPS = ('lockMovementX', 'lockMovementY', 'lockRotation', 'lockScalingX', 'lockScalingY')
TEXT_TYPES = ('Textbox', 'IText', 'Text')

SVG_MAX_BYTES = 500_000   # base64 costs ~4/3 and load_json refuses snapshots over 1 MB


def svg_with_explicit_size(svg: str) -> tuple[str, float, float]:
    """Return the SVG guaranteed to carry width/height, plus that size.

    An ``<svg>`` carrying only a ``viewBox`` has no intrinsic size, and a browser rendering it
    inside an ``<img>`` — which is what a Fabric ``Image`` ultimately is — falls back to
    300x150 and distorts it. Injecting the viewBox's own dimensions makes the result
    predictable and gives us a real size to scale against.

    :raises ValueError: if there is no ``<svg>`` element, or no usable size anywhere in it.
    """
    root = re.search(r'<svg\b[^>]*>', svg, re.IGNORECASE)
    if root is None:
        raise ValueError('no <svg> element found')
    tag = root.group(0)

    def attr(name: str) -> float | None:
        match = re.search(rf'\b{name}\s*=\s*["\']\s*([\d.]+)\s*(?:px)?\s*["\']', tag, re.IGNORECASE)
        return float(match.group(1)) if match else None

    width, height = attr('width'), attr('height')
    if width and height:
        return svg, width, height

    box = re.search(r'\bviewBox\s*=\s*["\']([^"\']+)["\']', tag, re.IGNORECASE)
    if box is None:
        raise ValueError('no usable width/height and no viewBox to fall back on')
    parts = box.group(1).replace(',', ' ').split()
    if len(parts) != 4:
        raise ValueError(f'malformed viewBox: {box.group(1)!r}')
    width, height = float(parts[2]), float(parts[3])
    if not (width and height):
        raise ValueError(f'degenerate viewBox: {box.group(1)!r}')
    return svg.replace(tag, f'{tag[:-1]} width="{width}" height="{height}">', 1), width, height


def elide(props: dict) -> dict:
    """Registry props with bulky values shortened — an SVG data URL would bury the rest."""
    out = {}
    for key, value in props.items():
        if isinstance(value, str) and len(value) > 80:
            out[key] = f'{value[:60]}… (+{len(value) - 60} chars)'
        elif isinstance(value, list) and len(value) > 8:
            out[key] = f'… {len(value)} entries'
        else:
            out[key] = value
    return out


@ui.page('/')  # per-visit page: a module-level canvas would be shared by ALL tabs and users
def index() -> None:
    size = {'w': 1200, 'h': 700}   # updated by fit_canvas; fallback placement centre

    # ------------------------------------------------------------------ canvas & events --
    def on_selection(e) -> None:
        log.push(f'selection {e.args["kind"]}: {len(e.args["ids"])} object(s)')
        refresh_selection()

    def on_modified(e) -> None:
        p = e.args['props']
        log.push(f'modified {e.args["id"][:6]} -> '
                 f'({p.get("left", 0):.0f}, {p.get("top", 0):.0f}) angle={p.get("angle", 0):.0f}')
        refresh_selection()   # geometry inputs must follow the drag

    canvas = FabricCanvas(
        width=size['w'], height=size['h'], background='#ffffff', keyboard_delete=True,
        on_selection=on_selection,
        on_modified=on_modified,
        on_added=lambda e: log.push(f'stroke captured {e.args["id"][:6]}'),
        on_text_changed=lambda e: log.push(f'text {e.args["id"][:6]} -> {e.args["text"]!r}'),
        on_error=lambda e: log.push(f'ERROR {e.args}'))

    def selected() -> list:
        """Current selection as (handle, props) pairs, skipping stale ids."""
        pairs = []
        for obj in canvas.get_selected():
            try:
                pairs.append((obj, obj.props))
            except KeyError:      # deleted while still listed as selected
                continue
        return pairs

    def refresh_json() -> None:
        json_code.content = json.dumps([elide(p) for _, p in selected()], indent=1)

    def put(id_: str, **props) -> None:
        """update_object that tolerates the object having been deleted meanwhile.

        Refreshes the JSON view but NOT the widgets: rebuilding the panel on every keystroke
        would destroy the very slider the user is still dragging.
        """
        try:
            canvas.update_object(id_, **props)
        except KeyError:
            return
        refresh_json()

    # ------------------------------------------------------------------ full-page sizing --
    async def fit_canvas() -> None:
        try:
            dims = await ui.run_javascript(
                "(() => { const p = document.querySelector('.q-page');"
                " return p ? [Math.floor(p.clientWidth), Math.floor(p.clientHeight)] : null; })()",
                timeout=3)
        except TimeoutError:
            return
        if not dims or dims[0] < 100 or dims[1] < 100:
            return
        size['w'], size['h'] = dims
        canvas.resize(*dims)
        status_size.text = f'{dims[0]} x {dims[1]}'

    async def on_ready() -> None:
        await canvas.initialized()
        # one debounced listener; emitEvent is NiceGUI's browser->Python event channel
        await ui.run_javascript(
            "window.addEventListener('resize', () => { clearTimeout(window.__nf_rs);"
            " window.__nf_rs = setTimeout(() => emitEvent('nf_resize'), 200); });",
            timeout=3)
        await fit_canvas()

    ui.timer(0, on_ready, once=True)
    ui.on('nf_resize', fit_canvas)

    def toggle_drawer(drawer) -> None:
        drawer.toggle()
        ui.timer(0.4, fit_canvas, once=True)   # refit once the slide animation is done

    # ------------------------------------------------------------------ shape creation --
    async def view_center() -> tuple[float, float]:
        """Scene point in the middle of what the user currently sees.

        With zoom or pan active the surface centre is off-screen half the time;
        ``getVpCenter`` is Fabric's own answer. Before init the call is dropped (NullResponse
        awaits to None), so fall back to the surface centre.
        """
        try:
            pt = await canvas.run_canvas_method('getVpCenter')
        except (asyncio.TimeoutError, TimeoutError):
            pt = None
        if isinstance(pt, dict) and 'x' in pt and 'y' in pt:
            return float(pt['x']), float(pt['y'])
        return size['w'] / 2, size['h'] / 2

    def style() -> dict:
        s: dict = {'fill': fill_in.value}
        if int(stroke_width_in.value) > 0:
            # strokeUniform keeps the stroke at its set width when the object is scaled.
            # Without it the stroke scales along with the geometry — a resized stroked rect
            # is then mathematically identical to a stretched bitmap of the original, which
            # is exactly what it looks like.
            s |= {'stroke': stroke_in.value, 'strokeWidth': int(stroke_width_in.value),
                  'strokeUniform': True}
        return s

    def line_style() -> dict:
        # a line with no stroke is invisible, so fall back to the fill colour
        w = int(stroke_width_in.value) or 3
        return {'stroke': stroke_in.value if int(stroke_width_in.value) else fill_in.value,
                'strokeWidth': w, 'strokeUniform': True}

    async def add(kind: str) -> None:
        x, y = await view_center()
        x += random.randint(-24, 24)   # jitter so repeated adds don't stack invisibly
        y += random.randint(-24, 24)
        if kind == 'rect':
            canvas.add_rect(left=x, top=y, width=140, height=90, **style())
        elif kind == 'circle':
            canvas.add_circle(left=x, top=y, radius=55, **style())
        elif kind == 'ellipse':
            canvas.add_ellipse(left=x, top=y, rx=80, ry=45, **style())
        elif kind == 'line':
            canvas.add_line(x - 70, y - 45, x + 70, y + 45, **line_style())
        elif kind == 'triangle':
            canvas.add_polygon([{'x': 0, 'y': 80}, {'x': 70, 'y': 0}, {'x': 140, 'y': 80}],
                               left=x, top=y, **style())
        elif kind == 'zigzag':
            canvas.add_polyline([{'x': 0, 'y': 0}, {'x': 40, 'y': 60}, {'x': 80, 'y': 0},
                                 {'x': 120, 'y': 60}], left=x, top=y, fill='', **line_style())
        elif kind == 'heart':
            canvas.add_path('M 0 0 C 0 -30 40 -30 40 0 C 40 30 0 50 0 70 C 0 50 -40 30 -40 0 '
                            'C -40 -30 0 -30 0 0 z', left=x, top=y, **style())
        elif kind == 'text':
            canvas.add_text('double-click to edit', left=x, top=y, fontSize=28,
                            fill=fill_in.value)

    # ------------------------------------------------------------------ file operations --
    def save() -> None:
        app.storage.general[SLOT] = canvas.to_dict()
        kb = len(canvas.to_json().encode('utf-8')) / 1000
        ui.notify(f'saved {len(canvas.to_dict()["objects"])} objects ({kb:.0f} KB)')

    def load() -> None:
        data = app.storage.general.get(SLOT)
        if data is None:
            ui.notify('nothing saved yet')
            return
        try:
            canvas.load_json(data)
        except ValueError as err:
            # save is uncapped, load is not (1 MB / 1000 objects) — demo 01 explains
            ui.notify(f'load failed: {err}', type='negative')
            return
        refresh_selection()
        ui.notify('loaded')

    def export_json() -> None:
        # to_json is uncapped, so this download always succeeds — even for a canvas that
        # load_json/Import would refuse (demo 01's 1 MB trap)
        ui.download(canvas.to_json().encode(), 'canvas.json')

    async def import_json(e) -> None:
        text = (await e.file.read()).decode('utf-8', errors='replace')
        json_uploader.reset()
        try:
            canvas.load_json(text)   # str goes through the same caps as a dict
        except ValueError as err:
            ui.notify(f'{e.file.name}: {err}', type='negative')
            return
        json_dialog.close()
        refresh_selection()
        ui.notify(f'imported {e.file.name}')

    async def export_svg() -> None:
        try:
            svg = await canvas.to_svg()
        except (RuntimeError, asyncio.TimeoutError) as err:
            ui.notify(f'SVG export failed: {err}', type='negative')
            return
        # a download, never re-inlined into the page: exported SVG embeds user-controlled
        # URLs and is a script vector when rendered as a document
        ui.download(svg.encode(), 'canvas.svg')

    async def export_png() -> None:
        try:
            data_url = await canvas.to_data_url()
        except (RuntimeError, asyncio.TimeoutError) as err:
            ui.notify(f'PNG export failed: {err}', type='negative')
            return
        ui.download(base64.b64decode(data_url.split(',', 1)[1]), 'canvas.png')

    def clear_all() -> None:
        canvas.clear_objects()
        refresh_selection()

    async def insert_svg(e) -> None:
        """Insert an uploaded SVG as ONE flattened Image — demo 01 measured the why.

        (A ``Group`` renders but is silently dropped by ``load_json``; a
        ``data:image/svg+xml`` Image survives the round trip.)
        """
        source = (await e.file.read()).decode('utf-8', errors='replace')
        uploader.reset()
        try:
            svg, width, height = svg_with_explicit_size(source)
        except ValueError as err:
            ui.notify(f'{e.file.name}: {err}', type='negative')
            return
        x, y = await view_center()
        scale = min(size['w'] / width, size['h'] / height, 1.0)
        canvas.add_image('data:image/svg+xml;base64,' + base64.b64encode(svg.encode()).decode(),
                         left=x, top=y, scaleX=scale, scaleY=scale)
        log.push(f'inserted {e.file.name} as one Image ({width:.0f}x{height:.0f} @ {scale:.2f})')

    async def insert_image_url() -> None:
        url = (url_in.value or '').strip()
        # add_image passes src through unvalidated (its README says so) — so validate here,
        # mirroring load_json's scheme allow-list
        if not url.startswith(('https://', 'http://', 'data:image/')):
            ui.notify('need an https://, http:// or data:image/ URL', type='negative')
            return
        x, y = await view_center()
        canvas.add_image(url, left=x, top=y)
        url_in.value = ''

    # ------------------------------------------------------------------ selection panel --
    def lock_toggle() -> None:
        pairs = selected()
        if not pairs:
            return
        lock = not all(p.get('lockMovementX') for _, p in pairs)
        for obj, _ in pairs:
            put(obj.id, hasControls=not lock, **{prop: lock for prop in LOCK_PROPS})
        log.push(f'{"locked" if lock else "unlocked"} {len(pairs)} object(s)')
        refresh_selection()

    def duplicate() -> None:
        for _, props in selected():
            copy = dict(props)
            copy.pop('id', None)
            type_ = copy.pop('type')
            copy['left'] = copy.get('left', 0) + 24
            copy['top'] = copy.get('top', 0) + 24
            canvas.add_object(type_, **copy)

    def delete_selected() -> None:
        canvas.remove_selected()
        refresh_selection()

    def refresh_selection() -> None:
        """Rebuild the panel from the registry — no widget state to keep in sync."""
        pairs = selected()
        sel_box.clear()
        refresh_json()
        with sel_box:
            if not pairs:
                ui.label('nothing selected').classes('text-xs text-gray-500')
                return

            if len(pairs) == 1:
                obj, props = pairs[0]
                ui.label(f'{props["type"]} · {obj.id[:8]}').classes('text-xs font-mono text-gray-500')
                with ui.grid(columns=2).classes('w-full gap-1 items-center'):
                    ui.number('x', value=round(props.get('left', 0), 1), format='%.0f',
                              on_change=lambda e: e.value is not None and put(obj.id, left=e.value)) \
                        .props('dense').classes('nf-x')
                    ui.number('y', value=round(props.get('top', 0), 1), format='%.0f',
                              on_change=lambda e: e.value is not None and put(obj.id, top=e.value)) \
                        .props('dense').classes('nf-y')
                    # clamped: scale 0 collapses the transform matrix and the object vanishes
                    # for good, so the panel refuses to go below 0.05
                    ui.number('scale x', value=round(props.get('scaleX', 1), 2), step=0.1,
                              format='%.2f',
                              on_change=lambda e: e.value is not None
                              and put(obj.id, scaleX=max(float(e.value), 0.05))) \
                        .props('dense').classes('nf-scalex')
                    ui.number('scale y', value=round(props.get('scaleY', 1), 2), step=0.1,
                              format='%.2f',
                              on_change=lambda e: e.value is not None
                              and put(obj.id, scaleY=max(float(e.value), 0.05))) \
                        .props('dense').classes('nf-scaley')
                ui.label('angle').classes('text-xs text-gray-600 mt-1')
                ui.slider(min=0, max=359, value=round(props.get('angle', 0)) % 360,
                          on_change=lambda e: put(obj.id, angle=e.value)) \
                    .props('label').classes('nf-angle')
                ui.label('opacity').classes('text-xs text-gray-600')
                ui.slider(min=0, max=1, step=0.05, value=props.get('opacity', 1),
                          on_change=lambda e: put(obj.id, opacity=e.value)) \
                    .props('label').classes('nf-opacity')
                with ui.row().classes('w-full gap-2 no-wrap'):
                    ui.color_input('fill', value=props.get('fill') or '',
                                   on_change=lambda e: put(obj.id, fill=e.value)) \
                        .props('dense').classes('nf-fill grow')
                    ui.color_input('stroke', value=props.get('stroke') or '',
                                   on_change=lambda e: put(obj.id, stroke=e.value)) \
                        .props('dense').classes('nf-stroke grow')
                ui.label('stroke width').classes('text-xs text-gray-600')
                # strokeUniform rides along so a stroke added here stays put under scaling
                ui.slider(min=0, max=20, value=int(props.get('strokeWidth') or 0),
                          on_change=lambda e: put(obj.id, strokeWidth=e.value,
                                                  strokeUniform=True)) \
                    .props('label').classes('nf-strokewidth')
                if props['type'] in TEXT_TYPES:
                    ui.label('font size').classes('text-xs text-gray-600')
                    ui.slider(min=8, max=96, value=int(props.get('fontSize') or 28),
                              on_change=lambda e: put(obj.id, fontSize=e.value)) \
                        .props('label').classes('nf-fontsize')
            else:
                ui.label(f'{len(pairs)} objects selected').classes('text-sm font-bold')

            locked = all(p.get('lockMovementX') for _, p in pairs)
            with ui.grid(columns=2).classes('w-full gap-1 mt-2'):
                ui.button('Unlock' if locked else 'Lock', icon='lock_open' if locked else 'lock',
                          on_click=lock_toggle).props('dense outline no-caps').classes('nf-lock')
                ui.button('Duplicate', icon='content_copy', on_click=duplicate) \
                    .props('dense outline no-caps')
                ui.button('To front', icon='flip_to_front',
                          on_click=lambda: [canvas.bring_to_front(o) for o, _ in selected()]) \
                    .props('dense outline no-caps')
                ui.button('To back', icon='flip_to_back',
                          on_click=lambda: [canvas.send_to_back(o) for o, _ in selected()]) \
                    .props('dense outline no-caps')
                ui.button('Delete', icon='delete', on_click=delete_selected) \
                    .props('dense outline no-caps color=red')
                ui.button('Deselect', icon='deselect', on_click=canvas.discard_selection) \
                    .props('dense outline no-caps')

    # ================================================================== layout ==========
    ui.query('.nicegui-content').classes('p-0 gap-0')

    with ui.dialog() as json_dialog, ui.card().classes('w-96'):
        ui.label('Import canvas JSON').classes('font-bold')
        ui.label('Replaces everything on the canvas via load_json — its caps apply: '
                 '1 MB, 1000 objects, allow-listed types.').classes('text-xs text-gray-500')
        json_uploader = ui.upload(label='canvas.json', auto_upload=True,
                                  max_file_size=2_000_000, on_upload=import_json,
                                  on_rejected=lambda: ui.notify('file too large',
                                                                type='negative')) \
            .props('accept=".json,application/json" flat dense no-thumbnails').classes('w-full')

    with ui.header().classes('bg-slate-900 items-center gap-2 px-3 py-1'):
        ui.button(icon='menu', on_click=lambda: toggle_drawer(left)) \
            .props('flat dense color=white')
        ui.label('NiceFabric editor').classes('text-base font-bold')
        ui.space()
        with ui.button('File', icon='folder').props('flat dense no-caps color=white'):
            with ui.menu():
                ui.menu_item('Save', save)
                ui.menu_item('Load', load)
                ui.separator()
                ui.menu_item('Export JSON', export_json)
                ui.menu_item('Import JSON…', json_dialog.open)
                ui.separator()
                ui.menu_item('Export SVG', export_svg)
                ui.menu_item('Export PNG', export_png)
                ui.separator()
                ui.menu_item('Clear canvas', clear_all)
        ui.button(icon='tune', on_click=lambda: toggle_drawer(right)) \
            .props('flat dense color=white')

    with ui.left_drawer(value=True, bordered=True).props('width=230').classes('p-3') as left:
        ui.label('Tool').classes('text-xs font-bold text-gray-500 uppercase')

        def set_tool(t: str) -> None:
            brush_box.set_visibility(t == 'draw')
            if t == 'draw':
                canvas.enable_drawing(brush_color.value, int(brush_width.value))
            else:
                canvas.disable_drawing()

        ui.toggle({'select': 'Select', 'draw': 'Draw'}, value='select',
                  on_change=lambda e: set_tool(e.value)).props('dense no-caps spread') \
            .classes('w-full')

        with ui.column().classes('w-full gap-1') as brush_box:
            def apply_brush() -> None:
                if canvas.draw_mode:   # enable_drawing is what builds the brush — re-call it
                    canvas.enable_drawing(brush_color.value, int(brush_width.value))
            brush_color = ui.color_input('brush', value='#111827', on_change=apply_brush) \
                .props('dense').classes('w-full')
            brush_width = ui.slider(min=1, max=30, value=3, on_change=apply_brush) \
                .props('label')
        brush_box.set_visibility(False)

        ui.separator().classes('my-2')
        ui.label('Shapes').classes('text-xs font-bold text-gray-500 uppercase')
        with ui.grid(columns=2).classes('w-full gap-1'):
            for kind, icon in [('rect', 'crop_square'), ('circle', 'circle'),
                               ('ellipse', 'vignette'), ('line', 'show_chart'),
                               ('triangle', 'change_history'), ('zigzag', 'timeline'),
                               ('heart', 'favorite'), ('text', 'title')]:
                ui.button(kind, icon=icon,
                          on_click=lambda _, k=kind: add(k)) \
                    .props('dense outline no-caps').classes(f'nf-add-{kind}')

        ui.label('New-shape style').classes('text-xs font-bold text-gray-500 uppercase mt-2')
        fill_in = ui.color_input('fill', value='#3b82f6').props('dense').classes('w-full')
        stroke_in = ui.color_input('stroke', value='#1e293b').props('dense').classes('w-full')
        ui.label('stroke width').classes('text-xs text-gray-600')
        stroke_width_in = ui.slider(min=0, max=12, value=0).props('label') \
            .classes('nf-new-strokewidth')

        ui.separator().classes('my-2')
        ui.label('Insert').classes('text-xs font-bold text-gray-500 uppercase')
        uploader = ui.upload(label='SVG file', auto_upload=True, max_file_size=SVG_MAX_BYTES,
                             on_upload=insert_svg,
                             on_rejected=lambda: ui.notify(
                                 f'SVG too large (max {SVG_MAX_BYTES // 1000} KB)',
                                 type='negative')) \
            .props('accept=".svg,image/svg+xml" flat dense no-thumbnails').classes('w-full')
        with ui.row().classes('w-full gap-1 no-wrap items-center'):
            url_in = ui.input(placeholder='image URL…').props('dense').classes('grow nf-url')
            ui.button(icon='add_photo_alternate', on_click=insert_image_url).props('dense flat')

    with ui.right_drawer(value=True, bordered=True).props('width=270').classes('p-3') as right:
        ui.label('Canvas').classes('text-xs font-bold text-gray-500 uppercase')
        ui.color_input('background', value='#ffffff',
                       on_change=lambda e: canvas.set_background(e.value)) \
            .props('dense').classes('w-full nf-background')
        ui.label('zoom').classes('text-xs text-gray-600')

        def set_zoom(z: float) -> None:
            canvas.set_zoom(z)
            status_zoom.text = f'{z:.2f}x'

        zoom_in = ui.slider(min=0.25, max=3, step=0.05, value=1,
                            on_change=lambda e: set_zoom(e.value)).props('label')

        def reset_view() -> None:
            zoom_in.value = 1          # triggers set_zoom(1)
            canvas.absolute_pan(0, 0)

        ui.button('Reset view', icon='center_focus_strong', on_click=reset_view) \
            .props('dense outline no-caps').classes('w-full')

        ui.separator().classes('my-2')
        ui.label('Selection').classes('text-xs font-bold text-gray-500 uppercase')
        sel_box = ui.column().classes('w-full gap-1')

        with ui.expansion('Selection JSON').classes('w-full mt-2'):
            json_code = ui.code('[]', language='json') \
                .classes('w-full max-h-64 overflow-auto text-xs nf-json')
        with ui.expansion('Event log').classes('w-full'):
            log = ui.log(max_lines=12).classes('w-full h-36 text-xs nf-log')

    # the canvas itself was created at the top of this builder, so it is already the page
    # content; header, drawers and footer are layout elements and do not displace it

    with ui.footer().classes('bg-slate-900 text-white items-center gap-4 px-3 py-1 text-xs'):
        status_objects = ui.label('0 objects').classes('nf-objects font-mono')
        status_size = ui.label().classes('font-mono')
        status_zoom = ui.label('1.00x').classes('font-mono')
        ui.space()
        ui.label('double-click text to edit · Delete removes selection').classes('text-gray-400')

    def refresh_status() -> None:
        status_objects.text = f'{len(canvas.to_dict()["objects"])} objects'

    ui.timer(1.0, refresh_status)
    refresh_selection()   # initial "nothing selected"


if __name__ in {'__main__', '__mp_main__'}:
    ui.run(port=PORT, title='NiceFabric 02 — editor', show=False, reload=True)
