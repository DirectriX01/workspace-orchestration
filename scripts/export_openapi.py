"""Export the FastAPI app's OpenAPI schema to ``docs/openapi.json``.

Standalone entrypoint (adds the project root to ``sys.path``, like
``scripts/seed_and_sync.py``). The three provider knobs are forced to their
deterministic, network-free modes *before* ``app.main`` is imported, mirroring
``tests/conftest.py``, so this never needs a database, Redis, or an OpenAI
key -- it only needs to import the ASGI app object to read its route table.

Run::

    .venv/bin/python scripts/export_openapi.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# Force deterministic providers BEFORE importing anything from ``app``.        #
# Environment variables take precedence over any values in a local .env file,  #
# so this reliably pins the fake LLM / fake embeddings / mock Google clients.  #
# --------------------------------------------------------------------------- #
os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("EMBEDDINGS_PROVIDER", "fake")
os.environ.setdefault("MOCK_GOOGLE", "true")
os.environ.setdefault("OPENAI_API_KEY", "")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.main import app  # noqa: E402

_OUTPUT_PATH = _PROJECT_ROOT / "docs" / "openapi.json"


def main() -> None:
    """Write the app's OpenAPI schema to ``docs/openapi.json``."""
    schema = app.openapi()
    _OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT_PATH.write_text(
        json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    route_count = len(schema.get("paths", {}))
    print(f"Wrote {_OUTPUT_PATH} ({route_count} paths)")


if __name__ == "__main__":
    main()
