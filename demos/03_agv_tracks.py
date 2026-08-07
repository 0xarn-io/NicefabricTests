"""Demo 03 — an AGV route editor: a modern take on fleet-commissioning software.

Demo 02 is a general drawing tool. This is a *domain* editor built on the same element, laid
out the way commissioning suites are: mode tabs across the top, the site field in the middle,
a stacked Objects/Properties dock on the right, a vehicle telemetry dock underneath, and a
status bar carrying live cursor coordinates and a sync state.

The field is drawn in that idiom too — white site, metric grid with edge rulers, the laser
scan underneath as red returns, violet route splines, numbered node circles at junctions and
stations, and direction chevrons along each segment.

The parts worth reading:

* **One object per element.** Each route element is a generated SVG rendered into a single
  Fabric ``Image`` through a ``data:image/svg+xml`` URL — the flattened form demo 01 measured
  as the only single-object representation that survives ``to_dict()`` → ``load_json()``.
* **The element kind rides on the object.** ``add_image(url, kind='curve')`` stores a custom
  prop, and ``load_json`` deep-copies unknown props, so ``kind`` survives a save/load round
  trip. That matters because ``load_json`` **re-ids every object**, so a side table keyed by
  object id would be destroyed by the first load — metadata has to travel *on* the piece.
* **Ports are derived, never stored.** ``kind`` gives the base ports and Fabric's ``angle``
  gives the rotation; connectivity rotates one by the other. A clockwise turn advances every
  port one step along ``'NESW'``, so there is no rotation table to fall out of date.
* **Node captions are a derived layer.** They are separate ``Text`` objects held at
  ``angle=0``, because a caption baked into the tile art would rotate with the tile and read
  upside down at 180°. They carry ``kind='label'`` so the connectivity pass skips them, and
  are rebuilt only when the layout signature changes.
* **Snapping is server-side.** Drops report a pointer position and ``on_modified`` reports the
  position after a drag; both are divided back through the zoom, rounded to the cell grid and
  to 90°, then written with ``update_object``. The browser never decides where a piece is.

Run with::

    python demos/03_agv_tracks.py
"""
import asyncio
import base64
import math
import random

from nicegui import app, ui

from nicefabric import FabricCanvas

PORT = 9092
TILE = 80            # px per grid cell
CELL_M = 1.0         # metres per cell — the layout is dimensioned, not merely pixel-sized
SLOT = 'nicefabric-demo-03'

# Field palette: white site, violet routes, red laser returns, orange named positions.
GRID_MINOR = '#eff4f9'
GRID_MAJOR = '#dbe5f0'
SCAN = '#ef6b6b'       # laser scan returns
BLOCK = '#3f3f46'      # pallet positions
ROUTE = '#6d5bd0'      # route centreline
CHEVRON = '#a3adc2'    # direction markers
STATION_RING = '#f59e0b'
ZONE = '#7aa2e8'
VEHICLE_FILL = '#a8cdf0'
VEHICLE_EDGE = '#2563eb'
NAV = '#1b2431'
APPLY = '#16a34a'

STATION_TINT = {'charge': '#c9f0dc', 'load': '#c9e7ea',
                'unload': '#cfe0f5', 'end': '#f6d3d3'}
STATION_CODE = {'charge': 'Charger', 'load': 'Pick', 'unload': 'Drop', 'end': 'EndStop'}
# nodes are drawn at decision points and endpoints, not mid-segment — as these tools do
NODE_KINDS = ('fork', 'cross', 'charge', 'load', 'unload', 'end')

DIRS = 'NESW'
DELTA = {'N': (0, -1), 'E': (1, 0), 'S': (0, 1), 'W': (-1, 0)}
OPPOSITE = {'N': 'S', 'S': 'N', 'E': 'W', 'W': 'E'}

_ARC = 'M 0 40 A 40 40 0 0 1 40 80'


def _svg(body: str, size: int = TILE, box: int = TILE) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
            f'viewBox="0 0 {box} {box}">{body}</svg>')


def _chev(x: float, y: float, rot: int) -> str:
    """A small travel-direction chevron, drawn beside the centreline."""
    return (f'<path d="M -3 -3 L 1 0 L -3 3" fill="none" stroke="{CHEVRON}" stroke-width="1.3" '
            f'stroke-linecap="round" stroke-linejoin="round" '
            f'transform="translate({x} {y}) rotate({rot})"/>')


def _route(d: str, chevrons: str = '') -> str:
    return (f'<path d="{d}" fill="none" stroke="{ROUTE}" stroke-width="1.5" '
            f'stroke-linecap="round"/>{chevrons}')


def _node(ring: str = ROUTE) -> str:
    return f'<circle cx="40" cy="40" r="4.6" fill="#ffffff" stroke="{ring}" stroke-width="1.5"/>'


def _station(kind: str, glyph: str) -> str:
    """A stub into a compact tinted body with an orange node ring — a named position."""
    return (f'{_route("M 0 40 L 30 40")}'
            f'<rect x="30" y="27" width="38" height="26" rx="3" fill="{STATION_TINT[kind]}" '
            f'stroke="{ROUTE}" stroke-width="1.2"/>{glyph}'
            f'<circle cx="30" cy="40" r="4.6" fill="#ffffff" stroke="{STATION_RING}" '
            f'stroke-width="1.6"/>')


PIECES: dict[str, dict] = {
    'straight': {'label': 'Segment', 'ports': 'WE', 'length': 1.0,
                 'body': _route('M 0 40 L 80 40',
                                _chev(28, 33, 0) + _chev(52, 47, 180))},
    'curve': {'label': 'Curve 90°', 'ports': 'WS', 'length': 0.7854,
              'body': _route(_ARC, _chev(20, 55, 55))},
    # the branch leaves as a spline rather than a right angle: a turnout a vehicle could drive
    'fork': {'label': 'Turnout', 'ports': 'WES', 'length': 1.5,
             'body': _route('M 0 40 L 80 40 M 14 40 C 32 40 40 54 40 80',
                            _chev(64, 33, 0) + _chev(46, 68, 90)) + _node()},
    'cross': {'label': 'Crossing', 'ports': 'NESW', 'length': 2.0,
              'body': _route('M 0 40 L 80 40 M 40 0 L 40 80',
                             _chev(66, 33, 0) + _chev(47, 66, 90)) + _node()},
    'charge': {'label': 'Charger', 'ports': 'W', 'length': 0.5,
               'body': _station('charge',
                                f'<polygon points="52,32 45,42 50,42 48,50 56,39 51,39" '
                                f'fill="#0f766e"/>')},
    'load': {'label': 'Pick station', 'ports': 'W', 'length': 0.5,
             'body': _station('load',
                              '<path d="M 51 47 L 51 34 M 46 39 L 51 33 L 56 39" fill="none" '
                              'stroke="#0f5f6b" stroke-width="1.7"/>')},
    'unload': {'label': 'Drop station', 'ports': 'W', 'length': 0.5,
               'body': _station('unload',
                                '<path d="M 51 33 L 51 46 M 46 41 L 51 47 L 56 41" fill="none" '
                                'stroke="#1e4d91" stroke-width="1.7"/>')},
    'end': {'label': 'End stop', 'ports': 'W', 'length': 0.5,
            'body': _station('end',
                             '<path d="M 46 31 L 46 49 M 52 34 L 58 40 M 52 40 L 58 46" '
                             'fill="none" stroke="#9b1c1c" stroke-width="1.7"/>')},
}

for _piece in PIECES.values():
    _piece['svg'] = _svg(_piece['body'])
    _piece['thumb'] = _svg(_piece['body'], 38)
    _piece['url'] = ('data:image/svg+xml;base64,'
                     + base64.b64encode(_piece['svg'].encode()).decode())

# A vehicle carries its detection zone in the same artwork, so the fleet layer stays one
# object per vehicle. The body outline is a forklift footprint, not a rectangle.
_VB = 220
VEHICLE_URL = 'data:image/svg+xml;base64,' + base64.b64encode(_svg(
    f'<circle cx="110" cy="110" r="92" fill="{ZONE}" fill-opacity="0.16"/>'
    f'<circle cx="110" cy="110" r="58" fill="{ZONE}" fill-opacity="0.14"/>'
    f'<path d="M 74 90 L 116 90 L 128 98 L 150 98 L 150 122 L 128 122 L 116 130 L 74 130 Z" '
    f'fill="{VEHICLE_FILL}" stroke="{VEHICLE_EDGE}" stroke-width="1.6" '
    f'stroke-linejoin="round"/>'
    f'<rect x="84" y="104" width="26" height="12" rx="3" fill="{VEHICLE_EDGE}"/>'
    f'<line x1="150" y1="110" x2="176" y2="110" stroke="{SCAN}" stroke-width="1.4"/>'
    f'<circle cx="110" cy="110" r="3" fill="#ffffff" stroke="{VEHICLE_EDGE}" '
    f'stroke-width="1.4"/>', _VB, _VB).encode()).decode()

PLACED_PROPS = {'lockScalingX': True, 'lockScalingY': True,
                'borderColor': VEHICLE_EDGE, 'cornerColor': '#ffffff',
                'cornerStrokeColor': VEHICLE_EDGE, 'transparentCorners': False,
                'cornerSize': 7, 'padding': 2}
LABEL_PROPS = {'kind': 'label', 'selectable': False, 'evented': False,
               'fontSize': 11, 'fontFamily': 'Inter, Arial, sans-serif'}


def _scan_environment(width_m: float, height_m: float) -> str:
    """A plausible laser scan of a site: red returns along walls and racking, dark pallets.

    Seeded, so the site is identical across restarts — a map that reshuffled every reload
    would make screenshots and tests non-reproducible.
    """
    rng = random.Random(20260807)
    w, h = width_m * TILE, height_m * TILE
    parts: list[str] = []

    def wall(x1: float, y1: float, x2: float, y2: float) -> None:
        span = math.hypot(x2 - x1, y2 - y1)
        marks = []
        for i in range(max(2, int(span / 6))):
            if rng.random() < 0.14:            # real scans have gaps and shadows
                continue
            t = i / max(2, int(span / 6))
            x = x1 + (x2 - x1) * t + rng.uniform(-1.2, 1.2)
            y = y1 + (y2 - y1) * t + rng.uniform(-1.2, 1.2)
            marks.append(f'M{x:.0f} {y:.0f}h1.6')
        parts.append(f'<path d="{"".join(marks)}" stroke="{SCAN}" stroke-width="2.4" '
                     f'stroke-linecap="round" fill="none" opacity="0.85"/>')

    def box(x: float, y: float, bw: float, bh: float) -> None:
        wall(x, y, x + bw, y), wall(x + bw, y, x + bw, y + bh)
        wall(x + bw, y + bh, x, y + bh), wall(x, y + bh, x, y)

    m = TILE
    box(0.5 * m, 0.5 * m, (width_m - 1) * m, (height_m - 1) * m)
    for row_y in (1.8, height_m - 3.0):
        for run_x in (2.0, width_m - 9.5):
            box(run_x * m, row_y * m, 7.5 * m, 1.1 * m)
            # pallet positions along the run: small blocks, as a scan of racking shows them
            for i in range(12):
                parts.append(f'<rect x="{(run_x + 0.25 + i * 0.6) * m:.0f}" '
                             f'y="{(row_y + 0.28) * m:.0f}" width="{0.34 * m:.0f}" '
                             f'height="{0.54 * m:.0f}" fill="{BLOCK}" opacity="0.88"/>')
    box(width_m * m - 3.6 * m, (height_m / 2 - 1.6) * m, 2.6 * m, 3.4 * m)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}" height="{h:.0f}" '
            f'viewBox="0 0 {w:.0f} {h:.0f}">{"".join(parts)}</svg>')


SITE_W_M, SITE_H_M = 24, 13
SCAN_URL = ('data:image/svg+xml;base64,'
            + base64.b64encode(_scan_environment(SITE_W_M, SITE_H_M).encode()).decode())


def ports_of(kind: str, quarter: int) -> str:
    """Ports of ``kind`` after ``quarter`` 90° clockwise turns — an index shift along DIRS."""
    return ''.join(DIRS[(DIRS.index(d) + quarter) % 4] for d in PIECES[kind]['ports'])


def cell_of(left: float, top: float) -> tuple[int, int]:
    return round((left - TILE / 2) / TILE), round((top - TILE / 2) / TILE)


def centre_of(cell: tuple[int, int]) -> tuple[float, float]:
    return cell[0] * TILE + TILE / 2, cell[1] * TILE + TILE / 2


@ui.page('/')  # per-visit page: a module-level canvas would be shared by ALL tabs and users
def index() -> None:
    state = {'w': 1200, 'h': 700, 'zoom': 1.0, 'labels': '',
             'scan': True, 'grid': True, 'nodes': True}

    def on_modified(e) -> None:
        snap(e.args['id'], e.args['props'])
        refresh()

    # The scan is a data: URL and contains ';base64,'. NiceGUI's inline-style parser splits on
    # ';', so it can never go in a style attribute — it lives in real CSS, on its own layer
    # behind the transparent canvas. Grid rides the same way, so both toggle independently.
    ui.add_css(f'''
        .nf-scan {{ background-image:url({SCAN_URL});
            background-size:{SITE_W_M * TILE}px {SITE_H_M * TILE}px;
            background-repeat:no-repeat; }}
        .nf-grid {{ background-image:
            linear-gradient(to right, {GRID_MAJOR} 1px, transparent 1px),
            linear-gradient(to bottom, {GRID_MAJOR} 1px, transparent 1px),
            linear-gradient(to right, {GRID_MINOR} 1px, transparent 1px),
            linear-gradient(to bottom, {GRID_MINOR} 1px, transparent 1px);
            background-size:{TILE * 5}px {TILE * 5}px, {TILE * 5}px {TILE * 5}px,
            {TILE}px {TILE}px, {TILE}px {TILE}px; }}
    ''')
    ui.add_css('''
        .nf-ruler-top { position:absolute; top:0; left:0; right:0; height:16px; z-index:5;
            background:#fbfcfe; border-bottom:1px solid #e3ebf3; pointer-events:none; }
        .nf-ruler-right { position:absolute; top:0; right:0; bottom:0; width:32px; z-index:5;
            background:#fbfcfe; border-left:1px solid #e3ebf3; pointer-events:none; }
        .nf-tick { position:absolute; font:10px ui-monospace,monospace; color:#9fb0c4; }
        .nf-dock { position:absolute; z-index:10; background:#fff; border:1px solid #dde5ee;
            border-radius:5px; box-shadow:0 4px 14px rgba(15,23,42,.08); }
        .nf-panelhead { font:600 10px system-ui; letter-spacing:.09em; color:#64748b;
            text-transform:uppercase; }
    ''')

    with ui.element('div').classes('relative w-full h-full nf-stage bg-white'):
        # painted in DOM order: scan, then grid, then the transparent canvas on top
        scan_layer = ui.element('div').classes('absolute inset-0 nf-scan')
        grid_layer = ui.element('div').classes('absolute inset-0 nf-grid')
        canvas = FabricCanvas(
            width=state['w'], height=state['h'], background='', keyboard_delete=True,
            on_selection=lambda e: refresh(),
            on_modified=on_modified,
            on_error=lambda e: log.push(f'ERROR {e.args}')) \
            .classes('nf-canvas absolute inset-0')
        ui.html('<div class="nf-ruler-top"></div><div class="nf-ruler-right"></div>')

        # both sit clear of the right-hand docks (right:50px, 206px wide) and the ruler strip
        with ui.element('div').classes('absolute z-10 flex items-center gap-1 bg-white '
                                       'border border-slate-300 rounded px-2 py-0.5') \
                .style('top:24px; right:268px'):
            ui.icon('my_location', size='12px').classes('text-slate-400')
            ui.label('—').classes('text-[11px] font-mono text-slate-700 nf-cursor')
        with ui.element('div').classes('absolute z-10 flex flex-col gap-1') \
                .style('bottom:16px; right:268px'):
            for icon, fn, cls in [('center_focus_strong', lambda: set_zoom(1.0), 'nf-fit'),
                                  ('add', lambda: set_zoom(state['zoom'] * 1.25), 'nf-zin'),
                                  ('remove', lambda: set_zoom(state['zoom'] / 1.25), 'nf-zout')]:
                ui.button(icon=icon, on_click=fn).props('round flat dense size=sm color=grey-8') \
                    .classes(f'bg-white border border-slate-300 {cls}')

    def pieces() -> list[dict]:
        return [o for o in canvas.to_dict()['objects'] if o.get('kind') in PIECES]

    def vehicles() -> list[dict]:
        return [o for o in canvas.to_dict()['objects'] if o.get('kind') == 'agv']

    def snap(id_: str, props: dict) -> None:
        cell = cell_of(props.get('left', 0), props.get('top', 0))
        left, top = centre_of(cell)
        quarter = round((props.get('angle', 0) % 360) / 90) % 4
        try:
            canvas.update_object(id_, left=left, top=top, angle=quarter * 90)
        except KeyError:
            pass

    # ------------------------------------------------------------------ view --------------
    def set_zoom(z: float) -> None:
        state['zoom'] = max(0.35, min(2.5, z))
        canvas.set_zoom(state['zoom'])
        status_zoom.text = f'{state["zoom"] * 100:.0f}%'
        ui.run_javascript(f'window.__nfZoom={state["zoom"]};'
                          f'window.__nfPaintRulers&&window.__nfPaintRulers();', timeout=2)

    def apply_layers() -> None:
        scan_layer.set_visibility(state['scan'])
        grid_layer.set_visibility(state['grid'])

    def set_layer(name: str, on: bool) -> None:
        state[name] = on
        if name in ('scan', 'grid'):
            apply_layers()
        else:                                   # node captions are canvas objects
            for entry in canvas.to_dict()['objects']:
                if entry.get('kind') == 'label':
                    canvas.update_object(entry['id'], visible=on)

    # ------------------------------------------------------------------ placement --------
    def place(kind: str, cell: tuple[int, int], quarter: int = 0) -> None:
        for existing in pieces():                # one element per cell: replace, not stack
            if cell_of(existing['left'], existing['top']) == cell:
                canvas.remove_object(existing['id'])
        left, top = centre_of(cell)
        canvas.add_image(PIECES[kind]['url'], left=left, top=top, angle=quarter * 90,
                         kind=kind, **PLACED_PROPS)
        log.push(f'{kind:<9} {cell[0] * CELL_M:>7.2f} {cell[1] * CELL_M:>7.2f} '
                 f'{quarter * 90:>3}°')

    def on_drop(e) -> None:
        kind = e.args.get('kind')
        if kind not in PIECES:
            return
        # the pointer is in screen pixels; dividing by the zoom returns scene units, or a drop
        # made while zoomed would land on the wrong cell
        z = state['zoom']
        cell = (int((e.args['x'] / z) // TILE), int((e.args['y'] / z) // TILE))
        place(kind, cell, quarter=int(palette_rotation.value) // 90)
        refresh()

    ui.on('nf_drop', on_drop)

    async def wire_client() -> None:
        await canvas.initialized()
        await ui.run_javascript(f"""
            window.__nfZoom = 1;
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
                const x = (ev.clientX - r.left) / window.__nfZoom / {TILE} * {CELL_M};
                const y = (ev.clientY - r.top) / window.__nfZoom / {TILE} * {CELL_M};
                el.textContent = 'x ' + x.toFixed(3) + '  y ' + y.toFixed(3);
            }});
            // rulers are plain DOM: cheaper than canvas objects, and never enter the registry
            window.__nfPaintRulers = () => {{
                const stage = document.querySelector('.nf-stage');
                const top = document.querySelector('.nf-ruler-top');
                const right = document.querySelector('.nf-ruler-right');
                if (!stage || !top || !right) return;
                const step = {TILE} * window.__nfZoom;
                let a = '', b = '';
                for (let i = 0, x = 0; x < stage.clientWidth - 32; i++, x += step)
                    a += '<span class="nf-tick" style="left:' + (x + 3) + 'px;top:2px">'
                       + (i * {CELL_M}).toFixed(0) + '</span>';
                for (let i = 0, y = 0; y < stage.clientHeight; i++, y += step)
                    b += '<span class="nf-tick" style="right:4px;top:' + (y + 19) + 'px">'
                       + (i * {CELL_M}).toFixed(0) + '</span>';
                top.innerHTML = a; right.innerHTML = b;
            }};
            window.addEventListener('resize', () => {{ clearTimeout(window.__nfRs);
                window.__nfRs = setTimeout(() => emitEvent('nf_resize'), 200); }});
        """, timeout=5)
        await fit_canvas()

    async def fit_canvas() -> None:
        try:
            dims = await ui.run_javascript(
                "(() => { const s = document.querySelector('.nf-stage');"
                " return s ? [s.clientWidth, s.clientHeight] : null; })()", timeout=3)
        except TimeoutError:
            return
        if not dims or dims[0] < TILE or dims[1] < TILE:
            return
        state['w'], state['h'] = dims
        canvas.resize(*dims)
        status_extent.text = f'{dims[0] / TILE * CELL_M:.0f} x {dims[1] / TILE * CELL_M:.0f} m'
        ui.run_javascript('window.__nfPaintRulers && window.__nfPaintRulers();', timeout=2)

    ui.timer(0, wire_client, once=True)
    ui.on('nf_resize', fit_canvas)

    # ------------------------------------------------------------------ node identity -----
    def node_table() -> list[dict]:
        """Nodes in reading order, with the 4-digit ids these tools use.

        Numbering is positional, so the same layout always yields the same ids — including
        after a ``load_json``, which regenerates every underlying object id.
        """
        out = []
        for i, entry in enumerate(sorted((p for p in pieces() if p['kind'] in NODE_KINDS),
                                         key=lambda o: (o['top'], o['left']))):
            cell = cell_of(entry['left'], entry['top'])
            out.append({'nid': 1001 + i, 'kind': entry['kind'], 'cell': cell,
                        'id': entry['id'], 'angle': entry.get('angle', 0)})
        return out

    def sync_labels() -> None:
        """Rebuild node captions when the layout changes.

        Captions are separate ``Text`` objects at ``angle=0``: baked into the tile art they
        would rotate with the tile and read upside down at 180°. Rebuilt only when the
        signature changes, so a plain drag does not churn the registry.
        """
        wanted = []
        for node in node_table():
            left, top = centre_of(node['cell'])
            caption = (f'{STATION_CODE[node["kind"]]} ({node["nid"]})'
                       if node['kind'] in STATION_CODE else str(node['nid']))
            colour = STATION_RING if node['kind'] in STATION_CODE else '#8b93ab'
            wanted.append((left + 8, top - 30, caption, colour))
        for i, entry in enumerate(sorted(vehicles(), key=lambda o: (o['top'], o['left'])), 1):
            wanted.append((entry['left'] + 26, entry['top'] + 6, f'Vehicle {i}', VEHICLE_EDGE))

        signature = repr(wanted)
        if signature == state['labels']:
            return
        state['labels'] = signature
        for old in canvas.to_dict()['objects']:
            if old.get('kind') == 'label':
                canvas.remove_object(old['id'])
        for left, top, text, colour in wanted:
            canvas.add_text(text, left=left, top=top, width=150, fill=colour,
                            visible=state['nodes'], **LABEL_PROPS)

    # ------------------------------------------------------------------ fleet -------------
    def add_vehicles() -> None:
        report = analyse()
        candidates = [c for c in sorted(report['grid'], key=lambda c: (c[1], c[0]))
                      if report['grid'][c]['kind'] not in STATION_CODE]
        if not candidates:
            ui.notify('build some route first')
            return
        # spread the fleet around the network instead of bunching it at the first cells
        spots = candidates[::max(1, len(candidates) // 3)][:3]
        for cell in spots:
            left, top = centre_of(cell)
            canvas.add_image(VEHICLE_URL, left=left, top=top, kind='agv', **PLACED_PROPS)
        refresh()

    def clear_vehicles() -> None:
        for entry in vehicles():
            canvas.remove_object(entry['id'])
        refresh()

    # ------------------------------------------------------------------ connectivity ------
    def analyse() -> dict:
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
                nb = grid.get((cell[0] + dx, cell[1] + dy))
                if nb and OPPOSITE[port] in nb['ports']:
                    links += 1
                else:
                    open_ends.append((cell, port))
        return {'grid': grid, 'links': links // 2, 'open': open_ends, 'stacked': stacked,
                'stations': sum(1 for p in grid.values() if p['kind'] in STATION_CODE),
                'length': sum(PIECES[p['kind']]['length'] for p in grid.values()) * CELL_M}

    # ------------------------------------------------------------------ file ops ----------
    def save() -> None:
        app.storage.general[SLOT] = canvas.to_dict()
        ui.notify(f'project saved — {len(pieces())} elements')

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
        state['labels'] = ''       # object ids changed on load; force the caption layer to rebuild
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
        state['labels'] = ''
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
        state['labels'] = ''
        refresh()

    # ------------------------------------------------------------------ selection ---------
    def rotate(step: int) -> None:
        for obj in canvas.get_selected():
            try:
                props = obj.props
            except KeyError:
                continue
            if props.get('kind') not in PIECES:
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

    # ------------------------------------------------------------------ panels ------------
    def prop_row(name: str, value: str, unit: str = '') -> None:
        with ui.row().classes('w-full items-baseline gap-1 py-px'):
            ui.label(name).classes('text-[11px] text-slate-500 flex-1')
            ui.label(value).classes('text-[11px] font-mono text-slate-800')
            ui.label(unit).classes('text-[10px] text-slate-400 w-7 text-right')

    def refresh() -> None:
        sync_labels()
        report = analyse()
        nodes = node_table()
        chosen = {s.id for s in canvas.get_selected()}
        selected = [o for o in pieces() + vehicles() if o['id'] in chosen]

        status_count.text = f'{len(report["grid"])} elements'
        status_open.text = f'{len(report["open"])} open'
        status_sync.text = ('network closed — synchronized' if report['grid']
                            and not report['open'] else
                            f'{len(report["open"])} open port(s)')
        # `replace` drops every existing class, so the nf-sync hook has to be restated here
        status_sync.classes(replace='nf-sync text-[11px] font-mono ' + (
            'text-emerald-600' if report['grid'] and not report['open'] else 'text-amber-600'))

        # --- objects tree ---
        objects.clear()
        with objects:
            for label, key in (('Laser scan', 'scan'), ('Grid', 'grid'), ('Node labels', 'nodes')):
                with ui.row().classes('items-center gap-1 w-full'):
                    ui.checkbox(value=state[key],
                                on_change=lambda e, k=key: set_layer(k, e.value)) \
                        .props('dense size=xs').classes(f'nf-layer-{key}')
                    ui.label(label).classes('text-[11px] text-slate-700')
            ui.separator().classes('my-1')
            ui.label(f'Nodes ({len(nodes)})').classes('text-[11px] text-slate-500')
            with ui.column().classes('gap-0 max-h-32 overflow-auto w-full pl-2'):
                for node in nodes:
                    caption = (f'{node["nid"]}  {STATION_CODE[node["kind"]]}'
                               if node['kind'] in STATION_CODE else f'{node["nid"]}')
                    ui.label(caption).classes(
                        'text-[11px] font-mono ' + ('text-amber-600'
                                                    if node['kind'] in STATION_CODE
                                                    else 'text-slate-600'))

        # --- properties ---
        # A nested function, not an inline block: the early returns below used to exit
        # refresh() itself, so the telemetry panel underneath never rendered at all.
        def fill_props() -> None:
            if not selected:
                prop_row('elements', str(len(report['grid'])))
                prop_row('connections', str(report['links']))
                prop_row('stations', str(report['stations']))
                prop_row('vehicles', str(len(vehicles())))
                prop_row('route length', f'{report["length"]:.2f}', 'm')
                if report['stacked']:
                    prop_row('overlapping', str(report['stacked']))
                return
            if len(selected) > 1:
                prop_row('selected', str(len(selected)))
                return
            entry = selected[0]
            cell = cell_of(entry['left'], entry['top'])
            if entry.get('kind') == 'agv':
                prop_row('type', 'Vehicle')
                prop_row('x', f'{cell[0] * CELL_M:.3f}', 'm')
                prop_row('y', f'{cell[1] * CELL_M:.3f}', 'm')
                return
            quarter = round((entry.get('angle', 0) % 360) / 90) % 4
            nid = next((n['nid'] for n in nodes if n['id'] == entry['id']), None)
            prop_row('id', str(nid) if nid else '—')
            prop_row('type', PIECES[entry['kind']]['label'])
            prop_row('x', f'{cell[0] * CELL_M:.3f}', 'm')
            prop_row('y', f'{cell[1] * CELL_M:.3f}', 'm')
            prop_row('angle', f'{quarter * 90}', 'deg')
            prop_row('ports', ports_of(entry['kind'], quarter))
            prop_row('length', f'{PIECES[entry["kind"]]["length"] * CELL_M:.2f}', 'm')

        props.clear()
        with props:
            fill_props()
            if selected:   # keyboard R / Delete also work, but not everyone reaches for them
                with ui.grid(columns=2).classes('w-full gap-1 mt-2'):
                    ui.button('Rotate', on_click=lambda: rotate(1)) \
                        .props('dense flat no-caps size=sm color=primary').classes('nf-rotate')
                    ui.button('Back', on_click=lambda: rotate(-1)) \
                        .props('dense flat no-caps size=sm color=primary')
                    ui.button('Delete', on_click=delete_selected) \
                        .props('dense flat no-caps size=sm color=red').classes('nf-delete')
                    ui.button('Deselect', on_click=canvas.discard_selection) \
                        .props('dense flat no-caps size=sm color=grey-7')

        # --- vehicle telemetry ---
        telemetry.clear()
        with telemetry:
            fleet = vehicles()
            if not fleet:
                ui.label('no vehicle on the layout').classes('text-[11px] text-slate-400')
            else:
                cell = cell_of(fleet[0]['left'], fleet[0]['top'])
                for name, value, unit in (
                        ('Battery voltage', '26.1', 'V'), ('Battery power', '42', '%'),
                        ('Pose (x, y)',
                         f'({cell[0] * CELL_M:.3f}, {cell[1] * CELL_M:.3f})', 'm'),
                        ('Pose (angle)', '179.9', 'deg'),
                        ('Position uncertainty (95%)', '0.012', 'm'),
                        ('Angular uncertainty (95%)', '0.171', 'deg'),
                        ('Linear speed', '0.000', 'm/s')):
                    prop_row(name, value, unit)

    # ================================================================== layout ============
    ui.query('.nicegui-content').classes('p-0 gap-0 h-screen')
    ui.query('.q-page').classes('h-full')

    with ui.dialog() as json_dialog, ui.card().classes('w-96'):
        ui.label('Import layout JSON').classes('text-sm font-medium')
        json_uploader = ui.upload(label='agv-layout.json', auto_upload=True,
                                  max_file_size=2_000_000, on_upload=import_json) \
            .props('accept=".json,application/json" flat dense no-thumbnails').classes('w-full')

    with ui.header().classes('items-center gap-0 px-4 py-0').style(f'background:{NAV}'):
        ui.label('ANTLR').classes('text-sm font-semibold text-white tracking-wide mr-1')
        ui.label('route studio').classes('text-[11px] font-light text-slate-400 mr-6')
        for tab in ('Vehicles', 'Localization', 'Routes', 'Devices', 'Monitoring'):
            ui.label(tab).classes(
                'text-[12px] px-3 py-3 cursor-pointer '
                + ('text-white border-b-2 border-cyan-400' if tab == 'Routes'
                   else 'text-slate-400 hover:text-slate-200'))
        ui.space()
        ui.label('server / localhost:9000').classes('text-[11px] font-mono text-slate-400 mr-3')
        ui.button('APPLY PROJECT', on_click=save).props('dense unelevated no-caps size=sm') \
            .style(f'background:{APPLY};color:#fff').classes('nf-apply')
        with ui.button(icon='folder').props('flat dense size=sm color=grey-5'):
            with ui.menu():
                ui.menu_item('Load project', load)
                ui.separator()
                ui.menu_item('Export JSON', export_json)
                ui.menu_item('Import JSON…', json_dialog.open)
                ui.separator()
                ui.menu_item('Export SVG', export_svg)
                ui.menu_item('Export PNG', export_png)
                ui.separator()
                ui.menu_item('Place vehicles', add_vehicles)
                ui.menu_item('Clear vehicles', clear_vehicles)
                ui.separator()
                ui.menu_item('Clear layout', clear_all)

    with ui.element('div').classes('nf-dock p-2').style('left:12px; top:70px; width:190px'):
        ui.label('ELEMENTS').classes('nf-panelhead')
        for kind, piece in PIECES.items():
            with ui.row().classes('w-full items-center gap-2 rounded px-1 py-0.5 '
                                  'hover:bg-slate-100'):
                ui.html(f'<div class="nf-piece nf-piece-{kind}" draggable="true" '
                        f'data-kind="{kind}" title="{piece["label"]}" '
                        f'style="width:38px;height:38px;flex:none;cursor:grab;'
                        f'border:1px solid #eef2f7;border-radius:3px;background:#fff">'
                        f'{piece["thumb"]}</div>')
                with ui.column().classes('gap-0'):
                    ui.label(piece['label']).classes('text-[11px] text-slate-700 leading-tight')
                    ui.label(f'{piece["ports"]} · {piece["length"] * CELL_M:.2f} m') \
                        .classes('text-[10px] font-mono text-slate-400 leading-tight')
        ui.separator().classes('my-2')
        ui.label('HEADING').classes('nf-panelhead')
        palette_rotation = ui.toggle({0: '0°', 90: '90°', 180: '180°', 270: '270°'}, value=0) \
            .props('dense no-caps spread size=sm unelevated').classes('w-full nf-droprot')

    # one stacked sidebar rather than three fixed-position docks: the panels grow with their
    # content (the node list especially), so absolute tops would collide as a layout grows
    with ui.element('div').classes('nf-dock p-2 overflow-auto') \
            .style('right:50px; top:70px; bottom:34px; width:210px'):
        ui.label('OBJECTS').classes('nf-panelhead')
        objects = ui.column().classes('w-full gap-0 mt-1')
        ui.separator().classes('my-2')
        ui.label('PROPERTIES').classes('nf-panelhead')
        props = ui.column().classes('w-full gap-0 mt-1 nf-sel')
        ui.separator().classes('my-2')
        ui.label('VEHICLE').classes('nf-panelhead')
        telemetry = ui.column().classes('w-full gap-0 mt-1 nf-telemetry')
        ui.separator().classes('my-2')
        with ui.expansion('Log').classes('w-full text-[11px]'):
            log = ui.log(max_lines=10).classes('w-full h-20 text-[10px] nf-log')

    with ui.footer().classes('bg-white border-t border-slate-200 items-center gap-5 px-4 py-0.5'):
        status_count = ui.label('0 elements') \
            .classes('text-[11px] font-mono text-slate-600 nf-count')
        status_open = ui.label('0 open').classes('text-[11px] font-mono text-slate-600 nf-open')
        status_extent = ui.label().classes('text-[11px] font-mono text-slate-400')
        status_zoom = ui.label('100%').classes('text-[11px] font-mono text-slate-400 nf-zoom')
        ui.label(f'grid {CELL_M:.1f} m').classes('text-[11px] font-mono text-slate-400')
        ui.space()
        status_sync = ui.label('—').classes('text-[11px] font-mono text-slate-400 nf-sync')

    apply_layers()
    refresh()


if __name__ in {'__main__', '__mp_main__'}:
    ui.run(port=PORT, title='AGV route studio', show=False, reload=True)
