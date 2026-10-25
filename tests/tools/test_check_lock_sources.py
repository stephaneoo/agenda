from pathlib import Path

from tools.check_lock_sources import ROOT, check_lock_sources

GOOD = '''
version = 1

[[package]]
name = "agenda"
version = "0.1.0"
source = { virtual = "." }

[[package]]
name = "django"
version = "5.2.7"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://files.pythonhosted.org/packages/aa/django-5.2.7.tar.gz", hash = "sha256:00" }
wheels = [
    { url = "https://files.pythonhosted.org/packages/bb/django-5.2.7-py3-none-any.whl", hash = "sha256:00" },
]
'''


def lock(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "uv.lock"
    path.write_text(text)
    return path


def test_pypi_only_lock_passes(tmp_path: Path) -> None:
    assert check_lock_sources(lock(tmp_path, GOOD)) == []


def test_wheel_from_another_host_fails(tmp_path: Path) -> None:
    text = GOOD.replace("https://files.pythonhosted.org/packages/bb", "https://evil.example/packages/bb")
    problems = check_lock_sources(lock(tmp_path, text))
    assert len(problems) == 1 and "django" in problems[0] and "evil.example" in problems[0]


def test_lookalike_prefix_fails(tmp_path: Path) -> None:
    text = GOOD.replace("https://files.pythonhosted.org/packages/aa", "https://files.pythonhosted.org.evil.example/aa")
    assert check_lock_sources(lock(tmp_path, text))


def test_direct_url_git_and_path_sources_fail(tmp_path: Path) -> None:
    extra = '''
[[package]]
name = "a"
version = "1"
source = { url = "http://example.org/a-1.tar.gz" }

[[package]]
name = "b"
version = "1"
source = { git = "https://github.com/x/b?rev=1#abc" }

[[package]]
name = "c"
version = "1"
source = { path = "../c" }

[[package]]
name = "d"
version = "1"
source = { registry = "https://mirror.example/simple" }
'''
    problems = check_lock_sources(lock(tmp_path, GOOD + extra))
    assert {p.split(":")[0] for p in problems} == {"a 1", "b 1", "c 1", "d 1"}


def test_repo_lock_passes() -> None:
    assert check_lock_sources(ROOT / "uv.lock") == []
