"""Unit test tree.

Unit tests are pure logic: no PostgreSQL, no Redis, no HTTP. If a test in this
tree needs a live service it is an integration test and belongs in
``tests/integration/`` — the fixtures there are deliberately not importable
from here, so the boundary is enforced by the directory, not by discipline.
"""