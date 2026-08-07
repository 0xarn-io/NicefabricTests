"""Demo 04 — a P&ID editor that costs itself: every symbol carries a SKU, and the bill of
materials builds itself from the drawing.

Demo 03 snaps track pieces onto a grid. This one is a drafting tool: drag ISA symbols out of
a catalogue, draw pipe runs by clicking two points, and watch the BOM underneath aggregate by
part number — equipment and valves by count, pipe and signal cable **by length**, with an
extended price and a total.

The parts worth reading:

* **The catalogue is the source of truth.** ``CATALOG`` holds one entry per symbol: artwork,
  ISA tag prefix, SKU, description and unit price. Placing a symbol stores only its ``kind``;
  everything else is looked up, so a price change is a one-line edit and cannot desync from
  what is on the drawing.
* **Pipe is drawn with ``on_mouse_down``.** In pipe mode the first press records a corner and
  the second draws an orthogonal ``Polyline`` between them. The event reports **scene**
  coordinates, so no screen-to-canvas conversion is needed — but the pointer is still divided
  back through the zoom before snapping.
* **A Polyline's ``left``/``top`` is its bounding-box centre.** Points are passed relative to
  the box's top-left corner; measured against painted pixels, a run asked for at
  (150,120)→(600,380) lands on exactly that.
* **Tags are positional.** Symbols are sorted and numbered per prefix, so a drawing keeps its
  tag numbers even though ``load_json`` regenerates every underlying object id on load. Tag
  captions are a derived ``Text`` layer at ``angle=0``, tagged ``kind='label'`` so the BOM
  ignores them.
* **Line items are counted, never stored.** Quantities come from walking the registry each
  time, so a deleted valve leaves the BOM immediately and nothing can drift.

Run with::

    python demos/04_pid_bom.py
"""
import asyncio
import base64
import csv
import io
import math

from nicegui import app, ui

from nicefabric import FabricCanvas

PORT = 9093
SNAP = 20            # px placement grid
PX_PER_M = 80        # drawing scale, shared with the other demos
SLOT = 'nicefabric-demo-04'

INK = '#111827'
GRID_DOT = '#dfe5ec'
SIGNAL = '#6d5bd0'
PIPE = '#1f2937'
TAG_COLOUR = '#1d4ed8'


def _sym(w: int, h: int, body: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}">{body}</svg>')


def _bowtie(extra: str = '', w: int = 46, cy: int = 13) -> str:
    """The ISA valve body: two triangles meeting at the stem, with line stubs either side."""
    return (f'<path d="M3 3 L3 {cy * 2 - 3} L{w / 2:.0f} {cy} Z" fill="none" stroke="{INK}" '
            f'stroke-width="1.6" stroke-linejoin="round"/>'
            f'<path d="M{w - 3} 3 L{w - 3} {cy * 2 - 3} L{w / 2:.0f} {cy} Z" fill="none" '
            f'stroke="{INK}" stroke-width="1.6" stroke-linejoin="round"/>'
            f'<path d="M0 {cy} L3 {cy} M{w - 3} {cy} L{w} {cy}" stroke="{INK}" '
            f'stroke-width="1.6"/>{extra}')


CATALOG: dict[str, dict] = {
    'vessel': {
        'label': 'Reactor vessel', 'cat': 'Equipment', 'tag': 'V',
        'sku': 'EQ-VES-5000', 'desc': 'Jacketed reactor, 5 m3, 316L', 'price': 48500.0,
        'art': _sym(130, 164,
                    f'<path d="M8 26 Q8 6 65 6 Q122 6 122 26 L122 132 Q122 158 65 158 '
                    f'Q8 158 8 132 Z" fill="#ffffff" stroke="{INK}" stroke-width="1.8"/>'
                    f'<path d="M65 12 L65 122" stroke="{INK}" stroke-width="1.1" '
                    f'stroke-dasharray="5 4"/>'
                    f'<ellipse cx="65" cy="126" rx="26" ry="7" fill="none" stroke="{INK}" '
                    f'stroke-width="1.4"/>'
                    f'<rect x="56" y="0" width="18" height="8" fill="none" stroke="{INK}" '
                    f'stroke-width="1.4"/>')},
    'pump': {
        'label': 'Centrifugal pump', 'cat': 'Equipment', 'tag': 'P',
        'sku': 'EQ-PMP-2100', 'desc': 'Centrifugal pump, 15 m3/h, 316L', 'price': 7250.0,
        'art': _sym(64, 64,
                    f'<circle cx="32" cy="28" r="22" fill="#ffffff" stroke="{INK}" '
                    f'stroke-width="1.7"/>'
                    f'<path d="M32 28 L52 16 L52 40 Z" fill="none" stroke="{INK}" '
                    f'stroke-width="1.4"/>'
                    f'<path d="M12 50 L52 50 M14 50 L14 58 M50 50 L50 58" stroke="{INK}" '
                    f'stroke-width="1.6"/>')},
    'hex': {
        'label': 'Heat exchanger', 'cat': 'Equipment', 'tag': 'E',
        'sku': 'EQ-HEX-3300', 'desc': 'Shell & tube exchanger, 12 m2', 'price': 13400.0,
        'art': _sym(112, 64,
                    f'<rect x="4" y="10" width="104" height="44" rx="4" fill="#ffffff" '
                    f'stroke="{INK}" stroke-width="1.7"/>'
                    f'<path d="M12 20 L32 20 L32 44 L52 44 L52 20 L72 20 L72 44 L92 44 '
                    f'L100 44" fill="none" stroke="{INK}" stroke-width="1.5"/>')},
    'gate': {
        'label': 'Gate valve', 'cat': 'Valves', 'tag': 'HV',
        'sku': 'VLV-GAT-0050', 'desc': 'Gate valve, DN50 PN16, CF8M', 'price': 310.0,
        'art': _sym(46, 26, _bowtie())},
    'globe': {
        'label': 'Globe valve', 'cat': 'Valves', 'tag': 'HV',
        'sku': 'VLV-GLB-0050', 'desc': 'Globe valve, DN50 PN16', 'price': 395.0,
        'art': _sym(46, 26, _bowtie(f'<circle cx="23" cy="13" r="5" fill="{INK}"/>'))},
    'ball': {
        'label': 'Ball valve', 'cat': 'Valves', 'tag': 'HV',
        'sku': 'VLV-BAL-0050', 'desc': 'Ball valve, DN50 PN16, full bore', 'price': 265.0,
        'art': _sym(46, 26, _bowtie(f'<circle cx="23" cy="13" r="5" fill="#ffffff" '
                                    f'stroke="{INK}" stroke-width="1.5"/>'))},
    'check': {
        'label': 'Check valve', 'cat': 'Valves', 'tag': 'NRV',
        'sku': 'VLV-CHK-0050', 'desc': 'Swing check valve, DN50 PN16', 'price': 240.0,
        'art': _sym(46, 26, _bowtie(f'<path d="M23 13 L34 5" stroke="{INK}" '
                                    f'stroke-width="1.6"/>'))},
    'control': {
        'label': 'Control valve', 'cat': 'Valves', 'tag': 'FCV',
        'sku': 'VLV-CTL-0080', 'desc': 'Globe control valve DN80 + diaphragm actuator',
        'price': 4180.0,
        'art': _sym(46, 56,
                    f'<g transform="translate(0 30)">{_bowtie()}</g>'
                    f'<path d="M23 30 L23 18" stroke="{INK}" stroke-width="1.6"/>'
                    f'<path d="M9 18 L37 18 A14 10 0 0 0 9 18 Z" fill="#ffffff" '
                    f'stroke="{INK}" stroke-width="1.6"/>')},
    'field': {
        'label': 'Field instrument', 'cat': 'Instruments', 'tag': 'TT',
        'sku': 'INS-XMT-0100', 'desc': '2-wire field transmitter, 4-20 mA HART',
        'price': 1150.0,
        'art': _sym(44, 44,
                    f'<circle cx="22" cy="22" r="20" fill="#ffffff" stroke="{INK}" '
                    f'stroke-width="1.6"/>'
                    f'<path d="M2 22 L42 22" stroke="{INK}" stroke-width="1.1"/>')},
    'dcs': {
        'label': 'DCS function', 'cat': 'Instruments', 'tag': 'TIC',
        'sku': 'INS-DCS-0200', 'desc': 'DCS shared display / control point', 'price': 480.0,
        'art': _sym(46, 46,
                    f'<rect x="2" y="2" width="42" height="42" fill="#ffffff" stroke="{INK}" '
                    f'stroke-width="1.6"/>'
                    f'<circle cx="23" cy="23" r="19" fill="none" stroke="{INK}" '
                    f'stroke-width="1.6"/>'
                    f'<path d="M4 23 L42 23" stroke="{INK}" stroke-width="1.1"/>')},
    'plc': {
        'label': 'PLC logic', 'cat': 'Instruments', 'tag': 'UY',
        'sku': 'INS-PLC-0300', 'desc': 'PLC interlock / logic function block', 'price': 320.0,
        'art': _sym(46, 46,
                    f'<rect x="2" y="2" width="42" height="42" fill="#ffffff" stroke="{INK}" '
                    f'stroke-width="1.6"/>'
                    f'<path d="M23 5 L41 23 L23 41 L5 23 Z" fill="none" stroke="{INK}" '
                    f'stroke-width="1.6"/>'
                    f'<path d="M8 23 L38 23" stroke="{INK}" stroke-width="1.1"/>')},
    'flange': {
        'label': 'Flange pair', 'cat': 'Fittings', 'tag': '',
        'sku': 'FIT-FLG-0050', 'desc': 'Weld-neck flange pair DN50 PN16 + gasket set',
        'price': 86.0,
        'art': _sym(26, 26,
                    f'<path d="M0 13 L26 13 M10 3 L10 23 M16 3 L16 23" stroke="{INK}" '
                    f'stroke-width="1.7"/>')},
    'reducer': {
        'label': 'Reducer', 'cat': 'Fittings', 'tag': '',
        'sku': 'FIT-RED-5040', 'desc': 'Concentric reducer DN50 x DN40', 'price': 54.0,
        'art': _sym(40, 26,
                    f'<path d="M6 4 L34 9 L34 17 L6 22 Z" fill="none" stroke="{INK}" '
                    f'stroke-width="1.6" stroke-linejoin="round"/>'
                    f'<path d="M0 13 L6 13 M34 13 L40 13" stroke="{INK}" '
                    f'stroke-width="1.6"/>')},
}

# bulk items: priced per metre off the drawn length rather than per placed object
BULK = {
    'pipe': {'sku': 'PIP-CS-0050', 'desc': 'Pipe, CS A106-B DN50 sch40 (per m)', 'price': 38.0},
    'signal': {'sku': 'CBL-SIG-0002', 'desc': 'Instrument cable, 1pr 1.5mm2 shielded (per m)',
               'price': 6.4},
}

for _entry in CATALOG.values():
    _entry['url'] = ('data:image/svg+xml;base64,'
                     + base64.b64encode(_entry['art'].encode()).decode())

PLACED = {'lockScalingX': True, 'lockScalingY': True, 'borderColor': TAG_COLOUR,
          'cornerColor': '#ffffff', 'cornerStrokeColor': TAG_COLOUR,
          'transparentCorners': False, 'cornerSize': 7, 'padding': 3}
LABEL_PROPS = {'kind': 'label', 'selectable': False, 'evented': False, 'fontSize': 11,
               'fontFamily': 'Inter, Arial, sans-serif', 'fill': TAG_COLOUR}


def run_length(points: list[dict]) -> float:
    """Metres of run described by a polyline's points."""
    total = 0.0
    for a, b in zip(points, points[1:]):
        total += math.hypot(b['x'] - a['x'], b['y'] - a['y'])
    return total / PX_PER_M


@ui.page('/')  # per-visit page: a module-level canvas would be shared by ALL tabs and users
def index() -> None:
    state = {'tool': 'select', 'pending': None, 'labels': '', 'zoom': 1.0}

    def on_mouse_down(e) -> None:
        """Pipe and signal runs are drawn here — the event carries scene coordinates."""
        if state['tool'] == 'select':
            return
        x = round(e.args['x'] / SNAP) * SNAP
        y = round(e.args['y'] / SNAP) * SNAP
        if state['pending'] is None:
            state['pending'] = (x, y)
            status_hint.text = f'from {x / PX_PER_M:.2f}, {y / PX_PER_M:.2f} — click the end'
            return
        draw_run(state['pending'], (x, y), state['tool'])
        state['pending'] = None
        status_hint.text = 'click the start of the next run'
        refresh()

    def draw_run(start: tuple[float, float], end: tuple[float, float], kind: str) -> None:
        """An orthogonal run: across, then down. Points are relative to the bounding box, and
        left/top is that box's centre — which is what Fabric means by a Polyline's position."""
        (x1, y1), (x2, y2) = start, end
        if (x1, y1) == (x2, y2):
            return
        mid = (x1 + x2) / 2
        pts = [(x1, y1), (mid, y1), (mid, y2), (x2, y2)]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        rel = [{'x': p[0] - min(xs), 'y': p[1] - min(ys)} for p in pts]
        style = ({'stroke': PIPE, 'strokeWidth': 3}
                 if kind == 'pipe' else
                 {'stroke': SIGNAL, 'strokeWidth': 1.4, 'strokeDashArray': [7, 5]})
        canvas.add_polyline(rel, left=(min(xs) + max(xs)) / 2, top=(min(ys) + max(ys)) / 2,
                            fill='', kind=kind, strokeUniform=True, **PLACED, **style)
        log.push(f'{kind:<6} {run_length(rel):.2f} m')

    canvas = FabricCanvas(width=1200, height=620, background='', keyboard_delete=True,
                          on_selection=lambda e: refresh(),
                          on_modified=lambda e: (snap(e.args['id'], e.args['props']), refresh()),
                          on_mouse_down=on_mouse_down,
                          on_error=lambda e: log.push(f'ERROR {e.args}'))

    def symbols() -> list[dict]:
        return [o for o in canvas.to_dict()['objects'] if o.get('kind') in CATALOG]

    def runs(kind: str) -> list[dict]:
        return [o for o in canvas.to_dict()['objects'] if o.get('kind') == kind]

    def snap(id_: str, props: dict) -> None:
        try:
            canvas.update_object(id_, left=round(props.get('left', 0) / SNAP) * SNAP,
                                 top=round(props.get('top', 0) / SNAP) * SNAP)
        except KeyError:
            pass

    # ------------------------------------------------------------------ tags & labels ----
    def tag_table() -> dict[str, str]:
        """id -> ISA tag. Numbering is positional, so tags survive load_json's re-iding."""
        counters: dict[str, int] = {}
        out: dict[str, str] = {}
        for entry in sorted(symbols(), key=lambda o: (o['top'], o['left'])):
            prefix = CATALOG[entry['kind']]['tag']
            if not prefix:
                continue
            counters[prefix] = counters.get(prefix, 0) + 1
            out[entry['id']] = f'{prefix}-{100 + counters[prefix]}'
        return out

    def sync_labels() -> None:
        tags = tag_table()
        wanted = [(o['left'], o['top'] + 30, tags[o['id']])
                  for o in symbols() if o['id'] in tags]
        signature = repr(sorted(wanted))
        if signature == state['labels']:
            return
        state['labels'] = signature
        for old in canvas.to_dict()['objects']:
            if old.get('kind') == 'label':
                canvas.remove_object(old['id'])
        for left, top, text in wanted:
            canvas.add_text(text, left=left, top=top, width=120, textAlign='center',
                            **LABEL_PROPS)

    # ------------------------------------------------------------------ bill of materials -
    def bom_rows() -> list[dict]:
        """Aggregate the drawing into line items — counted fresh every time, never stored."""
        counts: dict[str, int] = {}
        for entry in symbols():
            counts[entry['kind']] = counts.get(entry['kind'], 0) + 1
        rows = []
        for kind, qty in sorted(counts.items(), key=lambda kv: CATALOG[kv[0]]['sku']):
            part = CATALOG[kind]
            rows.append({'sku': part['sku'], 'desc': part['desc'], 'qty': f'{qty}',
                         'unit': f'{part["price"]:,.2f}',
                         'ext': f'{qty * part["price"]:,.2f}',
                         '_ext': qty * part['price']})
        for kind, part in BULK.items():
            metres = sum(run_length(o.get('points', [])) for o in runs(kind))
            if metres <= 0:
                continue
            rows.append({'sku': part['sku'], 'desc': part['desc'], 'qty': f'{metres:.2f} m',
                         'unit': f'{part["price"]:,.2f}',
                         'ext': f'{metres * part["price"]:,.2f}',
                         '_ext': metres * part['price']})
        return rows

    def export_bom() -> None:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(['SKU', 'Description', 'Qty', 'Unit price', 'Extended'])
        for row in bom_rows():
            writer.writerow([row['sku'], row['desc'], row['qty'], row['unit'], row['ext']])
        writer.writerow([])
        writer.writerow(['', '', '', 'TOTAL', f'{sum(r["_ext"] for r in bom_rows()):,.2f}'])
        ui.download(buffer.getvalue().encode(), 'bom.csv')

    # ------------------------------------------------------------------ placement --------
    def on_drop(e) -> None:
        kind = e.args.get('kind')
        if kind not in CATALOG:
            return
        z = state['zoom']
        left = round(e.args['x'] / z / SNAP) * SNAP
        top = round(e.args['y'] / z / SNAP) * SNAP
        canvas.add_image(CATALOG[kind]['url'], left=left, top=top, kind=kind, **PLACED)
        log.push(f'{CATALOG[kind]["sku"]}  {left / PX_PER_M:.2f}, {top / PX_PER_M:.2f}')
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
                const x = (ev.clientX - r.left) / window.__nfZoom / {PX_PER_M};
                const y = (ev.clientY - r.top) / window.__nfZoom / {PX_PER_M};
                el.textContent = 'x ' + x.toFixed(2) + '  y ' + y.toFixed(2) + ' m';
            }});
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
        if not dims or dims[0] < 200 or dims[1] < 200:
            return
        canvas.resize(*dims)
        status_sheet.text = f'{dims[0] / PX_PER_M:.1f} x {dims[1] / PX_PER_M:.1f} m'

    ui.timer(0, wire_client, once=True)
    ui.on('nf_resize', fit_canvas)

    # ------------------------------------------------------------------ file ops ---------
    def save() -> None:
        app.storage.general[SLOT] = canvas.to_dict()
        ui.notify(f'saved — {len(symbols())} symbols')

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
        state['labels'] = ''    # ids changed on load; force the caption layer to rebuild
        refresh()
        ui.notify(f'loaded {len(symbols())} symbols')

    def export_json() -> None:
        ui.download(canvas.to_json().encode(), 'pid.json')

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
        ui.notify(f'imported {len(symbols())} symbols')

    async def export_svg() -> None:
        try:
            svg = await canvas.to_svg()
        except (RuntimeError, asyncio.TimeoutError) as err:
            ui.notify(f'SVG export failed: {err}', type='negative')
            return
        ui.download(svg.encode(), 'pid.svg')          # a download, never re-inlined

    async def export_png() -> None:
        try:
            data_url = await canvas.to_data_url()
        except (RuntimeError, asyncio.TimeoutError) as err:
            ui.notify(f'PNG export failed: {err}', type='negative')
            return
        ui.download(base64.b64decode(data_url.split(',', 1)[1]), 'pid.png')

    def clear_all() -> None:
        canvas.clear_objects()
        state['labels'] = ''
        state['pending'] = None
        refresh()

    def delete_selected() -> None:
        canvas.remove_selected()
        refresh()

    def set_tool(tool: str) -> None:
        state['tool'] = tool
        state['pending'] = None
        status_hint.text = ('drag a symbol onto the sheet' if tool == 'select'
                            else f'click the start of a {tool} run')

    # ------------------------------------------------------------------ panels -----------
    def prop_row(name: str, value: str, unit: str = '') -> None:
        with ui.row().classes('w-full items-baseline gap-1 py-px'):
            ui.label(name).classes('text-[11px] text-slate-500 flex-1')
            ui.label(value).classes('text-[11px] font-mono text-slate-800 text-right')
            ui.label(unit).classes('text-[10px] text-slate-400 w-6 text-right')

    def refresh() -> None:
        sync_labels()
        rows = bom_rows()
        total = sum(r['_ext'] for r in rows)
        tags = tag_table()
        chosen = {s.id for s in canvas.get_selected()}
        picked = [o for o in canvas.to_dict()['objects']
                  if o['id'] in chosen and o.get('kind') != 'label']

        bom_table.rows = [{k: v for k, v in r.items() if not k.startswith('_')} for r in rows]
        bom_table.update()
        bom_total.text = f'{total:,.2f}'
        status_symbols.text = f'{len(symbols())} symbols'
        status_pipe.text = (f'{sum(run_length(o.get("points", [])) for o in runs("pipe")):.2f} m '
                            f'pipe')
        status_total.text = f'BOM {total:,.2f}'

        props.clear()
        with props:
            if not picked:
                prop_row('symbols', str(len(symbols())))
                prop_row('line items', str(len(rows)))
                prop_row('pipe', f'{sum(run_length(o.get("points", [])) for o in runs("pipe")):.2f}', 'm')
                prop_row('signal', f'{sum(run_length(o.get("points", [])) for o in runs("signal")):.2f}', 'm')
                return
            if len(picked) > 1:
                prop_row('selected', str(len(picked)))
                return
            entry = picked[0]
            kind = entry.get('kind')
            if kind in BULK:
                prop_row('type', kind.capitalize() + ' run')
                prop_row('sku', BULK[kind]['sku'])
                prop_row('length', f'{run_length(entry.get("points", [])):.2f}', 'm')
                prop_row('cost', f'{run_length(entry.get("points", [])) * BULK[kind]["price"]:,.2f}')
                return
            part = CATALOG[kind]
            prop_row('tag', tags.get(entry['id'], '—'))
            prop_row('type', part['label'])
            prop_row('sku', part['sku'])
            prop_row('x', f'{entry["left"] / PX_PER_M:.2f}', 'm')
            prop_row('y', f'{entry["top"] / PX_PER_M:.2f}', 'm')
            prop_row('unit price', f'{part["price"]:,.2f}')
            ui.label(part['desc']).classes('text-[10px] text-slate-500 mt-1 leading-snug')
            with ui.grid(columns=2).classes('w-full gap-1 mt-2'):
                ui.button('Delete', on_click=delete_selected) \
                    .props('dense flat no-caps size=sm color=red').classes('nf-delete')
                ui.button('Deselect', on_click=canvas.discard_selection) \
                    .props('dense flat no-caps size=sm color=grey-7')

    # ================================================================== layout ===========
    ui.query('.nicegui-content').classes('p-0 gap-0 h-screen')
    ui.query('.q-page').classes('h-full flex flex-col')
    ui.add_css(f'''
        .nf-sheet {{ background-color:#ffffff;
            background-image:radial-gradient({GRID_DOT} 1px, transparent 1px);
            background-size:{SNAP}px {SNAP}px; }}
        .nf-dock {{ position:absolute; z-index:10; background:#fff; border:1px solid #dde5ee;
            border-radius:5px; box-shadow:0 4px 14px rgba(15,23,42,.08); }}
        .nf-panelhead {{ font:600 10px system-ui; letter-spacing:.09em; color:#64748b;
            text-transform:uppercase; }}
    ''')

    with ui.dialog() as json_dialog, ui.card().classes('w-96'):
        ui.label('Import drawing JSON').classes('text-sm font-medium')
        json_uploader = ui.upload(label='pid.json', auto_upload=True, max_file_size=2_000_000,
                                  on_upload=import_json) \
            .props('accept=".json,application/json" flat dense no-thumbnails').classes('w-full')

    with ui.header().classes('items-center gap-0 px-4 py-0').style('background:#1b2431'):
        ui.label('P&ID').classes('text-sm font-semibold text-white tracking-wide mr-1')
        ui.label('studio').classes('text-[11px] font-light text-slate-400 mr-6')
        for tab in ('Diagram', 'Bill of materials', 'Datasheets', 'Revisions'):
            ui.label(tab).classes('text-[12px] px-3 py-3 cursor-pointer ' + (
                'text-white border-b-2 border-cyan-400' if tab == 'Diagram'
                else 'text-slate-400 hover:text-slate-200'))
        ui.space()
        ui.button('EXPORT BOM', icon='download', on_click=export_bom) \
            .props('dense unelevated no-caps size=sm').style('background:#16a34a;color:#fff') \
            .classes('nf-exportbom')
        with ui.button(icon='folder').props('flat dense size=sm color=grey-5'):
            with ui.menu():
                ui.menu_item('Save drawing', save)
                ui.menu_item('Load drawing', load)
                ui.separator()
                ui.menu_item('Export JSON', export_json)
                ui.menu_item('Import JSON…', json_dialog.open)
                ui.separator()
                ui.menu_item('Export SVG', export_svg)
                ui.menu_item('Export PNG', export_png)
                ui.separator()
                ui.menu_item('Clear sheet', clear_all)

    # drawing sheet on top, bill of materials underneath — the usual drafting split
    with ui.element('div').classes('relative w-full flex-1 min-h-0 nf-stage') as stage:
        # the canvas is built above (the handlers below close over it), so it has to be moved
        # into the stage rather than merely styled — `absolute inset-0` needs this ancestor
        canvas.move(stage)
        canvas.classes('nf-canvas nf-sheet absolute inset-0')

        with ui.element('div').classes('nf-dock p-2').style('left:12px; top:12px; width:186px'):
            ui.label('TOOL').classes('nf-panelhead')
            ui.toggle({'select': 'Select', 'pipe': 'Pipe', 'signal': 'Signal'}, value='select',
                      on_change=lambda e: set_tool(e.value)) \
                .props('dense no-caps spread size=sm unelevated').classes('w-full nf-tool mb-2')
            ui.label('CATALOGUE').classes('nf-panelhead')
            with ui.column().classes('w-full gap-0 max-h-[420px] overflow-auto'):
                for group in ('Equipment', 'Valves', 'Instruments', 'Fittings'):
                    ui.label(group).classes('text-[10px] text-slate-400 mt-1')
                    for kind, part in CATALOG.items():
                        if part['cat'] != group:
                            continue
                        with ui.row().classes('w-full items-center gap-2 rounded px-1 '
                                              'hover:bg-slate-100'):
                            ui.html(f'<div class="nf-piece nf-piece-{kind}" draggable="true" '
                                    f'data-kind="{kind}" title="{part["desc"]}" '
                                    f'style="width:34px;height:34px;flex:none;cursor:grab;'
                                    f'display:flex;align-items:center;justify-content:center">'
                                    f'{part["art"]}</div>')
                            with ui.column().classes('gap-0'):
                                ui.label(part['label']) \
                                    .classes('text-[11px] text-slate-700 leading-tight')
                                ui.label(part['sku']) \
                                    .classes('text-[10px] font-mono text-slate-400 leading-tight')

        with ui.element('div').classes('nf-dock p-2').style('right:12px; top:12px; width:198px'):
            ui.label('PROPERTIES').classes('nf-panelhead')
            props = ui.column().classes('w-full gap-0 mt-1 nf-props')
            ui.separator().classes('my-2')
            with ui.expansion('Log').classes('w-full text-[11px]'):
                log = ui.log(max_lines=10).classes('w-full h-20 text-[10px] nf-log')

        with ui.element('div').classes('absolute z-10 flex items-center gap-1 bg-white '
                                       'border border-slate-300 rounded px-2 py-0.5') \
                .style('right:224px; top:14px'):
            ui.icon('my_location', size='12px').classes('text-slate-400')
            ui.label('—').classes('text-[11px] font-mono text-slate-700 nf-cursor')

    with ui.element('div').classes('w-full bg-white border-t border-slate-200 px-3 py-1') \
            .style('height:252px'):
        with ui.row().classes('w-full items-baseline gap-3'):
            ui.label('BILL OF MATERIALS').classes('nf-panelhead')
            ui.space()
            ui.label('TOTAL').classes('text-[10px] tracking-widest text-slate-400')
            bom_total = ui.label('0.00').classes('text-sm font-mono font-semibold '
                                                 'text-slate-900 nf-total')
        bom_table = ui.table(
            columns=[{'name': 'sku', 'label': 'SKU', 'field': 'sku', 'align': 'left'},
                     {'name': 'desc', 'label': 'Description', 'field': 'desc', 'align': 'left'},
                     {'name': 'qty', 'label': 'Qty', 'field': 'qty', 'align': 'right'},
                     {'name': 'unit', 'label': 'Unit', 'field': 'unit', 'align': 'right'},
                     {'name': 'ext', 'label': 'Extended', 'field': 'ext', 'align': 'right'}],
            rows=[], row_key='sku').props('dense flat bordered').classes('w-full nf-bom') \
            .style('height:206px')

    with ui.footer().classes('bg-white border-t border-slate-200 items-center gap-5 px-4 py-0.5'):
        status_symbols = ui.label('0 symbols') \
            .classes('text-[11px] font-mono text-slate-600 nf-symbols')
        status_pipe = ui.label('0.00 m pipe') \
            .classes('text-[11px] font-mono text-slate-600 nf-pipe')
        status_total = ui.label('BOM 0.00') \
            .classes('text-[11px] font-mono text-slate-600 nf-status-total')
        status_sheet = ui.label().classes('text-[11px] font-mono text-slate-400')
        ui.space()
        status_hint = ui.label('drag a symbol onto the sheet') \
            .classes('text-[11px] text-slate-400 nf-hint')

    refresh()


if __name__ in {'__main__', '__mp_main__'}:
    ui.run(port=PORT, title='P&ID studio', show=False, reload=True)
