"""Operational sub-commands. Heavy modules are imported lazily per command."""

from __future__ import annotations

import argparse


def register(sub: argparse._SubParsersAction) -> None:
    # Filled in as modules are implemented (fetch, recon, report, run, web, schedule ...).
    return None
