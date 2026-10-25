import json
import subprocess
from pathlib import Path

import pytest

from tools.check_openapi_version import check_openapi_version


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args], cwd=repo, check=True,
                   capture_output=True)


def spec(version: str, paths: dict | None = None) -> str:
    return json.dumps({"openapi": "3.1.0", "info": {"version": version}, "paths": paths or {}})


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "README").write_text("x")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "root")
    (tmp_path / "api").mkdir()
    return tmp_path


def commit(repo: Path, text: str, tag: str | None = None) -> None:
    (repo / "api" / "openapi.json").write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "spec")
    if tag:
        git(repo, "tag", tag)


def test_no_deploy_tag_and_new_file_passes(repo: Path) -> None:
    commit(repo, spec("1.0.0"))
    ok, message = check_openapi_version(repo)
    assert ok and "nothing deployed" in message


def test_changed_spec_without_bump_fails(repo: Path) -> None:
    commit(repo, spec("1.0.0"), tag="deploy-20261025T000000Z")
    (repo / "api" / "openapi.json").write_text(spec("1.0.0", {"/x": {}}))
    ok, message = check_openapi_version(repo)
    assert not ok and "info.version" in message


def test_changed_spec_with_bump_passes(repo: Path) -> None:
    commit(repo, spec("1.0.0"), tag="deploy-20261025T000000Z")
    (repo / "api" / "openapi.json").write_text(spec("1.1.0", {"/x": {}}))
    assert check_openapi_version(repo)[0]


def test_unchanged_spec_passes(repo: Path) -> None:
    commit(repo, spec("1.0.0"), tag="deploy-20261025T000000Z")
    assert check_openapi_version(repo)[0]
