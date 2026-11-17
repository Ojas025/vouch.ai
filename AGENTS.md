# Agent Instructions

- Implement only what the task asks for. Avoid unrelated changes, speculative features, and broad refactors.
- Inspect relevant code and the working tree first. Follow existing patterns and preserve unrelated user changes.
- Prefer the simplest solution. Reuse existing abstractions; avoid unnecessary classes, wrappers, dependencies, and configuration.
- Do not change business logic, public contracts, authorization, execution order, retries, timeouts, or error semantics unless explicitly asked. If the task appears to require this, explain why before changing it.
- Use `uv` for Python dependencies and project commands. Follow the conventions in `pyproject.toml`; do not use `pip` directly.
- Treat external, LLM, and tool data as untrusted. Never log secrets, raw prompts, artifact contents, or unrestricted tool arguments/results.
- Run relevant tests and checks after changes. Report what actually ran and its result; never claim unrun checks passed.
- Review the final diff and ensure it contains only intended changes.
- Always work on a separate branch, unless asked otherwise for a small task. Do not make too many commits, always use commit prefixes and scope.
- Do not add multiline comments or docstrings, keep them minimal and informative
- For writing tests, always use the @test-driven-development skill.
