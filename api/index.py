"""
Vercel serverless entry point.

Vercel's Python runtime serves the WSGI application object named `app`. We reuse
the exact same Flask app that runs locally (see app.py at the repo root), so
there is one codebase for both `python app.py` and the deployed function.

The repo root is added to sys.path so `app` and `newsletter_digest` import
cleanly from inside the api/ function directory.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402  (must follow the sys.path tweak)

# Vercel looks for a module-level `app` WSGI callable.
__all__ = ["app"]
