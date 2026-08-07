"""Demo 03 — an AGV track editor: drag pieces from a palette, snap them into a layout.

Demo 02 is a general drawing tool. This one is a *domain* editor built on the same element:
the canvas is a grid of 80 px cells, and every object on it is one track piece — straight,
90° curve, fork, crossing, charging/load/unload station, end stop. You drag a piece out of
the palette, it lands on the nearest cell, and the right drawer tells you which ends are
still open.

How the pieces work:

* **One object per piece.** Each piece is a small SVG rendered into a single Fabric ``Image``
  via a ``data:image/svg+xml`` URL — the flattened form demo 01 measured as the only
  single-object representation that survives ``to_dict()`` → ``load_json()``.
* **The piece kind rides on the object.** ``add_image(url, kind='curve')`` stores a custom
  prop, and ``load_json`` deep-copies unknown props, so ``kind`` survives a save/load round
  trip. That matters because ``load_json`` **re-ids every object**, so a side table keyed by
  object id would be destroyed by the first load — the metadata has to travel *on* the piece.
* **Ports are derived, never stored.** ``kind`` gives the base ports (compass directions) and
  Fabric's own ``angle`` gives the rotation; the connectivity pass rotates one by the other.
  Nothing can drift out of sync because there is only ever one copy of each fact.
* **Snapping is server-side.** Dropping emits the pointer position, and ``on_modified``
  reports the position after a drag; both get rounded to the cell grid and to 90°, then
  written back with ``update_object``. The browser is never the authority on where a piece is.

Run with::

    python demos/03_agv_tracks.py
"""
import asyncio
import base64
import json

from nicegui import app, ui

from nicefabric import FabricCanvas

PORT = 9092
TILE = 80
SLOT = 'nicefabric-demo-03'

RAIL = '#64748b'
DASH = '#f8fafc'

# Compass order matters: rotating a piece by 90° clockwise advances every port by one step.
DIRS = 'NESW'
DELTA = {'N': (0, -1), 'E': (1, 0), 'S': (0, 1), 'W': (-1, 0)}
OPPOSITE = {'N': 'S', 'S': 'N', 'E': 'W', 'W': 'E'}


def _svg(body: str, size: int = TILE) -> str:
    """Wrap piece artwork at any pixel size; the viewBox keeps the geometry in tile units."""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
            f'viewBox="0 0 {TILE} {TILE}">{body}</svg>')


def _station(colour: str, glyph: str) -> str:
    """A stub of track running in from the west edge, ending in a coloured station box."""
    return (f'<rect x="0" y="28" width="46" height="24" fill="{RAIL}"/>'
            f'<line x1="0" y1="40" x2="46" y2="40" stroke="{DASH}" stroke-width="2" '
            f'stroke-dasharray="8 6"/>'
            f'<rect x="30" y="16" width="46" height="48" rx="9" fill="{colour}"/>{glyph}')


_ARC = 'M 0 40 A 40 40 0 0 1 40 80'   # west edge to south edge, one quarter turn

_DASHED_H = (f'<line x1="0" y1="40" x2="80" y2="40" stroke="{DASH}" stroke-width="2" '
             f'stroke-dasharray="8 6"/>')

PIECES: dict[str, dict] = {
    'straight': {
        'label': 'Straight', 'ports': 'WE',
        'body': f'<rect x="0" y="28" width="80" height="24" fill="{RAIL}"/>{_DASHED_H}'},
    'curve': {
        'label': 'Curve 90°', 'ports': 'WS',
        'body': f'<path d="{_ARC}" fill="none" stroke="{RAIL}" stroke-width="24"/>'
                f'<path d="{_ARC}" fill="none" stroke="{DASH}" stroke-width="2" '
                f'stroke-dasharray="8 6"/>'},
    'fork': {
        'label': 'Fork (T)', 'ports': 'WES',
        'body': f'<rect x="0" y="28" width="80" height="24" fill="{RAIL}"/>'
                f'<rect x="28" y="40" width="24" height="40" fill="{RAIL}"/>{_DASHED_H}'
                f'<line x1="40" y1="40" x2="40" y2="80" stroke="{DASH}" stroke-width="2" '
                f'stroke-dasharray="8 6"/>'},
    'cross': {
        'label': 'Crossing', 'ports': 'NESW',
        'body': f'<rect x="0" y="28" width="80" height="24" fill="{RAIL}"/>'
                f'<rect x="28" y="0" width="24" height="80" fill="{RAIL}"/>{_DASHED_H}'
                f'<line x1="40" y1="0" x2="40" y2="80" stroke="{DASH}" stroke-width="2" '
                f'stroke-dasharray="8 6"/>'},
    'charge': {
        'label': 'Charging', 'ports': 'W',
        'body': _station('#16a34a',
                         '<polygon points="56,24 44,44 52,44 48,58 62,38 54,38" fill="#fff"/>')},
    # The station arrows run ALONG the track stub, not up/down the page: the whole tile
    # rotates with the piece, so an arrow across the stub would point somewhere arbitrary
    # once placed. Pointing along it, "into the track" still means load at every rotation.
    'load': {
        'label': 'Load', 'ports': 'W',
        'body': _station('#f59e0b',
                         '<polygon points="38,40 52,27 52,34 68,34 68,46 52,46 52,53" '
                         'fill="#fff"/>')},
    'unload': {
        'label': 'Unload', 'ports': 'W',
        'body': _station('#3b82f6',
                         '<polygon points="68,40 54,27 54,34 38,34 38,46 54,46 54,53" '
                         'fill="#fff"/>')},
    'end': {
        'label': 'End stop', 'ports': 'W',
        'body': _station('#ef4444', '<rect x="48" y="22" width="11" height="36" rx="3" '
                                    'fill="#fff"/>')},
}

for _piece in PIECES.values():
    _piece['svg'] = _svg(_piece['body'])            # full size, drawn on the canvas
    _piece['thumb'] = _svg(_piece['body'], 62)      # palette swatch
    _piece['url'] = ('data:image/svg+xml;base64,'
                     + base64.b64encode(_piece['svg'].encode()).decode())


def ports_of(kind: str, quarter: int) -> str:
    """Ports of ``kind`` after ``quarter`` 90° clockwise turns.

    Rotating clockwise moves N->E->S->W, which is one step along ``DIRS`` — so the whole
    rotation is an index shift, and no per-piece rotation table is needed.
    """
    base = PIECES[kind]['ports']
    return ''.join(DIRS[(DIRS.index(d) + quarter) % 4] for d in base)


def cell_of(left: float, top: float) -> tuple[int, int]:
    """Grid cell holding a piece centred at ``left``/``top`` (Fabric centres objects)."""
    return round((left - TILE / 2) / TILE), round((top - TILE / 2) / TILE)


def centre_of(cell: tuple[int, int]) -> tuple[float, float]:
    return cell[0] * TILE + TILE / 2, cell[1] * TILE + TILE / 2


@ui.page('/')  # per-visit page: a module-level canvas would be shared by ALL tabs and users
def index() -> None:
    size = {'w': 1200, 'h': 700}

    def on_modified(e) -> None:
        # the browser moved a piece; the grid, not the pointer, decides where it lands
        snap(e.args['id'], e.args['props'])
        refresh()

    canvas = FabricCanvas(
        width=size['w'], height=size['h'], background='', keyboard_delete=True,
        on_selection=lambda e: refresh(),
        on_modified=on_modified,
        on_error=lambda e: log.push(f'ERROR {e.args}')) \
        .classes('nf-canvas') \
        .style('background-image:'
               'linear-gradient(to right, #e2e8f0 1px, transparent 1px),'
               'linear-gradient(to bottom, #e2e8f0 1px, transparent 1px);'
               f'background-size: {TILE}px {TILE}px; background-color: #ffffff')

    def pieces() -> list[dict]:
        """Registry entries that are track pieces, newest last."""
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
        for existing in pieces():                     # one piece per cell: replace, not stack
            if cell_of(existing['left'], existing['top']) == cell:
                canvas.remove_object(existing['id'])
        left, top = centre_of(cell)
        canvas.add_image(PIECES[kind]['url'], left=left, top=top, angle=quarter * 90,
                         kind=kind)                   # custom prop survives load_json
        log.push(f'placed {kind} at {cell}')

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
        # HTML5 drag and drop: palette tiles are the drag sources, the canvas wrapper is the
        # drop target. emitEvent is NiceGUI's browser->Python channel.
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
        # whole tiles only, so the CSS grid never ends on a half cell
        size['w'], size['h'] = (dims[0] // TILE) * TILE, (dims[1] // TILE) * TILE
        canvas.resize(size['w'], size['h'])
        status_grid.text = f'{size["w"] // TILE} x {size["h"] // TILE} cells'

    ui.timer(0, wire_dnd, once=True)
    ui.on('nf_resize', fit_canvas)

    # ------------------------------------------------------------------ connectivity ------
    def analyse() -> dict:
        """Walk the placed pieces and pair up ports with their neighbours.

        Ports are recomputed from ``kind`` + ``angle`` every time rather than cached, so a
        piece that was dragged, rotated or reloaded cannot disagree with its own metadata.
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
        return {'grid': grid, 'links': links // 2,   # counted once from each side
                'open': open_ends, 'stacked': stacked}

    # ------------------------------------------------------------------ file ops ----------
    def save() -> None:
        app.storage.general[SLOT] = canvas.to_dict()
        ui.notify(f'saved {len(pieces())} pieces')

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
        ui.notify(f'loaded {len(pieces())} pieces')

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
        ui.notify(f'imported {len(pieces())} pieces')

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
        # rotate on R, but never while the user is typing into an input
        if e.action.keydown and not e.action.repeat and str(e.key).lower() == 'r':
            rotate(1)

    ui.keyboard(on_key=on_key, ignore=['input', 'select', 'textarea'])

    # ------------------------------------------------------------------ panels ------------
    def refresh() -> None:
        report = analyse()
        selection = [o for o in pieces()
                     if o['id'] in {s.id for s in canvas.get_selected()}]

        status_pieces.text = f'{len(report["grid"])} pieces'
        status_open.text = f'{len(report["open"])} open ends'

        summary.clear()
        with summary:
            ui.label(f'{len(report["grid"])} pieces · {report["links"]} connections') \
                .classes('text-sm font-bold')
            if report['stacked']:
                ui.label(f'{report["stacked"]} overlapping piece(s)') \
                    .classes('text-xs text-red-600')
            if not report['open']:
                ui.label('every end is connected' if report['grid'] else 'empty layout') \
                    .classes('text-xs text-green-700')
            else:
                ui.label(f'{len(report["open"])} open end(s)').classes('text-xs text-amber-700')
                with ui.column().classes('gap-0 max-h-40 overflow-auto'):
                    for cell, port in report['open'][:40]:
                        ui.label(f'({cell[0]}, {cell[1]}) {port}') \
                            .classes('text-xs font-mono text-gray-600')

        sel_box.clear()
        with sel_box:
            if not selection:
                ui.label('nothing selected').classes('text-xs text-gray-500')
                return
            if len(selection) == 1:
                entry = selection[0]
                cell = cell_of(entry['left'], entry['top'])
                quarter = round((entry.get('angle', 0) % 360) / 90) % 4
                ui.label(PIECES[entry['kind']]['label']).classes('text-sm font-bold')
                ui.label(f'cell ({cell[0]}, {cell[1]}) · {quarter * 90}° · '
                         f'ports {ports_of(entry["kind"], quarter)}') \
                    .classes('text-xs font-mono text-gray-600 nf-selinfo')
            else:
                ui.label(f'{len(selection)} pieces selected').classes('text-sm font-bold')
            with ui.grid(columns=2).classes('w-full gap-1 mt-1'):
                ui.button('Rotate', icon='rotate_right', on_click=lambda: rotate(1)) \
                    .props('dense outline no-caps').classes('nf-rotate')
                ui.button('Back', icon='rotate_left', on_click=lambda: rotate(-1)) \
                    .props('dense outline no-caps')
                ui.button('Delete', icon='delete', on_click=delete_selected) \
                    .props('dense outline no-caps color=red').classes('nf-delete')
                ui.button('Deselect', icon='deselect', on_click=canvas.discard_selection) \
                    .props('dense outline no-caps')

    # ================================================================== layout ============
    ui.query('.nicegui-content').classes('p-0 gap-0')

    with ui.dialog() as json_dialog, ui.card().classes('w-96'):
        ui.label('Import layout JSON').classes('font-bold')
        json_uploader = ui.upload(label='agv-layout.json', auto_upload=True,
                                  max_file_size=2_000_000, on_upload=import_json) \
            .props('accept=".json,application/json" flat dense no-thumbnails').classes('w-full')

    with ui.header().classes('bg-slate-900 items-center gap-2 px-3 py-1'):
        ui.button(icon='menu', on_click=lambda: left.toggle()).props('flat dense color=white')
        ui.label('AGV track editor').classes('text-base font-bold')
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
                ui.menu_item('Clear layout', clear_all)
        ui.button(icon='tune', on_click=lambda: right.toggle()).props('flat dense color=white')

    with ui.left_drawer(value=True, bordered=True).props('width=250').classes('p-3') as left:
        ui.label('Palette').classes('text-xs font-bold text-gray-500 uppercase')
        ui.label('Drag a piece onto the grid.').classes('text-xs text-gray-500')
        with ui.grid(columns=2).classes('w-full gap-2 mt-1'):
            for kind, piece in PIECES.items():
                with ui.column().classes('items-center gap-0'):
                    ui.html(f'<div class="nf-piece nf-piece-{kind}" draggable="true" '
                            f'data-kind="{kind}" title="{piece["label"]}" '
                            f'style="width:64px;height:64px;cursor:grab;'
                            f'border:1px solid #cbd5e1;border-radius:6px;background:#fff">'
                            f'{piece["thumb"]}</div>')
                    ui.label(piece['label']).classes('text-xs text-gray-600')

        ui.separator().classes('my-2')
        ui.label('Drop rotation').classes('text-xs font-bold text-gray-500 uppercase')
        palette_rotation = ui.toggle({0: '0°', 90: '90°', 180: '180°', 270: '270°'}, value=0) \
            .props('dense no-caps spread').classes('w-full nf-droprot')
        ui.label('Pieces snap to the cell grid. Press R to rotate the selection.') \
            .classes('text-xs text-gray-500 mt-2')

    with ui.right_drawer(value=True, bordered=True).props('width=270').classes('p-3') as right:
        ui.label('Track').classes('text-xs font-bold text-gray-500 uppercase')
        summary = ui.column().classes('w-full gap-1')

        ui.separator().classes('my-2')
        ui.label('Selection').classes('text-xs font-bold text-gray-500 uppercase')
        sel_box = ui.column().classes('w-full gap-1')

        with ui.expansion('Event log').classes('w-full mt-2'):
            log = ui.log(max_lines=12).classes('w-full h-36 text-xs nf-log')

    with ui.footer().classes('bg-slate-900 text-white items-center gap-4 px-3 py-1 text-xs'):
        status_pieces = ui.label('0 pieces').classes('font-mono nf-count')
        status_open = ui.label('0 open ends').classes('font-mono nf-open')
        status_grid = ui.label().classes('font-mono')
        ui.space()
        ui.label('drag from the palette · R rotates · Delete removes') \
            .classes('text-gray-400')

    refresh()


if __name__ in {'__main__', '__mp_main__'}:
    ui.run(port=PORT, title='NiceFabric 03 — AGV tracks', show=False, reload=True)
