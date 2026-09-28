# qa-ceg — cause-effect graphing QA

A QA system built on one doctrine: **a good cause-effect graph never uses
an OR gate.** All logic is mapped with AND plus carefully constrained
inputs. Every input on the page goes in the graph, every requirement maps
to effects, every possible outcome is evaluated, and only then is the test
suite generated.

## The rules (`CEG_FORMAT.md`)

1. No OR gates. Apparent "or" is one of three things, and must be named:
   - `O(...)` — one and only one (exclusive)
   - `I(...)` — inclusive (at least one)
   - unconstrained — bifurcate: enumerate every combination
2. Success logic and failure logic are separate graphs (`[SUCCESS]`,
   `[FAIL]`). Masks use `M(a, b)`: while `a` holds, `b`'s logic is hidden.
3. Inputs are bifurcated into explicit states (`name : off | on`).
   State index 0 is always the off/absent/inactive state.
4. Every requirement points at the effect(s) its logic produces.

## Layout

```
CEG_FORMAT.md   the format and the rules
ceg.py          parser, Cartesian enumerator, constraint evaluator,
                AND-only success/failure evaluator, suite generator.
                Exit code = number of requirements with no covering case.
ceg/            one .ceg graph per product or flow
suites/         generated test suites (.tests.md grouped by distinct
                outcome signature; full .tests.json is regenerable)
qa_daily.py     daily runner: detect -> regen -> test -> issues -> sync
```

## Usage

```bash
python3 ceg.py --all        # regenerate every suite; must exit 0
python3 ceg.py --graph forum  # regenerate one suite
python3 qa_daily.py detect  # new commits since watermarks
python3 qa_daily.py regen   # re-run the generator; fail if uncovered
python3 qa_daily.py test    # run automated checks on the current release
python3 qa_daily.py issues  # file deduped GitHub issues for failures
python3 qa_daily.py sync    # reconcile GitHub qa issues with the bug database
```

`ceg.py --all` exits nonzero if any requirement has no covering test
case — the graphs are the contract, and the contract must be total.

## Roles

The QA manager finds and flags issues with reproduction steps. It never
debugs and never fixes — programmers debug their own issues. Findings are
filed as GitHub issues (label `qa`, deduplicated by title) and tracked in
a local bug database (`bugs.db`, not published) that stays synced both
ways with GitHub; a human-readable export lands in `PRIVATE_LEDGER.md`,
which is also not published.
