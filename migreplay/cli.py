"""Run the same real rehearsal pipeline for user manifests and bundled demos."""
import argparse
from importlib.resources import files
from pathlib import Path
import platform
import sqlite3

from . import __version__
from .inputs import load_case, no_links
from .model import Diagnostic, InvalidInput, Limits, OutputRequest, SafeError
from .report import validate_destination
from .runner import coordinate_run
from .status import failure_summary


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise InvalidInput('invalid_input')


def _print(summary, label=None):
    prefix = (label + ': ') if label else ''
    print(prefix + f'{summary.status} exit={summary.exit_code} unexpected={summary.unexpected_count} '
          f'expected={summary.expected_count} assertion_failed={summary.assertion_failed_count}')


def _run(manifest, destination):
    limits = Limits()
    manifest = Path(manifest).absolute()
    try:
        case = load_case(manifest, limits)
        summary = coordinate_run(case, limits, sink=OutputRequest(destination, case._input_paths))
    except SafeError as exc:
        # No SQL is dispatched. Protect the manifest location and its parent.
        try:
            no_links(manifest.parent)
        except (OSError, SafeError):
            return failure_summary('invalid_path')
        summary = coordinate_run(None, limits, sink=OutputRequest(destination, (manifest, manifest.parent)),
                                 initial_failure=Diagnostic(exc.code, exc.source))
    return summary


def main(argv=None):
    parser = Parser(prog='migreplay', description='Rehearse SQLite migrations on synthetic data.')
    parser.add_argument('--version', action='version', version=f'MigReplay {__version__} | Python {platform.python_version()} | SQLite {sqlite3.sqlite_version}')
    commands = parser.add_subparsers(dest='command', required=True, parser_class=Parser)
    run = commands.add_parser('run')
    run.add_argument('--manifest', required=True, type=Path)
    run.add_argument('--out-dir', type=Path, default=Path('reports/run'))
    demo = commands.add_parser('demo')
    demo.add_argument('--out-dir', type=Path, default=Path('reports/demo'))
    try:
        args = parser.parse_args(argv)
        if args.command == 'run':
            summary = _run(args.manifest, args.out_dir)
            _print(summary)
            return summary.exit_code
        destination = validate_destination(args.out_dir, ())
        results = []
        for name in ('notes-loss', 'notes-preserved'):
            manifest = Path(str(files('migreplay').joinpath('demo_cases', name, 'case.json')))
            summary = _run(manifest, destination / name)
            _print(summary, name)
            results.append(summary.exit_code)
        return max(results)
    except KeyboardInterrupt:
        summary = failure_summary('cancelled')
    except SafeError as exc:
        summary = failure_summary(exc.code)
    except (OSError, ValueError):
        summary = failure_summary('invalid_input')
    _print(summary)
    return summary.exit_code
