#!/usr/bin/env bash
#
# scripts/pre-publish-check.sh
# ----------------------------
# Verifies the repo is safe to push to a public GitHub repository.
# Run this BEFORE your first `git push`, and any time you add a new
# file with potentially sensitive content.
#
# Exits 0 if safe, 1 if anything is suspicious. Each check is
# independent and prints its own status.

set -u

# Always run from the repo root, regardless of where the script is called from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

PASS=0
FAIL=0
WARN=0

ok()   { echo "  ✅ $*"; PASS=$((PASS+1)); }
fail() { echo "  ❌ $*"; FAIL=$((FAIL+1)); }
warn() { echo "  ⚠️  $*"; WARN=$((WARN+1)); }

echo
echo "Pre-publish check for $REPO_ROOT"
echo "================================="
echo

# ---------- 1. Required tooling ----------
echo "1. Tooling"
if ! command -v git >/dev/null 2>&1; then
    fail "git not found in PATH"
else
    ok "git available"
fi
if ! command -v grep >/dev/null 2>&1; then
    fail "grep not found in PATH"
else
    ok "grep available"
fi

# ---------- 2. Tracked files containing real secrets ----------
echo
echo "2. Tracked files containing real secrets"
# Look for any tracked file that has a non-placeholder value for our secrets.
# The placeholder strings are 'replace_me' and 'your_cas_password_here'.
SECRETS_PATTERN='ANGEL_(API_KEY|CLIENT_CODE|MPIN|TOTP_SECRET)=[A-Za-z0-9]+|cas_pdf_password[[:space:]]*=[[:space:]]*"[A-Za-z0-9]+'

LEAKS=$(git ls-files \
    | grep -vE '\.example$' \
    | xargs grep -lE "$SECRETS_PATTERN" 2>/dev/null \
    | grep -vE 'replace_me|your_cas_password_here' || true)

# Refine: exclude files where the only matches are placeholder lines.
SUSPECT=""
for f in $LEAKS; do
    HITS=$(grep -nE "$SECRETS_PATTERN" "$f" 2>/dev/null \
        | grep -vE 'replace_me|your_cas_password_here' || true)
    [ -n "$HITS" ] && SUSPECT="$SUSPECT $f"
done

if [ -n "$SUSPECT" ]; then
    fail "Found real-looking secrets in tracked files:"
    for f in $SUSPECT; do
        echo "       $f"
        grep -nE "$SECRETS_PATTERN" "$f" | grep -vE 'replace_me|your_cas_password_here' | sed 's/^/         /'
    done
else
    ok "No real secrets found in tracked files"
fi

# ---------- 3. .gitignore covers the sensitive files ----------
echo
echo "3. .gitignore protection"
for path in .env secrets.local.json mfs.json sgbs.json data/history.db; do
    if git check-ignore "$path" >/dev/null 2>&1; then
        ok "$path is gitignored"
    elif [ ! -e "$path" ]; then
        warn "$path does not exist (would be gitignored if present)"
    else
        fail "$path exists and is NOT gitignored!"
    fi
done

# ---------- 4. No secrets in git history ----------
echo
echo "4. Git history clean"
# Look for actual JWT signatures (eyJ prefix with two base64 segments)
# or cas_pdf_password with a value that's not 'replace_me' / 'your_cas_password_here'.
HISTORY_HITS=$(git log --all -p 2>/dev/null \
    | grep -E "eyJhbGciOiJIUzUxMiJ9\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+" \
    | head -5 || true)
if [ -n "$HISTORY_HITS" ]; then
    fail "Possible JWT tokens in git history! Review with 'git log -p | grep eyJ'"
    echo "$HISTORY_HITS" | sed 's/^/         /' | head -5
else
    ok "No JWT tokens found in git history"
fi

# Also check for non-placeholder cas_pdf_password values in history.
HISTORY_PW=$(git log --all -p 2>/dev/null \
    | grep -E '"cas_pdf_password"[[:space:]]*:[[:space:]]*"[^"]+"' \
    | grep -vE 'your_cas_password_here|your_pin|replace_me|""|<set |empty' \
    | head -5 || true)
if [ -n "$HISTORY_PW" ]; then
    fail "Possible CAS PDF password in git history!"
    echo "$HISTORY_PW" | sed 's/^/         /' | head -5
else
    ok "No real cas_pdf_password found in git history"
fi

# ---------- 5. Example files contain only placeholders ----------
echo
echo "5. Example files are placeholder-only"
for f in .env.example secrets.local.json.example mfs.json.example sgbs.json.example; do
    if [ ! -f "$f" ]; then
        warn "$f missing"
        continue
    fi
    # Look for any string that doesn't look like a placeholder
    # (e.g. a real fund name, a real client code, etc.)
    SUSPICIOUS=$(grep -nE "^(ANGEL_[A-Z_]+|.*cas_pdf_password)" "$f" 2>/dev/null \
        | grep -vE 'replace_me|your_cas_password_here|^#|^\s*$|"name"' \
        | head -3 || true)
    if [ -n "$SUSPICIOUS" ]; then
        warn "$f has non-placeholder values — review:"
        echo "$SUSPICIOUS" | sed 's/^/         /'
    else
        ok "$f contains only placeholders"
    fi
done

# ---------- 6. No large files in tracked paths ----------
echo
echo "6. No accidentally-committed large files"
LARGE=$(git ls-files | xargs -I{} ls -la "{}" 2>/dev/null \
    | awk '$5 > 1048576 {print $5, $9}' | head -5 || true)
if [ -n "$LARGE" ]; then
    warn "Files > 1 MB in tracked paths:"
    echo "$LARGE" | sed 's/^/         /'
else
    ok "No large tracked files"
fi

# ---------- Summary ----------
echo
echo "================================="
echo "Pre-publish check complete"
echo "  ✅ $PASS passed"
echo "  ⚠️  $WARN warnings"
echo "  ❌ $FAIL failed"
echo

if [ "$FAIL" -gt 0 ]; then
    echo "DO NOT PUSH until the failures above are resolved."
    exit 1
fi

if [ "$WARN" -gt 0 ]; then
    echo "Safe to push, but please review the warnings."
    exit 0
fi

echo "Safe to push to a public repo. 🎉"
exit 0