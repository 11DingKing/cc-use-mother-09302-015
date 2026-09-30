"""启动捐赠隔离服务。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quarantine_service.api import run_server

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="非遗材料捐赠隔离服务")
    parser.add_argument("--db", default=str(ROOT / "data" / "quarantine.db"), help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    run_server(args.db, args.host, args.port)
