#!/usr/bin/env python3
"""install.py: copy hub-starter patterns into $HUBKIT_HOME. Run through ../install.sh.

Rules (each one is tested in tools/test_install.py):
  - never overwrites: a file that already exists is kept and listed, even if it differs
  - never writes settings.json unless you pass --wire-settings (then it merges only the hook entries
    that are missing, and keeps every entry you already have); never writes any other file under
    ~/.claude except agents/, commands/ and the memory folder, and only when the name is free
  - backs up an existing ~/.claude to ~/.claude.backup-<time> before the first write (skip with --no-backup)
  - idempotent: a second run changes nothing and logs nothing
  - --dry-run writes nothing, not even folders
  - fails loud (exit 2) when HOME is unset or the install folder cannot be written
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MARK = "hub-starter install.sh"


class Fail(Exception):
    pass


class Run:
    def __init__(self, dry):
        self.dry = dry
        self.new = []      # files written
        self.same = []     # already there, identical
        self.kept = []     # already there, different: kept yours
        self.written = []  # (absolute path, sha256) of every file this run created

    def put(self, dest, data, mode=0o644, label=None):
        """Create dest from data without ever replacing an existing path."""
        label = label or dest
        if os.path.lexists(dest):
            try:
                same = os.path.isfile(dest) and not os.path.islink(dest) and read(dest) == data
            except OSError:
                same = False
            (self.same if same else self.kept).append(label)
            return
        self.new.append(label)
        if self.dry:
            return
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)   # O_EXCL: cannot replace
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(dest, mode)
        self.written.append((dest, hashlib.sha256(data).hexdigest()))


def read(path):
    with open(path, "rb") as fh:
        return fh.read()


def load_patterns():
    pats = {}
    pdir = os.path.join(ROOT, "patterns")
    for name in sorted(os.listdir(pdir)):
        pj = os.path.join(pdir, name, "pattern.json")
        if not os.path.isfile(pj):
            continue
        with open(pj, encoding="utf-8") as fh:
            d = json.load(fh)
        if d.get("name") != name or not NAME_RE.match(name):
            raise Fail("pattern.json name does not match folder: %s" % name)
        for f in d.get("files", []):
            if not FILE_RE.match(f):
                raise Fail("unsafe file name in %s: %r" % (name, f))
        pats[name] = d
    return pats


def select(pats, args):
    if args.all:
        want = list(pats)
    elif args.only:
        want = [n.strip() for n in args.only.split(",") if n.strip()]
        bad = [n for n in want if n not in pats]
        if bad:
            raise Fail("unknown pattern(s): %s. Available: %s" % (", ".join(bad), ", ".join(pats)))
    else:
        want = [n for n, d in pats.items() if d.get("core")]
    out, added = [], []

    def add(n, why=None):
        if n in out:
            return
        for dep in pats[n].get("depends_on", []):
            if dep not in pats:
                raise Fail("%s depends on unknown pattern %s" % (n, dep))
            add(dep, "needed by %s" % n)
        out.append(n)
        if why:
            added.append("%s (%s)" % (n, why))

    for n in want:
        add(n)
    if "doctor" in pats and "doctor" not in out:     # TROUBLESHOOTING's first move needs it, so it is always there
        out.append("doctor")
        added.append("doctor (the health check; always installed)")
    return out, added


def prefixed_agent(data, prefix):
    if not prefix:
        return data
    return re.sub(rb"(?m)^name:\s*(\S+)\s*$", lambda m: b"name: " + prefix.encode() + m.group(1), data, count=1)


def hook_entries(descs, hubkit):
    hooks = {}
    for d in descs:
        for h in d.get("hooks", []):
            ent = {"hooks": [{"type": "command", "command": h["command"].replace("$HUBKIT_HOME", hubkit)}]}
            if h.get("matcher"):
                ent = {"matcher": h["matcher"], **ent}
            hooks.setdefault(h["event"], []).append(ent)
    return hooks


def installed_descs(hubkit):
    """pattern.json of every pattern now installed under hubkit (so the snippet covers earlier runs too)."""
    out = []
    if os.path.isdir(hubkit):
        for name in sorted(os.listdir(hubkit)):
            pj = os.path.join(hubkit, name, "pattern.json")
            if os.path.isfile(pj):
                try:
                    with open(pj, encoding="utf-8") as fh:
                        out.append(json.load(fh))
                except (OSError, ValueError):
                    pass
    return out


def manifest_path(hubkit):
    return os.path.join(hubkit, "state", "install-manifest.json")


def record_manifest(hubkit, written):
    """Remember every file we created (path + hash) so --uninstall can remove exactly those."""
    if not written:
        return
    mp = manifest_path(hubkit)
    cur = {}
    try:
        with open(mp, encoding="utf-8") as fh:
            cur = json.load(fh).get("files", {})
    except (OSError, ValueError, AttributeError):
        cur = {}
    for path, digest in written:
        cur[path] = digest
    os.makedirs(os.path.dirname(mp), exist_ok=True)
    with open(mp, "w", encoding="utf-8") as fh:
        json.dump({"_generated_by": MARK, "files": cur}, fh, indent=1, sort_keys=True)
        fh.write("\n")


def unwire_hooks(settings_path, hubkit, dry):
    """Remove only the hook entries whose command runs a script inside hubkit (what --wire-settings added).
    Everything else in settings.json is kept. A missing or unreadable file is left alone. Returns the count."""
    try:
        with open(settings_path, encoding="utf-8") as fh:
            cur = json.load(fh)
        hooks = cur.get("hooks")
        if not isinstance(hooks, dict):
            return 0
    except (OSError, ValueError, AttributeError):
        return 0
    prefix = hubkit.rstrip(os.sep) + os.sep
    removed = 0
    for event in list(hooks):
        arr = hooks[event]
        if not isinstance(arr, list):
            continue
        keep = []
        for e in arr:
            cmds = [h.get("command", "") for h in e.get("hooks", []) if isinstance(h, dict)] if isinstance(e, dict) else []
            if cmds and all(isinstance(c, str) and prefix in c for c in cmds):
                removed += 1
            else:
                keep.append(e)
        if keep:
            hooks[event] = keep
        else:
            del hooks[event]
    if removed and not dry:
        if not hooks:
            del cur["hooks"]
        tmp = settings_path + ".tmp-%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cur, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, settings_path)
    return removed


def uninstall(hubkit, dry, settings_path=None):
    """Remove files recorded in the install manifest, only if still byte-identical to what was written."""
    mp = manifest_path(hubkit)
    try:
        with open(mp, encoding="utf-8") as fh:
            files = json.load(fh)["files"]
    except (OSError, ValueError, KeyError):
        raise Fail("no install record at %s; nothing to uninstall (delete %s by hand if you want it gone)" % (mp, hubkit))
    unwired = unwire_hooks(settings_path, hubkit, dry) if settings_path else 0
    gone, kept, dirs = [], [], set()
    for path, digest in sorted(files.items()):
        if not os.path.isabs(path) or not os.path.lexists(path):
            continue
        try:
            same = os.path.isfile(path) and not os.path.islink(path) and hashlib.sha256(read(path)).hexdigest() == digest
        except OSError:
            same = False
        if not same:
            kept.append(path)
            continue
        gone.append(path)
        dirs.add(os.path.dirname(path))
        if not dry:
            os.remove(path)
    if not dry:
        for extra in (mp, os.path.join(hubkit, "state", "events.log")):
            if os.path.isfile(extra) and not (extra.endswith("events.log") and kept):
                os.remove(extra)
        dirs.add(os.path.join(hubkit, "state"))
        projects = os.path.join(os.environ.get("HOME", "/nonexistent"), ".claude", "projects")
        for d in sorted(dirs, key=len, reverse=True):          # only empty folders, deepest first
            while os.path.isdir(d):                             # climb only inside hubkit or the memory store; outside, just that one folder
                if os.path.basename(d) == "memory" and (d + os.sep).startswith(projects + os.sep):
                    try:
                        os.rmdir(os.path.join(d, "conversations"))    # the empty folder the installer made
                    except OSError:
                        pass
                try:
                    os.rmdir(d)
                except OSError:
                    break
                d = os.path.dirname(d)
                if not ((d + os.sep).startswith(hubkit + os.sep) or (d + os.sep).startswith(projects + os.sep) or d == projects):
                    break
    print("hub-starter uninstall%s -> %s" % (" (DRY RUN, nothing removed)" % () if False else (" (DRY RUN, nothing removed)" if dry else ""), hubkit))
    print("%s: %d   kept (you changed them): %d" % ("would remove" if dry else "removed", len(gone), len(kept)))
    for k in kept:
        print("  kept: %s" % k)
    if unwired:
        print("settings.json: %s %d kit hook entr%s (all your own entries kept)" % ("would remove" if dry else "removed", unwired, "y" if unwired == 1 else "ies"))
    else:
        print("Your settings.json was NOT touched (no kit hook entries found in it).")
    st = os.path.join(hubkit, "state")
    if os.path.isdir(st):
        print("Kept your data in %s (ledger and logs). Delete that folder by hand if you want it gone." % st)
    return 0


BACKUP_SKIP_DIRS = {"shell-snapshots", "statsig", "ide", "cache", "paste-cache", "telemetry", "todos"}


def backup_claude(claude, home):
    """Copy an existing ~/.claude to ~/.claude.backup-<time> (symlinks kept as links; chat transcripts and caches
    skipped because they are large and regenerable). Returns the backup path, or None if there was nothing to save."""
    if not os.path.isdir(claude) or not os.listdir(claude):
        return None
    dest = os.path.join(home, ".claude.backup-" + time.strftime("%Y%m%d-%H%M%S"))
    n = 1
    while os.path.lexists(dest):
        n += 1
        dest = os.path.join(home, ".claude.backup-%s-%d" % (time.strftime("%Y%m%d-%H%M%S"), n))

    def ignore(d, names):
        return [x for x in names if x.endswith(".jsonl") or d == claude and x in BACKUP_SKIP_DIRS]
    try:
        shutil.copytree(claude, dest, symlinks=True, ignore=ignore)
    except (OSError, shutil.Error) as e:
        raise Fail("could not back up %s to %s (%s); nothing installed" % (claude, dest, e))
    return dest


def merge_hooks(settings_path, hooks, dry):
    """Add the hook entries that are not there yet to settings.json. Existing entries are never changed or
    removed. Returns the number of entries added. A settings.json that is not valid JSON is never touched."""
    cur = {}
    if os.path.lexists(settings_path):
        try:
            with open(settings_path, encoding="utf-8") as fh:
                cur = json.load(fh)
        except (OSError, ValueError):
            raise Fail("%s is not valid JSON; fix it (or merge the snippet by hand). Nothing was changed there." % settings_path)
        if not isinstance(cur, dict) or not isinstance(cur.get("hooks", {}), dict):
            raise Fail("%s has an unexpected shape (hooks must be an object); nothing was changed there." % settings_path)
    added = 0
    cur_hooks = cur.setdefault("hooks", {})
    for event, entries in hooks.items():
        arr = cur_hooks.setdefault(event, [])
        if not isinstance(arr, list):
            raise Fail("%s: hooks.%s is not a list; nothing was changed there." % (settings_path, event))
        have = {h.get("command") for e in arr if isinstance(e, dict) for h in e.get("hooks", []) if isinstance(h, dict)}
        for ent in entries:
            if all(h["command"] in have for h in ent["hooks"]):
                continue
            arr.append(ent)
            added += 1
    if added and not dry:
        tmp = settings_path + ".tmp-%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cur, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, settings_path)
    return added


def main(argv):
    ap = argparse.ArgumentParser(prog="install.sh",
                                 description="Install hub-starter patterns. Never overwrites, never writes settings.json.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--core", action="store_true", help="core patterns (default)")
    g.add_argument("--only", metavar="a,b", help="only these patterns (plus what they depend on)")
    g.add_argument("--all", action="store_true", help="every pattern")
    ap.add_argument("--with-hub-command", action="store_true",
                    help="also copy the hub command (~/.claude/commands/hub.md) when using --only (default and --all copy it)")
    ap.add_argument("--uninstall", action="store_true",
                    help="remove exactly the files an earlier install wrote (files you edited are kept); honours --dry-run")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen, write nothing")
    ap.add_argument("--wire-settings", action="store_true",
                    help="also merge the missing hook entries into ~/.claude/settings.json (existing entries are kept)")
    ap.add_argument("--no-backup", action="store_true", help="skip the backup of an existing ~/.claude")
    ap.add_argument("--prefix", default="", help="prefix for agent and command files copied into ~/.claude")
    args = ap.parse_args(argv)
    if args.prefix and not re.match(r"^[A-Za-z0-9-]+$", args.prefix):
        raise Fail("--prefix may only use letters, digits and dashes")

    home = os.environ.get("HOME", "")
    if not home or not os.path.isabs(home):
        raise Fail("HOME is not set to an absolute path; refusing to guess where to install")
    hubkit = os.environ.get("HUBKIT_HOME") or os.path.join(home, ".claude", "hubkit")
    if not os.path.isabs(hubkit):
        raise Fail("HUBKIT_HOME must be an absolute path")
    claude = os.path.join(home, ".claude")
    # fail loud before writing anything: the nearest existing ancestor must be a writable folder
    probe = hubkit
    while not os.path.lexists(probe):
        probe = os.path.dirname(probe)
    if not os.path.isdir(probe) or not os.access(probe, os.W_OK | os.X_OK):
        raise Fail("cannot write under %s (not a writable folder); nothing installed" % probe)

    if args.uninstall:
        return uninstall(hubkit, args.dry_run, os.path.join(claude, "settings.json"))
    pats = load_patterns()
    chosen, added = select(pats, args)
    r = Run(args.dry_run)
    settings_path = os.path.join(claude, "settings.json")
    first_install = not os.path.isfile(manifest_path(hubkit))
    wire_pending = False
    if args.wire_settings and not args.dry_run:
        wire_pending = merge_hooks(settings_path, hook_entries([pats[n] for n in chosen], hubkit), True) > 0
    backup = None
    if not args.dry_run and not args.no_backup and (first_install or wire_pending):
        backup = backup_claude(claude, home)

    for name in chosen:
        d = pats[name]
        src = os.path.join(ROOT, "patterns", name)
        for f in ["README.md", "pattern.json"] + d["files"]:
            p = os.path.join(src, f)
            if not os.path.isfile(p):
                raise Fail("%s lists a missing file: %s" % (name, f))
            mode = 0o755 if os.access(p, os.X_OK) else 0o644
            r.put(os.path.join(hubkit, name, f), read(p), mode, "%s/%s" % (name, f))
        for f in sorted(d.get("agents", [])):
            r.put(os.path.join(claude, "agents", args.prefix + f), prefixed_agent(read(os.path.join(src, f)), args.prefix),
                  0o644, "~/.claude/agents/%s%s" % (args.prefix, f))
        for f in sorted(d.get("commands", [])):
            r.put(os.path.join(claude, "commands", args.prefix + f), read(os.path.join(src, f)), 0o644,
                  "~/.claude/commands/%s%s" % (args.prefix, f))
        if name == "memory":       # the three starter files of the memory store (never overwritten)
            mdir = os.environ.get("MEMORY_DIR") or os.path.join(claude, "projects", re.sub(r"[^A-Za-z0-9]", "-", home), "memory")
            for t_src, t_dst in (("MEMORY.template.md", "MEMORY.md"), ("ARCHIVE.template.md", "ARCHIVE.md"),
                                 ("conversations_index.template.md", "conversations_index.md")):
                r.put(os.path.join(mdir, t_dst), read(os.path.join(src, t_src)), 0o644, "~/.claude/memory store: " + t_dst)
            if not r.dry:
                os.makedirs(os.path.join(mdir, "conversations"), exist_ok=True)
    # kit: hub command (only if the name is free), templates, hook notes
    kit = os.path.join(ROOT, "kit")
    if not args.only or args.with_hub_command:       # a single-piece install never writes into ~/.claude/commands
        r.put(os.path.join(claude, "commands", args.prefix + "hub.md"), read(os.path.join(kit, "commands", "hub.md")),
              0o644, "~/.claude/commands/%shub.md" % args.prefix)
    for t in sorted(os.listdir(os.path.join(kit, "templates"))):
        r.put(os.path.join(hubkit, "templates", t), read(os.path.join(kit, "templates", t)), 0o644, "templates/" + t)
    r.put(os.path.join(hubkit, "HOOKS-VERIFIED.md"), read(os.path.join(kit, "HOOKS-VERIFIED.md")), 0o644, "HOOKS-VERIFIED.md")

    snip_path = os.path.join(hubkit, "settings.snippet.json")
    if r.dry:
        hooks = hook_entries([pats[n] for n in chosen], hubkit)
    else:
        hooks = hook_entries(installed_descs(hubkit), hubkit)
        data = (json.dumps({
            "_comment": "Merge the hooks you want into ~/.claude/settings.json yourself (%s writes nothing there). "
                        "Keep your existing hooks; add these entries to the same event arrays. Remove the _comment "
                        "and _generated_by keys." % MARK,
            "_generated_by": MARK, "hooks": hooks}, indent=2) + "\n").encode()
        if not os.path.lexists(snip_path):
            r.put(snip_path, data, 0o644, "settings.snippet.json")
        else:
            try:
                old = read(snip_path)
            except OSError:
                old = b""
            ours = os.path.isfile(snip_path) and not os.path.islink(snip_path) and MARK.encode() in old
            if old == data:
                r.same.append("settings.snippet.json")
            elif ours:
                tmp = snip_path + ".tmp-%d" % os.getpid()      # our own generated file, refreshed to match what is installed
                with open(tmp, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, snip_path)
                r.new.append("settings.snippet.json (refreshed)")
            else:
                r.kept.append("settings.snippet.json")

    wired = None
    if args.wire_settings:
        wired = merge_hooks(settings_path, hook_entries(installed_descs(hubkit), hubkit) if not r.dry
                            else hook_entries([pats[n] for n in chosen], hubkit), r.dry)

    if not r.dry:
        record_manifest(hubkit, r.written + ([(snip_path, hashlib.sha256(read(snip_path)).hexdigest())]
                                            if any(x.startswith("settings.snippet.json") for x in r.new) else []))

    # one local line, only when something was written (a no-op run logs nothing); nothing is sent anywhere
    if r.new and not r.dry:
        os.makedirs(os.path.join(hubkit, "state"), exist_ok=True)
        with open(os.path.join(hubkit, "state", "events.log"), "a", encoding="utf-8") as fh:
            fh.write("%s install %d new\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), len(r.new)))

    p = print
    outside = [x for x in r.new if x.startswith("~/")]
    p("hub-starter install%s -> %s" % (" (DRY RUN, nothing written)" if r.dry else "", hubkit))
    p("patterns: %s" % ", ".join(chosen))
    for a in added:
        p("  also added: %s" % a)
    p("%s: %d   already there and identical: %d   kept yours (differs, not touched): %d"
      % ("would add" if r.dry else "added", len(r.new), len(r.same), len(r.kept)))
    for k in r.kept:
        p("  kept: %s" % k)
    for o in outside:
        p("  outside hubkit: %s" % o)
    if not r.new and not r.kept:
        p("nothing changed")
    p("")
    if backup:
        p("Backup of your existing ~/.claude: %s" % backup)
        p("  (chat transcripts and caches are not copied; to restore a file, copy it back from there)")
    if wired is not None:
        p("settings.json: %s %d hook entr%s (yours were kept)" % ("would add" if r.dry else "added", wired, "y" if wired == 1 else "ies"))
        p("")
    else:
        p("Your settings.json was NOT touched. To turn the hooks on, run ./install.sh --wire-settings, or merge these entries yourself:")
        p("")
        p(json.dumps({"hooks": hooks}, indent=2))
        p("")
    p("Saved copy: %s" % snip_path)
    p("Remove everything this installer wrote: ./install.sh --uninstall   (preview with --dry-run)")
    p("To preview a merge without touching your file: jq -s '.[0] * .[1]' ~/.claude/settings.json %s" % snip_path)
    p("  (that replaces arrays; if you already have hooks, add these entries to your arrays by hand)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Fail as e:
        print("install: %s" % e, file=sys.stderr)
        sys.exit(2)
    except (OSError, ValueError) as e:
        print("install: failed: %s" % e, file=sys.stderr)
        sys.exit(2)
