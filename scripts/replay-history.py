#!/usr/bin/env python3
"""replay-history.py -- judge a repository's git history with the Stop gate, at zero tokens.

Usage:
  replay-history.py REPO [--commits N] [--path PREFIX] [--plugin-root DIR] [--jobs J]
                    [--out FILE.jsonl] [--summary FILE.json] [--name NAME]
  replay-history.py REPO --static [--path PREFIX] [--plugin-root DIR] [--summary FILE.json] [--name NAME]
  replay-history.py REPO --sample K --out FILE.jsonl [--seed S]

History replay (the default). Each of the last N non-merge commits of REPO
(default 200) is judged as if one Claude Code session had written exactly
that commit: the files it added or modified are the session's changes, its
first parent is the baseline, and the checks are those of the Stop gate of
the plugin at --plugin-root (default: the plugin this script belongs to).
Pointing --plugin-root at an older release replays the same history with
that release's rules, so two runs compare rule sets on the same commits.

Static corpus (--static). The tree at HEAD (under PREFIX) is copied as new,
untracked files into a temporary repository with one empty commit, the way
a session that imported it would look. The three command-line scanners run
there (craft-debt-scan.sh --quiet, craft-comment-sync.py --check-diff,
craft-metrics.py --report) and give counts per file and kind; the gate
checks give each finding its tier, and the function extractor gives the
share of functions and modules that receive a review signal.

Reading the results. The commits of experienced authors are the control
group: a hard finding that is not debt (TODO, NO-CEILING, NO-TRIGGER) is
almost always a false positive, while a bare TODO or XXX in their history
is a real violation of the plugin's rules. The summary keeps debt, BACKUP
and the heuristic findings in three columns. Only pct_hard_heuristic
measures the gate's errors, so only it has a target (under 2% of commits);
debt and BACKUP measure the repository's history and have none. Every
non-debt hard finding is listed on stdout with its commit, file and line
for a human to judge. --sample K prints K findings drawn at
random from a replay's JSON lines, with the code around each one.

Output. --out appends one JSON object per commit (sha, files, added lines,
hard and soft counts per kind, inexact files, the findings themselves), so
an interrupted run resumes where it stopped. --summary merges counts only
under the repository's name into a JSON file (bench/ keeps these): finding
messages quote source lines, and a quoted "# TODO" in a JSON file would be
read as debt by the plugin's own scan.

How it stays fast (redis/src has files of thousands of lines):
  - no checkout: blobs come from one `git cat-file --batch` process per
    worker and diffs from `git diff -U0 PARENT COMMIT`;
  - the plugin's code runs unchanged, whatever its version: the loaded
    craftlib is patched so that its repository-wide `git grep --untracked`
    runs on the commit's tree (restricted to PREFIX) and read_text() reads
    the commit's blobs;
  - scan() results are cached by content, since the parent's version of a
    file was usually read at the previous commit;
  - commits go to a process pool in contiguous chunks, in order.
"""

import argparse
import collections
import concurrent.futures
import hashlib
import importlib.util
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
DEBT_KINDS = {"TODO", "NO-CEILING", "NO-TRIGGER"}
FINDING = re.compile(r"^(?P<file>.+?):(?P<line>\d+): \[(?P<kind>[A-Z][A-Z-]*)\]")
MORE = re.compile(r"^\.\.\. (\d+) more$")
SCAN_CACHE = 512
# Function-level review signals name their function in quotes: "[RHYTHM] 'name'".
FUNCTION_KINDS = {"RHYTHM", "HELPER", "NO-DOC"}
MODULE_KINDS = {"NO-DESIGN", "NO-TEACHER", "NO-LAYOUT-ASSERT"}
QUOTED = re.compile(r"\] '([^']+)'")


def git(repo, *args):
    """Run git in `repo` and return stdout ('' on failure: callers treat it as no output)."""
    r = subprocess.run(["git", "-C", repo, "-c", "core.quotePath=false", *args], capture_output=True,
                       encoding="utf-8", errors="replace")
    return r.stdout if r.returncode == 0 else ""


class Blobs:
    """Read REV:PATH contents through one long-lived `git cat-file --batch` process.

    Why one process: on Windows every git spawn costs tens of milliseconds,
    and a replay reads two versions of every changed file of every commit.
    The process is owned by this object and dies with the worker.
    """

    def __init__(self, repo):
        self.proc = subprocess.Popen(["git", "-C", repo, "cat-file", "--batch"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE)

    def read(self, rev, path):
        """Return the bytes of `path` at `rev`, or None when it does not exist there."""
        self.proc.stdin.write(("%s:%s\n" % (rev, path)).encode("utf-8"))
        self.proc.stdin.flush()
        header = self.proc.stdout.readline().split()
        if len(header) != 3 or header[1] != b"blob":
            return None
        data = self.proc.stdout.read(int(header[2]))
        self.proc.stdout.read(1)
        return data


class Replay:
    """The plugin at one root, loaded and patched to judge commits of one repository.

    `rev` is the commit being judged; the patched craftlib.git and
    craftlib.read_text resolve repository-wide lookups against it.
    """

    def __init__(self, repo, plugin_root, prefix):
        self.repo, self.prefix, self.rev = os.path.abspath(repo), prefix, None
        self.mods = load_plugin(plugin_root)
        self.cl = self.mods["craftlib"]
        self.blobs = Blobs(self.repo)
        self._patch()

    def _patch(self):
        """Redirect craftlib's git grep, read_text and scan as the module docstring explains."""
        cl, real_git, real_read, real_scan = self.cl, self.cl.git, self.cl.read_text, self.cl.scan
        root = os.path.normcase(os.path.normpath(self.repo))
        cache = collections.OrderedDict()

        def patched_git(args, cwd):
            if not args or args[0] != "grep" or self.rev is None:
                return real_git(args, cwd)
            args = [a for a in args if a != "--untracked"]
            if "--" in args:
                at = args.index("--")
                args = args[:at] + [self.rev] + args[at:]
            else:
                args = args + [self.rev] + (["--", self.prefix] if self.prefix else [])
            out = real_git(args, cwd)
            if out is None:
                return None
            head = self.rev + ":"
            return "".join(line[len(head):] if line.startswith(head) else line for line in out.splitlines(True))

        def patched_read(path):
            full = os.path.normcase(os.path.normpath(path))
            if self.rev is None or not full.startswith(root + os.sep):
                return real_read(path)
            data = self.blobs.read(self.rev, full[len(root) + 1:].replace(os.sep, "/"))
            if data is None or len(data) > cl.MAX_BYTES:
                return None
            return data.decode("utf-8", "replace")

        def cached_scan(text, family):
            key = (family, hashlib.sha1(text.encode("utf-8", "replace")).digest())
            if key in cache:
                cache.move_to_end(key)
                return list(cache[key])
            lines = real_scan(text, family)
            cache[key] = lines
            if len(cache) > SCAN_CACHE:
                cache.popitem(last=False)
            return list(lines)

        cl.git, cl.read_text, cl.scan = patched_git, patched_read, cached_scan

    def changes(self, sha):
        """Return the craftlib Change list of commit `sha`, judged against its first parent."""
        cl = self.cl
        parent = git(self.repo, "rev-parse", "-q", "--verify", sha + "^1").strip() or EMPTY_TREE
        spec = ["--", self.prefix] if self.prefix else []
        names = git(self.repo, "diff", "--name-only", "--diff-filter=AMR", parent, sha, *spec).splitlines()
        added = hunks(git(self.repo, "diff", "-U0", "--no-color", "--no-ext-diff", parent, sha, *spec))
        out = []
        for rel in names:
            if not cl.debt_scannable(rel):
                continue
            data = self.blobs.read(sha, rel)
            if data is None or len(data) > cl.MAX_BYTES:
                continue
            text = data.decode("utf-8", "replace")
            before = None if parent == EMPTY_TREE else self.blobs.read(parent, rel)
            new = before is None
            old = "" if new or not cl.family_of(rel) else before.decode("utf-8", "replace")
            lines = set(range(1, text.count("\n") + 2)) if new else added.get(rel, set())
            out.append(cl.Change(rel, os.path.join(self.repo, rel), text, old, new, lines))
        return out

    def touches_tests(self, sha):
        """Return True if commit `sha` changes a test file anywhere in the repository, PREFIX or not."""
        parent = git(self.repo, "rev-parse", "-q", "--verify", sha + "^1").strip() or EMPTY_TREE
        names = git(self.repo, "diff", "--name-only", "--diff-filter=AMR", parent, sha).splitlines()
        return any(self.mods["craft-metrics"].TEST_PATH.search(rel) for rel in names)

    def judge(self, sha):
        """Return the result record of one commit (see the module docstring, Output)."""
        self.rev = sha
        try:
            changes = self.changes(sha)
            hard, soft = run_checks(self.mods, self.repo, changes)
            # Why: --path hides the test files outside PREFIX (redis keeps its
            # code in src/, its tests in tests/), and NO-TEST would blame the commit for that.
            if self.prefix and any("[NO-TEST]" in message for _, message in soft) and self.touches_tests(sha):
                soft = [item for item in soft if "[NO-TEST]" not in item[1]]
        finally:
            self.rev = None
        inexact = getattr(self.cl, "inexact_reason", None)
        return record(sha, changes, hard, soft,
                      [ch.rel for ch in changes if inexact and inexact(ch.rel, ch.text)])


def hunks(diff):
    """Map each file of a `git diff -U0` to the set of its added or changed line numbers."""
    out, current = {}, None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@") and current:
            m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if m:
                start = int(m.group(1))
                out.setdefault(current, set()).update(range(start, start + int(m.group(2) or 1)))
    return out


def load_plugin(root):
    """Import craftlib and the four gate scripts from `root`/bin as fresh modules.

    Why sys.path: the scripts run `import craftlib`, and it must resolve to
    the same release's craftlib. Bytecode is never written, so replaying an
    unpacked release leaves its folder untouched.
    """
    bin_dir = os.path.join(os.path.abspath(root), "bin")
    sys.dont_write_bytecode = True
    sys.path.insert(0, bin_dir)
    sys.modules.pop("craftlib", None)
    import craftlib  # noqa: E402  (resolved through bin_dir, see above)
    mods = {"craftlib": craftlib}
    for name in ("craft-debt-scan", "craft-comment-sync", "craft-metrics", "craft-gate"):
        spec = importlib.util.spec_from_file_location(name.replace("-", "_"), os.path.join(bin_dir, name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        mods[name] = module
    return mods


def run_checks(mods, root, changes):
    """Return (hard, soft) (key, message) lists for `changes`, the way the plugin's gate tiers them.

    Releases from 0.0.4 expose craft-gate.collect(); 0.0.3 has no such
    entry point, and its gate treats every debt and sync finding as hard and
    every metrics finding as a review signal.
    """
    if not changes:
        return [], []
    gate = mods["craft-gate"]
    if hasattr(gate, "collect"):
        return gate.collect(root, changes)
    hard = mods["craft-debt-scan"].gate_findings(root, changes) + mods["craft-comment-sync"].gate_findings(root, changes)
    return hard, mods["craft-metrics"].gate_findings(root, changes)


def record(sha, changes, hard, soft, inexact):
    """Build one commit's JSON record; findings are [tier, kind, file, line, message]."""
    findings = []
    for tier, items in (("hard", hard), ("soft", soft)):
        for _, message in items:
            m = FINDING.match(message)
            if m:
                findings.append([tier, m.group("kind"), m.group("file"), int(m.group("line")), message[:400]])
    count = collections.Counter
    return {"sha": sha, "files": len(changes), "lines": sum(len(ch.added) for ch in changes),
            "hard": dict(count(f[1] for f in findings if f[0] == "hard")),
            "soft": dict(count(f[1] for f in findings if f[0] == "soft")),
            "inexact": inexact, "findings": findings}


# Worker side of the process pool: one Replay per process, built once.
# Why two module-level helpers: they are the process boundary.
# ProcessPoolExecutor pickles its initializer and the mapped function by
# name, and on Windows (spawn) a lambda or a bound method of a parent-side
# Replay cannot cross; inlining them would break --jobs > 1.
_WORKER = None


def _init_worker(repo, plugin_root, prefix):
    """Pool initializer: build this process's Replay (git blob reader and plugin modules) once."""
    global _WORKER
    _WORKER = Replay(repo, plugin_root, prefix)


def _judge(sha):
    """Pool task: judge one commit with this process's Replay; return its JSON record."""
    return _WORKER.judge(sha)


def replay(args):
    """History mode: judge the last --commits non-merge commits; return the exit code."""
    repo = os.path.abspath(args.repo)
    spec = ["--", args.path] if args.path else []
    shas = git(repo, "rev-list", "--no-merges", "-n", str(args.commits), "HEAD", *spec).split()
    shas.reverse()

    # Resume: commits already in --out are not judged again.
    done = {}
    if args.out and os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    rec = json.loads(line)
                    done[rec["sha"]] = rec
    todo = [s for s in shas if s not in done]

    # Judge the rest over a process pool, appending each record as it arrives.
    sink = open(args.out, "a", encoding="utf-8") if args.out else None
    try:
        if todo:
            jobs = max(1, min(args.jobs, len(todo)))
            with concurrent.futures.ProcessPoolExecutor(jobs, initializer=_init_worker,
                                                        initargs=(repo, args.plugin_root, args.path)) as pool:
                for rec in pool.map(_judge, todo, chunksize=max(1, len(todo) // (jobs * 4))):
                    done[rec["sha"]] = rec
                    if sink:
                        sink.write(json.dumps(rec) + "\n")
                        sink.flush()
    finally:
        if sink:
            sink.close()

    records = [done[s] for s in shas if s in done]
    summary = dict(summarize(records, plugin_version(args.plugin_root)), path=args.path)
    for sha, kind, where, line in summary.pop("hard_non_debt_list"):
        print("%s %s:%d [%s]" % (sha[:12], where, line, kind))
    print(json.dumps({k: v for k, v in summary.items() if k != "per_commit"}, sort_keys=True))
    if args.summary:
        merge_summary(args.summary, args.name or os.path.basename(repo.rstrip("/\\")), summary)
    return 0


def summarize(records, version):
    """Aggregate commit records into the counts --summary stores (see the module docstring).

    Hard findings fall in three columns: debt (a bare TODO is a real
    violation in anyone's history), BACKUP (commented-out code: a real
    violation too, but one a human should confirm) and heuristic (the rest,
    which on expert commits is almost always a false positive of the checks).
    """
    kinds = {"hard": collections.Counter(), "soft": collections.Counter()}
    groups = collections.Counter()
    per_commit, listed, inexact = [], [], set()
    for rec in records:
        hard_debt = sum(n for k, n in rec["hard"].items() if k in DEBT_KINDS)
        hard_backup = rec["hard"].get("BACKUP", 0)
        hard_heuristic = sum(n for k, n in rec["hard"].items() if k not in DEBT_KINDS and k != "BACKUP")
        soft = sum(rec["soft"].values())
        kinds["hard"].update(rec["hard"])
        kinds["soft"].update(rec["soft"])
        groups.update({"debt": bool(hard_debt), "backup": bool(hard_backup), "heuristic": bool(hard_heuristic),
                       "soft": bool(soft)})
        inexact.update(rec.get("inexact") or [])
        listed += [(rec["sha"], f[1], f[2], f[3]) for f in rec["findings"] if f[0] == "hard" and f[1] not in DEBT_KINDS]
        per_commit.append([rec["sha"][:12], rec["files"], rec["lines"], hard_debt, hard_backup, hard_heuristic, soft])
    n = len(records)
    pct = (lambda k: round(100.0 * groups[k] / n, 1) if n else 0.0)
    return {"plugin": version, "commits": n,
            "commits_hard_debt": groups["debt"], "commits_hard_backup": groups["backup"],
            "commits_hard_heuristic": groups["heuristic"],
            "pct_hard_debt": pct("debt"), "pct_hard_backup": pct("backup"), "pct_hard_heuristic": pct("heuristic"),
            "commits_soft": groups["soft"], "pct_soft": pct("soft"),
            "kinds": {t: dict(sorted(c.items())) for t, c in kinds.items()}, "inexact_files": sorted(inexact),
            "per_commit_columns": ["sha", "files", "lines", "hard_debt", "hard_backup", "hard_heuristic", "soft"],
            "per_commit": per_commit, "hard_non_debt_list": listed}


def plugin_version(root):
    """Return the version in `root`/.claude-plugin/plugin.json, or '?'."""
    try:
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
            return json.load(fh).get("version", "?")
    except (OSError, ValueError):
        return "?"


def merge_summary(path, name, summary):
    """Store `summary` under repos[name] in the JSON file at `path`, keeping the other repositories."""
    data = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    data.setdefault("repos", {})[name] = summary
    data["plugin"] = summary["plugin"]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
        fh.write("\n")


def static(args):
    """Static mode: import HEAD as new files, run the scanners and the gate checks; return the exit code."""
    repo, plugin = os.path.abspath(args.repo), os.path.abspath(args.plugin_root)
    tmp = tempfile.mkdtemp(prefix="craft-corpus-")
    try:
        # 1. A repository with one empty commit, then every file of HEAD (under PREFIX) as new.
        for cmd in (["init", "-q"], ["config", "core.autocrlf", "false"],
                    ["-c", "user.name=bench", "-c", "user.email=bench@example.com", "-c", "commit.gpgsign=false",
                     "commit", "-q", "--allow-empty", "-m", "empty"]):
            subprocess.run(["git", "-C", tmp, *cmd], check=True, capture_output=True)
        spec = ["--", args.path] if args.path else []
        for rel in git(repo, "ls-files", *spec).splitlines():
            src, dst = os.path.join(repo, rel), os.path.join(tmp, rel)
            if os.path.isfile(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)

        # 2. The three command-line scanners, as a user would run them. Why only totals:
        #    the text reports stop after 20 lines ("... N more"), so per-file counts
        #    come from the gate checks below, which see every finding.
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        bin_dir = os.path.join(plugin, "bin")
        clis = {"debt": ["bash", os.path.join(bin_dir, "craft-debt-scan.sh").replace("\\", "/"), "--quiet"],
                "sync": [sys.executable, os.path.join(bin_dir, "craft-comment-sync.py"), "--check-diff"]}
        cli = {}
        for name, cmd in clis.items():
            proc = subprocess.run(cmd, cwd=tmp, capture_output=True, encoding="utf-8", errors="replace", env=env)
            lines = proc.stdout.splitlines()
            more = sum(int(m.group(1)) for m in (MORE.match(x) for x in lines) if m)
            cli[name] = {"exit": proc.returncode, "findings": sum(bool(FINDING.match(x)) for x in lines) + more}
        proc = subprocess.run([sys.executable, os.path.join(bin_dir, "craft-metrics.py"), "--report"], cwd=tmp,
                              capture_output=True, encoding="utf-8", errors="replace", env=env)
        report = json.loads(proc.stdout or "{}")
        cli["metrics"] = {"exit": proc.returncode, "findings": report.get("f", {}).get("n", 0)}

        # 3. The gate checks over the same session: tiers, and the share of functions and modules signalled.
        mods = load_plugin(plugin)
        changes = mods["craft-gate"].session_changes(tmp, {})
        hard, soft = run_checks(mods, tmp, changes)
        rec = record("static", changes, hard, soft,
                     [ch.rel for ch in changes if getattr(mods["craftlib"], "inexact_reason", None)
                      and mods["craftlib"].inexact_reason(ch.rel, ch.text)])
        coverage = signal_coverage(mods, changes, rec["findings"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    summary = {"plugin": plugin_version(plugin), "path": args.path, "files": rec["files"], "lines": rec["lines"],
               "hard": rec["hard"], "soft": rec["soft"], "inexact_files": rec["inexact"],
               "rhythm_ok": report.get("total", {}).get("rhythm_ok"), "coverage": coverage, "cli": cli,
               "per_file": per_file(rec["findings"])}
    for f in rec["findings"]:
        print("%s %s:%d [%s]" % (f[0], f[2], f[3], f[1]))
    print(json.dumps({k: v for k, v in summary.items() if k != "per_file"}, sort_keys=True))
    if args.summary:
        merge_summary(args.summary, args.name or os.path.basename(repo.rstrip("/\\")), summary)
    return 0


def per_file(findings):
    """Return {file: {"tier:kind": count}} over `findings`, for the files that have any."""
    out = collections.defaultdict(collections.Counter)
    for tier, kind, where, _, _ in findings:
        out[where]["%s:%s" % (tier, kind)] += 1
    return {f: dict(sorted(c.items())) for f, c in sorted(out.items())}


def signal_coverage(mods, changes, findings):
    """Return the share of functions, long functions and modules that received a review signal.

    A function is signalled when a function-level signal names it in its
    file. modules_signalled counts a module with any soft signal, one comment
    among them included; module_level_signalled only those a module-level
    check (MODULE_KINDS) reported, the level the review thresholds speak of.
    Long means at least the metrics script's LONG_FN lines of code.
    """
    metrics, cl = mods["craft-metrics"], mods["craftlib"]
    named = collections.defaultdict(set)
    rhythm = set()
    modules = set()
    module_level = set()
    for tier, kind, where, _, message in findings:
        if tier != "soft":
            continue
        modules.add(where)
        if kind in MODULE_KINDS:
            module_level.add(where)
        m = QUOTED.search(message)
        if kind in FUNCTION_KINDS and m:
            named[where].add(m.group(1))
            if kind == "RHYTHM":
                rhythm.add((where, m.group(1)))
    total = long_total = signalled = long_signalled = code_files = 0
    for ch in changes:
        if not cl.family_of(ch.rel):
            continue
        code_files += 1
        lines, funcs = metrics.functions(ch.path, ch.text)
        for f in funcs:
            total += 1
            signalled += f.name in named[ch.rel]
            if metrics.body_stats(lines, f)[0] >= metrics.LONG_FN:
                long_total += 1
                long_signalled += (ch.rel, f.name) in rhythm
    share = (lambda a, b: round(a / b, 3) if b else 0.0)
    return {"functions": total, "functions_signalled": share(signalled, total), "long_functions": long_total,
            "long_rhythm": share(long_signalled, long_total), "modules": code_files,
            "modules_signalled": share(len(modules), code_files),
            "module_level_signalled": share(len(module_level), code_files)}


def sample(args):
    """Sample mode: print --sample findings drawn from --out, each with its code context."""
    findings = []
    with open(args.out, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rec = json.loads(line)
                findings += [(rec["sha"], f) for f in rec["findings"]]
    rng = random.Random(args.seed)
    for sha, (tier, kind, where, line, message) in rng.sample(findings, min(args.sample, len(findings))):
        text = git(args.repo, "show", "%s:%s" % (sha, where)).split("\n")
        lo, hi = max(1, line - 3), min(len(text), line + 3)
        print("=== %s %s %s:%d [%s] %s" % (sha[:12], tier, where, line, kind, message.split("] ", 1)[-1]))
        for no in range(lo, hi + 1):
            print("%s%5d  %s" % (">" if no == line else " ", no, text[no - 1]))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", help="git repository to judge")
    parser.add_argument("--commits", type=int, default=200, help="non-merge commits to replay (default 200)")
    parser.add_argument("--path", default="", help="judge only files under this path (e.g. src/)")
    parser.add_argument("--plugin-root", default=os.path.dirname(HERE), help="plugin release whose rules judge")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1, help="worker processes")
    parser.add_argument("--out", help="JSON lines file of per-commit records (resumable)")
    parser.add_argument("--summary", help="JSON file that keeps the counts per repository")
    parser.add_argument("--name", help="name of this repository in --summary (default: its folder)")
    parser.add_argument("--static", action="store_true", help="corpus mode: import HEAD as new files")
    parser.add_argument("--sample", type=int, help="print this many findings drawn from --out")
    parser.add_argument("--seed", type=int, default=0, help="random seed for --sample")
    args = parser.parse_args()
    if args.sample:
        return sample(args)
    return static(args) if args.static else replay(args)


if __name__ == "__main__":
    sys.exit(main())
