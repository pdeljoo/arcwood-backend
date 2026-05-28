"""
Arcwood Millwork - Cabinet Extraction API (single-file Vercel function)
GET  /api/index  -> health check
POST /api/index  -> { pdf_base64, pages?: [0-indexed], waste_pct?: 0.25 }
"""
from http.server import BaseHTTPRequestHandler
import json
import base64
import os
import tempfile
import traceback
import re
from collections import defaultdict
from typing import List, Dict, Optional

import pdfplumber

# ============================================================
#  CABINET EXTRACTOR CORE
# ============================================================

HW_CODE_PATTERN = re.compile(r'^(PK|PF|AP|MW|MT|S\d|T\d|D\d|L\d|WC|WT|GL|BA|FP)\d+(\+\d+)?$')

MATERIAL_CODES = {
    'MW1': '3/4" Low-VOC MDF painted',
    'MW2': 'Wood veneer flat panel',
    'MW3': 'Painted flat panel',
    'MW4': 'Tall slim storage',
    'MT1': '1" round metal rod',
    'S01': 'Stone countertop',
    'S02': 'Dekton countertop',
}

CABINET_TYPES = [
    (['GARBAGE', 'COMPACTOR'], 'Garbage Compactor Housing', 'Houses AP9 compactor', 1, 0),
    (['RECYCLING', 'PULL-OUT'], 'Recycling Pull-out', 'Pull-out unit', 1, 0),
    (['PANEL-READY', 'DW'], 'Panel-Ready Dishwasher Housing', 'DW panel housing', 1, 0),
    (['SINK', 'GARBURATOR'], 'Sink Base + Garburator', 'Sink with garburator', 2, 0),
    (['GARBURATOR'], 'Sink Base + Garburator', 'Sink with garburator', 2, 0),
    (['PANTRY', 'PULL-OUT'], 'Pantry Tall — Pull-out shelves', 'Plywood pull-out shelves', 1, 0),
    (['ROBO', 'VAC'], 'Robo-Vac Closet', 'TBC on model', 1, 0),
    (['BLIND', 'CORNER'], 'Blind Corner Base', 'Blind corner', 1, 0),
    (['COFFEE'], 'Coffee Bar Cabinet', 'Coffee station', 2, 2),
    (['WALL', 'OVEN'], 'Wall Oven Tower', 'Wall oven + microwave', 2, 0),
    (['ENTRY', 'CLOSET'], 'Entry Closet', 'Closet w/ shelves', 2, 2),
    (['COAT', 'CLOSET'], 'Coat Closet', 'Coat closet', 1, 0),
    (['OPEN', 'SHELVES'], 'Open Shelving', 'Open shelves', 0, 0),
    (['OPEN', 'SHELF'], 'Open Shelving', 'Open shelves', 0, 0),
    (['HIDDEN', 'STORAGE'], 'Hidden Storage Cabinet', 'Touch-latch hidden', 1, 0),
    (['DISPLAY', 'NICHE'], 'Display Niche Cabinet', 'Display niche', 0, 0),
    (['FIXED', 'SHELF'], 'Base Cabinet — Fixed Shelf', 'Fixed shelf', 1, 0),
    (['PULL-OUT'], 'Base Cabinet — Pull-out', 'Full pull-out', 0, 0),
    (['DWRS'], 'Base Cabinet — Drawers', 'Drawer stack', 0, 3),
]

DEFAULT_BODY = '3/4" Birch Plywood'
DEFAULT_BACK = '5/8" Birch Plywood'


def parse_dimension(word: Dict, all_chars: List[Dict]) -> Optional[float]:
    """Parse dimension with architectural fraction notation."""
    text = word['text'].strip()
    has_quote = '"' in text
    text_clean = text.replace('"', '').strip()

    nearby_frac = None
    if not has_quote:
        word_right = word['x1']
        word_top = word['top']
        word_bottom = word['bottom']
        word_height = word_bottom - word_top

        for c in all_chars:
            if (word_right - 3 <= c['x0'] <= word_right + 10 and
                c['top'] > word_top + word_height * 0.2 and
                c['top'] < word_bottom + 8 and
                c['text'] in '23458'):
                nearby_frac = c['text']
                break

    if nearby_frac and re.match(r'^\d+$', text_clean):
        base = text_clean[:-1] if len(text_clean) > 1 else "0"
        num = text_clean[-1]
        try:
            base_val = int(base) if base else 0
            denom = int(nearby_frac)
            num_val = int(num)
            if denom > num_val:
                return base_val + (num_val / denom)
        except (ValueError, ZeroDivisionError):
            pass

    m = re.match(r'^(\d+)"?$', text_clean)
    if m:
        return float(m.group(1))

    m = re.match(r"^(\d+)'(\d+)", text_clean)
    if m:
        return int(m.group(1)) * 12 + int(m.group(2))

    return None


def extract_all_dimensions(page) -> List[Dict]:
    words = page.extract_words()
    chars = page.chars

    dims = []
    for w in words:
        val = parse_dimension(w, chars)
        if val is not None and 0.5 <= val <= 500:
            dims.append({
                'value': val, 'text': w['text'],
                'x': w['x0'], 'x1': w['x1'], 'y': w['top'],
            })
    return dims


def find_dimension_lines(dims: List[Dict], min_dims: int = 3) -> List[Dict]:
    y_groups = defaultdict(list)
    for d in dims:
        y_groups[round(d['y'] / 5) * 5].append(d)

    lines = []
    for y, group in sorted(y_groups.items()):
        if len(group) < min_dims:
            continue

        cleaned = []
        for d in sorted(group, key=lambda x: x['x']):
            if d['text'] in '248' and len(d['text']) == 1:
                if any(abs(o['x'] - d['x']) < 15 and o != d and o['text'] not in '248' for o in group):
                    continue
            cleaned.append(d)

        if len(cleaned) >= min_dims:
            lines.append({'y': y, 'dims': cleaned, 'total': sum(d['value'] for d in cleaned)})
    return lines


def match_dim_lines_to_totals(dim_lines: List[Dict], total_dims: List[Dict]) -> List[Dict]:
    """Match dim lines to total widths, then deduplicate vertically close lines.
    
    When two dim lines are within ~30 PDF units vertically, the one with MORE
    dimensions is the cabinet row; the other is a sub-total/reference line.
    """
    matched = []
    for line in dim_lines:
        for tw in total_dims:
            if abs(line['total'] - tw['value']) < 20:
                matched.append({'line': line, 'matched_total': tw['value']})
                break

    # Deduplicate: when two matched lines are vertically close (within 30 units),
    # keep only the one with more individual dimensions (= more cabinets)
    matched.sort(key=lambda m: m['line']['y'])
    deduped = []
    for m in matched:
        is_duplicate = False
        for kept in deduped:
            if abs(m['line']['y'] - kept['line']['y']) < 30:
                # They overlap - keep the one with more dims
                if len(m['line']['dims']) > len(kept['line']['dims']):
                    deduped.remove(kept)
                    deduped.append(m)
                is_duplicate = True
                break
        if not is_duplicate:
            deduped.append(m)
    return deduped


def detect_pdf_scale(dim_line: Dict) -> float:
    dims = sorted(dim_line['dims'], key=lambda d: d['x'])
    if len(dims) < 2:
        return 3.0
    samples = []
    for i in range(len(dims) - 1):
        d1, d2 = dims[i], dims[i + 1]
        expected_inches = (d1['value'] + d2['value']) / 2
        if expected_inches > 0:
            samples.append((d2['x'] - d1['x']) / expected_inches)
    if samples:
        samples.sort()
        return samples[len(samples) // 2]
    return 3.0


def find_cabinet_boundaries_from_edges(page, y_top: float, y_bottom: float,
                                        x_min: float = 0, x_max: float = 99999) -> List[float]:
    """Find actual cabinet vertical boundaries from PDF vector edges.

    Cabinet boxes have tall vertical lines forming their left/right sides.
    These extend the full height of the cabinet body, unlike interior details
    (shelves, drawers) which have shorter lines.
    """
    edges = page.edges
    height_threshold = (y_bottom - y_top) * 0.7  # at least 70% of cabinet height

    verticals = [e for e in edges
                 if abs(e['x0'] - e['x1']) < 1
                 and e['height'] > height_threshold
                 and abs(e['top'] - y_top) < 20  # starts near top
                 and x_min <= e['x0'] <= x_max]

    # Get unique X positions and cluster nearby ones
    xs = sorted(set(round(e['x0'], 1) for e in verticals))

    clustered = []
    for x in xs:
        if not clustered or x - clustered[-1] > 8:
            clustered.append(x)

    return clustered


def map_cabinets_to_positions(dim_line: Dict, scale: float = 3.0, page=None) -> List[Dict]:
    """Map dimensions to cabinet x-extents using vertical edge detection when possible.

    Strategy:
    1. If page provided, find actual cabinet boundaries from vertical edges
    2. Match each dim (in order) to the nearest box width
    3. Fallback: use dim text center as box center
    """
    dims = sorted(dim_line['dims'], key=lambda d: d['x'])
    if not dims:
        return []

    # Try to find actual boundaries if we have access to the page
    boundaries = None
    if page is not None:
        # Cabinet zone: above the dim line
        y_top = dim_line['y'] - 130   # cabinet top
        y_bottom = dim_line['y'] - 25  # cabinet bottom (just above dim line)

        # Search in the x range covered by dims
        x_min = dims[0]['x'] - 50
        x_max = dims[-1]['x1'] + 50

        boundaries = find_cabinet_boundaries_from_edges(page, y_top, y_bottom, x_min, x_max)

    # If boundaries found and roughly match dim count, use them
    if boundaries and len(boundaries) >= 2:
        # Compute box widths between consecutive boundaries
        box_widths_px = [boundaries[i+1] - boundaries[i] for i in range(len(boundaries) - 1)]

        # Match each dim to a box by order (left-to-right)
        # Skip filler dims that are too small to be cabinets
        cabinets = []
        cumulative = 0
        box_idx = 0
        for d in dims:
            # Assign to next available box
            if box_idx < len(box_widths_px):
                x_left = boundaries[box_idx]
                x_right = boundaries[box_idx + 1]
                box_idx += 1
            else:
                # Fallback to dim center
                dim_center = (d['x'] + d['x1']) / 2
                half_w = d['value'] * scale / 2
                x_left = dim_center - half_w
                x_right = dim_center + half_w

            cabinets.append({
                'dim_text': d['text'],
                'width_inches': d['value'],
                'x_left': x_left,
                'x_right': x_right,
                'x_center': (x_left + x_right) / 2,
                'cumulative_from_left': cumulative,
            })
            cumulative += d['value']
        return cabinets

    # Fallback: use midpoint method
    cabinets = []
    cumulative = 0
    for i, d in enumerate(dims):
        dim_center = (d['x'] + d['x1']) / 2

        if i == 0:
            x_left = dim_center - (d['value'] * scale / 2)
        else:
            prev_center = (dims[i-1]['x'] + dims[i-1]['x1']) / 2
            x_left = (prev_center + dim_center) / 2

        if i == len(dims) - 1:
            x_right = dim_center + (d['value'] * scale / 2)
        else:
            next_center = (dims[i+1]['x'] + dims[i+1]['x1']) / 2
            x_right = (dim_center + next_center) / 2

        cabinets.append({
            'dim_text': d['text'],
            'width_inches': d['value'],
            'x_left': x_left,
            'x_right': x_right,
            'x_center': dim_center,
            'cumulative_from_left': cumulative,
        })
        cumulative += d['value']
    return cabinets


def find_labels_in_cabinet(cabinet: Dict, words: List[Dict], y_min: float, y_max: float,
                            tolerance: float = 15) -> Dict:
    """Find all text labels within a cabinet's spatial extent.

    `tolerance` allows labels slightly outside the strict x_left/x_right range
    to be associated with this cabinet (labels in architectural drawings can
    be offset from box center by a few pixels).
    """
    labels = []
    hardware = []
    for w in words:
        wx = (w['x0'] + w['x1']) / 2
        if cabinet['x_left'] - tolerance <= wx <= cabinet['x_right'] + tolerance and y_min <= w['top'] <= y_max:
            text = w['text']
            if HW_CODE_PATTERN.match(text):
                hardware.append(text)
            elif len(text) >= 2 and text.replace('-', '').replace('/', '').isalpha():
                labels.append(text.upper())
    return {'labels': list(set(labels)), 'hardware': list(set(hardware)),
            'labels_text': ' '.join(set(labels)).upper()}


def classify_cabinet(cabinet: Dict, label_info: Dict) -> Dict:
    labels_text = label_info['labels_text']
    hardware = label_info['hardware']

    cab_type = "Base Cabinet"
    function = ""
    doors = 1
    drawers = 0

    for keywords, c_type, c_func, c_doors, c_drawers in CABINET_TYPES:
        if all(kw in labels_text for kw in keywords):
            cab_type = c_type
            function = c_func
            doors = c_doors
            drawers = c_drawers
            break

    width = cabinet['width_inches']
    if width < 5:
        cab_type = "Filler / Wall"
        doors = 0
        drawers = 0
    elif width < 12:
        cab_type = "Spacer / Trim"
        doors = 0

    material = DEFAULT_BODY
    for hw in hardware:
        if hw in MATERIAL_CODES:
            material = MATERIAL_CODES[hw]
            break

    return {
        'type': cab_type, 'function': function,
        'doors': doors, 'drawers': drawers,
        'material_body': material, 'material_back': DEFAULT_BACK,
        'hardware_codes': hardware, 'all_labels': label_info['labels'],
    }


def extract_page(pdf, page_index: int, page_label: str = None) -> Dict:
    page = pdf.pages[page_index]
    words = page.extract_words()
    dims = extract_all_dimensions(page)
    total_dims = [d for d in dims if d['value'] >= 80]
    dim_lines = find_dimension_lines(dims)
    matched = match_dim_lines_to_totals(dim_lines, total_dims)

    result = {
        'page_index': page_index,
        'page_label': page_label or f'Page {page_index + 1}',
        'elevations': [],
    }

    seen_y = set()
    for m in matched:
        line = m['line']
        if line['y'] in seen_y:
            continue
        seen_y.add(line['y'])

        scale = detect_pdf_scale(line)
        cabinets_raw = map_cabinets_to_positions(line, scale, page=page)

        y_min = line['y'] - 250
        y_max = line['y'] - 10

        cabinets = []
        for i, cab in enumerate(cabinets_raw, 1):
            label_info = find_labels_in_cabinet(cab, words, y_min, y_max)
            classification = classify_cabinet(cab, label_info)
            cabinets.append({
                'index': i,
                'width_inches': cab['width_inches'],
                'dim_text': cab['dim_text'],
                **classification,
            })

        result['elevations'].append({
            'dim_line_y': line['y'],
            'total_width': round(line['total'], 2),
            'matched_to_total': m['matched_total'],
            'scale_px_per_inch': round(scale, 2),
            'cabinet_count': len([c for c in cabinets if 'Filler' not in c['type'] and 'Spacer' not in c['type']]),
            'cabinets': cabinets,
        })
    return result


def extract_pdf(pdf_path: str, pages: List[int] = None) -> Dict:
    results = {'pdf_path': pdf_path, 'pages': []}
    with pdfplumber.open(pdf_path) as pdf:
        if pages is None:
            pages = range(len(pdf.pages))
        for p_idx in pages:
            if p_idx >= len(pdf.pages):
                continue
            page_result = extract_page(pdf, p_idx)
            if page_result['elevations']:
                results['pages'].append(page_result)
    return results


def compute_material_takeoff(extraction: Dict, waste_pct: float = 0.25) -> Dict:
    materials = defaultdict(float)
    cabinet_count = 0

    for page in extraction['pages']:
        for elev in page['elevations']:
            for cab in elev['cabinets']:
                if 'Filler' in cab['type'] or 'Spacer' in cab['type']:
                    continue
                cabinet_count += 1

                w = cab['width_inches']
                h = 34.5
                d = 24
                if 'Tall' in cab['type'] or 'Pantry' in cab['type'] or 'Closet' in cab['type']:
                    h = 90
                elif 'Upper' in cab['type'] or 'Open' in cab['type']:
                    h = 30
                    d = 12

                sqft = ((w * h * 2) + (w * d * 2) + (h * d * 2)) / 144
                sheets_body = max(1, sqft / 32)
                sheets_back = sheets_body * 0.35

                materials[f'{DEFAULT_BODY} — Body'] += sheets_body
                materials[f'{DEFAULT_BACK} — Back & Drawer Box'] += sheets_back

                if cab.get('doors', 0) > 0:
                    materials['Cabinet Doors — MDF Painted'] += cab['doors']
                    materials['Soft-close Hinges (pair)'] += cab['doors'] * 2
                    materials['Cabinet Pulls/Handles'] += cab['doors']

                if cab.get('drawers', 0) > 0:
                    materials['Drawer Box + Slide Set'] += cab['drawers']
                    materials['Cabinet Pulls/Handles'] += cab['drawers']

                edge_ft = ((w + h) * 2 + (d + h) * 2) / 12
                materials['Edge Banding (ft)'] += edge_ft

    materials_with_waste = {}
    for k, v in materials.items():
        unit = 'sheets' if ('Plywood' in k or 'MDF' in k) and 'Door' not in k else (
            'ft' if 'ft' in k else (
                'pair' if 'pair' in k else 'each'
            )
        )
        materials_with_waste[k] = {
            'net': round(v, 2),
            'with_waste': round(v * (1 + waste_pct), 2),
            'unit': unit,
        }

    return {
        'cabinet_count': cabinet_count,
        'waste_factor': waste_pct,
        'materials': materials_with_waste,
    }




# ============================================================
#  VERCEL HTTP HANDLER
# ============================================================
class handler(BaseHTTPRequestHandler):

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        self._json(200, {
            "status": "ok",
            "service": "arcwood-cabinet-extractor",
            "version": "1.0.0",
        })

    def do_POST(self):
        tmp_path = None
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length <= 0:
                self._json(400, {"error": "Empty request body"})
                return

            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as e:
                self._json(400, {"error": "Invalid JSON", "detail": str(e)})
                return

            pdf_b64 = payload.get("pdf_base64")
            pages = payload.get("pages")
            waste_pct = float(payload.get("waste_pct", 0.25))

            if not pdf_b64:
                self._json(400, {"error": "Missing 'pdf_base64' field"})
                return

            try:
                pdf_bytes = base64.b64decode(pdf_b64)
            except Exception as e:
                self._json(400, {"error": "Invalid base64", "detail": str(e)})
                return

            with tempfile.NamedTemporaryFile(suffix=".pdf", dir="/tmp", delete=False) as tmp:
                tmp.write(pdf_bytes)
                tmp_path = tmp.name

            extraction = extract_pdf(tmp_path, pages=pages)
            takeoff = compute_material_takeoff(extraction, waste_pct=waste_pct)

            self._json(200, {"extraction": extraction, "takeoff": takeoff})

        except Exception as e:
            self._json(500, {
                "error": "Internal server error",
                "detail": str(e),
                "trace": traceback.format_exc(),
            })
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
