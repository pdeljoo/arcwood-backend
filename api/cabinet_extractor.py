"""
Arcwood Millwork - Cabinet Extraction API
Vercel Serverless Function (Python)
 
Endpoint:  POST /api/extract_cabinets
Health:    GET  /api/extract_cabinets
Input:     { "pdf_base64": "<b64>", "pages": [15], "waste_pct": 0.25 }
           - pages are 0-indexed (page 16 in PDF = index 15)
           - pages is optional (default: all pages)
Output:    { "extraction": {...}, "takeoff": {...} }
"""
 
from http.server import BaseHTTPRequestHandler
import json
import base64
import os
import sys
import tempfile
import traceback
 
# Make cabinet_extractor.py (in repo root) importable from this file in /api
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
 
try:
    from cabinet_extractor import extract_pdf, compute_material_takeoff
    _import_error = None
except Exception as e:
    extract_pdf = None
    compute_material_takeoff = None
    _import_error = f"{type(e).__name__}: {e}"
 
 
class handler(BaseHTTPRequestHandler):
    """Vercel discovers this class by name. Methods: do_GET / do_POST / do_OPTIONS."""
 
    # ---------- helpers ----------
    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
 
    def _send_json(self, status_code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)
 
    # ---------- routes ----------
    def do_OPTIONS(self):
        """CORS preflight."""
        self.send_response(204)
        self._cors_headers()
        self.end_headers()
 
    def do_GET(self):
        """Health check — open the endpoint in a browser to verify deploy."""
        self._send_json(200, {
            "status": "ok",
            "service": "arcwood-cabinet-extractor",
            "version": "1.0.0",
            "module_loaded": extract_pdf is not None,
            "import_error": _import_error,
            "usage": "POST JSON: { pdf_base64, pages?: [0-indexed], waste_pct?: 0.25 }",
        })
 
    def do_POST(self):
        tmp_path = None
        try:
            if extract_pdf is None:
                self._send_json(500, {
                    "error": "cabinet_extractor module failed to import",
                    "detail": _import_error,
                })
                return
 
            # ----- read body -----
            content_length = int(self.headers.get("Content-Length", 0) or 0)
            if content_length <= 0:
                self._send_json(400, {"error": "Empty request body"})
                return
 
            raw = self.rfile.read(content_length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as e:
                self._send_json(400, {"error": "Invalid JSON", "detail": str(e)})
                return
 
            pdf_b64 = payload.get("pdf_base64")
            pages = payload.get("pages")              # list of 0-indexed ints (optional)
            waste_pct = float(payload.get("waste_pct", 0.25))
 
            if not pdf_b64:
                self._send_json(400, {"error": "Missing 'pdf_base64' field"})
                return
 
            # ----- decode PDF -----
            try:
                pdf_bytes = base64.b64decode(pdf_b64)
            except Exception as e:
                self._send_json(400, {"error": "Invalid base64", "detail": str(e)})
                return
 
            # ----- extract_pdf expects a file path, so write to /tmp (Vercel-writable) -----
            with tempfile.NamedTemporaryFile(
                suffix=".pdf", dir="/tmp", delete=False
            ) as tmp:
                tmp.write(pdf_bytes)
                tmp_path = tmp.name
 
            # ----- run extraction -----
            extraction = extract_pdf(tmp_path, pages=pages)
            takeoff = compute_material_takeoff(extraction, waste_pct=waste_pct)
 
            self._send_json(200, {
                "extraction": extraction,
                "takeoff": takeoff,
            })
 
        except Exception as e:
            self._send_json(500, {
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
