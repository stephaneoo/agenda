"""Expand/contract check for Django migrations.

Lints every migration file added since the last deploy (the latest `deploy-*` tag,
or the first commit when there is none) with django-migration-linter's SQL analyser,
one operation at a time. A backward-incompatible operation fails the check unless it
carries a `# contract-of: <app>.<migration>` comment naming an expand migration that
was already present at that deploy.

    python -m tools.check_migrations [--base REF]

It needs a database connection to render SQL; run it under postgis-fixture.
"""

from __future__ import annotations

import argparse
import ast
import io
import os
import re
import subprocess
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path

CONTRACT_OF = re.compile(r"#\s*contract-of:\s*([\w]+)\.([\w]+)")


@dataclass(frozen=True)
class Violation:
    migration: str  # "<app>.<name>"
    operation: int | None  # index in `operations`, None for the whole file
    message: str


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def default_base_ref(repo: Path) -> str:
    """The latest deploy-* tag, or the first commit when nothing was deployed yet."""
    tags = sorted(_git(repo, "tag", "--list", "deploy-*").split())
    if tags:
        return tags[-1]
    return _git(repo, "rev-list", "--max-parents=0", "HEAD").split()[-1]


def _changed_migration_files(repo: Path, base_ref: str) -> list[tuple[str, Path]]:
    """(status, path) of migration files added or modified since base_ref, untracked included."""
    out = _git(repo, "diff", "--name-status", "--no-renames", "--diff-filter=AM", base_ref, "--")
    entries = [(line.split("\t", 1)[0], line.split("\t", 1)[1]) for line in out.splitlines() if line]
    entries += [("A", p) for p in _git(repo, "ls-files", "--others", "--exclude-standard").splitlines()]
    return [
        (status, repo / p)
        for status, p in entries
        if Path(p).parent.name == "migrations" and re.match(r"\d{4}_\w+\.py$", Path(p).name)
    ]


def _app_label_for(path: Path) -> str | None:
    from django.apps import apps

    migrations_dir = path.resolve().parent
    for config in apps.get_app_configs():
        if (Path(config.path).resolve() / "migrations") == migrations_dir:
            return config.label
    return None


def _contract_refs(source: str) -> dict[int, tuple[str, str]]:
    """Map each operation index to the (app, migration) its contract-of comment names.

    A comment belongs to an operation when it sits inside the operation's source, or on
    the lines between the previous operation and this one.
    """
    comments = [
        (tok.start[0], m.groups())
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type == tokenize.COMMENT and (m := CONTRACT_OF.search(tok.string))
    ]
    ops_list = None
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "operations" for t in node.targets
        ):
            ops_list = node.value
    if not isinstance(ops_list, ast.List):
        return {}
    refs: dict[int, tuple[str, str]] = {}
    previous_end = ops_list.lineno
    for index, op in enumerate(ops_list.elts):
        end = op.end_lineno or op.lineno
        for line, ref in comments:
            if previous_end < line <= end or (index == 0 and line == ops_list.lineno):
                refs[index] = ref
        previous_end = end
    return refs


def _operation_sql(app_label: str, name: str) -> list[list[str]]:
    """SQL of each operation of a migration, rendered without touching the database."""
    from django.db import connection
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(connection, ignore_no_migrations=True)
    migration = loader.get_migration(app_label, name)
    state = loader.project_state((app_label, name), at_end=False)
    rendered: list[list[str]] = []
    for operation in migration.operations:
        new_state = state.clone()
        operation.state_forwards(app_label, new_state)
        if not operation.reduces_to_sql:
            rendered.append([])
        else:
            with connection.schema_editor(collect_sql=True, atomic=False) as editor:
                operation.database_forwards(app_label, editor, state, new_state)
            rendered.append(list(editor.collected_sql))
        state = new_state
    return rendered


def _lint(app_label: str, name: str) -> list[list[str]]:
    """Error messages of each operation."""
    from django.db import connection
    from django_migration_linter.sql_analyser import analyse_sql_statements, get_sql_analyser_class

    analyser = get_sql_analyser_class(connection.vendor)
    issues: list[list[str]] = []
    created_tables: list[str] = []
    for statements in _operation_sql(app_label, name):
        # Tables created by earlier operations of the same migration are new, so
        # constraints added to them can't break running code. Give the analyser that context.
        errors, _ignored, _warnings = analyse_sql_statements(analyser, created_tables + statements)
        issues.append([f"{e.code}: {e.message}" + (f" ({e.table})" if e.table else "") for e in errors])
        created_tables += [s for s in statements if s.startswith("CREATE TABLE")]
    return issues


def _present_at(repo: Path, ref: str, app_label: str, name: str) -> bool:
    from django.apps import apps

    try:
        migrations_dir = Path(apps.get_app_config(app_label).path).resolve() / "migrations"
    except LookupError:
        return False
    rel = (migrations_dir / f"{name}.py").relative_to(repo)
    cmd = ["git", "cat-file", "-e", f"{ref}:{rel.as_posix()}"]
    return subprocess.run(cmd, cwd=repo, capture_output=True).returncode == 0


def check_migrations(base_ref: str, repo: Path | None = None) -> list[Violation]:
    repo = Path(_git(repo or Path.cwd(), "rev-parse", "--show-toplevel").strip())
    violations: list[Violation] = []
    for status, path in _changed_migration_files(repo, base_ref):
        app_label = _app_label_for(path)
        if app_label is None:
            continue  # not a migration of an installed app (e.g. test fixtures)
        name = path.stem
        label = f"{app_label}.{name}"
        if status == "M":
            violations.append(Violation(label, None, f"modified after it was deployed at {base_ref}"))
            continue
        refs = _contract_refs(path.read_text())
        # Every reference is checked, even on an operation that needs none, so a typo can't hide.
        for index, (ref_app, ref_name) in sorted(refs.items()):
            if not _present_at(repo, base_ref, ref_app, ref_name):
                violations.append(Violation(
                    label, index, f"contract-of {ref_app}.{ref_name}: that expand migration is not deployed yet ({base_ref})"
                ))
        for index, errors in enumerate(_lint(app_label, name)):
            if errors and index not in refs:
                violations.append(Violation(label, index, "backward-incompatible: " + "; ".join(errors)))
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", help="git ref of the last deploy (default: latest deploy-* tag)")
    args = parser.parse_args()

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agenda_site.settings")
    import django

    django.setup()
    repo = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
    base = args.base or default_base_ref(repo)
    violations = check_migrations(base, repo)
    for v in violations:
        where = f"operation {v.operation}" if v.operation is not None else "file"
        print(f"{v.migration} [{where}]: {v.message}", file=sys.stderr)
    print(f"check_migrations: {len(violations)} violation(s) since {base}")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
