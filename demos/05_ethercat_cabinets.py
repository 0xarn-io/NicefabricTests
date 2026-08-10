"""Demo 05 — an EtherCAT I/O cabinet planner: build cabinets, populate DIN rails, get a BOM
per location and an E-bus current check.

Demo 04 places symbols on a free sheet. This one is about **containment**: a terminal belongs
to a rail, a rail belongs to a cabinet, and terminals pack left to right along the rail with
no gaps — drop one in the middle and the rest shuffle right, exactly like handling real
terminals. That structure is what makes a per-location bill of materials meaningful.

The drawing also gets checked, not just counted:

* **E-bus current budget.** An ``EK1100`` coupler supplies 2000 mA to the E-bus; every
  terminal to its right draws that down, and an ``EL9410`` refreshes it back to 2000 mA. Run
  out and the segment will not start. The panel walks each rail in order and reports the
  headroom left, flagging the terminal where it goes negative.
* **Bus end cap.** A segment has to finish with an ``EL9011``; a rail without one is flagged.
* **Rail fill.** Terminal widths are summed in millimetres against the usable rail length.

How the model survives a save/load: ``load_json`` **re-ids every object**, so containment
cannot be stored as object references. Each terminal carries its location designation and rail
index as plain custom props (``loc='+CAB01'``, ``rail=0``), and its order along the rail is
simply its ``left`` — sorted and re-packed on every layout pass. Nothing points at an id.

Two simplifications worth knowing before reading the checks:

* **Each rail is treated as its own E-bus segment.** On real hardware a segment continues from
  one rail to the next through an ``EK1110``/``EK1100`` pair, so the budget would carry across.
  Here every rail needs its own supply — a coupler or an ``EL9410`` — and is audited alone.
* **Cabinets are schematic.** The enclosure is drawn to fit its rails, not to enclosure scale,
  and EtherCAT cables are counted as pre-assembled patch leads rather than measured off the
  drawing.

.. warning::
   Part numbers are real Beckhoff designations, but the widths, E-bus figures and prices here
   are **representative values for the demo**, not a datasheet or a price list. Check the
   current documentation before ordering anything.

Run with::

    python demos/05_ethercat_cabinets.py
"""
import asyncio
import base64
import csv
import io

from nicegui import app, ui

from nicefabric import FabricCanvas

PORT = 9094
SLOT = 'nicefabric-demo-05'

PX_PER_MM = 1.6                      # terminals are 12 mm wide; this keeps them legible
RAIL_MM = 300                        # usable DIN rail length per rail
RAILS_PER_CAB = 2
TERM_H = round(100 * PX_PER_MM)      # terminal height, 100 mm
RAIL_LEN = round(RAIL_MM * PX_PER_MM)
CAB_PAD = 26
RAIL_PITCH = TERM_H + 58
CAB_W = RAIL_LEN + 2 * CAB_PAD
CAB_H = RAILS_PER_CAB * RAIL_PITCH + 2 * CAB_PAD

EBUS_SUPPLY = 2000                   # mA delivered by a coupler / refresh terminal

BODY = '#f0c437'                     # the yellow of an EL terminal
INFRA = '#d5dbe2'                    # couplers, end caps, potential distribution
EDGE = '#3f3f46'
CAB_EDGE = '#334155'
RAIL_FILL = '#c3ccd6'
CABLE = '#15803d'
LABEL_COLOUR = '#0f172a'

# stripe colours by signal type — an editor affordance, not Beckhoff livery
STRIPE = {'DI': '#22c55e', 'DO': '#ef4444', 'AI': '#3b82f6', 'AO': '#8b5cf6',
          'MOT': '#f97316', 'COM': '#0ea5e9', 'PWR': '#64748b', 'SYS': '#64748b',
          'IOL': '#0891b2', 'LED': '#d946ef', 'SAF': '#b91c1c'}

# part -> width_mm, E-bus mA (positive supplies, negative draws), price, signal group
CATALOG: dict[str, dict] = {
    'EK1100': {'desc': 'EtherCAT coupler, E-bus', 'w': 44, 'ebus': EBUS_SUPPLY,
               'price': 118.0, 'grp': 'SYS', 'cat': 'Infrastructure'},
    'EK1110': {'desc': 'EtherCAT extension, E-bus to RJ45', 'w': 12, 'ebus': -60,
               'price': 96.0, 'grp': 'SYS', 'cat': 'Infrastructure'},
    'EL9410': {'desc': 'E-bus power supply refresh', 'w': 12, 'ebus': EBUS_SUPPLY,
               'price': 96.0, 'grp': 'PWR', 'cat': 'Infrastructure'},
    'EL9011': {'desc': 'Bus end cap', 'w': 8, 'ebus': 0, 'price': 7.0,
               'grp': 'SYS', 'cat': 'Infrastructure'},
    'EL9186': {'desc': 'Potential distribution, 24 V, 8x', 'w': 12, 'ebus': 0, 'price': 28.0,
               'grp': 'PWR', 'cat': 'Infrastructure'},
    'EL9187': {'desc': 'Potential distribution, 0 V, 8x', 'w': 12, 'ebus': 0, 'price': 28.0,
               'grp': 'PWR', 'cat': 'Infrastructure'},
    'EL1008': {'desc': 'Digital input, 8 ch, 24 V DC', 'w': 12, 'ebus': -90, 'price': 96.0,
               'grp': 'DI', 'cat': 'Digital'},
    'EL1409': {'desc': 'Digital input, 16 ch, 24 V DC, 3 ms, positive switching', 'w': 12,
               'ebus': -90, 'price': 155.0, 'grp': 'DI', 'cat': 'Digital'},
    'EL1809': {'desc': 'Digital input, 16 ch, 24 V DC', 'w': 12, 'ebus': -90, 'price': 155.0,
               'grp': 'DI', 'cat': 'Digital'},
    'EL2008': {'desc': 'Digital output, 8 ch, 24 V DC 0.5 A', 'w': 12, 'ebus': -110,
               'price': 102.0, 'grp': 'DO', 'cat': 'Digital'},
    'EL2409': {'desc': 'Digital output, 16 ch, 24 V DC 0.5 A, positive switching', 'w': 12,
               'ebus': -140, 'price': 168.0, 'grp': 'DO', 'cat': 'Digital'},
    'EL2809': {'desc': 'Digital output, 16 ch, 24 V DC 0.5 A', 'w': 12, 'ebus': -140,
               'price': 168.0, 'grp': 'DO', 'cat': 'Digital'},
    'EL3054': {'desc': 'Analog input, 4 ch, 4..20 mA', 'w': 12, 'ebus': -130, 'price': 260.0,
               'grp': 'AI', 'cat': 'Analog'},
    'EL3062': {'desc': 'Analog input, 2 ch, 0..10 V', 'w': 12, 'ebus': -130, 'price': 200.0,
               'grp': 'AI', 'cat': 'Analog'},
    'EL4004': {'desc': 'Analog output, 4 ch, 0..10 V', 'w': 12, 'ebus': -130, 'price': 310.0,
               'grp': 'AO', 'cat': 'Analog'},
    'EL5101': {'desc': 'Incremental encoder interface', 'w': 12, 'ebus': -200, 'price': 280.0,
               'grp': 'COM', 'cat': 'Comms & special'},
    'EL6001': {'desc': 'Serial interface, RS232', 'w': 12, 'ebus': -100, 'price': 190.0,
               'grp': 'COM', 'cat': 'Comms & special'},
    'EL6224': {'desc': 'IO-Link master, 4 ch, HD housing', 'w': 12, 'ebus': -130,
               'price': 325.0, 'grp': 'IOL', 'cat': 'Comms & special'},
    # up to 2048 pixels across its four channels; each channel needs its own external 5..24 V
    'EL2574': {'desc': 'Pixel LED output, 4 ch, ext. 5..24 V per channel', 'w': 12,
               'ebus': -130, 'price': 340.0, 'grp': 'LED', 'cat': 'Comms & special'},
    'EL7031': {'desc': 'Stepper motor terminal, 24 V, 1.5 A', 'w': 24, 'ebus': -130,
               'price': 260.0, 'grp': 'MOT', 'cat': 'Comms & special'},
    'EL6910': {'desc': 'TwinSAFE Logic terminal', 'w': 12, 'ebus': -200, 'price': 1150.0,
               'grp': 'SAF', 'cat': 'Safety'},
    # a compact controller with its own EtherCAT connectors, so it heads its own segment
    # rather than drawing from an upstream coupler
    'EK1960': {'desc': 'TwinSAFE Compact Controller, 20 safe DI / 24 safe DO (2 A)', 'w': 126,
               'ebus': EBUS_SUPPLY, 'price': 1800.0, 'grp': 'SAF', 'cat': 'Safety',
               'grey': True},
}

CABINET_PART = {'part': 'CAB-600x800', 'desc': 'Enclosure 600x800x210 with mounting plate',
                'price': 540.0}
CABLE_PART = {'part': 'ZK1090-9191-0050', 'desc': 'EtherCAT patch cable RJ45, 5 m',
              'price': 42.0}


def _terminal_art(part: str) -> str:
    """A terminal: coloured body, signal stripe, LED column, part number printed vertically."""
    spec = CATALOG[part]
    w, h = round(spec['w'] * PX_PER_MM), TERM_H
    fill = INFRA if spec.get('grey', spec['cat'] == 'Infrastructure') else BODY
    stripe = STRIPE[spec['grp']]
    body = (f'<rect x="0.6" y="0.6" width="{w - 1.2}" height="{h - 1.2}" rx="2" '
            f'fill="{fill}" stroke="{EDGE}" stroke-width="1.1"/>'
            f'<rect x="0.6" y="0.6" width="{w - 1.2}" height="5" rx="1.5" fill="{stripe}"/>')
    if w >= 15:      # an 8 mm end cap has no room for a legible part number
        body += (f'<text x="{w / 2}" y="{h * 0.56}" font-family="Arial, Helvetica, sans-serif" '
                 f'font-size="9" fill="#111827" text-anchor="middle" '
                 f'transform="rotate(-90 {w / 2} {h * 0.56})">{part}</text>')
        for i in range(3):    # status LEDs, as on the real front face
            body += (f'<circle cx="{w / 2}" cy="{16 + i * 8}" r="1.7" fill="#ffffff" '
                     f'stroke="{EDGE}" stroke-width="0.5"/>')
        body += (f'<rect x="2.5" y="{h - 34}" width="{w - 5}" height="26" rx="1.5" '
                 f'fill="none" stroke="{EDGE}" stroke-width="0.7"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}">{body}</svg>')


def _cabinet_art() -> str:
    """An enclosure outline with its DIN rails — schematic, not drawn to enclosure scale."""
    parts = [f'<rect x="1" y="1" width="{CAB_W - 2}" height="{CAB_H - 2}" rx="4" '
             f'fill="#fbfcfe" stroke="{CAB_EDGE}" stroke-width="2"/>']
    for r in range(RAILS_PER_CAB):
        y = CAB_PAD + r * RAIL_PITCH + TERM_H
        parts.append(f'<rect x="{CAB_PAD}" y="{y}" width="{RAIL_LEN}" height="10" rx="1.5" '
                     f'fill="{RAIL_FILL}" stroke="#94a3b8" stroke-width="0.8"/>')
        parts.append(f'<line x1="{CAB_PAD}" y1="{y - TERM_H}" x2="{CAB_PAD}" '
                     f'y2="{y + 10}" stroke="#cbd5e1" stroke-width="1"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{CAB_W}" height="{CAB_H}" '
            f'viewBox="0 0 {CAB_W} {CAB_H}">{"".join(parts)}</svg>')


def _url(svg: str) -> str:
    return 'data:image/svg+xml;base64,' + base64.b64encode(svg.encode()).decode()


for _part, _spec in CATALOG.items():
    _spec['art'] = _terminal_art(_part)
    _spec['url'] = _url(_spec['art'])
CABINET_URL = _url(_cabinet_art())

PLACED = {'lockScalingX': True, 'lockScalingY': True, 'borderColor': '#2563eb',
          'cornerColor': '#ffffff', 'cornerStrokeColor': '#2563eb',
          'transparentCorners': False, 'cornerSize': 6, 'padding': 2}
LABEL_PROPS = {'kind': 'label', 'selectable': False, 'evented': False, 'fontSize': 12,
               'fontFamily': 'Inter, Arial, sans-serif', 'fill': LABEL_COLOUR}


@ui.page('/')  # per-visit page: a module-level canvas would be shared by ALL tabs and users
def index() -> None:
    state = {'tool': 'select', 'pending': None, 'labels': '', 'view': 'layout',
             'bom': 'general'}

    def live_props() -> dict:
        """Objects only react while Select is active — otherwise a click meant to pick a
        cabinet for a cable would select and drag a terminal instead."""
        live = state['tool'] == 'select'
        return {'selectable': live, 'evented': live}

    def on_mouse_down(e) -> None:
        if state['tool'] != 'cable':
            return
        hit = cabinet_at(e.args['x'], e.args['y'])
        if hit is None:
            status_hint.text = 'click on a cabinet to start the cable'
            return
        if state['pending'] is None:
            state['pending'] = hit['id']
            status_hint.text = f'from {designations().get(hit["id"], "?")} — click the far end'
            return
        if state['pending'] != hit['id']:
            draw_cable(state['pending'], hit['id'])
        state['pending'] = None
        status_hint.text = 'click a cabinet to start another cable'
        refresh()

    canvas = FabricCanvas(width=1400, height=700, background='',
                          keyboard_delete=True,
                          on_selection=lambda e: refresh(),
                          on_modified=lambda e: (reflow(), refresh()),
                          on_mouse_down=on_mouse_down,
                          on_error=lambda e: log.push(f'ERROR {e.args}'))

    # ------------------------------------------------------------------ registry views ---
    def objs() -> list[dict]:
        return canvas.to_dict()['objects']

    def cabinets() -> list[dict]:
        return sorted((o for o in objs() if o.get('kind') == 'cabinet'),
                      key=lambda o: (o['top'], o['left']))

    def terminals() -> list[dict]:
        return [o for o in objs() if o.get('kind') in CATALOG]

    def cables() -> list[dict]:
        return [o for o in objs() if o.get('kind') == 'cable']

    def designations() -> dict[str, str]:
        """Cabinet id -> location designation, numbered by position so it survives a load."""
        return {c['id']: f'+CAB{i:02d}' for i, c in enumerate(cabinets(), 1)}

    def cabinet_at(x: float, y: float) -> dict | None:
        for cab in cabinets():
            if (abs(x - cab['left']) <= CAB_W / 2) and (abs(y - cab['top']) <= CAB_H / 2):
                return cab
        return None

    def rail_origin(cab: dict, rail: int) -> tuple[float, float]:
        """Top-left of a rail's terminal row, in scene coordinates."""
        return (cab['left'] - CAB_W / 2 + CAB_PAD,
                cab['top'] - CAB_H / 2 + CAB_PAD + rail * RAIL_PITCH)

    # ------------------------------------------------------------------ containment ------
    def reflow() -> None:
        """Re-home every terminal to the cabinet and rail it is sitting over, then pack each
        rail left to right with no gaps. Order along the rail is just ``left``, so dropping
        a terminal between two others inserts it there and pushes the rest along."""
        by_rail: dict[tuple[str, int], list[dict]] = {}
        for term in terminals():
            cab = cabinet_at(term['left'], term['top'])
            if cab is None:                       # dropped outside any cabinet: park it
                continue
            top_edge = cab['top'] - CAB_H / 2 + CAB_PAD
            rail = max(0, min(RAILS_PER_CAB - 1,
                              int((term['top'] - top_edge + TERM_H / 2) // RAIL_PITCH)))
            by_rail.setdefault((cab['id'], rail), []).append(term)

        for (cab_id, rail), items in by_rail.items():
            cab = next(c for c in cabinets() if c['id'] == cab_id)
            ox, oy = rail_origin(cab, rail)
            cursor = 0.0
            for term in sorted(items, key=lambda t: t['left']):
                width = CATALOG[term['kind']]['w'] * PX_PER_MM
                try:
                    canvas.update_object(term['id'], left=ox + cursor + width / 2,
                                         top=oy + TERM_H / 2, loc=cab_id, rail=rail)
                except KeyError:
                    continue
                cursor += width

    def rail_contents() -> dict[tuple[str, int], list[dict]]:
        out: dict[tuple[str, int], list[dict]] = {}
        for term in terminals():
            cab = cabinet_at(term['left'], term['top'])
            if cab is None:
                continue
            top_edge = cab['top'] - CAB_H / 2 + CAB_PAD
            rail = max(0, min(RAILS_PER_CAB - 1,
                              int((term['top'] - top_edge + TERM_H / 2) // RAIL_PITCH)))
            out.setdefault((cab['id'], rail), []).append(term)
        for items in out.values():
            items.sort(key=lambda t: t['left'])
        return out

    # ------------------------------------------------------------------ checks -----------
    def audit() -> list[dict]:
        """Walk each rail in EtherCAT order and check the E-bus budget, end cap and fill."""
        tags = designations()
        report = []
        for (cab_id, rail), items in sorted(rail_contents().items(),
                                            key=lambda kv: (tags.get(kv[0][0], ''), kv[0][1])):
            budget, worst, culprit, seen_supply = 0, None, None, False
            for term in items:
                ebus = CATALOG[term['kind']]['ebus']
                if ebus > 0:
                    budget, seen_supply = ebus, True
                else:
                    budget += ebus
                    if worst is None or budget < worst:
                        worst, culprit = budget, term['kind']
            used_mm = sum(CATALOG[t['kind']]['w'] for t in items)
            issues = []
            if items and not seen_supply:
                issues.append('no coupler feeding this rail')
            elif worst is not None and worst < 0:
                issues.append(f'E-bus short by {abs(worst)} mA at {culprit} — add an EL9410')
            if items and items[-1]['kind'] != 'EL9011':
                issues.append('segment does not end with an EL9011 end cap')
            if used_mm > RAIL_MM:
                issues.append(f'rail overfull: {used_mm} mm on a {RAIL_MM} mm rail')
            report.append({'loc': tags.get(cab_id, '?'), 'rail': rail, 'count': len(items),
                           'used_mm': used_mm, 'headroom': budget if seen_supply else None,
                           'issues': issues})
        return report

    # ------------------------------------------------------------------ bill of materials -
    def bom_rows(by_location: bool) -> list[dict]:
        tags = designations()
        buckets: dict[tuple[str, str], int] = {}

        def add(loc: str, part: str, n: int = 1) -> None:
            buckets[(loc if by_location else '', part)] = \
                buckets.get((loc if by_location else '', part), 0) + n

        for cab in cabinets():
            add(tags[cab['id']], CABINET_PART['part'])
        for term in terminals():
            cab = cabinet_at(term['left'], term['top'])
            add(tags.get(cab['id'], '(unplaced)') if cab else '(unplaced)', term['kind'])
        for cable in cables():
            add(tags.get(cable.get('fromId'), '(inter-cabinet)'), CABLE_PART['part'])

        def spec(part: str) -> tuple[str, float]:
            if part == CABINET_PART['part']:
                return CABINET_PART['desc'], CABINET_PART['price']
            if part == CABLE_PART['part']:
                return CABLE_PART['desc'], CABLE_PART['price']
            return CATALOG[part]['desc'], CATALOG[part]['price']

        rows = []
        for (loc, part), qty in sorted(buckets.items()):
            desc, price = spec(part)
            rows.append({'loc': loc or 'ALL', 'part': part, 'desc': desc, 'qty': str(qty),
                         'unit': f'{price:,.2f}', 'ext': f'{qty * price:,.2f}',
                         '_ext': qty * price})
        return rows

    def export_bom() -> None:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        for label, per_loc in (('GENERAL', False), ('BY LOCATION', True)):
            writer.writerow([label])
            writer.writerow(['Location', 'Part', 'Description', 'Qty', 'Unit', 'Extended'])
            rows = bom_rows(per_loc)
            for row in rows:
                writer.writerow([row['loc'], row['part'], row['desc'], row['qty'],
                                 row['unit'], row['ext']])
            writer.writerow(['', '', '', '', 'TOTAL', f'{sum(r["_ext"] for r in rows):,.2f}'])
            writer.writerow([])
        ui.download(buffer.getvalue().encode(), 'ethercat-bom.csv')

    # ------------------------------------------------------------------ placement --------
    def add_cabinet() -> None:
        # start clear of the palette dock (12..206): a cabinet underneath it would put the
        # left end of its rails out of reach of a drop
        existing = cabinets()
        left = 240 + CAB_W / 2 + len(existing) * (CAB_W + 70)
        top = 36 + CAB_H / 2
        canvas.add_image(CABINET_URL, left=left, top=top, kind='cabinet',
                         **PLACED, **live_props())
        log.push(f'cabinet added at {left:.0f},{top:.0f}')
        refresh()

    def on_drop(e) -> None:
        part = e.args.get('kind')
        if part not in CATALOG:
            return
        x, y = e.args['x'], e.args['y']
        if cabinet_at(x, y) is None:
            ui.notify('drop terminals onto a rail inside a cabinet', type='warning')
            return
        canvas.add_image(CATALOG[part]['url'], left=x, top=y, kind=part,
                         **PLACED, **live_props())
        reflow()
        log.push(f'{part} placed')
        refresh()

    ui.on('nf_drop', on_drop)

    def draw_cable(from_id: str, to_id: str) -> None:
        a = next((c for c in cabinets() if c['id'] == from_id), None)
        b = next((c for c in cabinets() if c['id'] == to_id), None)
        if a is None or b is None:
            return
        pts = [(a['left'], a['top'] + CAB_H / 2 + 14),
               ((a['left'] + b['left']) / 2, a['top'] + CAB_H / 2 + 14),
               ((a['left'] + b['left']) / 2, b['top'] + CAB_H / 2 + 14),
               (b['left'], b['top'] + CAB_H / 2 + 14)]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        rel = [{'x': p[0] - min(xs), 'y': p[1] - min(ys)} for p in pts]
        canvas.add_polyline(rel, left=(min(xs) + max(xs)) / 2, top=(min(ys) + max(ys)) / 2,
                            fill='', stroke=CABLE, strokeWidth=2.4, strokeUniform=True,
                            kind='cable', fromId=from_id, toId=to_id,   # camelCase: the library warns otherwise
                            **PLACED, **live_props())
        log.push('EtherCAT cable added')

    # ------------------------------------------------------------------ labels -----------
    def sync_labels() -> None:
        tags = designations()
        wanted = [(c['left'] - CAB_W / 2 + 8, c['top'] - CAB_H / 2 - 22, tags[c['id']])
                  for c in cabinets()]
        signature = repr(sorted(wanted))
        if signature == state['labels']:
            return
        state['labels'] = signature
        for old in objs():
            if old.get('kind') == 'label':
                canvas.remove_object(old['id'])
        for left, top, text in wanted:
            canvas.add_text(text, left=left + 40, top=top, width=140, **LABEL_PROPS)

    # ------------------------------------------------------------------ file ops ---------
    def save() -> None:
        app.storage.general[SLOT] = canvas.to_dict()
        ui.notify(f'saved — {len(cabinets())} cabinets, {len(terminals())} terminals')

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
        state['labels'] = ''
        refresh()
        ui.notify(f'loaded {len(cabinets())} cabinets')

    def export_json() -> None:
        ui.download(canvas.to_json().encode(), 'ethercat-layout.json')

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

    async def export_png() -> None:
        try:
            data_url = await canvas.to_data_url()
        except (RuntimeError, asyncio.TimeoutError) as err:
            ui.notify(f'PNG export failed: {err}', type='negative')
            return
        ui.download(base64.b64decode(data_url.split(',', 1)[1]), 'ethercat-layout.png')

    def clear_all() -> None:
        canvas.clear_objects()
        state['labels'] = ''
        state['pending'] = None
        refresh()

    def delete_selected() -> None:
        canvas.remove_selected()
        reflow()
        refresh()

    # ------------------------------------------------------------------ modes -----------
    def set_tool(tool: str) -> None:
        state['tool'] = tool
        state['pending'] = None
        canvas.discard_selection()
        live = live_props()
        for entry in objs():
            if entry.get('kind') == 'label':
                continue
            try:
                canvas.update_object(entry['id'], **live)
            except KeyError:
                pass
        canvas.run_canvas_method('set', {'defaultCursor': 'default' if tool == 'select'
                                         else 'crosshair'})
        status_hint.text = ('drag terminals onto a rail' if tool == 'select'
                            else 'click a cabinet to start an EtherCAT cable')
        refresh()

    def set_view(name: str) -> None:
        state['view'] = name
        layout = name == 'layout'
        stage.set_visibility(layout)
        bom_panel.style(replace='height:250px' if layout else '')
        bom_panel.classes(replace='w-full bg-white border-t border-slate-200 px-3 py-1'
                                  + ('' if layout else ' flex-1 min-h-0'))
        bom_table.style(replace='height:204px' if layout else 'height:calc(100% - 34px)')
        for view, label in tabs.items():
            label.classes(replace='text-[12px] px-3 py-3 cursor-pointer ' + (
                'text-white border-b-2 border-cyan-400' if view == name
                else 'text-slate-400 hover:text-slate-200'))

    def set_bom_mode(mode: str) -> None:
        state['bom'] = mode
        refresh()

    # ------------------------------------------------------------------ refresh ---------
    def prop_row(name: str, value: str, unit: str = '') -> None:
        with ui.row().classes('w-full items-baseline gap-1 py-px'):
            ui.label(name).classes('text-[11px] text-slate-500 flex-1')
            ui.label(value).classes('text-[11px] font-mono text-slate-800')
            ui.label(unit).classes('text-[10px] text-slate-400 w-8 text-right')

    def refresh() -> None:
        sync_labels()
        report = audit()
        rows = bom_rows(state['bom'] == 'location')
        grand = sum(r['_ext'] for r in rows)
        problems = sum(len(r['issues']) for r in report)

        bom_table.columns = ([{'name': 'loc', 'label': 'Location', 'field': 'loc',
                               'align': 'left'}] if state['bom'] == 'location' else []) + [
            {'name': 'part', 'label': 'Part', 'field': 'part', 'align': 'left'},
            {'name': 'desc', 'label': 'Description', 'field': 'desc', 'align': 'left'},
            {'name': 'qty', 'label': 'Qty', 'field': 'qty', 'align': 'right'},
            {'name': 'unit', 'label': 'Unit', 'field': 'unit', 'align': 'right'},
            {'name': 'ext', 'label': 'Extended', 'field': 'ext', 'align': 'right'}]
        bom_table.rows = [{k: v for k, v in r.items() if not k.startswith('_')} for r in rows]
        bom_table.update()
        bom_total.text = f'{grand:,.2f}'

        status_cab.text = f'{len(cabinets())} cabinets'
        status_term.text = f'{len(terminals())} terminals'
        status_total.text = f'BOM {grand:,.2f}'
        status_issues.text = 'no issues' if not problems else f'▲ {problems} issue(s)'
        status_issues.classes(replace='text-[11px] font-mono nf-issues ' + (
            'text-emerald-600' if not problems else 'text-amber-600'))

        checks.clear()
        with checks:
            if not report:
                ui.label('no terminals placed').classes('text-[11px] text-slate-400')
            for entry in report:
                head = f'{entry["loc"]} rail {entry["rail"] + 1}'
                with ui.row().classes('w-full items-baseline gap-1'):
                    ui.label(head).classes('text-[11px] font-mono text-slate-700 flex-1')
                    ui.label(f'{entry["used_mm"]}/{RAIL_MM} mm') \
                        .classes('text-[10px] font-mono text-slate-400')
                head_room = entry['headroom']
                ui.label(f'E-bus headroom {head_room} mA' if head_room is not None
                         else 'E-bus not fed').classes(
                    'text-[10px] font-mono ' + ('text-slate-500' if head_room and head_room >= 0
                                                else 'text-amber-600'))
                for issue in entry['issues']:
                    ui.label(f'▲ {issue}').classes('text-[10px] text-amber-700 '
                                                        'leading-snug nf-issue')

        props.clear()
        with props:
            chosen = {s.id for s in canvas.get_selected()}
            picked = [o for o in objs() if o['id'] in chosen and o.get('kind') != 'label']
            if len(picked) != 1:
                prop_row('cabinets', str(len(cabinets())))
                prop_row('terminals', str(len(terminals())))
                prop_row('cables', str(len(cables())))
                prop_row('line items', str(len(rows)))
                return
            entry = picked[0]
            kind = entry.get('kind')
            if kind == 'cabinet':
                prop_row('type', 'Enclosure')
                prop_row('location', designations().get(entry['id'], '?'))
                prop_row('part', CABINET_PART['part'])
                prop_row('rails', str(RAILS_PER_CAB))
            elif kind == 'cable':
                prop_row('type', 'EtherCAT cable')
                prop_row('part', CABLE_PART['part'])
                prop_row('unit', f'{CABLE_PART["price"]:,.2f}')
            elif kind in CATALOG:
                spec = CATALOG[kind]
                cab = cabinet_at(entry['left'], entry['top'])
                prop_row('part', kind)
                prop_row('location', designations().get(cab['id'], '—') if cab else '—')
                prop_row('width', str(spec['w']), 'mm')
                prop_row('E-bus', f'{spec["ebus"]:+d}', 'mA')
                prop_row('unit', f'{spec["price"]:,.2f}')
                ui.label(spec['desc']).classes('text-[10px] text-slate-500 leading-snug mt-1')
            with ui.grid(columns=2).classes('w-full gap-1 mt-2'):
                ui.button('Delete', on_click=delete_selected) \
                    .props('dense flat no-caps size=sm color=red').classes('nf-delete')
                ui.button('Deselect', on_click=canvas.discard_selection) \
                    .props('dense flat no-caps size=sm color=grey-7')

    # ------------------------------------------------------------------ client wiring ----
    async def wire_client() -> None:
        await canvas.initialized()
        await ui.run_javascript("""
            document.addEventListener('dragstart', (ev) => {
                const el = ev.target.closest('.nf-piece');
                if (el) ev.dataTransfer.setData('text/plain', el.dataset.kind);
            });
            const wrap = document.querySelector('.nf-canvas');
            wrap.addEventListener('dragover', (ev) => ev.preventDefault());
            wrap.addEventListener('drop', (ev) => {
                ev.preventDefault();
                const r = wrap.getBoundingClientRect();
                emitEvent('nf_drop', {kind: ev.dataTransfer.getData('text/plain'),
                                      x: ev.clientX - r.left, y: ev.clientY - r.top});
            });
            window.addEventListener('resize', () => { clearTimeout(window.__nfRs);
                window.__nfRs = setTimeout(() => emitEvent('nf_resize'), 200); });
        """, timeout=5)
        await fit_canvas()

    async def fit_canvas() -> None:
        try:
            dims = await ui.run_javascript(
                "(() => { const s = document.querySelector('.nf-stage');"
                " return s ? [s.clientWidth, s.clientHeight] : null; })()", timeout=3)
        except TimeoutError:
            return
        if dims and dims[0] > 200 and dims[1] > 200:
            canvas.resize(*dims)

    ui.timer(0, wire_client, once=True)
    ui.on('nf_resize', fit_canvas)

    # ================================================================== layout ===========
    ui.query('.nicegui-content').classes('p-0 gap-0 h-screen')
    ui.query('.q-page').classes('h-full flex flex-col')
    ui.add_css('''
        .nf-sheet { background-color:#f6f8fb;
            background-image:radial-gradient(#dde4ec 1px, transparent 1px);
            background-size:20px 20px; }
        .nf-dock { position:absolute; z-index:10; background:#fff; border:1px solid #dde5ee;
            border-radius:5px; box-shadow:0 4px 14px rgba(15,23,42,.08); }
        .nf-panelhead { font:600 10px system-ui; letter-spacing:.09em; color:#64748b;
            text-transform:uppercase; }
    ''')

    with ui.dialog() as json_dialog, ui.card().classes('w-96'):
        ui.label('Import layout JSON').classes('text-sm font-medium')
        json_uploader = ui.upload(label='ethercat-layout.json', auto_upload=True,
                                  max_file_size=2_000_000, on_upload=import_json) \
            .props('accept=".json,application/json" flat dense no-thumbnails').classes('w-full')

    with ui.header().classes('items-center gap-0 px-4 py-0').style('background:#1b2431'):
        ui.label('EtherCAT').classes('text-sm font-semibold text-white tracking-wide mr-1')
        ui.label('cabinet planner').classes('text-[11px] font-light text-slate-400 mr-6')
        tabs: dict[str, ui.label] = {}
        for view, caption in (('layout', 'Layout'), ('bom', 'Bill of materials')):
            tabs[view] = ui.label(caption).classes('text-[12px] px-3 py-3 cursor-pointer')
            tabs[view].on('click', lambda _, v=view: set_view(v))
        ui.space()
        ui.button('EXPORT BOM', icon='download', on_click=export_bom) \
            .props('dense unelevated no-caps size=sm').style('background:#16a34a;color:#fff') \
            .classes('nf-exportbom')
        with ui.button(icon='folder').props('flat dense size=sm color=grey-5'):
            with ui.menu():
                ui.menu_item('Save layout', save)
                ui.menu_item('Load layout', load)
                ui.separator()
                ui.menu_item('Export JSON', export_json)
                ui.menu_item('Import JSON…', json_dialog.open)
                ui.menu_item('Export PNG', export_png)
                ui.separator()
                ui.menu_item('Clear all', clear_all)

    with ui.element('div').classes('relative w-full flex-1 min-h-0 nf-stage') as stage:
        canvas.move(stage)
        canvas.classes('nf-canvas nf-sheet absolute inset-0')

        with ui.element('div').classes('nf-dock p-2').style('left:12px; top:12px; width:194px'):
            ui.label('TOOL').classes('nf-panelhead')
            ui.toggle({'select': 'Select', 'cable': 'Cable'}, value='select',
                      on_change=lambda e: set_tool(e.value)) \
                .props('dense no-caps spread size=sm unelevated').classes('w-full nf-tool')
            ui.button('Add cabinet', icon='add_box', on_click=add_cabinet) \
                .props('dense outline no-caps size=sm').classes('w-full mt-1 nf-addcab')
            ui.separator().classes('my-2')
            ui.label('TERMINALS').classes('nf-panelhead')
            with ui.column().classes('w-full gap-0 max-h-[430px] overflow-auto'):
                for group in ('Infrastructure', 'Digital', 'Analog', 'Comms & special',
                              'Safety'):
                    ui.label(group).classes('text-[10px] text-slate-400 mt-1')
                    for part, spec in CATALOG.items():
                        if spec['cat'] != group:
                            continue
                        with ui.row().classes('w-full items-center gap-2 rounded px-1 '
                                              'hover:bg-slate-100'):
                            ui.html(f'<div class="nf-piece nf-piece-{part}" draggable="true" '
                                    f'data-kind="{part}" title="{spec["desc"]}" '
                                    f'style="width:20px;height:44px;flex:none;cursor:grab;'
                                    f'display:flex;align-items:center;overflow:hidden">'
                                    f'<svg viewBox="0 0 {spec["w"] * PX_PER_MM:.0f} {TERM_H}" '
                                    f'width="20" height="44">{spec["art"]}</svg></div>')
                            with ui.column().classes('gap-0'):
                                ui.label(part).classes('text-[11px] font-mono text-slate-800 '
                                                       'leading-tight')
                                ui.label(f'{spec["w"]} mm · {spec["ebus"]:+d} mA') \
                                    .classes('text-[10px] font-mono text-slate-400 '
                                             'leading-tight')

        with ui.element('div').classes('nf-dock p-2 overflow-auto') \
                .style('right:12px; top:12px; width:212px; max-height:calc(100% - 24px)'):
            ui.label('SEGMENT CHECKS').classes('nf-panelhead')
            checks = ui.column().classes('w-full gap-0.5 mt-1 nf-checks')
            ui.separator().classes('my-2')
            ui.label('PROPERTIES').classes('nf-panelhead')
            props = ui.column().classes('w-full gap-0 mt-1 nf-props')
            ui.separator().classes('my-2')
            with ui.expansion('Log').classes('w-full text-[11px]'):
                log = ui.log(max_lines=10).classes('w-full h-20 text-[10px] nf-log')

    with ui.element('div').classes('w-full bg-white border-t border-slate-200 px-3 py-1') \
            .style('height:250px') as bom_panel:
        with ui.row().classes('w-full items-center gap-3'):
            ui.label('BILL OF MATERIALS').classes('nf-panelhead')
            ui.toggle({'general': 'General', 'location': 'By location'}, value='general',
                      on_change=lambda e: set_bom_mode(e.value)) \
                .props('dense no-caps size=sm unelevated').classes('nf-bommode')
            ui.space()
            ui.label('representative figures — not a Beckhoff price list') \
                .classes('text-[10px] text-slate-400')
            ui.label('TOTAL').classes('text-[10px] tracking-widest text-slate-400')
            bom_total = ui.label('0.00').classes('text-sm font-mono font-semibold '
                                                 'text-slate-900 nf-total')
        bom_table = ui.table(columns=[], rows=[], row_key='part') \
            .props('dense flat bordered').classes('w-full nf-bom').style('height:204px')

    with ui.footer().classes('bg-white border-t border-slate-200 items-center gap-5 px-4 py-0.5'):
        status_cab = ui.label('0 cabinets').classes('text-[11px] font-mono text-slate-600 nf-cab')
        status_term = ui.label('0 terminals') \
            .classes('text-[11px] font-mono text-slate-600 nf-term')
        status_total = ui.label('BOM 0.00') \
            .classes('text-[11px] font-mono text-slate-600 nf-statustotal')
        status_issues = ui.label('no issues').classes('text-[11px] font-mono nf-issues')
        ui.space()
        status_hint = ui.label('add a cabinet, then drag terminals onto a rail') \
            .classes('text-[11px] text-slate-400 nf-hint')

    set_view('layout')
    refresh()


if __name__ in {'__main__', '__mp_main__'}:
    ui.run(port=PORT, title='EtherCAT cabinet planner', show=False, reload=True)
