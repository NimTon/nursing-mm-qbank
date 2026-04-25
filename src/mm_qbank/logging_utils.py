from __future__ import annotations

import logging
import sys


def configure_logging(*, verbose: bool = False, quiet: bool = False) -> None:
    """配置根日志：默认 INFO；``-v`` 为 DEBUG；``-q`` 为 WARNING。"""
    if quiet and verbose:
        quiet = False
    if quiet:
        level = logging.WARNING
    elif verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO
    root = logging.getLogger()
    if root.handlers:
        root.handlers.clear()
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
        force=True,
    )
