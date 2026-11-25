#!/usr/bin/env python3
"""启动入口：python run.py [--host 127.0.0.1] [--port 8899]"""
from __future__ import annotations

import argparse
import os

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("WXWB_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("WXWB_PORT", "8899")))
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload,
                log_level="info", proxy_headers=True)


if __name__ == "__main__":
    main()
