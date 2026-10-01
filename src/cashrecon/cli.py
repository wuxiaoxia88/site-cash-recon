"""Command line entry point: ``cashrecon <command>``."""

from __future__ import annotations

import argparse
import sys
from importlib import resources
from pathlib import Path

from cashrecon import __version__
from cashrecon.config import ConfigError, load_settings, mask
from cashrecon.logging_setup import setup_logging
from cashrecon.paths import Paths, make_private_file


def _example(name: str) -> str:
    """Bundled example files shipped inside the package."""
    return resources.files("cashrecon").joinpath("examples", name).read_text(encoding="utf-8")


def cmd_init(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.home).ensure()
    created = []
    if not paths.config.exists():
        paths.config.write_text(_example("config.example.toml"), encoding="utf-8")
        make_private_file(paths.config)
        created.append(str(paths.config))
    if not paths.secrets.exists():
        paths.secrets.write_text(_example("secrets.env.example"), encoding="utf-8")
        make_private_file(paths.secrets)
        created.append(str(paths.secrets))
    from cashrecon.db import Store
    with Store(paths.database):
        pass
    print(f"数据目录：{paths.home}")
    for item in created:
        print(f"  已创建 {item}（请按实际网点修改）")
    print("下一步：编辑 config.toml 与 secrets.env，然后运行 `cashrecon doctor`。")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from cashrecon.doctor import run_doctor
    return run_doctor(Paths.resolve(args.home), online=not args.offline)


def cmd_version(_: argparse.Namespace) -> int:
    print(__version__)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cashrecon", description="网点现金对账系统")
    parser.add_argument("--home", type=Path, help="数据目录（默认 ~/.site-cash-recon 或 CASHRECON_HOME）")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="显示版本").set_defaults(func=cmd_version)
    sub.add_parser("init", help="初始化数据目录与示例配置").set_defaults(func=cmd_init)
    doctor = sub.add_parser("doctor", help="自检：配置、凭据、数据源、数据库、调度")
    doctor.add_argument("--offline", action="store_true", help="不访问网络")
    doctor.set_defaults(func=cmd_doctor)

    from cashrecon import commands
    commands.register(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command not in {"init", "version"}:
        paths = Paths.resolve(args.home)
        secrets: list[str] = []
        try:
            settings = load_settings(paths)
            secrets = [v for v in settings.secrets.values() if v]
        except ConfigError:
            pass
        setup_logging(paths.logs if paths.home.exists() else None, verbose=args.verbose, secrets=secrets)
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


__all__ = ["main", "mask"]
