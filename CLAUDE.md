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