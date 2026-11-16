"""Capture a local backup, HTTP contract and offline pytest baseline.

Run with .venv/Scripts/python.exe scripts/capture_refactor_baseline.py.
Does not repair or mutate Git metadata. Backups include local configuration.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
import zipfile


ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
OUT = ROOT / "recovery-backups" / ("baseline-" + STAMP)
OUT.mkdir(parents=True)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    os.chdir(ROOT)
    excluded = {"node_modules", ".venv", "dist", ".pytest_tmp", ".pytest_cache",
                "__pycache__", "recovery-backups"}
    manifest = {}
    archive_path = OUT / "workspace-and-git.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in excluded]
            for name in files:
                path = Path(base) / name
                relative = path.relative_to(ROOT).as_posix()
                archive.write(path, relative)
                manifest[relative] = digest(path)
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.testzip() is None
    write_json("backup-manifest.json", manifest)
    write_json("backup-checksum.json", {"sha256": digest(archive_path), "files": len(manifest)})
    print(f"Verified backup: {OUT}", flush=True)

    # Tests run with an in-memory default DB and isolated runtime files, even if
    # one call path fails to use the test client's dependency override.
    os.environ["DATABASE_URL"] = "sqlite://"
    sys.path.insert(0, str(ROOT / "apps" / "server"))
    from app.core import config
    runtime = OUT / "runtime"
    for key, directory in (("DATA_DIR", runtime), ("UPLOAD_DIR", runtime / "uploads"),
                           ("ASSET_DIR", runtime / "assets"), ("PREVIEW_DIR", runtime / "previews"),
                           ("EXPORT_DIR", runtime / "exports"), ("LOG_DIR", runtime / "logs")):
        directory.mkdir(parents=True, exist_ok=True)
        setattr(config, key, directory)
    config.JIANYING_DRAFT_DIR = str(runtime / "jianying")

    attempts = []
    local_socketpair = threading.local()
    original_socketpair = socket.socketpair
    def internal_socketpair(*args, **kwargs):
        # Windows implements asyncio's internal wakeup pipe with loopback TCP.
        # Allow only creation of that local socket pair, not application traffic.
        local_socketpair.active = True
        try:
            return original_socketpair(*args, **kwargs)
        finally:
            local_socketpair.active = False
    def blocked_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not getattr(local_socketpair, "active", False):
            attempts.append({"host": str(address[0]), "port": address[1],
                             "test": os.environ.get("PYTEST_CURRENT_TEST", "outside pytest")})
            raise OSError("Baseline runner blocks TCP network access")
        return original_connect(sock, address)
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    socket.socket.connect = blocked_connect
    socket.socket.connect_ex = blocked_connect
    socket.socketpair = internal_socketpair
    try:
        from app.main import app
        schema = app.openapi()  # No lifespan/startup and no database migrations.
        write_json("openapi.json", schema)
        methods = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}
        endpoints = [{"method": method.upper(), "path": path,
                      "operationId": operation.get("operationId")}
                     for path, item in schema["paths"].items()
                     for method, operation in item.items() if method in methods]
        write_json("endpoints.json", endpoints)
        import pytest
        code = int(pytest.main(["-q", "--tb=short", "--basetemp", str(OUT / "pytest-temp"),
                               "--junitxml", str(OUT / "pytest-results.xml")]))
    finally:
        socket.socket.connect = original_connect
        socket.socket.connect_ex = original_connect_ex
        socket.socketpair = original_socketpair
        write_json("network-attempts.json", attempts)

    changed_sources = [name for name, checksum in manifest.items()
                       if name.startswith(("apps/server/app/", "apps/web/src/", "tests/", "docs/"))
                       and (not (ROOT / name).is_file() or digest(ROOT / name) != checksum)]
    db_names = [name for name in manifest if name.endswith((".db", ".sqlite", ".sqlite3"))]
    changed_databases = [name for name in db_names
                         if not (ROOT / name).is_file() or digest(ROOT / name) != manifest[name]]
    write_json("summary.json", {"created_at_shanghai": STAMP, "pytest_exit_code": code,
               "paths": len(schema["paths"]), "operations": len(endpoints),
               "tcp_attempts_blocked": len(attempts), "changed_sources": changed_sources,
               "changed_databases": changed_databases,
               "limits": ["OpenAPI does not describe all SSE events or actual response shapes.",
                          "No real model, object storage, Redis or PostgreSQL integration verified."]})
    print(f"Baseline artifacts: {OUT}", flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
