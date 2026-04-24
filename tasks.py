from invoke.tasks import task


@task
def test(c, cov=False, k=None):
    """Run the test suite."""
    cmd = "pytest -q"
    if cov:
        cmd += " --cov --cov-report=term-missing"
    if k:
        cmd += f" -k {k!r}"
    c.run(cmd)


@task
def lint(c):
    """Run ruff linter."""
    c.run("ruff check .")


@task
def fmt(c, check=False):
    """Format code with ruff."""
    cmd = "ruff format ."
    if check:
        cmd += " --check"
    c.run(cmd)


@task
def typecheck(c):
    """Run pyright type checker."""
    c.run("pyright")


@task
def check(c):
    """Run lint + typecheck + tests."""
    lint(c)
    typecheck(c)
    test(c)
