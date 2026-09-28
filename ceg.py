#!/usr/bin/env python3
"""ceg.py - cause-effect graph evaluator and test-suite generator.

Reads a text .ceg file (see CEG_FORMAT.md), enumerates every possible
input-state combination, applies the constraints (O/I/R/M), evaluates
the SUCCESS and FAIL graphs (AND-only, no OR gates), and emits one test
case per surviving combination.

Usage:
    python3 ceg.py <file.ceg>        # one file
    python3 ceg.py --all             # every .ceg in ceg/

Outputs (into suites/):
    <name>.tests.md    human-readable test table (effect-reaching cases)
    <name>.tests.json   machine-readable table (every surviving combination)

Exit code = number of requirements with zero covering test cases.
A graph is incomplete until that number is zero.
"""
import itertools
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
CEG_DIR = os.path.join(BASE, "ceg")
SUITES_DIR = os.path.join(BASE, "suites")


class CEG:
    def __init__(self):
        self.page = ""
        self.inputs = {}
        self.constraints = []
        self.effects = {}
        self.rules = []
        self.requirements = {}


def parse(path):
    g = CEG()
    section = None
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip().upper()
                continue
            if section == "PAGE":
                g.page += line + " "
            elif section == "INPUTS":
                m = re.match(r"(\w+)\s*=\s*([\w\- ]+?)\s*:\s*(.+)", line)
                if not m:
                    raise ValueError("bad INPUTS line: " + line)
                cid, name = m.group(1), m.group(2).strip()
                states = [s.strip() for s in m.group(3).split("|")]
                if len(states) < 2:
                    raise ValueError(cid + " has one state; bifurcate or drop: " + line)
                g.inputs[cid] = {"name": name, "states": states}
            elif section == "CONSTRAINTS":
                m = re.match(r"([OIRM])\s*\(([^)]+)\)", line)
                if not m:
                    raise ValueError("bad CONSTRAINTS line: " + line)
                kind = m.group(1)
                cids = [c.strip() for c in m.group(2).split(",")]
                for c in cids:
                    if c not in g.inputs:
                        raise ValueError("constraint on unknown input " + c + ": " + line)
                g.constraints.append((kind, cids))
            elif section in ("SUCCESS", "FAIL"):
                m = re.match(r"(\w+)\s*<=\s*(.+)", line)
                if m:
                    eid, conds = m.group(1), m.group(2)
                    if eid not in g.effects:
                        raise ValueError("rule for undeclared effect " + eid + ": " + line)
                    parsed = []
                    for cond in conds.split("AND"):
                        cm = re.match(r"\s*(\w+)\s*=\s*([\w\- ]+?)\s*$", cond)
                        if not cm:
                            raise ValueError("bad condition in: " + line)
                        cid, state = cm.group(1), cm.group(2).strip()
                        if cid not in g.inputs:
                            raise ValueError("rule on unknown input " + cid + ": " + line)
                        if state not in g.inputs[cid]["states"]:
                            raise ValueError("unknown state " + state + " for " + cid)
                        parsed.append((cid, state))
                    g.rules.append((eid, section, parsed))
                else:
                    m = re.match(r"(\w+)\s*=\s*(.+)", line)
                    if not m:
                        raise ValueError("bad effect line: " + line)
                    g.effects[m.group(1)] = {"desc": m.group(2).strip(),
                                             "graph": section}
            elif section == "REQUIREMENTS":
                m = re.match(r"([\w\-]+)\s*:\s*(.+?)\s*->\s*([\w,\s]+)$", line)
                if not m:
                    raise ValueError("bad REQUIREMENTS line: " + line)
                req, text = m.group(1), m.group(2).strip()
                effs = [e.strip() for e in m.group(3).split(",")]
                for e in effs:
                    if e not in g.effects:
                        raise ValueError("requirement maps to unknown effect " + e)
                g.requirements[req] = {"text": text, "effects": effs}
            elif section == "NEW":
                pass  # staging area; mapped into graphs by the daily run
            elif section is None:
                raise ValueError("line outside any section: " + line)
    return g


def active(combo, cid):
    return combo[cid] != 0


def combo_ok(g, combo):
    for kind, cids in g.constraints:
        if kind == "O":
            if sum(1 for c in cids if active(combo, c)) != 1:
                return False
        elif kind == "I":
            if not any(active(combo, c) for c in cids):
                return False
        elif kind == "R":
            a, b = cids[0], cids[1]
            if active(combo, a) and not active(combo, b):
                return False
        elif kind == "M":
            pass  # handled at evaluation: masked rules are skipped
    return True


def masked_inputs(g, combo):
    out = set()
    for kind, cids in g.constraints:
        if kind == "M" and active(combo, cids[0]):
            out.add(cids[1])
    return out


def state_of(g, combo, cid):
    return g.inputs[cid]["states"][combo[cid]]


def evaluate(g, combo):
    masked = masked_inputs(g, combo)
    reached = {"SUCCESS": [], "FAIL": []}
    for eid, graph, conds in g.rules:
        if any(c in masked for c, _ in conds):
            continue
        if all(state_of(g, combo, c) == s for c, s in conds):
            reached[graph].append(eid)
    return reached


def generate(path):
    g = parse(path)
    cids = list(g.inputs.keys())
    pools = [range(len(g.inputs[c]["states"])) for c in cids]
    total = 0
    tests = []
    for values in itertools.product(*pools):
        total += 1
        combo = dict(zip(cids, values))
        if not combo_ok(g, combo):
            continue
        reached = evaluate(g, combo)
        tests.append({
            "inputs": {c: state_of(g, combo, c) for c in cids
                       if state_of(g, combo, c) != g.inputs[c]["states"][0]},
            "success": reached["SUCCESS"],
            "fail": reached["FAIL"],
        })
    eff_req = {}
    for req, r in g.requirements.items():
        for e in r["effects"]:
            eff_req.setdefault(e, []).append(req)
    covered = set()
    for tc in tests:
        for e in tc["success"] + tc["fail"]:
            covered.update(eff_req.get(e, []))
    uncovered = [r for r in g.requirements if r not in covered]
    return g, tests, total, uncovered, eff_req


def emit(path, g, tests, total, uncovered, eff_req):
    name = os.path.splitext(os.path.basename(path))[0]
    os.makedirs(SUITES_DIR, exist_ok=True)
    inert = 0
    rows = []
    md = []
    md.append("# Test suite: " + name)
    md.append("")
    md.append("Page/flow: " + g.page.strip())
    md.append("")
    md.append("Generated by ceg.py: every input-state combination enumerated, "
              "constraints applied, AND-graphs evaluated. No OR gates.")
    md.append("")
    md.append(f"Combinations enumerated: {total}. "
              f"Surviving constraints: {len(tests)}.")
    md.append("")
    md.append("Rows are grouped by outcome signature: every distinct "
              "(success-effects, fail-effects) outcome the logic can produce "
              "gets one row, with the count of input combinations that reach "
              "it and the simplest representative combination. The JSON file "
              "carries every combination individually.")
    md.append("")
    md.append("| TC | combos | representative inputs (non-default states) | "
              "success effects | fail effects | requirements |")
    md.append("|---|---|---|---|---|---|")
    groups = {}
    for i, tc in enumerate(tests, 1):
        tc["id"] = "T%04d" % i
        if not tc["success"] and not tc["fail"]:
            inert += 1
            continue
        sig = (tuple(tc["success"]), tuple(tc["fail"]))
        groups.setdefault(sig, []).append(tc)
        rows.append(tc)
    for n, (sig, members) in enumerate(
            sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])), 1):
        rep = min(members, key=lambda t: (len(t["inputs"]), t["id"]))
        desc = ", ".join(g.inputs[c]["name"] + "=" + s
                         for c, s in rep["inputs"].items())
        reqs = sorted({r for e in sig[0] + sig[1]
                       for r in eff_req.get(e, [])})
        md.append("| G%03d | %d | %s | %s | %s | %s |" % (
            n, len(members), desc, ", ".join(sig[0]),
            ", ".join(sig[1]), ", ".join(reqs)))
    md.append("")
    md.append("%d outcome groups, %d inert combinations (no effect, no "
              "assertion — counted, not listed)." % (len(groups), inert))
    md.append("")
    if uncovered:
        md.append("UNCOVERED REQUIREMENTS: " + ", ".join(uncovered))
    else:
        md.append("All %d requirements are covered by at least one test case."
                  % len(g.requirements))
    md.append("")
    with open(os.path.join(SUITES_DIR, name + ".tests.md"), "w") as f:
        f.write("\n".join(md))
    with open(os.path.join(SUITES_DIR, name + ".tests.json"), "w") as f:
        json.dump({
            "page": g.page.strip(),
            "inputs": {c: v for c, v in g.inputs.items()},
            "constraints": g.constraints,
            "effects": g.effects,
            "requirements": g.requirements,
            "total_combinations": total,
            "surviving": len(tests),
            "inert": inert,
            "uncovered_requirements": uncovered,
            "tests": tests,
        }, f, indent=1)
    return uncovered


def main(argv):
    if len(argv) < 2:
        sys.stderr.write("usage: ceg.py <file.ceg> | --all\n")
        return 2
    files = []
    if argv[1] == "--all":
        files = sorted(os.path.join(CEG_DIR, f) for f in os.listdir(CEG_DIR)
                       if f.endswith(".ceg"))
    else:
        files = [argv[1]]
    bad = 0
    for path in files:
        try:
            g, tests, total, uncovered, eff_req = generate(path)
            emit(path, g, tests, total, uncovered, eff_req)
            print("%s: %d combos, %d surviving, %d uncovered requirements" %
                  (os.path.basename(path), total, len(tests), len(uncovered)))
            bad += len(uncovered)
        except ValueError as e:
            sys.stderr.write("ERROR %s: %s\n" % (path, e))
            bad += 1
    return bad


if __name__ == "__main__":
    sys.exit(main(sys.argv))
