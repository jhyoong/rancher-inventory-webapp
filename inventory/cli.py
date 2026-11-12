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


def _serve() -> int:
    import logging

    import uvicorn

    from inventory.collector.thread import CollectorThread
    from inventory.db.connection import connect
    from inventory.db.migrate import migrate
    from inventory.settings import Settings
    from inventory.web.app import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()
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
    scheme = "https" if settings.tls_cert_file and settings.tls_key_file else "http"
    try:
        r = httpx.get(f"{scheme}://127.0.0.1:{settings.listen_port}/healthz", verify=False, timeout=10)
        ok = r.status_code == 200 and r.json().get("ok") is True
    except (httpx.HTTPError, ValueError):
        ok = False
    print("ok" if ok else "not ok")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
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
    print("not implemented yet")
    return 2


if __name__ == "__main__":
    sys.exit(main())
