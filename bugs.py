#!/usr/bin/env python3
"""bugs.py - bug management database for the QA system.

SQLite database (bugs.db, private, never published) that is the system of
record for QA findings. It stays synced both ways with the GitHub issues
(label `qa`) on Kit's public repositories:

  * push: a QA finding with no GitHub issue yet is filed there (deduplicated
    by title per repo); ledger-only products (forum, profile) stay local.
  * pull: GitHub issue state is refreshed into the DB; qa-labeled issues
    filed by hand on GitHub are imported.
  * verify: a ledger entry becomes `verified-closed` only after its check
    passes on re-test - never merely because a programmer closed the
    GitHub issue.

PRIVATE_LEDGER.md is a human-readable export of this database
(`export-ledger`), not the record itself.

qa_status vocabulary:
  open                        filed, awaiting the programmer
  closed-on-github-unverified GitHub issue closed but no passing re-test yet
  fixed-unverified            programmer claims fixed, re-test pending
  verified-closed             the check passed on re-test; done
"""
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

QA_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(QA_DIR, "bugs.db")
LEDGER = os.path.join(QA_DIR, "PRIVATE_LEDGER.md")
GH = os.path.expanduser("~/workspace/skills/github/bin/gh_api.py")
LOGS = os.path.join(QA_DIR, "logs")

# product -> {github slug or None, local dir or None}
REPOS = {
    "r-theory-rewrite": {"github": "cosbykit-afk/r-theory-rewrite",
                         "dir": os.path.expanduser("~/workspace/r-theory-rewrite")},
    "forum": {"github": None,
              "dir": os.path.expanduser("~/workspace/forum")},
    "lampy-installer": {"github": "cosbykit-afk/lampy-installer",
                        "dir": os.path.expanduser("~/workspace/lampy-installer")},
    "bible-project": {"github": "cosbykit-afk/bible-project",
                      "dir": os.path.expanduser("~/workspace/bible-project")},
    "profile": {"github": None, "dir": None},
    "qa-ceg": {"github": "cosbykit-afk/qa-ceg",
               "dir": QA_DIR},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
  slug TEXT PRIMARY KEY,
  product TEXT NOT NULL,
  local_dir TEXT,
  github_enabled INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS issues (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  qa_id TEXT UNIQUE NOT NULL,
  product TEXT NOT NULL,
  repo_slug TEXT,
  gh_number INTEGER,
  gh_node_id TEXT,
  title TEXT NOT NULL,
  body TEXT,
  labels TEXT,
  gh_state TEXT,
  qa_status TEXT NOT NULL DEFAULT 'open',
  check_name TEXT,
  observed TEXT,
  repro TEXT,
  suite_ref TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  closed_at TEXT,
  verified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_issues_repo_state ON issues(repo_slug, gh_state);
CREATE INDEX IF NOT EXISTS idx_issues_qa_status ON issues(qa_status);
CREATE TABLE IF NOT EXISTS sync_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  direction TEXT NOT NULL,
  repo_slug TEXT,
  detail TEXT
);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = connect()
    con.executescript(SCHEMA)
    for product, info in REPOS.items():
        slug = info["github"] or ("local:" + product)
        con.execute(
            "INSERT OR IGNORE INTO repos(slug, product, local_dir, github_enabled)"
            " VALUES(?,?,?,?)",
            (slug, product, info["dir"], 1 if info["github"] else 0))
    con.commit()
    con.close()


def sh(cmd, timeout=90):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def gh_api(method, path, data=None):
    cmd = [sys.executable, GH, method, path]
    if data is not None:
        cmd += ["--data-json", json.dumps(data)]
    r = sh(cmd)
    if r.returncode != 0:
        raise RuntimeError("gh_api failed: " + r.stderr.strip()[:300])
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return r.stdout


def log(con, direction, repo_slug, detail):
    con.execute("INSERT INTO sync_log(ts, direction, repo_slug, detail)"
                " VALUES(?,?,?,?)", (now(), direction, repo_slug, detail))


def next_qa_id(con):
    row = con.execute(
        "SELECT qa_id FROM issues ORDER BY id DESC LIMIT 1").fetchone()
    n = int(row["qa_id"].split("-")[1]) + 1 if row else 1
    return "QA-%04d" % n


def open_qa_issues(slug):
    return [i for i in gh_api(
        "GET", "/repos/%s/issues?state=open&per_page=100" % slug)
        if any(l["name"] == "qa" for l in i.get("labels", []))]


# ---------------- push: file a QA finding ----------------

def file_issue(product, check_name, observed, repro_steps, suite_ref=None,
               day=None):
    """Record a QA failure. Creates the GitHub issue (deduped by title)
    when the product has a repo; always records the DB row. Returns qa_id."""
    init_db()
    con = connect()
    info = REPOS[product]
    slug = info["github"]
    title = "[QA] %s: %s" % (product, check_name)
    body = ("Automated QA check failed on %s.\n\n**Observed:** %s\n\n"
            "**Reproduction:**\n%s\n\n_Logged by the QA manager; "
            "debugging belongs to the programmers._"
            % (day or datetime.now(timezone.utc).date().isoformat(),
               observed or "see check output",
               "\n".join("%d. %s" % (i + 1, s)
                         for i, s in enumerate(repro_steps))))
    gh_number, gh_node, gh_url, gh_state = None, None, None, None
    if slug:
        existing = [i for i in open_qa_issues(slug) if i["title"] == title]
        db_dup = con.execute(
            "SELECT gh_number FROM issues WHERE repo_slug=? AND title=?"
            " AND gh_state='open' AND gh_number IS NOT NULL",
            (slug, title)).fetchone()
        if existing:
            gh_number, gh_node = existing[0]["number"], existing[0]["node_id"]
            gh_url, gh_state = existing[0]["html_url"], "open"
            log(con, "push", slug, "dedupe: %s already open as #%d"
                % (title, gh_number))
        elif db_dup:
            gh_number = db_dup["gh_number"]
            log(con, "push", slug, "dedupe: %s already tracked as #%d"
                % (title, gh_number))
        else:
            created = gh_api("POST", "/repos/%s/issues" % slug,
                             {"title": title, "body": body, "labels": ["qa"]})
            gh_number, gh_node = created["number"], created["node_id"]
            gh_url, gh_state = created["html_url"], "open"
            log(con, "push", slug, "filed #%d: %s" % (gh_number, title))
    qid = next_qa_id(con)
    con.execute(
        "INSERT INTO issues(qa_id, product, repo_slug, gh_number, gh_node_id,"
        " title, body, labels, gh_state, qa_status, check_name, observed,"
        " repro, suite_ref, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (qid, product, slug or ("local:" + product), gh_number, gh_node,
         title, (body + ("\n\n" + gh_url if gh_url else "")), "qa",
         gh_state or ("n/a" if not slug else "open"),
         "open", check_name, observed,
         "\n".join("%d. %s" % (i + 1, s) for i, s in enumerate(repro_steps)),
         suite_ref, now(), now()))
    con.commit()
    con.close()
    return qid, gh_url


# ---------------- pull + reconcile ----------------

def sync(passed=()):
    """Two-way sync. `passed` is a set of (product, check_name) that passed
    on the latest re-test; those flip to verified-closed. Returns a report."""
    init_db()
    con = connect()
    report = []
    # 1. refresh tracked GitHub issues
    for row in con.execute(
            "SELECT * FROM issues WHERE gh_number IS NOT NULL"
            " AND qa_status NOT IN ('verified-closed')"):
        slug, num = row["repo_slug"], row["gh_number"]
        try:
            issue = gh_api("GET", "/repos/%s/issues/%s" % (slug, num))
        except Exception as ex:
            report.append("%s: could not read %s#%s (%s)"
                          % (row["qa_id"], slug, num, ex))
            continue
        gh_state = issue["state"]
        updates, new_status = {"gh_state": gh_state,
                               "updated_at": now()}, row["qa_status"]
        if gh_state == "closed" and row["qa_status"] == "open":
            new_status = "closed-on-github-unverified"
            updates["closed_at"] = now()
        if (row["product"], row["check_name"]) in passed and \
                new_status != "verified-closed":
            new_status = "verified-closed"
            updates["verified_at"] = now()
        updates["qa_status"] = new_status
        con.execute("UPDATE issues SET %s WHERE id=?" %
                    ",".join("%s=?" % k for k in updates),
                    tuple(updates.values()) + (row["id"],))
        if new_status != row["qa_status"]:
            report.append("%s: %s -> %s (GitHub %s#%s is %s)"
                          % (row["qa_id"], row["qa_status"], new_status,
                             slug, num, gh_state))
            log(con, "pull", slug, "%s %s -> %s"
                % (row["qa_id"], row["qa_status"], new_status))
    # 2. import qa-labeled GitHub issues missing from the DB
    for product, info in REPOS.items():
        slug = info["github"]
        if not slug:
            continue
        try:
            remote = open_qa_issues(slug)
        except Exception as ex:
            report.append("%s: pull failed (%s)" % (slug, ex))
            continue
        for i in remote:
            hit = con.execute(
                "SELECT qa_id FROM issues WHERE repo_slug=? AND gh_number=?",
                (slug, i["number"])).fetchone()
            if not hit:
                qid = next_qa_id(con)
                con.execute(
                    "INSERT INTO issues(qa_id, product, repo_slug, gh_number,"
                    " gh_node_id, title, body, labels, gh_state, qa_status,"
                    " observed, created_at, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (qid, product, slug, i["number"], i["node_id"],
                     i["title"], i.get("body", ""), "qa", i["state"], "open",
                     "filed on GitHub, imported by sync",
                     now(), now()))
                report.append("%s: imported %s#%d from GitHub"
                              % (qid, slug, i["number"]))
                log(con, "pull", slug, "imported #%d" % i["number"])
    # 3. ledger-only products need no GitHub traffic; nothing to do
    con.commit()
    con.close()
    return report


# ---------------- export ----------------

LEDGER_HEAD = """# QA Private Ledger

The system of record for quality-assurance findings is the bug database
(`bugs.db`, private, never published). This file is its human-readable
export, regenerated by `bugs.py export-ledger` on every sync. GitHub
issues (label `qa`) on the public repos stay synced both ways with the
database by `bugs.py sync`.

## Sync protocol

- Every confirmed issue gets a QA id here AND a GitHub issue in the
  product's repo (programmers work from GitHub; QA tracks in the database).
- The forum and the profile routine have no GitHub repository, so their
  issues are **ledger-only** until Kit says otherwise.
- Dedupe: the runner never files a second open issue with the same title
  in the same repo.
- A ledger issue closes only after the check passes on re-test
  (status `verified-closed`). A GitHub issue closed by a programmer
  without a passing re-test stays `closed-on-github-unverified` here until
  the next run verifies it.
- The QA manager finds and flags with reproduction steps. Debugging
  belongs to the programmers — no fixes are made from QA.

## Repos and issue targets

| product | repo / location | GitHub issues |
|---|---|---|
| r-theory-rewrite | cosbykit-afk/r-theory-rewrite | yes |
| forum | ~/workspace/forum (no repo) | no — ledger only |
| lampy-installer | cosbykit-afk/lampy-installer | yes |
| bible-project | cosbykit-afk/bible-project | yes |
| profile | standing routine (no repo) | no — ledger only |
| qa-ceg | cosbykit-afk/qa-ceg | yes |

## Issues
"""


def export_ledger(path=LEDGER):
    init_db()
    con = connect()
    parts = [LEDGER_HEAD]
    rows = con.execute(
        "SELECT * FROM issues ORDER BY id DESC").fetchall()
    if not rows:
        parts.append("\n(No issues logged yet. The daily run appends entries"
                     " below, newest first.)\n")
    for r in rows:
        day = r["created_at"][:10]
        parts.append("\n## %s — %s — %s\n" % (r["qa_id"], day, r["product"]))
        if r["gh_number"]:
            slug = r["repo_slug"]
            url = "https://github.com/%s/issues/%d" % (slug, r["gh_number"])
            parts.append("- GitHub: #%d %s (%s)\n"
                         % (r["gh_number"], url, r["gh_state"]))
        else:
            parts.append("- GitHub: n/a (no repo — ledger only)\n")
        if r["suite_ref"]:
            parts.append("- Suite: %s\n" % r["suite_ref"])
        parts.append("- Check: %s\n" % (r["check_name"] or r["title"]))
        parts.append("- Observed: %s\n" % (r["observed"] or ""))
        parts.append("- Repro:\n")
        for i, s in enumerate((r["repro"] or "").splitlines(), 1):
            parts.append("  %d. %s\n" % (i, s))
        parts.append("- Status: %s\n" % r["qa_status"])
    con.close()
    with open(path, "w") as f:
        f.write("".join(parts))


def cmd_list():
    init_db()
    con = connect()
    for r in con.execute("SELECT qa_id, product, gh_number, qa_status, title"
                         " FROM issues ORDER BY id DESC"):
        gh = "#%d" % r["gh_number"] if r["gh_number"] else "ledger-only"
        print("%s %-16s %-12s %-22s %s"
              % (r["qa_id"], r["product"], gh, r["qa_status"], r["title"][:60]))
    con.close()


def main(argv):
    cmds = {"init": lambda: init_db(),
            "pull": lambda: print("\n".join(sync()) or "sync: no changes"),
            "sync": lambda: print("\n".join(sync()) or "sync: no changes"),
            "export-ledger": lambda: export_ledger(),
            "list": cmd_list}
    if len(argv) < 2 or argv[1] not in cmds:
        sys.stderr.write("usage: bugs.py init|pull|sync|export-ledger|list\n")
        return 2
    cmds[argv[1]]()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
