# Cause-Effect Graphing — text format (.ceg)

Standing rules (Kit, 2026-09-28):

1. **A good .ceg never uses an OR gate.** All logic is mapped with AND
   plus carefully constrained inputs.
2. "Or" in requirements is one of three things, and must be named:
   - `O(...)` — one and only one (exclusive): exactly one cause active.
   - `I(...)` — inclusive: at least one cause active.
   - unconstrained — bifurcate: enumerate every combination, no constraint.
3. Between success, logic, and logic behind masks:
   - success logic and failure logic are separate graphs (`[SUCCESS]`,
     `[FAIL]`). Fail logic never shares a graph with success logic.
   - `M(a, b)` — a masks b: while a holds, b's logic is hidden (skipped).
4. **Bifurcate inputs.** Every input is split into discrete states
   (`name : off | on`, `query : empty | valid | ...`). State index 0 is
   always the off/absent/inactive state. No continuous values, no "maybe".
5. **All inputs from the page go in the .ceg.** If the page has it, the
   graph has it. An input with one state is not an input — bifurcate it
   or drop it.
6. **All requirements are mapped by the logic.** Each requirement points
   at the effect(s) its logic produces (`REQ-014 -> e3`).
7. **Evaluate every possible outcome, then generate the test suite.**
   The generator takes the Cartesian product of input states, applies
   the constraints, evaluates every graph rule, and emits one test case
   per surviving combination. A requirement is covered only if some
   test case reaches its effect.

## File layout

```
[PAGE]        one page or flow per file; URL or route
[INPUTS]      c1 = name : state0 | state1 | state2 ...
[CONSTRAINTS] O(...) / I(...) / R(a, b) / M(a, b)
[SUCCESS]     e1 = observable outcome
              e1 <= c1=on AND c2=valid ...
[FAIL]        separate graph, same syntax
[REQUIREMENTS] REQ-001 : text -> e1, e2
[NEW]         appended by the daily run; mapped into the graphs, never OR'd
```

Comments start with `#`. Blank lines are ignored.

## Constraint semantics

- `O(c1, c2, c3)` — exactly one of the listed causes is in a non-zero
  state. Combinations with zero or two-plus active are discarded.
- `I(c1, c2)` — at least one of the listed causes is non-zero.
- `R(c1, c2)` — c1 non-zero requires c2 non-zero; combos violating this
  are discarded.
- `M(c1, c2)` — c1 masks c2: combinations with c1 non-zero skip c2's
  rules (c2's effects are not evaluated for that combination).
- No constraint listed = unconstrained = full bifurcation, every
  combination evaluated.

## Rule syntax

```
e1 <= c1=on AND c2=valid AND c3=off
```

Left side is an effect id. Right side is AND-joined `cause=state`
conditions. A combination satisfies the rule when every condition holds.
Multiple rules may set the same effect (separate success paths stay in
`[SUCCESS]`; failures stay in `[FAIL]`). No OR keyword exists — if you
reach for one, you are holding an O, an I, or an unconstrained
bifurcation. Name it.

## The generator (ceg.py)

`python3 ceg.py <file.ceg>` parses, enumerates, constrains, evaluates,
and writes the test suite. `python3 ceg.py --all` does every .ceg in
`ceg/`. Output: `suites/<name>.tests.md` (human table) and
`suites/<name>.tests.json` (machine table). Exit code is the count of
requirements with zero covering test cases — the graph is incomplete
until that number is zero.
