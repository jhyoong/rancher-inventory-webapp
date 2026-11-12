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
    from inventory.settings import Settings
    from inventory.web import auth

    settings = Settings()
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


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "check-config":
        return _check_config()
    if args.command == "collect-once":
        return _collect_once()
    if args.command == "user":
        return _user_command(args)
    print("not implemented yet")
    return 2


if __name__ == "__main__":
    sys.exit(main())
