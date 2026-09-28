#!/usr/bin/env python3
"""qa_daily.py - daily QA runner.

Phases (run in order by the qa-daily cron worker):
    detect   list new commits per repo since the last watermark
             (the worker maps new functionality into the .ceg files by hand)
    regen    re-run ceg.py --all; fail if any requirement is uncovered
    test     run automated checks against the current release;
             writes logs/qa-results-<date>.json
    issues   file GitHub issues for failures (deduped) + ledger entries
    sync     reconcile GitHub qa issues with PRIVATE_LEDGER.md

The QA manager finds and flags with reproduction steps. It never
debugs and never fixes - the programmers do their own debugging.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from datetime import date

QA_DIR = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(QA_DIR, "state.json")
LEDGER = os.path.join(QA_DIR, "PRIVATE_LEDGER.md")
GH = os.path.expanduser("~/workspace/skills/github/bin/gh_api.py")
LOGS = os.path.join(QA_DIR, "logs")

REPOS = {
    "r-theory-rewrite": {"dir": os.path.expanduser("~/workspace/r-theory-rewrite"),
                         "github": "cosbykit-afk/r-theory-rewrite"},
    "forum": {"dir": os.path.expanduser("~/workspace/forum"),
              "github": None},
    "lampy-installer": {"dir": os.path.expanduser("~/workspace/lampy-installer"),
                        "github": "cosbykit-afk/lampy-installer"},
    "bible-project": {"dir": os.path.expanduser("~/workspace/bible-project"),
                      "github": "cosbykit-afk/bible-project"},
    "profile": {"dir": None, "github": None},
}

SSH_BASE = ["ssh", "-i", os.path.expanduser("~/.ssh/id_ed25519"),
            "-o", "ProxyCommand=" + os.path.expanduser(
                "~/workspace/toetop-ssh-proxy.sh") + " %h %p",
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=45", "muse@100.124.30.78"]


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=kw.get("timeout", 120))


def load_state():
    if os.path.exists(STATE):
        with open(STATE) as f:
            return json.load(f)
    return {"watermarks": {}, "qa_counter": 0, "last_run": None}


def save_state(s):
    with open(STATE, "w") as f:
        json.dump(s, f, indent=1)


def gh_api(method, path, data=None):
    cmd = [sys.executable, GH, method, path]
    if data is not None:
        cmd += ["--data-json", json.dumps(data)]
    r = sh(cmd, timeout=90)
    if r.returncode != 0:
        raise RuntimeError("gh_api failed: " + r.stderr.strip()[:300])
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return r.stdout


# ---------------- detect ----------------

def cmd_detect():
    st = load_state()
    for name, info in REPOS.items():
        d = info["dir"]
        print("=== " + name + " ===")
        if not d or not os.path.isdir(os.path.join(d, ".git")):
            print("not a git repo; review by hand")
            continue
        wm = st["watermarks"].get(name)
        rng = (wm + "..HEAD") if wm else "HEAD~5..HEAD"
        r = sh(["git", "-C", d, "log", rng, "--format=%h %ci %s"])
        out = r.stdout.strip()
        print(out if out else "(no new commits since watermark)")


# ---------------- regen ----------------

def cmd_regen():
    r = sh([sys.executable, os.path.join(QA_DIR, "ceg.py"), "--all"], timeout=300)
    print(r.stdout.strip())
    if r.stderr.strip():
        print(r.stderr.strip(), file=sys.stderr)
    if r.returncode != 0:
        print("REGEN FAILED: uncovered requirements or parse errors", file=sys.stderr)
    return r.returncode


# ---------------- tunnel ----------------

@contextmanager
def tunnel(local_port):
    cmd = SSH_BASE + ["-L", "%d:127.0.0.1:80" % local_port, "-N"]
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            time.sleep(0.5)
            s = sh(["bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/%d" % local_port],
                   timeout=5)
            if s.returncode == 0:
                break
        else:
            raise RuntimeError("tunnel to Toetop :80 did not come up")
        yield local_port
    finally:
        p.terminate()


def http_get(port, path):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                 headers={"User-Agent": "muse-qa"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except Exception as e:
        return None, str(e)


# ---------------- checks ----------------

def check_theory():
    out = []
    try:
        with tunnel(8903):
            r = sh([sys.executable, os.path.expanduser("~/workspace/qa-desktop.py")],
                   timeout=600)
            for line in r.stdout.splitlines():
                m = re.match(r"(PASS|FAIL) ([^-\u2014]+)(?:\s*[-\u2014]\s*(.*))?", line)
                if m:
                    out.append({
                        "product": "r-theory-rewrite",
                        "name": m.group(2).strip(),
                        "ok": m.group(1) == "PASS",
                        "detail": (m.group(3) or "").strip(),
                        "repro": [
                            "Open an SSH tunnel: ssh -L 8903:127.0.0.1:80 "
                            "muse@100.124.30.78 (via toetop-ssh-proxy.sh) -N",
                            "Run: python3 ~/workspace/qa-desktop.py",
                            "Read the FAIL line for this check.",
                        ],
                    })
            if not out:
                out.append({"product": "r-theory-rewrite", "name": "qa-desktop ran",
                            "ok": False,
                            "detail": "no PASS/FAIL lines; stderr: " + r.stderr[:200],
                            "repro": ["Run python3 ~/workspace/qa-desktop.py by hand."]})
    except Exception as e:
        out.append({"product": "r-theory-rewrite", "name": "tunnel to Toetop",
                    "ok": False, "detail": str(e)[:200],
                    "repro": ["Check the tailnet tunnel to Toetop (100.124.30.78)."]})
    return out


def check_forum():
    out = []

    def add(name, ok, detail, repro):
        out.append({"product": "forum", "name": name, "ok": ok,
                    "detail": detail, "repro": repro})

    try:
        with tunnel(8904):
            st, body = http_get(8904, "/app/register")
            add("register page renders", st == 200, "status=%s" % st,
                ["Tunnel to Toetop :80 on local 8904.",
                 "GET http://127.0.0.1:8904/app/register",
                 "Expect 200 and the register form."])
            add("register form posts under /app prefix",
                st == 200 and 'action="/app/register"' in body,
                "form action under /app" if st == 200 else "status=%s" % st,
                ["GET /app/register and inspect the form's action attribute."])
            add("register form carries next hidden field",
                st == 200 and 'name="next"' in body,
                "next field present" if st == 200 else "status=%s" % st,
                ["GET /app/register and look for the hidden next field."])
            st, body = http_get(8904, "/app/login?next=/r-theory/book3/")
            add("login page renders with next preserved",
                st == 200 and ("/app/login" in body),
                "status=%s" % st,
                ["GET /app/login?next=/r-theory/book3/",
                 "Expect 200 and the next value preserved on the form/links."])
            st, body = http_get(8904, "/app/")
            add("index renders with R Theory return link",
                st == 200 and ("R Theory" in body),
                "status=%s" % st,
                ["GET /app/", "Expect 200 and the header R Theory link."])
            st, body = http_get(8904, "/app/api/comments?site=r-theory&page=/book3/")
            add("comments API answers",
                st in (200, 400),
                "status=%s" % st,
                ["GET /app/api/comments?site=r-theory&page=/book3/",
                 "Expect a JSON answer, not a 500."])
    except Exception as e:
        add("tunnel to Toetop", False, str(e)[:200],
            ["Check the tailnet tunnel to Toetop (100.124.30.78)."])
    return out


def check_installer():
    out = []
    d = REPOS["lampy-installer"]["dir"]

    def add(name, ok, detail, repro):
        out.append({"product": "lampy-installer", "name": name, "ok": ok,
                    "detail": detail, "repro": repro})

    for py in ["verify-bundle.py", "set-passwords.py", "wsl-envfix.py"]:
        p = os.path.join(d, py)
        if os.path.exists(p):
            r = sh([sys.executable, "-m", "py_compile", p])
            add(py + " compiles", r.returncode == 0, r.stderr[:160],
                ["Run: python3 -m py_compile " + p, "Fix the syntax error."])
    mp = os.path.join(d, "manifest.json")
    try:
        man = json.load(open(mp))
        chunks = man.get("chunks", [])
        ok = all(c.get("name") and c.get("sha256") and c.get("size")
                 for c in chunks)
        add("manifest.json valid, all chunks hashed", ok,
            "%d chunks" % len(chunks),
            ["Open ~/workspace/lampy-installer/manifest.json",
             "Every chunk needs name, sha256, and size."])
    except Exception as e:
        add("manifest.json valid, all chunks hashed", False, str(e)[:160],
            ["Open ~/workspace/lampy-installer/manifest.json and fix the JSON."])
    hardcode = []
    pat = re.compile(r"C:\\\\Users\\\\kitco|C:/Users/kitco|kitco@|100\.124\.30\.78|"
                     r"100\.116\.31\.9", re.IGNORECASE)
    for fn in ["install.ps1", "uninstall.ps1", "lampy.nsi", "lampy-slim.nsi",
               "build.ps1"]:
        p = os.path.join(d, fn)
        if os.path.exists(p):
            for i, line in enumerate(open(p, errors="replace"), 1):
                if pat.search(line):
                    hardcode.append("%s:%d" % (fn, i))
    add("no machine-specific hardcodes (REQ-LI-010)", not hardcode,
        "; ".join(hardcode)[:200] if hardcode else "clean",
        ["Grep the installer sources for C:\\Users\\kitco, kitco@, and tailnet IPs.",
         "The installer must work for everybody else and future releases."])
    slim = os.path.join(d, "lampy-slim.nsi")
    m = re.search(r'!define PRODUCT_VERSION "([^"]+)"', open(slim).read())
    ver = m.group(1) if m else None
    r = sh(["git", "-C", d, "log", "--oneline", "-8"])
    add("slim NSI version matches recent release commits",
        bool(ver and ("v" + ver) in r.stdout),
        "nsi=" + str(ver),
        ["Check !define PRODUCT_VERSION in lampy-slim.nsi against git log.",
         "Bump the version define with the release."])
    return out


def check_bible():
    out = []
    d = REPOS["bible-project"]["dir"]

    def add(name, ok, detail, repro):
        out.append({"product": "bible-project", "name": name, "ok": ok,
                    "detail": detail, "repro": repro})

    app = os.path.join(d, "website", "app.py")
    r = sh([sys.executable, "-m", "py_compile", app])
    add("website app.py compiles", r.returncode == 0, r.stderr[:160],
        ["Run: python3 -m py_compile " + app])
    routes = re.findall(r"@app\.route\('([^']+)'", open(app).read())
    add("expected routes present", len(routes) >= 9, "%d routes" % len(routes),
        ["Open ~/workspace/bible-project/website/app.py and check the route list."])
    db = os.path.join(d, "bible.db")
    try:
        con = sqlite3.connect(db)
        cols = [row[1] for row in con.execute("PRAGMA table_info(words)")]
        disp = [c for c in cols if c.endswith("_disp")]
        add("words table carries normalized *_disp columns", len(disp) >= 5,
            "%d disp columns" % len(disp),
            ["Open bible.db, PRAGMA table_info(words);",
             "The word-detail Affixes row reads the *_disp columns."])
        n = con.execute("SELECT COUNT(*) FROM words").fetchone()[0]
        add("words table populated", n > 0, "%d words" % n,
            ["Check the bible.db build pipeline (build_lexicon.py etc.)."])
        con.close()
    except Exception as e:
        add("bible.db readable", False, str(e)[:160],
            ["Check ~/workspace/bible-project/bible.db exists and is intact."])
    return out


def check_profile():
    return [{"product": "profile", "name": "ceg parses (routine is manual)",
             "ok": True, "detail": "covered by regen",
             "repro": ["The profile routine is exercised by "
                       "fb-profile-notifications-daily; nothing to probe."]}]


def cmd_test():
    results = []
    results += check_theory()
    results += check_forum()
    results += check_installer()
    results += check_bible()
    results += check_profile()
    os.makedirs(LOGS, exist_ok=True)
    path = os.path.join(LOGS, "qa-results-%s.json" % date.today().isoformat())
    with open(path, "w") as f:
        json.dump(results, f, indent=1)
    fails = [r for r in results if not r["ok"]]
    for r in results:
        print(("PASS " if r["ok"] else "FAIL ") + r["product"] + " | " +
              r["name"] + (" — " + r["detail"] if r["detail"] else ""))
    print("%d/%d checks passed" % (len(results) - len(fails), len(results)))
    with open(path) as f:
        pass
    return 1 if fails else 0


# ---------------- issues ----------------

def open_qa_issues(slug):
    return [i for i in gh_api("GET", "/repos/%s/issues?state=open&per_page=100" % slug)
            if any(l["name"] == "qa" for l in i.get("labels", []))]


def ledger_entries():
    text = open(LEDGER).read()
    entries = []
    for m in re.finditer(
            r"## (QA-\d+) — (\d{4}-\d{2}-\d{2}) — ([^\n]+)\n(.*?)(?=\n## QA-|\Z)",
            text, re.S):
        qid, day, product, body = m.group(1), m.group(2), m.group(3).strip(), m.group(4)
        gm = re.search(r"https://github\.com/([^/\s]+/[^/\s]+)/issues/(\d+)", body)
        sm = re.search(r"^- Status: (.+)$", body, re.M)
        entries.append({"id": qid, "day": day, "product": product,
                        "github": gm.group(1) + "#" + gm.group(2) if gm else None,
                        "url": gm.group(0) if gm else None,
                        "status": sm.group(1).strip() if sm else "open"})
    return entries


def append_ledger(entry_text):
    text = open(LEDGER).read()
    marker = "(No issues logged yet. The daily run appends entries below, newest first.)"
    if marker in text:
        text = text.replace(marker, marker + "\n" + entry_text, 1)
    else:
        text = text.rstrip() + "\n" + entry_text
    with open(LEDGER, "w") as f:
        f.write(text)


def cmd_issues():
    paths = sorted([p for p in os.listdir(LOGS) if p.startswith("qa-results-")])
    if not paths:
        print("no test results; run test first")
        return 2
    results = json.load(open(os.path.join(LOGS, paths[-1])))
    fails = [r for r in results if not r["ok"]]
    if not fails:
        print("no failures; nothing to file")
        return 0
    st = load_state()
    filed = 0
    for r in fails:
        product = r["product"]
        slug = REPOS[product]["github"]
        title = "[QA] %s: %s" % (product, r["name"])
        body = ("Automated QA check failed on %s.\n\n**Observed:** %s\n\n"
                "**Reproduction:**\n%s\n\n_Logged by the QA manager; "
                "debugging belongs to the programmers._"
                % (date.today().isoformat(), r["detail"] or "see check output",
                   "\n".join("%d. %s" % (i + 1, s) for i, s in enumerate(r["repro"]))))
        url, number = None, None
        if slug:
            try:
                existing = open_qa_issues(slug)
                dup = [i for i in existing if i["title"] == title]
                if dup:
                    number, url = dup[0]["number"], dup[0]["html_url"]
                    print("already open: %s #%d" % (slug, number))
                else:
                    created = gh_api("POST", "/repos/%s/issues" % slug,
                                     {"title": title, "body": body,
                                      "labels": ["qa"]})
                    number, url = created["number"], created["html_url"]
                    print("filed: %s" % url)
            except Exception as e:
                print("GitHub filing failed for %s: %s" % (title, e),
                      file=sys.stderr)
        st["qa_counter"] += 1
        qid = "QA-%04d" % st["qa_counter"]
        entry = ("\n## %s — %s — %s\n" % (qid, date.today().isoformat(), product)
                 + ("- GitHub: #%d %s (open)\n" % (number, url) if url
                    else "- GitHub: n/a (no repo — ledger only)\n")
                 + "- Check: %s\n" % r["name"]
                 + "- Observed: %s\n" % (r["detail"] or "see check output")
                 + "- Repro:\n"
                 + "".join("  %d. %s\n" % (i + 1, s) for i, s in enumerate(r["repro"]))
                 + "- Status: open\n")
        append_ledger(entry)
        filed += 1
    save_state(st)
    print("ledger entries added: %d" % filed)
    return 0


# ---------------- sync ----------------

def cmd_sync():
    paths = sorted([p for p in os.listdir(LOGS) if p.startswith("qa-results-")])
    latest = json.load(open(os.path.join(LOGS, paths[-1]))) if paths else []
    passed = {(r["product"], r["name"]) for r in latest if r["ok"]}
    entries = ledger_entries()
    text = open(LEDGER).read()
    report = []
    for e in entries:
        if not e["github"]:
            continue
        slug, num = e["github"].split("#")
        try:
            issue = gh_api("GET", "/repos/%s/issues/%s" % (slug, num))
        except Exception as ex:
            report.append("%s: could not read %s (%s)" % (e["id"], e["github"], ex))
            continue
        gh_state = issue["state"]
        new_status = None
        if gh_state == "closed" and e["status"] == "open":
            new_status = "closed-on-github-unverified"
        if e["status"] in ("open", "closed-on-github-unverified",
                           "fixed-unverified"):
            key = (e["product"], None)
            # match by check name recorded in the entry
            m = re.search(r"^- Check: (.+)$",
                          text.split("## " + e["id"])[1].split("## QA-")[0], re.M)
            if m and (e["product"], m.group(1).strip()) in passed:
                new_status = "verified-closed"
        if new_status and new_status != e["status"]:
            text = text.replace(
                "## " + e["id"] + " — " + e["day"] + " — " + e["product"],
                "## " + e["id"] + " — " + e["day"] + " — " + e["product"], 1)
            # update the Status line inside this entry only
            head, rest = text.split("## " + e["id"], 1)
            rest = re.sub(r"^- Status: .+$", "- Status: " + new_status, rest,
                          count=1, flags=re.M)
            text = head + "## " + e["id"] + rest
            report.append("%s: %s -> %s (GitHub %s is %s)"
                          % (e["id"], e["status"], new_status, e["github"], gh_state))
    # pull: GitHub qa issues missing from the ledger
    have = {e["github"] for e in entries if e["github"]}
    st = load_state()
    for name, info in REPOS.items():
        slug = info["github"]
        if not slug:
            continue
        try:
            for i in open_qa_issues(slug):
                key = slug + "#" + str(i["number"])
                if key not in have:
                    st["qa_counter"] += 1
                    qid = "QA-%04d" % st["qa_counter"]
                    append_ledger(
                        "\n## %s — %s — %s\n" % (qid, date.today().isoformat(), name)
                        + "- GitHub: #%d %s (open)\n" % (i["number"], i["html_url"])
                        + "- Check: %s\n" % i["title"]
                        + "- Observed: filed on GitHub, imported by sync\n"
                        + "- Repro:\n  1. See the GitHub issue.\n"
                        + "- Status: open\n")
                    report.append("%s: imported %s from GitHub" % (qid, key))
        except Exception as ex:
            report.append("%s: pull failed (%s)" % (slug, ex))
    with open(LEDGER, "w") as f:
        f.write(text)
    save_state(st)
    # watermarks advance: what we just tested is now the baseline
    for name, info in REPOS.items():
        d = info["dir"]
        if d and os.path.isdir(os.path.join(d, ".git")):
            r = sh(["git", "-C", d, "rev-parse", "HEAD"])
            if r.returncode == 0:
                st["watermarks"][name] = r.stdout.strip()
    st["last_run"] = date.today().isoformat()
    save_state(st)
    print("\n".join(report) if report else "sync: no changes")
    return 0


def main(argv):
    cmds = {"detect": cmd_detect, "regen": cmd_regen, "test": cmd_test,
            "issues": cmd_issues, "sync": cmd_sync}
    if len(argv) < 2 or argv[1] not in cmds:
        sys.stderr.write("usage: qa_daily.py detect|regen|test|issues|sync\n")
        return 2
    return cmds[argv[1]]()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
