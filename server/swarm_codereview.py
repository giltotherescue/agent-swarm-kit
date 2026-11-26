"""Code review policy (docs/code-review.md): who reviews a PR, how many rounds, and where residuals go.

A small port of the impact-based review policy in agent-control (owner approved 2026-11-25). Standard library only.

  reviewer   the opposite model family from the one that wrote the code; the high-risk model for high-risk changes
  tier       high (auth, deletion, money, production data, the scripts and skills that can do those), ordinary, or none
             (typo, changelog, comment-only, docs-only). Path based, with an explicit override.
  cap        review rounds per PR: 2 ordinary (first review + one recheck), 3 high. A cap hit means ship with residuals
             recorded, or retire the PR with the reason written on it. It is not a Waiting-on-you card by default.
  recheck    verifies only the prior must-fix items, plus a new issue that itself meets the blocking bar
  residuals  non-blocking follow-ups go to one GitHub issue per PR, labeled review-followup, linked from the PR.
             The worker files it host-side (the coding session has no GitHub credentials).
"""
import fnmatch
import os
import re

TIERS = ("none", "ordinary", "high")
CAPS = {"none": 2, "ordinary": 2, "high": 3}
FOLLOWUP_LABEL = "review-followup"

# Path words that put a change in the high-risk tier. Matched against each path segment and file stem, case-insensitive.
HIGH_RISK_WORDS = (
    "auth", "oauth", "login", "logout", "session", "sessions", "password", "passwords", "permission", "permissions", "acl",
    "rbac", "role", "roles", "security", "secret", "secrets", "token", "tokens", "crypto", "csrf", "sso", "saml",
    "billing", "payment", "payments", "stripe", "invoice", "invoices", "checkout", "subscription", "subscriptions", "refund",
    "refunds", "entitlement", "entitlements", "pricing", "webhook", "webhooks", "charge", "charges", "charging", "payout",
    "payouts",
    "migration", "migrations", "migrate", "schema", "delete", "deletion", "purge", "retention", "gdpr", "erase",
    "backup", "backups", "restore", "prod", "production", "publish", "deploy",
)
HIGH_RISK_GLOBS = ("*.sql", "**/migrations/**", "**/db/migrate/**")
# Extra words that make a skill (instructions an agent runs) high risk: it can grant access as well as touch money.
SKILL_HIGH_RISK_WORDS = ("access", "grant", "grants", "admin", "sudo", "invite", "invites", "customer", "customers")
# Text files an agent or a build acts on: never docs-only, whatever their extension.
OPERATIONAL_GLOBS = ("requirements*.txt", "*requirements.txt", "constraints*.txt", "*.requirements.txt", "robots.txt",
                     "cmakelists.txt", "llms.txt", "skill.md", "agents.md", "claude.md", "gemini.md", "copilot-instructions.md")
OPERATIONAL_DIRS = ("skill", "skills", "prompt", "prompts", ".claude", ".cursor", ".github")
DOC_EXT = (".md", ".mdx", ".rst", ".txt", ".adoc")
DOC_NAMES = ("changelog", "license", "authors", "contributors", "readme", "notice")
COMMENT_LINE = re.compile(r"^\s*(#|//|/\*|\*|\*/|<!--|-->|--\s|;|\"\"\"|''')")
MARKER = re.compile(r"<!--\s*swarm-review:([^>]*)-->")


def _words(path):
    out = set()
    for seg in path.lower().split("/"):
        stem = seg.rsplit(".", 1)[0] if "." in seg[1:] else seg
        out.update(w for w in re.split(r"[^a-z0-9]+", stem) if w)
    return out


def extra_high_risk_globs(environ=None):
    """Per-install override: SWARM_REVIEW_HIGH_RISK_PATHS=glob,glob adds paths to the high tier."""
    raw = (environ if environ is not None else os.environ).get("SWARM_REVIEW_HIGH_RISK_PATHS", "")
    return tuple(g.strip() for g in raw.split(",") if g.strip())


def is_high_risk_path(path, extra_globs=()):
    p = path.lower()
    if any(fnmatch.fnmatch(p, g.lower()) for g in HIGH_RISK_GLOBS + tuple(extra_globs)):
        return True
    words = _words(p)
    if is_skill_path(p):  # a skill file (SKILL.md) is code an agent runs
        return bool(words & set(HIGH_RISK_WORDS + SKILL_HIGH_RISK_WORDS))
    if is_doc_path(p):
        return False  # prose about billing is not billing
    return bool(words & set(HIGH_RISK_WORDS))


def is_skill_path(path):
    segs = path.lower().split("/")
    return segs[-1] == "skill.md" or bool(set(segs[:-1]) & {"skill", "skills"})


def is_operational_path(path):
    """Text that something acts on: dependency lists, agent instructions, skills, prompts, CI config."""
    segs = path.lower().split("/")
    return (any(fnmatch.fnmatch(segs[-1], g) for g in OPERATIONAL_GLOBS) or bool(set(segs[:-1]) & set(OPERATIONAL_DIRS)))


def is_doc_path(path):
    """Prose only: documentation that nothing runs or installs. Operational text is checked first."""
    p = path.lower()
    if is_operational_path(p):
        return False
    base = p.rsplit("/", 1)[-1]
    return p.endswith(DOC_EXT) or base.split(".")[0] in DOC_NAMES


def comment_only(diff):
    """True when every added or removed line in a unified diff is blank or a comment."""
    changed = [ln[1:] for ln in (diff or "").splitlines()
               if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
    return bool(changed) and all(not ln.strip() or COMMENT_LINE.match(ln) for ln in changed)


def classify(paths, diff=None, override=None, extra_globs=()):
    """(tier, reason). override is high|ordinary|none|auto|None. A high-risk path always wins over none: a docs file
    named for billing or a skill that can publish still gets a full review."""
    paths = [p for p in (paths or []) if p]
    high = [p for p in paths if is_high_risk_path(p, extra_globs)]
    if override in ("high", "ordinary"):
        return override, "set by the caller"
    if override == "none":
        if high:
            return "high", f"high-risk path {high[0]} cannot skip review"
        return "none", "set by the caller"
    if not paths:
        return "ordinary", "no changed files seen"
    if high:
        return "high", f"high-risk path {high[0]}"
    if all(is_doc_path(p) for p in paths):
        return "none", "docs-only change"
    if diff is not None and comment_only(diff):
        return "none", "comment-only change"
    return "ordinary", "ordinary code change"


def higher(a, b):
    return a if TIERS.index(a) >= TIERS.index(b) else b


def family(provider):
    return "codex" if (provider or "").lower() == "codex" else "claude"


def reviewer_for(author_provider, tier, environ=None):
    """(provider, model) for the opposite family. Model names are settings so the kit does not pin a vendor's names."""
    env = environ if environ is not None else os.environ
    if family(author_provider) == "codex":
        return "claude", env.get("SWARM_REVIEW_CLAUDE_MODEL", "opus")
    ordinary = env.get("SWARM_REVIEW_CODEX_MODEL", "default")
    return "codex", env.get("SWARM_REVIEW_CODEX_HIGH_MODEL", ordinary) if tier == "high" else ordinary


def marker(round_no, tier="auto", model="policy", prior=None, author=None):
    """author: the family that wrote the code, so the worker can refuse a same-family reviewer. swarmctl puts the marker
    first in the prompt. Publication permission is not on the marker: it is the task's stored review_followups field."""
    parts = ([f"round={int(round_no)}", f"tier={tier}", f"model={model}"] + ([f"prior={prior}"] if prior else [])
             + ([f"author={family(author)}"] if author else []))
    return "<!-- swarm-review: " + " ".join(parts) + " -->"


def parse_marker(prompt):
    """Only a marker at the very start of the prompt counts: a recheck quotes earlier report text further down, and a
    marker quoted there must not set the round, tier or author."""
    m = MARKER.match((prompt or "").lstrip())
    if not m:
        return None
    out = dict(kv.split("=", 1) for kv in m.group(1).split() if "=" in kv)
    out.pop("followups", None)  # never authority: see the task's review_followups
    try:
        out["round"] = max(1, int(out.get("round", "1")))
    except ValueError:
        out["round"] = 1
    if out.get("tier") not in TIERS + ("auto",):
        out["tier"] = "auto"
    if out.get("author") not in ("claude", "codex"):
        out.pop("author", None)
    return out


def cap_message(round_no, tier):
    cap = CAPS[tier]
    return (f"Review cap reached: this would be round {round_no} and {tier}-risk PRs get {cap}. Do not queue another review. "
            "Ship only if a review with no must-fix findings covers the PR's current head and CI is green, with the residual "
            "non-blocking items recorded; otherwise retire the PR and close it with the reason written on it. File a "
            "Waiting-on-you item only if the choice truly needs the human.")


LIST_ITEM = re.compile(r"^\s{0,3}(?:[-*]|\d+[.)])\s+(.*)")
NONE_ITEM = re.compile(r"^(?:none|no findings|nothing found|n/a)\b", re.I)


def _section(title):
    title = title.lower()
    if "follow" in title or "non-blocking" in title or "residual" in title:
        return "follow"
    if "must" in title or "blocking" in title or "blocker" in title:
        return "must"
    return None


def has_findings_sections(report):
    """True when the report has the must-fix heading the review templates ask for. Without it (a legacy "## Findings"
    report, a failed run, no report) an empty must-fix list means "unknown", not "clean"."""
    return any(_section(h) == "must" for h in re.findall(r"^#+\s+(.*)", report or "", re.M))


def split_findings(report):
    """(must_fix, follow_ups): list items under a heading naming must-fix/blocking, and under one naming
    follow-up/non-blocking. Only top-level items; continuation lines are joined."""
    groups, cur, items = {"must": [], "follow": []}, None, None
    for ln in (report or "").splitlines():
        h = re.match(r"^#+\s+(.*)", ln)
        if h:
            cur = _section(h.group(1))
            items = None
            continue
        if cur is None:
            continue
        m = LIST_ITEM.match(ln)
        if m and not ln.startswith(("    ", "\t")):
            text = m.group(1).strip()
            if NONE_ITEM.match(text.strip("*_ ")):
                items = None
                continue
            groups[cur].append(text)
            items = groups[cur]
        elif items is not None and ln.strip() and ln.startswith((" ", "\t")):
            items[-1] += " " + ln.strip()
        elif not ln.strip():
            continue
        else:
            items = None
    return groups["must"], groups["follow"]


def followup_issue(pr_number, follow_ups, task_id=None):
    """(title, body) for the residuals issue. One per PR: the title starts with the PR number so it can be found again."""
    first = re.sub(r"[`*_]", "", follow_ups[0]) if follow_ups else "review follow-ups"
    first = re.sub(r"\s+", " ", first).strip()
    short = first if len(first) <= 70 else first[:67].rstrip() + "..."
    title = f"PR #{pr_number} review follow-ups: {short}"
    body = (f"Non-blocking residuals from the review of #{pr_number}"
            + (f" (review task {task_id})" if task_id else "") + ". They did not block the PR and do not start another fix "
            "round.\n\n" + "\n".join(f"- [ ] {f}" for f in follow_ups) + "\n")
    return title, body


def file_followups(repo, pr_number, follow_ups, run, task_id=None):
    """Find or create the PR's review-followup issue and link it from the PR. run(cmd) -> (code, stdout).
    Returns (url, note). Never raises on a gh failure: the residuals are still in the report."""
    if not (repo and pr_number and follow_ups):
        return None, None
    title, body = followup_issue(pr_number, follow_ups, task_id)
    prefix = f"PR #{pr_number} review follow-ups"
    code, out = run(["gh", "issue", "list", "-R", repo, "--state", "open", "--label", FOLLOWUP_LABEL, "--search",
                     f'"{prefix}" in:title', "--json", "number,title,url", "-q",
                     f'[.[] | select(.title | startswith("{prefix}"))][0].url // ""'])
    url = (out or "").strip() if code == 0 else ""
    if url:
        code, _ = run(["gh", "issue", "comment", url, "--body", "More residuals from a later review round:\n\n" +
                       "\n".join(f"- [ ] {f}" for f in follow_ups)])
        return (url, None) if code == 0 else (url, "could not add the new residuals to the existing issue")
    run(["gh", "label", "create", FOLLOWUP_LABEL, "-R", repo, "--color", "C5DEF5",
         "--description", "Non-blocking residuals from a code review"])  # fails harmlessly when it exists
    code, out = run(["gh", "issue", "create", "-R", repo, "--title", title, "--label", FOLLOWUP_LABEL, "--body", body])
    url = (out or "").strip().splitlines()[-1] if code == 0 and (out or "").strip() else ""
    if not url:
        return None, "could not open the review-followup issue; the residuals are only in this report"
    code, _ = run(["gh", "pr", "comment", str(pr_number), "-R", repo, "--body", f"Review follow-ups (non-blocking): {url}"])
    return url, None if code == 0 else "opened the issue but could not link it from the PR"


GREEN = ("SUCCESS", "NEUTRAL", "SKIPPED")
AFFIRMATIVE = re.compile(r"\b(ready to merge|merge after small fixes)\b", re.I)
NEGATIVE = re.compile(r"\bdo not merge\b|\bdon't merge\b|\bnot ready to merge\b|\bverdict:\s*fail\b", re.I)


def clean_review(report):
    """(ok, reason): does the report say, explicitly and without contradiction, that nothing blocks? Every must-fix
    section must hold exactly one "- None" item and nothing else, the report must say "Ready to merge" (or "Merge after
    small fixes"), and it must not say "Do not merge" anywhere. Anything else is unknown, not clean."""
    if not has_findings_sections(report):
        return False, "the report has no Must-fix findings section"
    cur, seen = None, []
    for ln in (report or "").splitlines():
        h = re.match(r"^#+\s+(.*)", ln)
        if h:
            cur = _section(h.group(1))
            if cur == "must":
                seen.append([])
            continue
        if cur == "must" and ln.strip():
            seen[-1].append(ln.strip())
    for body in seen:
        m = LIST_ITEM.match(body[0]) if len(body) == 1 else None
        if not (m and re.fullmatch(r"(?:none|no findings|nothing found|n/a)\.?", m.group(1).strip("*_ "), re.I)):
            return False, "its Must-fix findings section is not an explicit \"- None\" (it has other text or items)"
    if NEGATIVE.search(report):
        return False, "the report says it is not mergeable (\"Do not merge\" or a failing verdict)"
    if not AFFIRMATIVE.search(report):
        return False, "the report does not say \"Ready to merge\""
    return True, "the review found no must-fix items"


def ship_check(reviewed_head, pr):
    """(ok, reason): may a passing review be called shippable? pr is `gh pr view --json headRefOid,statusCheckRollup`
    output (a dict) or None when it could not be read. Ship advice needs the reviewed commit to be the PR's current head
    and every check to have passed, with at least one that actually succeeded (all skipped or neutral proves nothing);
    anything unknown is a no."""
    if not reviewed_head:
        return False, "the review recorded no reviewed commit"
    if not isinstance(pr, dict) or not pr.get("headRefOid"):
        return False, "could not read the PR's current head and CI from GitHub"
    if pr["headRefOid"] != reviewed_head:
        return False, f"the PR head is now {pr['headRefOid'][:12]}, not the reviewed {reviewed_head[:12]}"
    checks = pr.get("statusCheckRollup") or []
    if not checks:
        return False, "the PR has no CI results on its head"
    for c in checks:
        result = (c.get("conclusion") or c.get("state") or "").upper()
        if result not in GREEN:
            return False, f"CI is not green ({c.get('name') or c.get('context') or 'a check'}: {result.lower() or 'pending'})"
    if not any((c.get("conclusion") or c.get("state") or "").upper() == "SUCCESS" for c in checks):
        return False, "no CI check succeeded on the PR's head (every check was skipped or neutral)"
    return True, "reviewed head is the PR head and CI is green"
