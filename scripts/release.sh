#!/usr/bin/env bash
# Release-safety: tags v<version> only when every check passes. Never commits, pushes branches or deletes tags.
set -euo pipefail
usage() { echo "usage: $0 <version> [--remote origin] [--branch main] [--dry-run] [--skip-tests]" >&2; exit 2; }
fail() { echo "release: refused - $1" >&2; exit 1; }

remote=origin branch=main dry=0 tests=1 ver=""
while [ $# -gt 0 ]; do
  case "$1" in
    --remote) [ $# -ge 2 ] || usage; remote=$2; shift ;;
    --branch) [ $# -ge 2 ] || usage; branch=$2; shift ;;
    --dry-run) dry=1 ;;
    --skip-tests) tests=0 ;;
    -*) usage ;;
    *) [ -z "$ver" ] || usage; ver=$1 ;;
  esac
  shift
done
[ -n "$ver" ] || usage
echo "$ver" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+(-beta\.[0-9]+)?$' || fail "version '$ver' must be X.Y.Z or X.Y.Z-beta.N (no leading v)"
tag=v$ver base=${ver%%-*} beta=0
[ "$base" = "$ver" ] || beta=1

# 1. clean work tree
top=$(git rev-parse --show-toplevel 2>/dev/null) || fail "not inside a git work tree; cd into the repo"
cd "$top"
if git status --porcelain | grep -Ev '^\?\? \.venv/?$' | grep -q .; then
  fail "work tree is dirty; commit or remove the changes listed by 'git status'"
fi
# 2. the only network call
git fetch -q "$remote" || fail "git fetch $remote failed; check the remote name and network"
# 3. HEAD must be the pushed commit
if [ $beta = 0 ]; then
  [ "$(git rev-parse HEAD)" = "$(git rev-parse -q --verify "refs/remotes/$remote/$branch" || echo none)" ] \
    || fail "HEAD is not $remote/$branch; push the release commit to $branch and check it out"
else
  [ -n "$(git branch -r --contains HEAD)" ] || fail "HEAD is not on any remote branch; push it first"
fi
# 4. manifest version
mv=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["version"])' custom_components/foxess_charger/manifest.json) \
  || fail "cannot read custom_components/foxess_charger/manifest.json"
[ "$mv" = "$ver" ] || fail "manifest.json version is '$mv', expected '$ver'; fix it and push"
# 5. changelog heading
re() { printf '%s' "$1" | sed 's/[.]/\\./g'; }
grep -Eq "^## ($(re "$ver")|$(re "$base"))( |\$)" CHANGELOG.md \
  || fail "CHANGELOG.md has no '## $ver' heading (beta may use '## $base'); add it and push"
# 6. tag must not exist
if git rev-parse -q --verify "refs/tags/$tag" >/dev/null || [ -n "$(git ls-remote --tags "$remote" "refs/tags/$tag")" ]; then
  fail "tag $tag already exists (local or $remote); pick a new version, never reuse tags"
fi
# 7. tests
if [ $tests = 1 ]; then
  py=python3; [ ! -x .venv/bin/python ] || py=.venv/bin/python
  "$py" -m pytest -q || fail "tests failed; fix them (or --skip-tests if you ran them already)"
fi

echo "commit: $(git log -1 --format='%h %s')"
echo "tag to create: $tag"
if [ $dry = 1 ]; then echo "dry run: nothing tagged"; exit 0; fi
git tag -a "$tag" -m "$ver"
git push "$remote" "$tag"
if [ $beta = 1 ]; then echo "Next: publish $tag on GitHub as a pre-release."
else echo "Next: publish the GitHub release for $tag with the CHANGELOG section."; fi
