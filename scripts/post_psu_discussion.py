#!/usr/bin/env python3
"""Post the latest PSU evaluation as a comment on a GitHub Discussion.

Reads the most recent row of the precomputed PSU CSV (one row per trading day,
written by fetch_prices.py) and posts a markdown summary — mirroring the
"PSU Expected Grant" section of the dashboard (docs/index.html, renderPsu /
renderCompare in app.js) — as a new comment on GitHub Discussion #1.

Authentication uses a personal access token exposed via the DISCUSSIONS_TOKEN
environment variable (a repo secret, or a short-lived installation token
minted by actions/create-github-app-token in the workflow). The default
GITHUB_TOKEN cannot reliably write to discussions, so a PAT or a GitHub App
installation token is required. The token needs one of:
  - classic PAT: `repo` scope (covers discussions read + write), or
  - fine-grained PAT: "Discussions" account permission (read + write) on this
    repository, or
  - GitHub App installation access token: the app must have the "Discussions"
    permission set to Read & write and be installed on this repository. The
    workflow mints this token via actions/create-github-app-token, so the
    comment is authored by the app bot (<app-slug>[bot]) — a separate identity
    from the repo owner, which is what triggers a notification (GitHub never
    notifies a user about their own comments).

When DISCUSSIONS_TOKEN is unset the script is a no-op (exit 0, prints a
notice) so the price-update pipeline keeps working before the secret is
configured. Any GraphQL error (bad token, discussions disabled, #1 missing)
exits non-zero so the failure is visible in the Actions log.

Before posting, if the discussion is locked the script unlocks it (so the
comment isn't rejected) and re-locks it afterward with the original lock
reason; a locked announcement thread therefore stays locked around each
automated update. Re-locking runs as best-effort cleanup even when posting
fails, so a bot error never leaves the thread accidentally unlocked.

Environment (auto-provided by GitHub Actions unless noted):
  DISCUSSIONS_TOKEN   PAT secret (REQUIRED to actually post; see above).
  GITHUB_REPOSITORY   "owner/repo" of this repository.
  GITHUB_RUN_ID       Actions run id (footer link target).
  GITHUB_RUN_NUMBER   Actions run number (footer label).
  GITHUB_SERVER_URL   Base URL, e.g. https://github.com.
  DISCUSSION_NUMBER   Discussion to comment on (default 1).

Usage:
    python3 scripts/post_psu_discussion.py [--psu data/005930_kospi_psu.csv]
    python3 scripts/post_psu_discussion.py --dry-run   # print markdown only
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.request
from typing import NoReturn

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_PSU = os.path.join(ROOT, "data", "005930_kospi_psu.csv")

GITHUB_API = "https://api.github.com/graphql"


def die(msg: str, code: int = 1) -> NoReturn:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def fnum(v) -> float | None:
    if v == "" or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fmt_krw(v: float | None) -> str:
    # Mirrors the dashboard's Intl.NumberFormat(maximumFractionDigits: 2),
    # which keeps up to 2 decimals but strips trailing zeros.
    if v is None:
        return "—"
    s = "{:,.2f}".format(v)
    if s.endswith(".00"):
        s = s[:-3]
    elif s.endswith("0"):
        s = s[:-1]
    return "₩" + s


def fmt_int(v) -> str:
    n = fnum(v)
    return "—" if n is None else "{:,.0f}".format(n)


def fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return ("+" if v >= 0 else "") + "{:.2f}".format(v) + "%"


def fmt_mult(v: float | None) -> str:
    return "—" if v is None else "×{:.1f}".format(v)


def load_latest_psu(path: str) -> dict | None:
    """Return the last (chronologically latest) row of the PSU CSV, or None."""
    if not os.path.exists(path):
        return None
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows[-1] if rows else None


def build_markdown(row: dict) -> str:
    """Render the PSU section as markdown, mirroring docs/index.html."""
    as_of = row.get("as_of", "?")
    close = fnum(row.get("close"))
    pinned_dt = row.get("pinned_date", "?")
    pinned_v = fnum(row.get("pinned_vwap_mean"))
    vmean = fnum(row.get("vwap_mean"))
    diff = fnum(row.get("diff_ratio"))
    mult = fnum(row.get("multiplier"))

    run_id = os.environ.get("GITHUB_RUN_ID", "")
    run_no = os.environ.get("GITHUB_RUN_NUMBER", "")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_link = f"{server}/{repo}/actions/runs/{run_id}" if (run_id and repo) else ""
    if run_link and run_no:
        footer = f"_Automated update from GitHub Actions ([run #{run_no}]({run_link}))._"
    elif run_link:
        footer = f"_Automated update from GitHub Actions ([run]({run_link}))._"
    else:
        footer = "_Automated update from GitHub Actions._"

    return "\n".join([
        f"## PSU Expected Grant — {as_of}",
        "",
        f"**Diff {fmt_pct(diff)} · Multiplier {fmt_mult(mult)}**",
        "",
        "| Reference | VWAP mean |",
        "|---|---|",
        f"| Pinned · {pinned_dt} | {fmt_krw(pinned_v)} |",
        f"| Latest · {as_of} | {fmt_krw(vmean)} |",
        f"| Difference | {fmt_pct(diff)} |",
        "",
        "| Class | Base grant | Expected stocks | Expected evaluation |",
        "|---|---|---|---|",
        f"| CL1 / CL2 | {fmt_int(row.get('cl12_base'))} | {fmt_int(row.get('cl12_stocks'))} | {fmt_krw(fnum(row.get('cl12_eval')))} |",
        f"| CL3 / CL4 | {fmt_int(row.get('cl34_base'))} | {fmt_int(row.get('cl34_stocks'))} | {fmt_krw(fnum(row.get('cl34_eval')))} |",
        "",
        f"Evaluation = expected stocks × latest closing price ({fmt_krw(close)}). "
        "Tiers: ×0.5 @≥+20%, ×1.0 @≥+40%, ×1.3 @≥+60%, ×1.7 @≥+80%, ×2.0 @≥+100%.",
        "",
        footer,
        "",
    ])


def _graphql_request(token: str, query: str, variables: dict) -> dict:
    """Send a GitHub GraphQL request and return the parsed JSON response.

    Raises urllib.error.HTTPError on a non-2xx response; callers decide
    whether that is fatal.
    """
    data = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        GITHUB_API,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
            "User-Agent": "samsung-price/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def gql(token: str, query: str, variables: dict) -> dict:
    """Run a GitHub GraphQL request; die with the body on HTTP error."""
    try:
        return _graphql_request(token, query, variables)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        die(f"GitHub API HTTP {e.code}: {body}")


def gql_best_effort(token: str, query: str, variables: dict) -> dict | None:
    """Like gql, but swallow errors and return None instead of dying.

    Used for non-critical cleanup (e.g. re-locking a discussion after the
    comment has already been posted) so a failure there doesn't mask the
    success of the primary operation.
    """
    try:
        return _graphql_request(token, query, variables)
    except Exception as e:
        print(f"warning: best-effort GraphQL request failed: {e}", file=sys.stderr)
        return None


DISCUSSION_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    discussion(number: $number) { id number title locked activeLockReason }
  }
}
"""

ADD_COMMENT_MUTATION = """
mutation($discussionId: ID!, $body: String!) {
  addDiscussionComment(input: {discussionId: $discussionId, body: $body}) {
    comment { id url }
  }
}
"""

UNLOCK_MUTATION = """
mutation($id: ID!) {
  unlockLockable(input: {lockableId: $id}) {
    lockedRecord {
      ... on Discussion { id locked activeLockReason }
    }
  }
}
"""

LOCK_MUTATION = """
mutation($id: ID!, $reason: LockReason) {
  lockLockable(input: {lockableId: $id, lockReason: $reason}) {
    lockedRecord {
      ... on Discussion { id locked activeLockReason }
    }
  }
}
"""


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--psu", default=DEFAULT_PSU, help=f"PSU CSV path (default: {DEFAULT_PSU})")
    ap.add_argument("--dry-run", action="store_true", help="print the markdown without posting")
    args = ap.parse_args()

    row = load_latest_psu(args.psu)
    if not row:
        print(f"No PSU rows in {args.psu}; nothing to post.")
        return

    if args.dry_run:
        sys.stdout.write(build_markdown(row))
        return

    token = os.environ.get("DISCUSSIONS_TOKEN", "").strip()
    if not token:
        print("DISCUSSIONS_TOKEN not set; skipping discussion post.")
        return

    owner, _, name = os.environ.get("GITHUB_REPOSITORY", "").partition("/")
    if not owner or not name:
        die("GITHUB_REPOSITORY not set (expected 'owner/repo'); run inside GitHub Actions.")

    number = int(os.environ.get("DISCUSSION_NUMBER", "1"))

    resp = gql(token, DISCUSSION_QUERY, {"owner": owner, "name": name, "number": number})
    if resp.get("errors"):
        die(f"GraphQL error looking up discussion #{number}: {json.dumps(resp['errors'])}")
    repo_node = (resp.get("data") or {}).get("repository")
    if not repo_node:
        die(f"repository {owner}/{name} not visible to DISCUSSIONS_TOKEN (needs discussions read).")
    disc = repo_node.get("discussion")
    if not disc:
        die(f"discussion #{number} not found in {owner}/{name} (is Discussions enabled?).")
    disc_id = disc["id"]
    was_locked = bool(disc.get("locked"))
    lock_reason = disc.get("activeLockReason")  # None when not locked

    # A locked discussion rejects new comments, so unlock it first (only when
    # currently locked — unlockLockable errors on an already-unlocked
    # resource). The original lock reason is remembered so we can restore it.
    if was_locked:
        resp = gql(token, UNLOCK_MUTATION, {"id": disc_id})
        if resp.get("errors"):
            die(f"GraphQL error unlocking discussion #{number}: {json.dumps(resp['errors'])}")
        print(f"Unlocked discussion #{number} (was locked: {lock_reason}) to post comment.")

    body = build_markdown(row)
    try:
        resp = gql(token, ADD_COMMENT_MUTATION, {"discussionId": disc_id, "body": body})
        if resp.get("errors"):
            die(f"GraphQL error posting comment: {json.dumps(resp['errors'])}")
        url = resp["data"]["addDiscussionComment"]["comment"]["url"]
    finally:
        # Restore the original lock state even if posting failed, so a bot
        # error never leaves the discussion accidentally unlocked. Re-locking is
        # best-effort: a failure here shouldn't mask the post result above.
        if was_locked:
            reason = lock_reason or "OFF_TOPIC"
            rresp = gql_best_effort(token, LOCK_MUTATION, {"id": disc_id, "reason": reason})
            if rresp is None or rresp.get("errors"):
                if rresp and rresp.get("errors"):
                    detail = json.dumps(rresp["errors"])
                else:
                    detail = "request failed"
                print(f"warning: failed to re-lock discussion #{number} ({detail}); "
                      f"it may be left unlocked.", file=sys.stderr)
            else:
                print(f"Re-locked discussion #{number} (reason: {reason}).")

    print(f"Posted PSU evaluation for {row.get('as_of')} to discussion #{number}: {url}")


if __name__ == "__main__":
    main()
