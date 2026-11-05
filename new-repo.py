#!/usr/bin/env python3
"""Start a new code repository for the hive: create it on GitHub, give the publisher its push key, and register
it as a staged repository, so agents can work in it at once and their results reach GitHub only after review.

    new-repo.py --registry REGISTRY.json --name NAME --github-token FILE [--owner OWNER] [--description TEXT]
                [--collaborator LOGIN]... [--keys DIR] [--copies DIR] [--clones DIR] [--registry-repo BARE.git]
                [--dry-run]
    new-repo.py --registry REGISTRY.json --registry-repo BARE.git --publish-only
    new-repo.py --merge-registry FILE < OTHER_REGISTRY.json      (add OTHER's repositories that FILE lacks)

Why a tool, not an agent: creating a repository needs a GitHub credential that could also push to any default
branch, delete repositories or skip review. Agents never hold it. The chief of staff asks for a repository (a
request to the operators, naming who wanted it and why); an operator runs this, where the publisher's token
already is. Recording the new registry stays the operator's act: the record's SPEC lets only an operator record
`hive.registry_changed`, and until one does the gateway refuses every pin. This prints the digest to record.

Each step is skipped when already done, so a rerun after a failure finishes the job:
1. the repository on GitHub (OWNER defaults to the token's account; an organization's repository if OWNER is an
   organization), public, with a README on main;
2. a write deploy key for the publisher, DIR/<id> (ed25519, made here, never copied), added to the repository;
   then each --collaborator LOGIN invited with write access (people who merge the hive's pull requests; GitHub
   mails them an invitation to accept; already a collaborator or already invited is left alone);
3. its registry entry, staged: the hive copy COPIES/<id>.git first, GitHub second, local_path CLONES/<id>
   (the id is the name in lower case: the record's pins allow no capitals). The file is replaced atomically;
4. with --registry-repo, the registry committed to that bare repository's main, so the hive's other hosts can
   fetch it over the hive's git route (registry-pull.sh) instead of copying it by hand.

Then the host must make the hive copy and the bare clone (publisher.py --init-only does the copy) and restart the
gateway, which reads the registry at start. Sidecars and reviewers on hive-runtime >= v0.5.28 reread it when
the file changes. The deployment's own wrapper does all of that (proto-hive: deploy/new-repo).

The last line printed is `registry_digest=sha256:...` (needs hiverecord importable, as in the hive's venv).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")          # hivepin repository ids
NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")              # GitHub repository names
LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")       # GitHub logins


class Fail(Exception):
    pass


def github(token: str, method: str, path: str, data: dict | None = None) -> tuple[int, object]:
    req = urllib.request.Request(API + path, method=method, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                          "User-Agent": "hive-new-repo", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            return r.status, json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"message": body.decode(errors="replace")[:300]}


def dump_registry(reg: dict) -> str:
    """The registry's own layout: one key per line, short lists inline (as registry-from-github.py writes it)."""
    out = json.dumps(reg, indent=1)
    for k in ("fetch_urls", "allowed_ref_patterns"):
        out = re.sub(rf'"{k}": \[\s*([^\]]*?)\s*\]',
                     lambda m: f'"{k}": [' + ", ".join(x.strip() for x in m.group(1).split(",")) + "]", out)
    return out + "\n"


def write_atomic(path: Path, text: str) -> None:
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def git(repo: Path | None, *args: str, input: str | None = None) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "operator", "GIT_AUTHOR_EMAIL": "operator@hive.invalid",
           "GIT_COMMITTER_NAME": "operator", "GIT_COMMITTER_EMAIL": "operator@hive.invalid"}
    r = subprocess.run(["git", *(["-C", str(repo)] if repo else []), *args], input=input, capture_output=True,
                       text=True, env=env)
    if r.returncode:
        raise Fail(f"git {' '.join(args[:2])}: {(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout.strip()


def publish_registry(registry: Path, bare: Path, why: str) -> str | None:
    """Commit the registry file as registry.json on the bare repository's main (made if missing). Returns the new
    commit, or None when main already holds this registry. Plumbing only: no work tree, no push."""
    if not (bare / "HEAD").is_file():
        bare.parent.mkdir(parents=True, exist_ok=True)
        git(None, "init", "-q", "--bare", "--shared=group", "-b", "main", str(bare))
        git(bare, "config", "receive.denyDeletes", "true")
        git(bare, "config", "receive.denyNonFastForwards", "true")
        hook = Path(__file__).resolve().parent / "git" / "pre-receive"   # agents may only add hive/* branches
        if hook.is_file():
            (bare / "hooks" / "pre-receive").write_bytes(hook.read_bytes())
            (bare / "hooks" / "pre-receive").chmod(0o755)
    blob = git(bare, "hash-object", "-w", "--stdin", input=registry.read_text())
    tree = git(bare, "mktree", input=f"100644 blob {blob}\tregistry.json\n")
    parent = subprocess.run(["git", "-C", str(bare), "rev-parse", "-q", "--verify", "refs/heads/main"],
                            capture_output=True, text=True).stdout.strip()
    if parent and git(bare, "rev-parse", f"{parent}^{{tree}}") == tree:
        return None
    commit = git(bare, "commit-tree", tree, *(["-p", parent] if parent else []), "-m", why)
    git(bare, "update-ref", "refs/heads/main", commit, *([parent] if parent else []))
    return commit


def digest(registry: Path) -> str | None:
    try:
        from hiverecord.pins import registry_digest
    except ImportError:
        return None
    return registry_digest(str(registry))


def merge_registry(into: Path, other: dict) -> list[str]:
    """Add to the registry file `into` every repository `other` has and it lacks (a deployment's source copy taking
    what new-repo.py registered on the host). Existing entries are never changed. Returns the ids added."""
    reg = json.loads(into.read_text())
    added = sorted(set(other["repositories"]) - set(reg["repositories"]))
    for rid in added:
        reg["repositories"][rid] = other["repositories"][rid]
    if added:
        write_atomic(into, dump_registry(reg))
    return added


def invite_collaborators(token: str, full: str, me: str, logins: list[str], dry_run: bool) -> None:
    for login in dict.fromkeys(l.strip().lstrip("@") for l in logins if l.strip()):
        if not LOGIN.match(login):
            raise Fail(f"--collaborator {login!r} isn't a GitHub login")
        if login.lower() in (me.lower(), full.split("/")[0].lower()):
            continue                                          # the owner already has every right
        st, _ = github(token, "GET", f"/repos/{full}/collaborators/{login}")
        if st == 204:
            print(f"collaborator: {login} already on {full}")
            continue
        st, inv = github(token, "GET", f"/repos/{full}/invitations?per_page=100")
        if st == 200 and any((i.get("invitee") or {}).get("login", "").lower() == login.lower() for i in inv):
            print(f"collaborator: {login} already invited to {full}")
            continue
        if dry_run:
            print(f"would invite {login} to {full} (write)")
            continue
        st, r = github(token, "PUT", f"/repos/{full}/collaborators/{login}", {"permission": "push"})
        if st not in (201, 204):
            raise Fail(f"GitHub refused to add {login} to {full} ({st}: {(r or {}).get('message')})")
        print(f"collaborator: {login} {'invited to' if st == 201 else 'added to'} {full} (write)")


def main() -> int:
    if sys.argv[1:2] == ["--merge-registry"]:   # new-repo.py --merge-registry FILE < OTHER.json
        if len(sys.argv) != 3:
            sys.exit("usage: new-repo.py --merge-registry FILE < OTHER_REGISTRY.json")
        added = merge_registry(Path(sys.argv[2]), json.load(sys.stdin))
        if added:
            print(f"registry: took {', '.join(added)}")
        return 0
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--registry", required=True, type=Path)
    ap.add_argument("--name")
    ap.add_argument("--owner", help="GitHub account or organization (default: the token's account)")
    ap.add_argument("--description", default="")
    ap.add_argument("--github-token", type=Path, help="file holding a token that may create repositories and deploy keys")
    ap.add_argument("--collaborator", action="append", default=[], metavar="LOGIN",
                    help="GitHub login invited with write access (repeatable)")
    ap.add_argument("--keys", type=Path, default=Path("/var/lib/hive/publisher/keys"))
    ap.add_argument("--copies", type=Path, default=Path("/var/lib/hive/repos"))
    ap.add_argument("--clones", type=Path, default=Path("/var/lib/hive/clones"))
    ap.add_argument("--registry-repo", type=Path, help="bare repository the registry is committed to, for other hosts")
    ap.add_argument("--publish-only", action="store_true", help="only commit the registry to --registry-repo")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    try:
        return run(a)
    except Fail as e:
        print(f"new-repo: {e}", file=sys.stderr)
        return 1


def run(a) -> int:
    if a.publish_only:
        if not a.registry_repo:
            raise Fail("--publish-only needs --registry-repo")
        c = None if a.dry_run else publish_registry(a.registry, a.registry_repo, "registry")
        print(f"registry in {a.registry_repo}: {c or 'unchanged'}")
        return 0
    if not a.name or not NAME.match(a.name):
        raise Fail("--name: letters, digits, '.', '_' and '-' only")
    rid = a.name.lower()
    if not ID.match(rid):
        raise Fail(f"{rid!r} isn't a valid repository id (a letter or digit first, at most 64 characters)")
    if not a.github_token or not a.github_token.is_file():
        raise Fail("--github-token FILE is required")
    token = a.github_token.read_text().strip()
    bad = [l for l in a.collaborator if not LOGIN.match(l.strip().lstrip("@"))]
    if bad:
        raise Fail(f"--collaborator {bad[0]!r} isn't a GitHub login")

    st, me = github(token, "GET", "/user")
    if st != 200:
        raise Fail(f"GitHub didn't accept the token ({st}: {(me or {}).get('message')})")
    owner = a.owner or me["login"]
    full = f"{owner}/{a.name}"
    reg = json.loads(a.registry.read_text())
    if rid in reg["repositories"] and not any(u.rstrip("/").removesuffix(".git").lower().endswith("/" + full.lower())
                                               for u in reg["repositories"][rid].get("fetch_urls", [])):
        raise Fail(f"the registry already has a repository {rid!r} somewhere else: pick another name")

    # 1. the repository
    st, repo = github(token, "GET", f"/repos/{full}")
    if st == 200:
        print(f"exists: https://github.com/{repo['full_name']}")
    elif st == 404:
        if a.dry_run:
            print(f"would create: https://github.com/{full} (public, README)")
            repo = {"full_name": full, "clone_url": f"https://github.com/{full}.git"}
        else:
            path = "/user/repos" if owner.lower() == me["login"].lower() else f"/orgs/{owner}/repos"
            st, repo = github(token, "POST", path, {"name": a.name, "description": a.description[:350],
                                                     "private": False, "auto_init": True})
            if st != 201:
                raise Fail(f"GitHub refused to create {full} ({st}: {(repo or {}).get('message')}; "
                           f"{(repo or {}).get('errors') or ''})")
            print(f"created: {repo['html_url']}")
    else:
        raise Fail(f"can't look up {full} ({st}: {(repo or {}).get('message')})")
    if repo.get("private"):
        raise Fail(f"{full} is private: the hive's clones fetch over https without a login, so it must be public")
    full = repo["full_name"]

    # 2. the publisher's deploy key
    key = a.keys / rid
    if a.dry_run and not key.is_file():
        print(f"would make the publisher key {key} and add it to {full} (write)")
    else:
        if not key.is_file():
            a.keys.mkdir(parents=True, exist_ok=True, mode=0o700)
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"hive publisher: {full}", "-f", str(key)],
                           check=True)
            key.chmod(0o600)
        pub = " ".join(Path(f"{key}.pub").read_text().split()[:2])
        st, keys = github(token, "GET", f"/repos/{full}/keys")
        if st != 200:
            raise Fail(f"can't list {full}'s deploy keys ({st}: {(keys or {}).get('message')})")
        if any(" ".join(k.get("key", "").split()[:2]) == pub for k in keys):
            print(f"deploy key: already on {full}")
        elif a.dry_run:
            print(f"would add the publisher key to {full} (write)")
        else:
            st, r = github(token, "POST", f"/repos/{full}/keys", {"title": "hive publisher", "key": pub, "read_only": False})
            if st != 201:
                raise Fail(f"GitHub refused the deploy key for {full} ({st}: {(r or {}).get('message')})")
            print(f"deploy key: added to {full} (write)")
    invite_collaborators(token, full, me["login"], a.collaborator, a.dry_run)

    # 3. the registry entry
    if rid in reg["repositories"]:
        print(f"registry: {rid} already there")
    else:
        reg["repositories"][rid] = {
            "fetch_urls": [f"file://{a.copies / (rid + '.git')}", repo.get("clone_url") or f"https://github.com/{full}.git"],
            "object_format": "sha1",
            "allowed_ref_patterns": ["refs/heads/*"],
            "local_path": str(a.clones / rid),
        }
        if a.dry_run:
            print(f"would register {rid} (staged: {a.copies / (rid + '.git')} first, GitHub second)")
        else:
            write_atomic(a.registry, dump_registry(reg))
            print(f"registry: added {rid}")

    # 4. the registry for the other hosts
    if a.registry_repo and not a.dry_run:
        c = publish_registry(a.registry, a.registry_repo, f"registry: add {rid} (https://github.com/{full})")
        print(f"registry in {a.registry_repo}: {c or 'unchanged'}")
    if not a.dry_run:
        d = digest(a.registry)
        print(f"registry_digest={d}" if d else "registry_digest=? (hiverecord not importable: compute it in the hive's venv)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
