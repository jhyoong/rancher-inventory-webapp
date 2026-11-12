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


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "check-config":
        return _check_config()
    print("not implemented yet")
    return 2


if __name__ == "__main__":
    sys.exit(main())
