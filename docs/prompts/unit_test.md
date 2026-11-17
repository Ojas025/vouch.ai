Spawn a subagent - to plan the test suite for <file>

Use pytest.mark.parametrize to cover input variations.

Each planned test must prove its place:
- Does this test cover a distinct behavior?
- Can its variations be parameterized?
- Is this already covered by another test?
- Does it catch a failure that would otherwise go unnoticed? 

Follow:
- @test-driven-development 
- @.agents/skills/spec-driven-testing/spec_driven_testing.md 

There is no spec written for this tool, 
The tests should be written on what the function is supposed to do, test its security boundaries
These tests are not to be written just for the sake of making them pass
If there's a bug or mistake, the tests should reflect the failure in functionality of the tool.

Present me the plan.
 skill  test-driven-development 
 file  .agents/skills/spec-driven-testing/spec_driven_testing.md
