## Project Context
- Core Stack: Python 3.14+, FastAPI, FastMCP, SQLAlchemy, Pydantic
- Paradigm: Async-first, strict type-hinting, domain-driven design. 
- Tests: Unit tests should result in over 90% code coverage.
- Write clean, production-ready code. Do not add comments explaining how the code works unless the logic is highly complex or counter-intuitive. No obvious comments.

## Core Coding Principles (KISS & DRY)

### KISS (Keep It Simple, Stupid)
- Write the most straightforward solution that works. 
- Avoid clever hacks, overly dense one-liners, or speculative abstractions for future features.
- If a function or component is hard to explain in one sentence, break it down or simplify it.

### DRY (Don't Repeat Yourself)
- Extract duplicated logic, shared constants, or recurring UI patterns into reusable functions, hooks, or modules.
- *Exception:* Do not over-abstract prematurely. If duplication is superficial or decoupling creates higher complexity than repeating a few lines, keep them separate until a clear pattern emerges.

### Object Oriented
- All functions must be written as methods inside a class (or multiple classes if appropriate). No free-standing functions are allowed.
- All variables must be encapsulated within classes, either as instance attributes (defined in `__init__`), class attributes, or local variables strictly scoped inside methods. No global variables are allowed.
- The only code outside of a class should be a standard `if __name__ == "__main__":` block to instantiate the class and execute its main runner method.

#### Exceptions
These are the only places free-standing functions and module-level names are allowed. Each is forced by a framework's collection mechanism, not chosen:
- `alembic/versions/*` and `alembic/env.py`. Alembic collects `upgrade`, `downgrade` and the `revision` identifiers by name at module level. Migration-private helpers may stay module-level too, since a migration is a script rather than a component. Already excluded from ruff and from coverage in `pyproject.toml`.
- `tests/`. pytest collects `test_*` functions and `@pytest.fixture` factories at module level, and per-module test helpers may sit beside them.
- `routers/mcp.py`. FastMCP's `FileSystemProvider` discovers tools by scanning a module for top-level `Tool` objects, so the two `@tool` adapters cannot be methods. They delegate straight to `ResumeTools`; see that class's docstring.
- `main.py`'s trailing `application`/`app`. An ASGI server resolves `main:app` as a module attribute; this is the same kind of entrypoint as `if __name__ == "__main__":` and is two lines that only instantiate `Application`.

Everywhere else — a module-level constant, logger, or helper — belongs on a class, as a `ClassVar` or a `@staticmethod`. Type aliases (`RevisionOrder`, `Severity`, …) are declarations rather than variables and may stay at module level.
