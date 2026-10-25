"""Expand/contract check for Django migrations.

Lints every migration Django would load that wasn't present at the last deploy (the
latest `deploy-*` tag, or the first commit when there is none) with
django-migration-linter's SQL analyser, one operation at a time. A backward-incompatible
operation fails the check unless it carries a `# contract-of: <app>.<migration>` comment
naming an expand migration of the same app, already deployed, that acts on the same model
and field; a removed or renamed field must also be nullable or have a db_default at that
deploy. Editing or deleting a deployed migration fails too.

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
from typing import Any

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


def _app_migration_dirs(repo: Path) -> dict[str, str]:
    """Installed apps whose migrations live in this repo: label -> migrations dir, repo-relative."""
    from django.apps import apps

    dirs = {}
    for config in apps.get_app_configs():
        path = Path(config.path).resolve() / "migrations"
        if path.is_relative_to(repo):
            dirs[config.label] = path.relative_to(repo).as_posix()
    return dirs


def _files_at(repo: Path, ref: str) -> set[str]:
    return set(_git(repo, "ls-tree", "-r", "--name-only", ref).splitlines())


def _same_at(repo: Path, ref: str, rel: str) -> bool:
    deployed = subprocess.run(["git", "show", f"{ref}:{rel}"], cwd=repo, capture_output=True, check=True).stdout
    return deployed == (repo / rel).read_bytes()


def _target(operation: Any) -> tuple[str | None, set[str]]:
    """(model, field names) an operation acts on. Field names are empty for model operations."""
    if hasattr(operation, "model_name"):
        names = {getattr(operation, a) for a in ("name", "old_name", "new_name") if hasattr(operation, a)}
        return operation.model_name.lower(), {n.lower() for n in names}
    for attr in ("name", "old_name"):
        if hasattr(operation, attr):
            return getattr(operation, attr).lower(), set()
    return None, set()


def _contract_problem(
    loader: Any, base_state: Any, app_label: str, operation: Any, ref: tuple[str, str], deployed: set[tuple[str, str]]
) -> str | None:
    """Why `operation` can't be the contract step of the expand migration `ref`, or None."""
    from django.db.models import NOT_PROVIDED
    from django.db.migrations import RemoveField, RenameField

    ref_app, ref_name = ref
    label = f"contract-of {ref_app}.{ref_name}"
    if ref_app != app_label:
        return f"{label}: the expand migration must be in the same app ({app_label})"
    if (ref_app, ref_name) not in deployed:
        return f"{label}: that expand migration is not deployed yet"
    model, fields = _target(operation)
    field = operation.name if isinstance(operation, RemoveField) else getattr(operation, "old_name", None)
    expand = loader.get_migration(ref_app, ref_name)
    matches = [
        op for op in expand.operations
        if _target(op)[0] == model and (not fields or fields & _target(op)[1])
    ]
    if model is None or not matches:
        return f"{label}: it has no operation on the same model and field"
    if isinstance(operation, (RemoveField, RenameField)) and field:
        try:
            state_field = base_state.models[app_label, model].fields[field.lower()]
        except KeyError:
            return f"{label}: {model}.{field} not found in the deployed schema"
        if not (state_field.null or state_field.db_default is not NOT_PROVIDED):
            return f"{label}: {model}.{field} must be nullable or have a db_default at the last deploy"
    return None


def check_migrations(base_ref: str, repo: Path | None = None) -> list[Violation]:
    from django.db import connection
    from django.db.migrations.loader import MigrationLoader

    repo = Path(_git(repo or Path.cwd(), "rev-parse", "--show-toplevel").strip()).resolve()
    dirs = _app_migration_dirs(repo)
    at_base = _files_at(repo, base_ref)
    loader = MigrationLoader(connection, ignore_no_migrations=True)
    on_disk = {key for key in loader.disk_migrations if key[0] in dirs}
    path_of = {key: f"{dirs[key[0]]}/{key[1]}.py" for key in on_disk}
    deployed = {key for key in on_disk if path_of[key] in at_base}

    violations: list[Violation] = []
    # A deployed migration that is gone or changed no longer matches the deployed schema.
    for app_label, d in dirs.items():
        for rel in sorted(at_base):
            name = Path(rel).stem
            if Path(rel).parent.as_posix() == d and rel.endswith(".py") and not name.startswith("_"):
                if (app_label, name) not in on_disk:
                    violations.append(Violation(f"{app_label}.{name}", None, f"deleted after it was deployed at {base_ref}"))
    for key in sorted(deployed):
        if not _same_at(repo, base_ref, path_of[key]):
            violations.append(Violation(f"{key[0]}.{key[1]}", None, f"modified after it was deployed at {base_ref}"))

    deployed_leaves = [
        key for key in loader.graph.nodes
        if (key in deployed or key[0] not in dirs)
        and not any(c.key in deployed for c in loader.graph.node_map[key].children)
    ]
    base_state = loader.project_state(nodes=deployed_leaves, at_end=True)

    for key in sorted(on_disk - deployed):
        app_label, name = key
        label = f"{app_label}.{name}"
        migration = loader.get_migration(app_label, name)
        refs = _contract_refs((repo / path_of[key]).read_text())
        # Every reference is checked, even on an operation that needs none, so a typo can't hide.
        for index, ref in sorted(refs.items()):
            if index >= len(migration.operations):
                continue
            problem = _contract_problem(loader, base_state, app_label, migration.operations[index], ref, deployed)
            if problem:
                violations.append(Violation(label, index, problem))
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
