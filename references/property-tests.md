# Property-Based Tests (Phase 4, any level with invariants)

Contents: placement · where properties come from · patterns · writing one · generators · settings and CI · pitfalls · counterexamples

## Placement

Property-based tests live **inside the unit and integration test files**, next to the example tests for the same requirement. There is no separate property-test directory. Make them filterable:
- Use the framework marker or tag (`@pytest.mark.property`, a `describe("properties")` block, a `Prop` suffix), and/or a `prop` segment in the name: `test_PRC_014_prop_roundtrip`.
- Keep example tests and property tests for one requirement together so the requirement's coverage reads as one block.

Libraries by ecosystem: Hypothesis (Python), fast-check (JS/TS), jqwik (Java/Kotlin), native fuzzing or `rapid`/`gopter` (Go), RapidCheck (C++), proptest (Rust). Use what the repo already has; ask before adding a dependency.

## Where properties come from

Only from the spec's **Invariants** field. If a requirement has no stated invariant, write example tests only. Do not invent invariants to have properties, and do not derive them by reading the implementation.

Properties complement examples; they do not replace them. Examples document intent; properties explore inputs nobody thought of.

## Patterns to recognize when the spec states an invariant

| Pattern | Shape |
|---|---|
| Round trip | `decode(encode(x)) == x` |
| Bounds | result always within `[min, max]` |
| Preserved quantity | output length/count/sum equals input's |
| Idempotence | `f(f(x)) == f(x)` |
| Order independence | permuting inputs does not change the result |
| Monotonicity | larger input never yields smaller output |
| Metamorphic | doubling inputs doubles outputs; adding irrelevant data changes nothing |
| Independent oracle | compare to a deliberately simple, obviously correct model, or a trusted library |
| Closed outcomes | for any input the function either returns a valid result or raises the documented error, nothing else |
| Stateful safety | for any sequence of operations, an invariant (e.g., balance never negative) holds |

## Writing one

1. State the property in words, taken from the spec (put the sentence in a comment or docstring).
2. Define the input domain exactly as the spec states it.
3. Write the generator for that domain, including boundaries.
4. Assert the property.

Example (Python/Hypothesis):

```python
@pytest.mark.property
@given(st.integers(min_value=0, max_value=10**9))
def test_PRC_014_prop_roundtrip(cents):
    # PRC-014: parse(format(x)) == x for any integer x in [0, 10^9]
    assert parse_price(format_price(cents)) == cents
```

## Generators

- Cover the whole domain, not only "nice" values. Include boundaries, empty, huge, repeated, and odd-but-valid inputs.
- Add explicit examples for known-bad or boundary inputs through the library's example mechanism (`@example` in Hypothesis).
- Constrain generators to **valid** inputs when the property is about valid inputs. Test invalid inputs with separate tests.
- Avoid filtering generators so heavily that most examples are discarded.

## Settings and CI

- Make failures reproducible: fixed seed or the library's example database / derandomize mode in CI, and print the failing example.
- Cap the number of examples in CI for speed; allow more in scheduled runs.
- Set deadlines/timeouts so one slow example does not hang the suite.

## Pitfalls

- **Reimplementing the function inside the property** to compute the expected value. That is a tautology. Use invariants, round trips, or an independent oracle.
- **Generators that mirror the implementation's assumptions**, so the property can never fail.
- **Too-weak properties** ("output is not None"). Each property should be one a plausible bug would violate.
- **Properties without the spec.** If you cannot cite the requirement, do not write it.
- **Flaky properties** from unseeded randomness or time. Inject and seed.

## Counterexamples found later

When a property fails (during implementation or mutation work), the library reports a minimal failing input. Do not weaken the property. Report it. If it reveals a real bug, the implementer fixes the code. If it reveals a spec gap, take it to the user. Once resolved, keep the counterexample as a permanent example test with the requirement ID.
