#!/usr/bin/env python3
"""Export deterministic OpenAPI 3.1 JSON schema from FastAPI without starting a server."""
from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


def main() -> None:
    out_path = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
    dummy_settings = Settings(
        db_path=BACKEND_DIR / ".openapi_tmp.db",
        secret_key="openapi-schema-export-key",
        enable_background_poller=False,
    )
    fastapi_app = create_app(dummy_settings)
    schema = fastapi_app.openapi()
    text = json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"Exported OpenAPI schema -> {out_path}")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
