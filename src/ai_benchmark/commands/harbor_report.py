"""``harbor-report`` subcommand: summarise Harbor job output.

Parses one or more Harbor job/trial directories and prints a per-trial
regression table (task x condition, resolved bit, and FAIL_TO_PASS /
PASS_TO_PASS pass/fail breakdown with regressed test names). Optionally writes
the machine-readable JSON and the Markdown table to files.
"""

import argparse
import logging
from pathlib import Path

from ai_benchmark.harbor_report import (
    parse_paths,
    reports_to_json,
    reports_to_markdown,
)

_logger = logging.getLogger(__name__)


def add_parser(
    subparsers: argparse._SubParsersAction,
    common_parser: argparse.ArgumentParser,
) -> argparse.ArgumentParser:
    """Register the ``harbor-report`` subcommand.

    :param subparsers: Subparser action from the main parser.
    :param common_parser: Parent parser with common arguments.
    :return: The created subparser.
    """
    parser = subparsers.add_parser(
        "harbor-report",
        parents=[common_parser],
        help="Summarise Harbor job output at PASS_TO_PASS granularity",
        description=(
            "Parse Harbor job/trial directories and emit a per-trial regression "
            "table plus optional JSON."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="Harbor job or trial directories (searched recursively)",
    )
    parser.add_argument(
        "--json",
        dest="json_out",
        type=Path,
        default=None,
        help="Write the parsed records as JSON to this path",
    )
    parser.add_argument(
        "--markdown",
        dest="markdown_out",
        type=Path,
        default=None,
        help="Write the Markdown table to this path",
    )
    parser.set_defaults(func=run)
    return parser


def run(args: argparse.Namespace) -> int:
    """Execute the ``harbor-report`` command.

    :param args: Parsed command-line arguments.
    :return: Exit code (0 on success, 1 if no trials were found).
    """
    reports = parse_paths(args.paths)
    if not reports:
        _logger.error(
            "No Harbor trials found under: %s",
            ", ".join(str(path) for path in args.paths),
        )
        return 1

    _logger.info("Parsed %d trial(s)", len(reports))
    markdown = reports_to_markdown(reports)

    if args.json_out is not None:
        args.json_out.write_text(reports_to_json(reports) + "\n", encoding="utf-8")
        _logger.info("Wrote JSON to %s", args.json_out)
    if args.markdown_out is not None:
        args.markdown_out.write_text(markdown + "\n", encoding="utf-8")
        _logger.info("Wrote Markdown to %s", args.markdown_out)

    print(markdown)  # noqa: T201 -- primary CLI output
    return 0
