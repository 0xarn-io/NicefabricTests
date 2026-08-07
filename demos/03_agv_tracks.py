"""Demo 03 — an AGV layout editor: drag path elements from a palette, snap them to the grid.

Demo 02 is a general drawing tool. This one is a *domain* editor built on the same element,
drawn the way commissioning software draws a layout rather than the way a game draws a road:
a dark CAD field with a metric grid, thin path centrelines inside a translucent vehicle
envelope, node markers where segments meet, and compact geometric station symbols. The right
dock reports track length, connections and every port still open.

How the pieces work:

* **One object per element.** Each element is a generated SVG rendered into a single Fabric
  ``Image`` via a ``data:image/svg+xml`` URL — the flattened form demo 01 measured as the only
  single-object representation that survives ``to_dict()`` → ``load_json()``.
* **The element kind rides on the object.** ``add_image(url, kind='curve')`` stores a custom
  prop, and ``load_json`` deep-copies unknown props, so ``kind`` survives a save/load round
  trip. That matters because ``load_json`` **re-ids every object**, so a side table keyed by
  object id would be destroyed by the first load — the metadata has to travel *on* the piece.
* **Ports are derived, never stored.** ``kind`` gives the base ports (compass directions) and
  Fabric's own ``angle`` gives the rotation; the connectivity pass rotates one by the other.
  Nothing can drift out of sync because there is only ever one copy of each fact.
* **Snapping is server-side.** Dropping emits the pointer position, and ``on_modified``
  reports the position after a drag; both get rounded to the cell grid and to 90°, then
  written back with ``update_object``. The browser is never the authority on where a piece is.
* **Elements cannot be scaled.** ``lockScalingX``/``lockScalingY`` are set on every placed
  piece: path geometry is fixed, and a stretched curve would no longer meet its neighbours.

Run with::

    python demos/03_agv_tracks.py
"""
import asyncio
import base64

from nicegui import app, ui

from nicefabric import FabricCanvas

PORT = 9092
TILE = 80            # px per grid cell
CELL_M = 1.0         # metres per grid cell — the layout is dimensioned, not just pixels
SLOT = 'nicefabric-demo-03'

# CAD palette: a cool dark field, one accent for path geometry, muted accents per station.
FIELD = '#0b1220'
GRID_MINOR = '#16233a'
GRID_MAJOR = '#22314d'
ENVELOPE = '#22d3ee'   # vehicle envelope, drawn translucent under the centreline
CENTRE = '#7dd3fc'     # path centreline
NODE = '#94a3b8'       # connection markers at cell edges
ACCENT = {'charge': '#34d399', 'load': '#fbbf24', 'unload': '#60a5fa', 'end': '#f87171'}

# Compass order matters: rotating a piece by 90° clockwise advances every port by one step.
DIRS = 'NESW'
DELTA = {'N': (0, -1), 'E': (1, 0), 'S': (0, 1), 'W': (-1, 0)}
OPPOSITE = {'N': 'S', 'S': 'N', 'E': 'W', 'W': 'E'}

_ARC = 'M 0 40 A 40 40 0 0 1 40 80'   # west edge to south edge, one quarter turn


def _svg(body: str, size: int = TILE) -> str:
    """Wrap element artwork at any pixel size; the viewBox keeps geometry in tile units."""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
            f'viewBox="0 0 {TILE} {TILE}">{body}</svg>')


def _path(d: str, closed_ports: str) -> str:
    """Centreline inside a translucent vehicle envelope, plus a node dot at each port.

    Half of every node dot falls outside the tile and is clipped; the neighbouring tile draws
    the other half, so a connected joint renders as one whole dot and an open end as a half.
    """
    marks = ''
    for port in closed_ports:
        cx, cy = {'N': (40, 0), 'E': (80, 40), 'S': (40, 80), 'W': (0, 40)}[port]
        marks += f'<circle cx="{cx}" cy="{cy}" r="3.5" fill="{NODE}"/>'
    return (f'<path d="{d}" fill="none" stroke="{ENVELOPE}" stroke-width="26" '
            f'stroke-opacity="0.11" stroke-linecap="butt"/>'
            f'<path d="{d}" fill="none" stroke="{CENTRE}" stroke-width="1.8"/>{marks}')


def _station(kind: str, glyph: str) -> str:
    """A short stub running in from the west edge, ending in a bounded station symbol."""
    acc = ACCENT[kind]
    return (f'<path d="M 0 40 L 40 40" fill="none" stroke="{ENVELOPE}" stroke-width="26" '
            f'stroke-opacity="0.11"/>'
            f'<path d="M 0 40 L 40 40" fill="none" stroke="{CENTRE}" stroke-width="1.8"/>'
            f'<circle cx="0" cy="40" r="3.5" fill="{NODE}"/>'
            f'<rect x="38" y="23" width="35" height="34" rx="3" fill="{acc}" '
            f'fill-opacity="0.10" stroke="{acc}" stroke-opacity="0.55" stroke-width="1"/>'
            f'{glyph}')


PIECES: dict[str, dict] = {
    'straight': {
        'label': 'Straight', 'ports': 'WE', 'length': 1.0,
        'body': _path('M 0 40 L 80 40', 'WE')},
    'curve': {
        'label': 'Curve 90°', 'ports': 'WS', 'length': 0.7854,   # quarter arc, r = 0.5 m
        'body': _path(_ARC, 'WS')},
    'fork': {
        'label': 'Fork', 'ports': 'WES', 'length': 1.5,
        'body': _path('M 0 40 L 80 40 M 40 40 L 40 80', 'WES')
                + f'<circle cx="40" cy="40" r="2.6" fill="{CENTRE}"/>'},
    'cross': {
        'label': 'Crossing', 'ports': 'NESW', 'length': 2.0,
        'body': _path('M 0 40 L 80 40 M 40 0 L 40 80', 'NESW')
                + f'<circle cx="40" cy="40" r="2.6" fill="{CENTRE}"/>'},
    # Station symbols are read relative to the stub, because the whole tile rotates with the
    # piece — a glyph drawn "upright" would point somewhere arbitrary once placed.
    'charge': {
        'label': 'Charger', 'ports': 'W', 'length': 0.5,
        'body': _station('charge',
                         f'<rect x="44" y="31" width="21" height="18" rx="2" fill="none" '
                         f'stroke="{ACCENT["charge"]}" stroke-width="1.8"/>'
                         f'<rect x="65" y="36" width="4" height="8" rx="1" '
                         f'fill="{ACCENT["charge"]}"/>'
                         f'<rect x="47" y="34" width="7" height="12" '
                         f'fill="{ACCENT["charge"]}"/>')},
    'load': {
        'label': 'Load', 'ports': 'W', 'length': 0.5,
        'body': _station('load',
                         f'<polygon points="43,40 59,31 59,49" fill="none" '
                         f'stroke="{ACCENT["load"]}" stroke-width="1.8" '
                         f'stroke-linejoin="round"/>'
                         f'<rect x="62" y="31" width="8" height="18" rx="1" fill="none" '
                         f'stroke="{ACCENT["load"]}" stroke-width="1.8"/>')},
    'unload': {
        'label': 'Unload', 'ports': 'W', 'length': 0.5,
        'body': _station('unload',
                         f'<polygon points="69,40 53,31 53,49" fill="none" '
                         f'stroke="{ACCENT["unload"]}" stroke-width="1.8" '
                         f'stroke-linejoin="round"/>'
                         f'<rect x="42" y="31" width="8" height="18" rx="1" fill="none" '
                         f'stroke="{ACCENT["unload"]}" stroke-width="1.8"/>')},
    'end': {
        'label': 'End stop', 'ports': 'W', 'length': 0.5,
        'body': _station('end',
                         f'<line x1="46" y1="27" x2="46" y2="53" '
                         f'stroke="{ACCENT["end"]}" stroke-width="3"/>'
                         f'<line x1="50" y1="30" x2="58" y2="38" '
                         f'stroke="{ACCENT["end"]}" stroke-width="1.4"/>'
                         f'<line x1="50" y1="42" x2="58" y2="50" '
                         f'stroke="{ACCENT["end"]}" stroke-width="1.4"/>')},
}

for _piece in PIECES.values():
    _piece['svg'] = _svg(_piece['body'])
    _piece['thumb'] = _svg(_piece['body'], 44)
    _piece['url'] = ('data:image/svg+xml;base64,'
                     + base64.b64encode(_piece['svg'].encode()).decode())

# Track elements are fixed geometry: a stretched curve would not meet its neighbours, and the
# selection styling is part of looking like an engineering tool rather than a drawing app.
PLACED_PROPS = {'lockScalingX': True, 'lockScalingY': True,
                'borderColor': '#22d3ee', 'cornerColor': '#22d3ee',
                'cornerStrokeColor': '#0e7490', 'transparentCorners': False,
                'cornerSize': 7, 'padding': 2}


def ports_of(kind: str, quarter: int) -> str:
    """Ports of ``kind`` after ``quarter`` 90° clockwise turns.

    Rotating clockwise moves N->E->S->W, which is one step along ``DIRS`` — so the whole
    rotation is an index shift, and no per-piece rotation table is needed.
    """
    return ''.join(DIRS[(DIRS.index(d) + quarter) % 4] for d in PIECES[kind]['ports'])


def cell_of(left: float, top: float) -> tuple[int, int]:
    """Grid cell holding an element centred at ``left``/``top`` (Fabric centres objects)."""
    return round((left - TILE / 2) / TILE), round((top - TILE / 2) / TILE)


def centre_of(cell: tuple[int, int]) -> tuple[float, float]:
    return cell[0] * TILE + TILE / 2, cell[1] * TILE + TILE / 2


@ui.page('/')  # per-visit page: a module-level canvas would be shared by ALL tabs and users
def index() -> None:
    ui.dark_mode(True)
    size = {'w': 1200, 'h': 700}

    def on_modified(e) -> None:
        # the browser moved an element; the grid, not the pointer, decides where it lands
        snap(e.args['id'], e.args['props'])
        refresh()

    canvas = FabricCanvas(
        width=size['w'], height=size['h'], background='', keyboard_delete=True,
        on_selection=lambda e: refresh(),
        on_modified=on_modified,
        on_error=lambda e: log.push(f'ERROR {e.args}')) \
        .classes('nf-canvas') \
        .style(f'background-color: {FIELD};'
               # minor lines every cell, major every five — the usual CAD reading aid
               f'background-image:'
               f'linear-gradient(to right, {GRID_MAJOR} 1px, transparent 1px),'
               f'linear-gradient(to bottom, {GRID_MAJOR} 1px, transparent 1px),'
               f'linear-gradient(to right, {GRID_MINOR} 1px, transparent 1px),'
               f'linear-gradient(to bottom, {GRID_MINOR} 1px, transparent 1px);'
               f'background-size: {TILE * 5}px {TILE * 5}px, {TILE * 5}px {TILE * 5}px,'
               f'{TILE}px {TILE}px, {TILE}px {TILE}px')

    def pieces() -> list[dict]:
        return [o for o in canvas.to_dict()['objects'] if o.get('kind') in PIECES]

    def snap(id_: str, props: dict) -> None:
        cell = cell_of(props.get('left', 0), props.get('top', 0))
        left, top = centre_of(cell)
        quarter = round((props.get('angle', 0) % 360) / 90) % 4
        try:
            canvas.update_object(id_, left=left, top=top, angle=quarter * 90)
        except KeyError:
            pass

    # ------------------------------------------------------------------ placement --------
    def place(kind: str, cell: tuple[int, int], quarter: int = 0) -> None:
        for existing in pieces():                     # one element per cell: replace, not stack
            if cell_of(existing['left'], existing['top']) == cell:
                canvas.remove_object(existing['id'])
        left, top = centre_of(cell)
        canvas.add_image(PIECES[kind]['url'], left=left, top=top, angle=quarter * 90,
                         kind=kind, **PLACED_PROPS)   # custom prop survives load_json
        log.push(f'place {kind:<9} cell {cell[0]:>3},{cell[1]:<3} {quarter * 90:>3}°')

    def on_drop(e) -> None:
        kind = e.args.get('kind')
        if kind not in PIECES:
            return
        cell = (int(e.args['x'] // TILE), int(e.args['y'] // TILE))
        place(kind, cell, quarter=int(palette_rotation.value) // 90)
        refresh()

    ui.on('nf_drop', on_drop)

    async def wire_dnd() -> None:
        await canvas.initialized()
        # HTML5 drag and drop: palette rows are the drag sources, the canvas wrapper is the
        # drop target. emitEvent is NiceGUI's browser->Python channel. The cursor readout is
        # updated straight from JS — a coordinate display does not need a server round trip.
        await ui.run_javascript(f"""
            document.addEventListener('dragstart', (ev) => {{
                const el = ev.target.closest('.nf-piece');
                if (el) ev.dataTransfer.setData('text/plain', el.dataset.kind);
            }});
            const wrap = document.querySelector('.nf-canvas');
            wrap.addEventListener('dragover', (ev) => ev.preventDefault());
            wrap.addEventListener('drop', (ev) => {{
                ev.preventDefault();
                const r = wrap.getBoundingClientRect();
                emitEvent('nf_drop', {{kind: ev.dataTransfer.getData('text/plain'),
                                       x: ev.clientX - r.left, y: ev.clientY - r.top}});
            }});
            wrap.addEventListener('mousemove', (ev) => {{
                const r = wrap.getBoundingClientRect();
                const el = document.querySelector('.nf-cursor');
                if (!el) return;
                const x = ((ev.clientX - r.left) / {TILE} * {CELL_M}).toFixed(2);
                const y = ((ev.clientY - r.top) / {TILE} * {CELL_M}).toFixed(2);
                el.textContent = 'X ' + x + '  Y ' + y + ' m';
            }});
        """, timeout=5)
        await fit_canvas()

    async def fit_canvas() -> None:
        try:
            dims = await ui.run_javascript(
                "(() => { const p = document.querySelector('.q-page');"
                " return p ? [p.clientWidth, p.clientHeight] : null; })()", timeout=3)
        except TimeoutError:
            return
        if not dims or dims[0] < TILE * 2 or dims[1] < TILE * 2:
            return
        # whole cells only, so the CAD grid never ends on a half cell
        size['w'], size['h'] = (dims[0] // TILE) * TILE, (dims[1] // TILE) * TILE
        canvas.resize(size['w'], size['h'])
        status_extent.text = (f'{size["w"] // TILE * CELL_M:.0f} x '
                              f'{size["h"] // TILE * CELL_M:.0f} m')

    ui.timer(0, wire_dnd, once=True)
    ui.on('nf_resize', fit_canvas)

    # ------------------------------------------------------------------ connectivity ------
    def analyse() -> dict:
        """Pair up ports with their neighbours and total the track length.

        Ports are recomputed from ``kind`` + ``angle`` every time rather than cached, so an
        element that was dragged, rotated or reloaded cannot disagree with its own metadata.
        """
        grid: dict[tuple[int, int], dict] = {}
        stacked = 0
        for entry in pieces():
            cell = cell_of(entry['left'], entry['top'])
            if cell in grid:
                stacked += 1
            quarter = round((entry.get('angle', 0) % 360) / 90) % 4
            grid[cell] = {'kind': entry['kind'], 'ports': ports_of(entry['kind'], quarter),
                          'id': entry['id']}

        links, open_ends = 0, []
        for cell, piece in grid.items():
            for port in piece['ports']:
                dx, dy = DELTA[port]
                neighbour = grid.get((cell[0] + dx, cell[1] + dy))
                if neighbour and OPPOSITE[port] in neighbour['ports']:
                    links += 1
                else:
                    open_ends.append((cell, port))
        stations = sum(1 for p in grid.values() if p['kind'] in ACCENT)
        length = sum(PIECES[p['kind']]['length'] for p in grid.values()) * CELL_M
        return {'grid': grid, 'links': links // 2,   # counted once from each side
                'open': open_ends, 'stacked': stacked, 'stations': stations, 'length': length}

    # ------------------------------------------------------------------ file ops ----------
    def save() -> None:
        app.storage.general[SLOT] = canvas.to_dict()
        ui.notify(f'saved {len(pieces())} elements')

    def load() -> None:
        data = app.storage.general.get(SLOT)
        if data is None:
            ui.notify('nothing saved yet')
            return
        try:
            canvas.load_json(data)
        except ValueError as err:
            ui.notify(f'load failed: {err}', type='negative')
            return
        refresh()
        ui.notify(f'loaded {len(pieces())} elements')

    def export_json() -> None:
        ui.download(canvas.to_json().encode(), 'agv-layout.json')

    async def import_json(e) -> None:
        text = (await e.file.read()).decode('utf-8', errors='replace')
        json_uploader.reset()
        try:
            canvas.load_json(text)
        except ValueError as err:
            ui.notify(f'{e.file.name}: {err}', type='negative')
            return
        json_dialog.close()
        refresh()
        ui.notify(f'imported {len(pieces())} elements')

    async def export_svg() -> None:
        try:
            svg = await canvas.to_svg()
        except (RuntimeError, asyncio.TimeoutError) as err:
            ui.notify(f'SVG export failed: {err}', type='negative')
            return
        ui.download(svg.encode(), 'agv-layout.svg')   # a download, never re-inlined

    async def export_png() -> None:
        try:
            data_url = await canvas.to_data_url()
        except (RuntimeError, asyncio.TimeoutError) as err:
            ui.notify(f'PNG export failed: {err}', type='negative')
            return
        ui.download(base64.b64decode(data_url.split(',', 1)[1]), 'agv-layout.png')

    def clear_all() -> None:
        canvas.clear_objects()
        refresh()

    # ------------------------------------------------------------------ selection ---------
    def rotate(step: int) -> None:
        for obj in canvas.get_selected():
            try:
                props = obj.props
            except KeyError:
                continue
            quarter = (round((props.get('angle', 0) % 360) / 90) + step) % 4
            obj.update(angle=quarter * 90)
        refresh()

    def delete_selected() -> None:
        canvas.remove_selected()
        refresh()

    def on_key(e) -> None:
        if e.action.keydown and not e.action.repeat and str(e.key).lower() == 'r':
            rotate(1)

    ui.keyboard(on_key=on_key, ignore=['input', 'select', 'textarea'])

    # ------------------------------------------------------------------ docks -------------
    def stat(label: str, value: str) -> None:
        with ui.row().classes('w-full justify-between items-baseline gap-2'):
            ui.label(label).classes('text-[10px] uppercase tracking-wider text-slate-500')
            ui.label(value).classes('text-xs font-mono text-slate-200')

    def refresh() -> None:
        report = analyse()
        chosen = {s.id for s in canvas.get_selected()}
        selection = [o for o in pieces() if o['id'] in chosen]

        status_count.text = f'{len(report["grid"])} elem'
        status_open.text = f'{len(report["open"])} open'

        summary.clear()
        with summary:
            stat('elements', str(len(report['grid'])))
            stat('connections', str(report['links']))
            stat('stations', str(report['stations']))
            stat('track length', f'{report["length"]:.2f} m')
            if report['stacked']:
                stat('overlapping', str(report['stacked']))
            ui.separator().classes('my-1 bg-slate-700')
            if not report['grid']:
                ui.label('layout empty').classes('text-xs text-slate-500')
            elif not report['open']:
                ui.label('■ network closed — all ports connected') \
                    .classes('text-xs text-emerald-400 nf-verdict')
            else:
                ui.label(f'▲ {len(report["open"])} open port(s)') \
                    .classes('text-xs text-amber-400 nf-verdict')
                with ui.column().classes('gap-0 max-h-44 overflow-auto w-full mt-1'):
                    for cell, port in report['open'][:60]:
                        ui.label(f'{cell[0] * CELL_M:>6.1f} {cell[1] * CELL_M:>6.1f}   {port}') \
                            .classes('text-[11px] font-mono text-slate-400')

        sel_box.clear()
        with sel_box:
            if not selection:
                ui.label('no selection').classes('text-xs text-slate-500')
                return
            if len(selection) == 1:
                entry = selection[0]
                cell = cell_of(entry['left'], entry['top'])
                quarter = round((entry.get('angle', 0) % 360) / 90) % 4
                ui.label(PIECES[entry['kind']]['label'].upper()) \
                    .classes('text-xs font-mono tracking-wider text-cyan-300')
                stat('id', entry['id'][:8])
                stat('position', f'{cell[0] * CELL_M:.1f}, {cell[1] * CELL_M:.1f} m')
                stat('heading', f'{quarter * 90}°')
                stat('ports', ports_of(entry['kind'], quarter))
                stat('length', f'{PIECES[entry["kind"]]["length"] * CELL_M:.2f} m')
            else:
                ui.label(f'{len(selection)} ELEMENTS').classes('text-xs font-mono text-cyan-300')
            with ui.grid(columns=2).classes('w-full gap-1 mt-2'):
                ui.button('Rotate', icon='rotate_right', on_click=lambda: rotate(1)) \
                    .props('dense outline no-caps size=sm').classes('nf-rotate')
                ui.button('Back', icon='rotate_left', on_click=lambda: rotate(-1)) \
                    .props('dense outline no-caps size=sm')
                ui.button('Delete', icon='delete', on_click=delete_selected) \
                    .props('dense outline no-caps size=sm color=red').classes('nf-delete')
                ui.button('Clear sel', icon='deselect', on_click=canvas.discard_selection) \
                    .props('dense outline no-caps size=sm')

    # ================================================================== layout ============
    ui.query('.nicegui-content').classes('p-0 gap-0')
    ui.query('body').style(f'background-color: {FIELD}')

    with ui.dialog() as json_dialog, ui.card().classes('w-96 bg-slate-800'):
        ui.label('Import layout JSON').classes('text-sm font-mono text-slate-200')
        json_uploader = ui.upload(label='agv-layout.json', auto_upload=True,
                                  max_file_size=2_000_000, on_upload=import_json) \
            .props('accept=".json,application/json" flat dense no-thumbnails').classes('w-full')

    with ui.header().classes('bg-slate-950 border-b border-slate-800 items-center gap-2 px-3 '
                             'py-1'):
        ui.button(icon='menu', on_click=lambda: left.toggle()).props('flat dense color=grey-5')
        ui.label('AGV LAYOUT EDITOR') \
            .classes('text-sm font-mono tracking-widest text-slate-200')
        ui.label('· nicefabric').classes('text-xs font-mono text-slate-600')
        ui.space()
        with ui.button('File', icon='folder').props('flat dense no-caps color=grey-5'):
            with ui.menu().classes('bg-slate-800'):
                ui.menu_item('Save', save)
                ui.menu_item('Load', load)
                ui.separator()
                ui.menu_item('Export JSON', export_json)
                ui.menu_item('Import JSON…', json_dialog.open)
                ui.separator()
                ui.menu_item('Export SVG', export_svg)
                ui.menu_item('Export PNG', export_png)
                ui.separator()
                ui.menu_item('Clear layout', clear_all)
        ui.button(icon='tune', on_click=lambda: right.toggle()).props('flat dense color=grey-5')

    with ui.left_drawer(value=True, bordered=True).props('width=232') \
            .classes('bg-slate-900 p-3') as left:
        ui.label('ELEMENTS').classes('text-[10px] uppercase tracking-widest text-slate-500')
        with ui.column().classes('w-full gap-1 mt-1'):
            for kind, piece in PIECES.items():
                with ui.row().classes('w-full items-center gap-2 rounded px-1 py-0.5 '
                                      'hover:bg-slate-800'):
                    ui.html(f'<div class="nf-piece nf-piece-{kind}" draggable="true" '
                            f'data-kind="{kind}" title="{piece["label"]}" '
                            f'style="width:46px;height:46px;cursor:grab;flex:none;'
                            f'border:1px solid #1e293b;border-radius:3px;'
                            f'background:{FIELD}">{piece["thumb"]}</div>')
                    with ui.column().classes('gap-0'):
                        ui.label(piece['label']).classes('text-xs text-slate-300')
                        ui.label(f'{piece["ports"]} · {piece["length"] * CELL_M:.2f} m') \
                            .classes('text-[10px] font-mono text-slate-500')

        ui.separator().classes('my-3 bg-slate-800')
        ui.label('PLACEMENT HEADING') \
            .classes('text-[10px] uppercase tracking-widest text-slate-500')
        palette_rotation = ui.toggle({0: '0°', 90: '90°', 180: '180°', 270: '270°'}, value=0) \
            .props('dense no-caps spread size=sm').classes('w-full nf-droprot mt-1')
        ui.label('Drag an element onto the field. R rotates the selection; '
                 'Delete removes it.').classes('text-[11px] text-slate-500 mt-3 leading-snug')

    with ui.right_drawer(value=True, bordered=True).props('width=256') \
            .classes('bg-slate-900 p-3') as right:
        ui.label('NETWORK').classes('text-[10px] uppercase tracking-widest text-slate-500')
        summary = ui.column().classes('w-full gap-0.5 mt-1')

        ui.separator().classes('my-3 bg-slate-800')
        ui.label('SELECTION').classes('text-[10px] uppercase tracking-widest text-slate-500')
        sel_box = ui.column().classes('w-full gap-0.5 mt-1 nf-sel')

        ui.separator().classes('my-3 bg-slate-800')
        with ui.expansion('Log').classes('w-full text-xs'):
            log = ui.log(max_lines=14).classes('w-full h-32 text-[11px] nf-log')

    with ui.footer().classes('bg-slate-950 border-t border-slate-800 items-center gap-5 '
                             'px-3 py-0.5'):
        status_count = ui.label('0 elem').classes('text-[11px] font-mono text-slate-300 nf-count')
        status_open = ui.label('0 open').classes('text-[11px] font-mono text-slate-300 nf-open')
        status_extent = ui.label().classes('text-[11px] font-mono text-slate-500')
        ui.label(f'grid {CELL_M:.1f} m').classes('text-[11px] font-mono text-slate-500')
        ui.space()
        ui.label('—').classes('text-[11px] font-mono text-cyan-400 nf-cursor')

    refresh()


if __name__ in {'__main__', '__mp_main__'}:
    ui.run(port=PORT, title='AGV layout editor', show=False, reload=True)
