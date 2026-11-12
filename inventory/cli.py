import argparse
import asyncio
import sys


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="inventory")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("check-config")
    sub.add_parser("collect-once")
    sub.add_parser("healthcheck")
    bk = sub.add_parser("backup", help="consistent copy of the database while the app runs")
    bk.add_argument("file", help="destination file; must not exist")
    exp = sub.add_parser("export-discovery", help="anonymised discovery export (tar.gz)")
    exp.add_argument("--out", required=True, help="folder for the tar.gz")
    exp.add_argument("--site", help="only this site")
    exp.add_argument("--cluster", action="append", default=[], help="cluster id or site/id (repeatable)")
    exp.add_argument("--strict", action="store_true", help="also hash image, chart, CRD and StorageClass names")
    user = sub.add_parser("user")
    usub = user.add_subparsers(dest="user_command", required=True)
    for name in ("add", "passwd", "list", "disable"):
        sp = usub.add_parser(name)
        if name != "list":
            sp.add_argument("username")
        if name == "add":
            sp.add_argument("--role", choices=["admin", "viewer"], required=True)
    return p


def _check_config() -> int:
    from inventory.checkconfig import check_config, format_report
    from inventory.settings import ConfigError, Settings

    try:
        checks = asyncio.run(check_config(Settings()))
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    print(format_report(checks))
    return 0 if all(c.ok for c in checks) else 1


def _collect_once() -> int:
    import time

    from inventory.collector.runner import RunInProgress, Scope, create_run, execute_run
    from inventory.db.connection import connect
    from inventory.db.migrate import migrate
    from inventory.settings import Settings

    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        run_id = create_run(conn, "manual", "cli", Scope())
    except RunInProgress as e:
        print(f"run {e.run_id} is already in progress", file=sys.stderr)
        return 1
    finally:
        conn.close()
    t0 = time.monotonic()
    status = asyncio.run(execute_run(settings, run_id))
    print(f"run {run_id}: {status} in {time.monotonic() - t0:.1f} s")
    return 0 if status in ("ok", "partial") else 1


def _user_command(args) -> int:
    import getpass

    from inventory.db.connection import connect
    from inventory.db.migrate import migrate
    from inventory.settings import ConfigError, Settings
    from inventory.web import auth

    try:
        settings = Settings()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        cmd = args.user_command
        if cmd == "list":
            for u in auth.list_users(conn):
                print(f"{u.username}\t{u.role}\t{'disabled' if u.disabled else 'enabled'}")
        elif cmd == "disable":
            auth.set_disabled(conn, args.username, True)
            print(f"disabled {args.username}")
        else:
            password = getpass.getpass("Password: ")
            if password != getpass.getpass("Repeat password: "):
                raise ValueError("passwords do not match")
            if cmd == "add":
                auth.create_user(conn, args.username, password, args.role)
                print(f"created {args.username}")
            else:
                auth.set_password(conn, args.username, password)
                print(f"password changed for {args.username}")
        return 0
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def _tls_pair_ok(settings) -> bool:
    if bool(settings.tls_cert_file) != bool(settings.tls_key_file):
        print("config error: set both TLS_CERT_FILE and TLS_KEY_FILE, or neither", file=sys.stderr)
        return False
    return True


def _serve() -> int:
    import logging

    import uvicorn

    from inventory.collector.thread import CollectorThread
    from inventory.db.connection import connect
    from inventory.db.migrate import migrate
    from inventory.settings import Settings
    from inventory.web.app import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # no line per Rancher request
    settings = Settings()
    if not _tls_pair_ok(settings):
        return 1
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    try:
        migrate(conn)  # the collector marks unfinished runs before the app starts
    finally:
        conn.close()
    collector = CollectorThread(settings)
    collector.start()
    tls = {}
    if settings.tls_cert_file and settings.tls_key_file:
        tls = {"ssl_certfile": settings.tls_cert_file, "ssl_keyfile": settings.tls_key_file}
    try:
        uvicorn.run(create_app(settings, collector), host="0.0.0.0", port=settings.listen_port, **tls)
    finally:
        collector.stop(timeout=60)
    return 0


def _healthcheck() -> int:
    import httpx

    from inventory.settings import Settings

    settings = Settings()
    if not _tls_pair_ok(settings):
        return 1
    scheme = "https" if settings.tls_cert_file and settings.tls_key_file else "http"
    try:
        r = httpx.get(f"{scheme}://127.0.0.1:{settings.listen_port}/healthz", verify=False, timeout=10)
        ok = r.status_code == 200 and r.json().get("ok") is True
    except (httpx.HTTPError, ValueError):
        ok = False
    print("ok" if ok else "not ok")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    from pydantic import ValidationError

    args = _build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except ValidationError as e:
        for err in e.errors():
            field = ".".join(str(part) for part in err["loc"]).upper()
            print(f"config error: {field}: {err['msg'].removeprefix('Value error, ')}", file=sys.stderr)
        return 1


def backup(db_path, dest) -> None:
    """Copy the database with SQLite's online backup, then check the copy's integrity."""
    import sqlite3
    from pathlib import Path

    dest = Path(dest)
    if dest.exists():
        raise FileExistsError(f"{dest} already exists")
    src = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
    out = sqlite3.connect(dest)
    try:
        src.backup(out)
        result = out.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        out.close()
        src.close()
    if result != "ok":
        raise RuntimeError(f"integrity check of {dest} failed: {result}")


def _backup(args) -> int:
    from inventory.settings import Settings

    settings = Settings()
    try:
        backup(settings.db_path, args.file)
    except (FileExistsError, RuntimeError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"backup written to {args.file}, integrity ok")
    return 0


def _export_discovery(args) -> int:
    from pathlib import Path

    from inventory.collector.runner import Scope
    from inventory.db.connection import connect
    from inventory.db.migrate import migrate
    from inventory.export.discovery import ExportBlocked, ExportOptions, build_export
    from inventory.settings import ConfigError, Settings

    settings = Settings()
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        clusters = None
        if args.cluster:
            known = conn.execute("SELECT site, cluster_id FROM clusters").fetchall()
            clusters = []
            for item in args.cluster:
                site, _, cid = item.rpartition("/")
                site = site or args.site
                match = [(s, c) for s, c in known if c == cid and (site is None or s == site)]
                if len(match) != 1:
                    print(f"error: cluster {item!r} not found or ambiguous; use site/id", file=sys.stderr)
                    return 1
                clusters.append(match[0])
        scope = Scope(site=args.site, clusters=clusters)
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        path = build_export(conn, settings, ExportOptions(scope, strict=args.strict), out)
    except ExportBlocked as e:
        print("export blocked by the self-check:", file=sys.stderr)
        for h in e.hits:
            print(f"  {h.file} column {h.column}: {h.value_sample}", file=sys.stderr)
        return 1
    except (ConfigError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(path)
    return 0


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "check-config":
        return _check_config()
    if args.command == "collect-once":
        return _collect_once()
    if args.command == "user":
        return _user_command(args)
    if args.command == "serve":
        return _serve()
    if args.command == "healthcheck":
        return _healthcheck()
    if args.command == "backup":
        return _backup(args)
    if args.command == "export-discovery":
        return _export_discovery(args)
    print("not implemented yet")
    return 2


if __name__ == "__main__":
    sys.exit(main())
