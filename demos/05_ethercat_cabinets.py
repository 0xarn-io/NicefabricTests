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

The enclosure is a real part too. Each cabinet is a **Rittal AX** compact enclosure picked from
a small catalogue, and its published mounting plate is what sets the rail capacity: usable rail
length is the plate width less 100 mm of wiring duct and side margin, and the number of rails is
the plate height divided by a 250 mm rail pitch. An ``AX 1076.000`` (550 x 735 mm plate) gives
2 rails of 450 mm; an ``AX 1180.000`` (745 x 975 mm) gives 3 of 645 mm; an ``AX 1260.000``
(545 x 1175 mm) gives 4 of 445 mm. So choosing a smaller box really does run the rail-fill check
out of room, and the enclosure lands in the BOM alongside the terminals.

Simplifications worth knowing before reading the checks:

* **Each rail is treated as its own E-bus segment.** On real hardware a segment continues from
  one rail to the next through an ``EK1110``/``EK1100`` pair, so the budget would carry across.
  Here every rail needs its own supply — a coupler or an ``EL9410`` — and is audited alone.
* **The rail layout is derived, not drawn to plate scale.** Rail length and count come from the
  real plate dimensions, but the drawing spaces the rails for legibility rather than rendering
  the enclosure to scale.
* **Cables are pre-assembled parts, not measured runs.** A link carries a family and one of
  the offered lengths, so the BOM lists an orderable lead rather than metres off the drawing.
  The trailing ``xxx`` in each family is Beckhoff's own length code, deliberately left
  unresolved rather than inventing a digit encoding.

Housing colour is not decoration: on Beckhoff hardware **yellow means TwinSAFE**, so only the
safety devices are drawn yellow and everything else takes the standard light grey. The coloured
stripe along the top of each terminal is this editor's own signal-type coding, not Beckhoff
livery.

.. warning::
   Part numbers are real Beckhoff and Rittal designations and the enclosure/plate sizes are the
   published ones, but the terminal widths, E-bus figures and all prices here are
   **representative values for the demo**, not a datasheet or a price list. Check the current
   documentation before ordering anything.

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

PX_PER_MM = 1.35                     # terminals are 12 mm wide; this keeps them legible
TERM_H = round(100 * PX_PER_MM)      # terminal height, 100 mm
CAB_PAD = 26
RAIL_PITCH = TERM_H + 55             # rail-to-rail spacing on the drawing
CAB_X0, CAB_Y0, CAB_GAP = 240, 36, 50   # first cabinet clears the palette dock
MAX_ROW = 2000                          # wrap a row of cabinets past this width
ROW_BAND = RAIL_PITCH // 2              # top edges within this band count as the same row

# Rittal AX compact enclosures. Enclosure and mounting-plate sizes are the published ones; the
# rail layout is derived from the plate, which is what makes the choice of enclosure actually
# constrain the design rather than being decoration.
DUCT_MM = 100                        # wiring duct + side margin taken off the plate width
RAIL_PITCH_MM = 250                  # rail + 100 mm terminal + duct, per rail row
ENCLOSURES: dict[str, dict] = {
    'AX 1076.000': {'wh': (600, 760, 210), 'plate': (550, 735), 'price': 470.0},
    'AX 1180.000': {'wh': (800, 1000, 300), 'plate': (745, 975), 'price': 780.0},
    'AX 1260.000': {'wh': (600, 1200, 300), 'plate': (545, 1175), 'price': 690.0},
}
DEFAULT_MODEL = 'AX 1076.000'


def _geom(model: str) -> dict:
    """Drawing geometry and rail capacity derived from the enclosure's mounting plate."""
    plate_w, plate_h = ENCLOSURES[model]['plate']
    rail_mm = plate_w - DUCT_MM
    rails = max(1, int(plate_h // RAIL_PITCH_MM))
    rail_len = round(rail_mm * PX_PER_MM)
    return {'rail_mm': rail_mm, 'rails': rails, 'rail_len': rail_len,
            'w': rail_len + 2 * CAB_PAD, 'h': rails * RAIL_PITCH + 2 * CAB_PAD}


GEOM = {model: _geom(model) for model in ENCLOSURES}

EBUS_SUPPLY = 2000                   # mA delivered by a coupler / refresh terminal

# Housing colour carries real meaning on Beckhoff hardware: yellow is TwinSAFE. Standard
# EL/EK terminals are a light warm grey, so colouring everything yellow would read as a rail
# full of safety devices.
STANDARD = '#dcdcd4'                 # standard EL/EK housing
SAFETY = '#f2cd13'                   # TwinSAFE yellow — EL6910, EK1960
EDGE = '#3f3f46'
CAB_EDGE = '#334155'
RAIL_FILL = '#c3ccd6'
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
               'ebus': EBUS_SUPPLY, 'price': 1800.0, 'grp': 'SAF', 'cat': 'Safety'},
}



# Pre-assembled Beckhoff cable families. The trailing `xxx` is Beckhoff's length code, left
# unresolved here on purpose: the BOM carries the family plus the chosen length rather than a
# digit encoding invented for the demo. Price is modelled base + per metre.
CABLES: dict[str, dict] = {
    'ethercat': {'label': 'EtherCAT', 'family': 'ZK1090-9191-Cxxx',
                 'desc': 'EtherCAT patch cable, green, RJ45 plug 8-pin both ends',
                 'colour': '#15803d', 'stroke': 2.4, 'base': 18.0, 'per_m': 4.8},
    'ethercatp': {'label': 'EtherCAT P', 'family': 'ZK7001-0101-2xxx',
                  'desc': 'EtherCAT P cable, M8 male straight 4-pin both ends',
                  'colour': '#d97706', 'stroke': 3.2, 'base': 50.0, 'per_m': 9.0},
    'hybrid': {'label': 'Hybrid OCT', 'family': 'ZK4704-0421-2xxx',
               'desc': 'Motor cable 0.75 mm2 PUR, itec plug, OCT one-cable, drag-chain',
               'colour': '#1f2937', 'stroke': 4.4, 'base': 110.0, 'per_m': 16.0},
}
CABLE_LENGTHS = (2, 5, 10)      # the pre-assembled lengths this demo offers


def _terminal_art(part: str) -> str:
    """A terminal: coloured body, signal stripe, LED column, part number printed vertically."""
    spec = CATALOG[part]
    w, h = round(spec['w'] * PX_PER_MM), TERM_H
    fill = SAFETY if spec['cat'] == 'Safety' else STANDARD
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


def _cabinet_art(model: str) -> str:
    """Enclosure outline with the DIN rails its mounting plate affords — schematic, not drawn
    to enclosure scale, but the rail count and length do come from the real plate size."""
    g = GEOM[model]
    parts = [f'<rect x="1" y="1" width="{g["w"] - 2}" height="{g["h"] - 2}" rx="4" '
             f'fill="#fbfcfe" stroke="{CAB_EDGE}" stroke-width="2"/>']
    for r in range(g['rails']):
        y = CAB_PAD + r * RAIL_PITCH + TERM_H
        parts.append(f'<rect x="{CAB_PAD}" y="{y}" width="{g["rail_len"]}" height="9" rx="1.5" '
                     f'fill="{RAIL_FILL}" stroke="#94a3b8" stroke-width="0.8"/>')
        parts.append(f'<line x1="{CAB_PAD}" y1="{y - TERM_H}" x2="{CAB_PAD}" '
                     f'y2="{y + 9}" stroke="#cbd5e1" stroke-width="1"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{g["w"]}" height="{g["h"]}" '
            f'viewBox="0 0 {g["w"]} {g["h"]}">{"".join(parts)}</svg>')


def _url(svg: str) -> str:
    return 'data:image/svg+xml;base64,' + base64.b64encode(svg.encode()).decode()


for _part, _spec in CATALOG.items():
    _spec['art'] = _terminal_art(_part)
    _spec['url'] = _url(_spec['art'])
CABINET_URL = {m: _url(_cabinet_art(m)) for m in ENCLOSURES}

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
        # Row-major by the *top edge*, not the centre. Enclosures differ in height, so two
        # cabinets standing side by side in one row have different centres — sorting on `top`
        # would hand +CAB01 to whichever box happens to be shorter.
        def row_major(o: dict) -> tuple[float, float]:
            return round((o['top'] - geom_of(o)['h'] / 2) / ROW_BAND), o['left']

        return sorted((o for o in objs() if o.get('kind') == 'cabinet'), key=row_major)

    def terminals() -> list[dict]:
        return [o for o in objs() if o.get('kind') in CATALOG]

    def cables() -> list[dict]:
        return [o for o in objs() if o.get('kind') == 'cable']

    def designations() -> dict[str, str]:
        """Cabinet id -> location designation, numbered by position so it survives a load."""
        return {c['id']: f'+CAB{i:02d}' for i, c in enumerate(cabinets(), 1)}

    def geom_of(cab: dict) -> dict:
        """Geometry of one cabinet, from the enclosure model stored on the object."""
        return GEOM.get(cab.get('model'), GEOM[DEFAULT_MODEL])

    def cabinet_at(x: float, y: float) -> dict | None:
        for cab in cabinets():
            g = geom_of(cab)
            if abs(x - cab['left']) <= g['w'] / 2 and abs(y - cab['top']) <= g['h'] / 2:
                return cab
        return None

    def rail_index(cab: dict, y: float) -> int:
        g = geom_of(cab)
        top_edge = cab['top'] - g['h'] / 2 + CAB_PAD
        return max(0, min(g['rails'] - 1, int((y - top_edge + TERM_H / 2) // RAIL_PITCH)))

    def rail_origin(cab: dict, rail: int) -> tuple[float, float]:
        """Top-left of a rail's terminal row, in scene coordinates."""
        g = geom_of(cab)
        return (cab['left'] - g['w'] / 2 + CAB_PAD,
                cab['top'] - g['h'] / 2 + CAB_PAD + rail * RAIL_PITCH)

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
            by_rail.setdefault((cab['id'], rail_index(cab, term['top'])), []).append(term)

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
            out.setdefault((cab['id'], rail_index(cab, term['top'])), []).append(term)
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
            cab = next((c for c in cabinets() if c['id'] == cab_id), None)
            rail_mm = geom_of(cab)['rail_mm'] if cab else GEOM[DEFAULT_MODEL]['rail_mm']
            used_mm = sum(CATALOG[t['kind']]['w'] for t in items)
            issues = []
            if items and not seen_supply:
                issues.append('no coupler feeding this rail')
            elif worst is not None and worst < 0:
                issues.append(f'E-bus short by {abs(worst)} mA at {culprit} — add an EL9410')
            if items and items[-1]['kind'] != 'EL9011':
                issues.append('segment does not end with an EL9011 end cap')
            if used_mm > rail_mm:
                issues.append(f'rail overfull: {used_mm} mm on a {rail_mm} mm rail')
            report.append({'loc': tags.get(cab_id, '?'), 'rail': rail, 'count': len(items),
                           'used_mm': used_mm, 'rail_mm': rail_mm, 'headroom': budget if seen_supply else None,
                           'issues': issues})
        return report

    # ------------------------------------------------------------------ bill of materials -
    def cable_spec(cable: dict) -> tuple[dict, float]:
        """The catalogue entry and length behind a drawn cable, tolerating older saves."""
        kind = cable.get('cableType') if cable.get('cableType') in CABLES else 'ethercat'
        return CABLES[kind], float(cable.get('lengthM') or 5)

    def bom_rows(by_location: bool) -> list[dict]:
        """Aggregate the layout into line items. Cables group by family *and* length, since a
        2 m and a 10 m lead are different orderable parts."""
        tags = designations()
        buckets: dict[tuple[str, str, str, float], int] = {}

        def add(loc: str, part: str, desc: str, price: float, n: int = 1) -> None:
            key = (loc if by_location else '', part, desc, price)
            buckets[key] = buckets.get(key, 0) + n

        for cab in cabinets():
            model = cab.get('model', DEFAULT_MODEL)
            spec = ENCLOSURES[model]
            w, h, d = spec['wh']
            add(tags[cab['id']], f'Rittal {model}',
                f'Compact enclosure AX, sheet steel, {w}x{h}x{d} mm, with mounting plate',
                spec['price'])
        for term in terminals():
            cab = cabinet_at(term['left'], term['top'])
            spec = CATALOG[term['kind']]
            add(tags.get(cab['id'], '(unplaced)') if cab else '(unplaced)',
                term['kind'], spec['desc'], spec['price'])
        for cable in cables():
            spec, length = cable_spec(cable)
            add(tags.get(cable.get('fromId'), '(inter-cabinet)'), spec['family'],
                f'{spec["desc"]}, {length:g} m', spec['base'] + spec['per_m'] * length)

        rows = []
        for (loc, part, desc, price), qty in sorted(buckets.items()):
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
        # Packed left to right and wrapped past MAX_ROW, with rows advancing by the tallest
        # cabinet placed so far. Enclosures differ in size now, so a fixed grid would either
        # overlap the tall ones or waste a screen of space around the short ones.
        model = enclosure_model.value
        g = GEOM[model]
        placed = cabinets()
        if not placed:
            left, top = CAB_X0 + g['w'] / 2, CAB_Y0 + g['h'] / 2
        else:
            right = max(c['left'] + geom_of(c)['w'] / 2 for c in placed)
            row_top = min(c['top'] - geom_of(c)['h'] / 2 for c in placed
                          if c['top'] - geom_of(c)['h'] / 2
                          >= max(x['top'] - geom_of(x)['h'] / 2 for x in placed) - 1)
            if right + CAB_GAP + g['w'] <= MAX_ROW:
                left, top = right + CAB_GAP + g['w'] / 2, row_top + g['h'] / 2
            else:                                    # wrap below everything placed so far
                bottom = max(c['top'] + geom_of(c)['h'] / 2 for c in placed)
                left, top = CAB_X0 + g['w'] / 2, bottom + 60 + g['h'] / 2
        canvas.add_image(CABINET_URL[model], left=left, top=top, kind='cabinet', model=model,
                         **PLACED, **live_props())
        log.push(f'cabinet added at {left:.0f},{top:.0f}')
        ui.timer(0, fit_canvas, once=True)   # grow the sheet to hold the new row
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
        ay = a['top'] + geom_of(a)['h'] / 2 + 14
        by = b['top'] + geom_of(b)['h'] / 2 + 14
        pts = [(a['left'], ay), ((a['left'] + b['left']) / 2, ay),
               ((a['left'] + b['left']) / 2, by), (b['left'], by)]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        rel = [{'x': p[0] - min(xs), 'y': p[1] - min(ys)} for p in pts]
        kind = cable_type.value
        spec, length = CABLES[kind], float(cable_len.value)
        canvas.add_polyline(rel, left=(min(xs) + max(xs)) / 2, top=(min(ys) + max(ys)) / 2,
                            fill='', stroke=spec['colour'], strokeWidth=spec['stroke'],
                            strokeUniform=True, kind='cable',
                            # camelCase: the library warns on props containing "_"
                            cableType=kind, lengthM=length, fromId=from_id, toId=to_id,
                            **PLACED, **live_props())
        log.push(f'{spec["label"]} cable, {length:g} m')

    # ------------------------------------------------------------------ labels -----------
    def sync_labels() -> None:
        tags = designations()
        wanted = [(c['left'] - geom_of(c)['w'] / 2 + 8,
                   c['top'] - geom_of(c)['h'] / 2 - 22,
                   f'{tags[c["id"]]}  ·  {c.get("model", DEFAULT_MODEL)}')
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
                    ui.label(f'{entry["used_mm"]}/{entry["rail_mm"]} mm') \
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
                model = entry.get('model', DEFAULT_MODEL)
                spec, g = ENCLOSURES[model], GEOM[model]
                prop_row('part', f'Rittal {model}')
                prop_row('size', '{}x{}x{}'.format(*spec['wh']), 'mm')
                prop_row('plate', '{}x{}'.format(*spec['plate']), 'mm')
                prop_row('rails', f'{g["rails"]} x {g["rail_mm"]} mm')
                prop_row('unit', f'{spec["price"]:,.2f}')
            elif kind == 'cable':
                spec, length = cable_spec(entry)
                prop_row('type', spec['label'])
                prop_row('part', spec['family'])
                prop_row('length', f'{length:g}', 'm')
                prop_row('unit', f'{spec["base"] + spec["per_m"] * length:,.2f}')
                ui.label(spec['desc']).classes('text-[10px] text-slate-500 leading-snug mt-1')
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
        if not dims or dims[0] < 200 or dims[1] < 200:
            return
        # the sheet is at least the viewport, and grows to hold every cabinet — the stage
        # scrolls, so rows below the fold stay reachable
        placed = cabinets()
        need_w = max([c['left'] + geom_of(c)['w'] / 2 for c in placed], default=0) + 60
        need_h = max([c['top'] + geom_of(c)['h'] / 2 for c in placed], default=0) + 60
        canvas.resize(max(dims[0], round(need_w)), max(dims[1], round(need_h)))

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
        # the canvas scrolls inside its own layer; the docks are siblings of the scroller, so
        # they stay put instead of scrolling away with the sheet
        with ui.element('div').classes('absolute inset-0 overflow-auto nf-scroll') as scroller:
            canvas.move(scroller)
            canvas.classes('nf-canvas nf-sheet')

        with ui.element('div').classes('nf-dock p-2').style('left:12px; top:12px; width:194px'):
            ui.label('TOOL').classes('nf-panelhead')
            ui.toggle({'select': 'Select', 'cable': 'Cable'}, value='select',
                      on_change=lambda e: set_tool(e.value)) \
                .props('dense no-caps spread size=sm unelevated').classes('w-full nf-tool')
            ui.label('ENCLOSURE').classes('nf-panelhead mt-2')
            enclosure_model = ui.select(
                {m: f'{m}  ({GEOM[m]["rails"]} x {GEOM[m]["rail_mm"]} mm)'
                 for m in ENCLOSURES}, value=DEFAULT_MODEL) \
                .props('dense outlined options-dense').classes('w-full nf-model')
            ui.button('Add cabinet', icon='add_box', on_click=add_cabinet) \
                .props('dense outline no-caps size=sm').classes('w-full mt-1 nf-addcab')
            ui.label('CABLE').classes('nf-panelhead mt-2')
            cable_type = ui.toggle({k: v['label'] for k, v in CABLES.items()},
                                   value='ethercat') \
                .props('dense no-caps spread size=sm unelevated').classes('w-full nf-cabletype')
            cable_len = ui.toggle({n: f'{n} m' for n in CABLE_LENGTHS}, value=5) \
                .props('dense no-caps spread size=sm unelevated') \
                .classes('w-full mt-1 nf-cablelen')
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
