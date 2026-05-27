
Vercel Serverless Function: /api/extract-cabinets
Receives PDF as base64, returns extracted cabinets + material takeoff.
"""
import json
import base64
import io
import sys
import os

# Add parent directory to path so we can import cabinet_extractor
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cabinet_extractor import extract_pdf, compute_material_takeoff
import pdfplumber
import tempfile


def handler(request):
    """Vercel handler — accepts POST with base64 PDF."""

    # CORS headers
    headers = {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'POST, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type',
        'Content-Type': 'application/json',
    }

    # Handle CORS preflight
    if request.method == 'OPTIONS':
        return {'statusCode': 200, 'headers': headers, 'body': ''}

    if request.method != 'POST':
        return {
            'statusCode': 405,
            'headers': headers,
            'body': json.dumps({'error': 'Method not allowed'}),
        }

    try:
        body = json.loads(request.body) if isinstance(request.body, str) else request.body

        # Get base64 PDF
        pdf_b64 = body.get('pdf_base64')
        if not pdf_b64:
            return {
                'statusCode': 400,
                'headers': headers,
                'body': json.dumps({'error': 'Missing pdf_base64'}),
            }

        # Get specific pages (optional)
        pages = body.get('pages')  # list of 0-indexed page numbers
        waste_pct = body.get('waste_pct', 0.25)

        # Decode PDF
        pdf_bytes = base64.b64decode(pdf_b64)

        # Save to temp file (pdfplumber needs a file path)
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = tmp.name

        try:
            # Extract
            extraction = extract_pdf(tmp_path, pages)
            takeoff = compute_material_takeoff(extraction, waste_pct)

            return {
                'statusCode': 200,
                'headers': headers,
                'body': json.dumps({
                    'success': True,
                    'extraction': extraction,
                    'takeoff': takeoff,
                }),
            }
        finally:
            os.unlink(tmp_path)

    except Exception as e:
        import traceback
        return {
            'statusCode': 500,
            'headers': headers,
            'body': json.dumps({
                'error': str(e),
                'trace': traceback.format_exc(),
            }),
        }
