"""The review queue (docs/review-queue.md): one place where bots file things that need the human (decisions, copy, creative)
and the human (admin token, or the Chief of Staff on their behalf) decides them. A simple port of agent-control PR 62.

  file     PUT/POST /review/items        upsert by (project, bot, key); a bot files only as itself
  read     GET /review/items[/ID]        a bot sees only its own items; admin and chief see all
  decide   POST /review/items/ID/decision  approve | changes | reject, with the content_hash the decider saw
  deliver  an urgent bus message to the filing bot's agent (thread review:ID); without an agent the decision stays
           answered and unacked in GET /review/items?status=answered&acked=0 until the bot acks it
  ack      POST /review/items/ID/ack     the bot consumed the decision

Stale-click guard: every read carries content_hash (SHA-256 over the ask, the options and the recommendation). A decision must
send it back; if it does not match the item as stored at commit time the decision is refused (409 stale) and nothing is recorded.
Standard library only.
"""
import hashlib
import json
import re
import secrets
import time

TYPES = ("decision", "copy", "creative", "social_post", "deliverable", "pr")
OPEN = ("open", "changes_requested")
ACTIONS = {"approve": "approved", "changes": "changes_requested", "request_changes": "changes_requested", "reject": "rejected"}
KEY_RE = re.compile(r"^[A-Za-z0-9._:+-]{1,200}$")
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
FILER_FIELDS = ("type", "title", "ask", "body", "recommendation", "options", "links")
LIMITS = {"title": 200, "ask": 4000, "body": 100000, "recommendation": 2000}
MAX_OPTIONS = 12
MAX_LINKS = 20
LIST_MAX = 500
STALE_MSG = "This item changed since you opened it. Read it again and decide on the current version."


def now():
    return int(time.time())


def content_hash(ask, options, recommendation):
    """SHA-256 over a canonical JSON of what a decision answers: the ask, each option (label, reason, meaning) and the
    recommendation. Same recipe as agent-control's card-v1 hash for filed options (the kit generates no default options)."""
    rec = str(recommendation or "").strip() or None
    opts = [[str(o.get("label", "")), str(o.get("reason") or ""), str(o.get("label", ""))] for o in (options or [])]
    canon = json.dumps(["card-v1", str(ask or "").strip(), opts, rec], separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode()).hexdigest()


def row_hash(r):
    return content_hash(r["ask"], json.loads(r["options"] or "[]"), r["recommendation"])


class ReviewError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


class Review:
    """`be` is the Backend (store, bot lookup, bus, secret scan)."""

    def __init__(self, be):
        self.be = be
        self.s = be.s

    # ---------------- identity ----------------
    def identities(self, who):
        """Lower-cased names an item's `bot` may carry to belong to the caller (None = sees everything)."""
        if who["role"] in ("admin", "chief"):
            return None
        if who["role"] == "agent":
            a = who["agent"]
            ids = {a["name"]} | {r["alias"] for r in self.s.q("SELECT alias FROM agent_aliases WHERE name = ?", a["name"])}
            if a.get("bot_id"):
                ids.add(a["bot_id"].lower())
            return ids
        if who["role"] == "bot":
            ids = {who["bot"].lower()}
            ids |= {r["name"] for r in self.s.q("SELECT name FROM agents WHERE lower(bot_id) = lower(?) AND retired_at IS NULL", who["bot"])}
            return ids
        return set()

    def filer(self, who, b):
        """The `bot` an item is filed as: the caller itself; admin and chief name it with `bot`."""
        if who["role"] == "agent":
            return who["agent"]["name"]
        if who["role"] == "bot":
            if b.get("bot") not in (None, "") and str(b["bot"]).lower() != who["bot"].lower():
                raise ReviewError(403, "a bot can only file items as itself")
            return who["bot"]
        bot = str(b.get("bot") or (who.get("bot") or "")).strip()
        if not bot or len(bot) > 80:
            raise ReviewError(400, "bot is required (1-80 characters) with the admin token")
        return bot

    def own(self, who, item_id):
        r = self.s.one("SELECT * FROM review_items WHERE id = ?", str(item_id))
        ids = self.identities(who)
        if not r or (ids is not None and (r["bot"] or "").lower() not in ids):
            raise ReviewError(404, "not found")
        return r

    # ---------------- validation ----------------
    def _text(self, b, k, errors):
        v = b.get(k)
        if v is None:
            return None
        if not isinstance(v, str):
            errors.append(f"{k} must be text")
            return None
        v = v.strip()
        if len(v) > LIMITS[k]:
            errors.append(f"{k} is too long (max {LIMITS[k]})")
        return v or None

    def _options(self, raw, errors):
        if raw is None:
            return []
        if not isinstance(raw, list) or len(raw) > MAX_OPTIONS:
            errors.append(f"options must be a list of at most {MAX_OPTIONS}")
            return []
        out = []
        for o in raw:
            if isinstance(o, str):
                o = {"label": o}
            if not isinstance(o, dict) or not isinstance(o.get("label"), str) or not o["label"].strip():
                errors.append("each option is text or {label, reason, suggested}")
                continue
            out.append({"label": o["label"].strip()[:200], "reason": str(o.get("reason") or "").strip()[:1000],
                        "suggested": o.get("suggested") is True})
        if sum(o["suggested"] for o in out) > 1:
            errors.append("at most one option may be suggested")
        if len({o["label"] for o in out}) != len(out):
            errors.append("option labels must be unique")
        return out

    def _links(self, raw, errors):
        if raw is None:
            return []
        if not isinstance(raw, list) or len(raw) > MAX_LINKS:
            errors.append(f"links must be a list of at most {MAX_LINKS}")
            return []
        out = []
        for x in raw:
            if not isinstance(x, str) or not re.match(r"^https?://\S{1,2000}$", x.strip()):
                errors.append("links must be http(s) URLs")
                continue
            out.append(x.strip())
        return out

    def parse(self, b, cur):
        """The filer fields after this request: a field left out keeps its stored value, null clears it."""
        errors = []
        f = {}
        for k in FILER_FIELDS:
            if k not in b:
                f[k] = (json.loads(cur[k] or "[]") if k in ("options", "links") else cur[k]) if cur else None
            elif k == "options":
                f[k] = self._options(b[k], errors)
            elif k == "links":
                f[k] = self._links(b[k], errors)
            elif k == "type":
                f[k] = b[k]
            else:
                f[k] = self._text(b, k, errors)
        f["type"] = f["type"] or "decision"
        if f["type"] not in TYPES:
            errors.append("type must be one of " + ", ".join(TYPES))
        f["options"], f["links"] = f["options"] or [], f["links"] or []
        if not f["title"] and not f["ask"]:
            errors.append("title or ask is required")
        f["ask"] = f["ask"] or f["title"]
        f["title"] = f["title"] or (f["ask"] or "")[:200]
        if errors:
            raise ReviewError(400, "; ".join(errors), errors=errors)
        if self.be.secret_hit(json.dumps(f)):
            raise ReviewError(400, "the item looks like it contains a secret value; not stored")
        return f

    # ---------------- file ----------------
    def upsert(self, who, b):
        project, key = str(b.get("project") or "").strip(), str(b.get("key") or b.get("dedupe_key") or "").strip()
        errors = []
        if not PROJECT_RE.match(project):
            errors.append("project is required (letters, numbers, . _ -)")
        if not KEY_RE.match(key):
            errors.append("key is required: 1-200 characters A-Za-z0-9._:+-")
        if errors:
            raise ReviewError(400, "; ".join(errors), errors=errors)
        bot = self.filer(who, b)
        t = now()
        with self.s.tx():
            latest = self.s.one("SELECT * FROM review_items WHERE project = ? AND lower(bot) = lower(?) AND key = ?"
                                " ORDER BY created_at DESC, rowid DESC LIMIT 1", project, bot, key)
            live = latest if latest and latest["status"] in OPEN else None
            f = self.parse(b, live)
            if live:
                same = all((json.loads(live[k] or "[]") if k in ("options", "links") else live[k]) == f[k] for k in FILER_FIELDS)
                if same:
                    return 200, {"item": self.shape(live, who), "result": "unchanged"}
                reopened = live["status"] == "changes_requested"
                self.s.run("UPDATE review_items SET type=?, title=?, ask=?, body=?, recommendation=?, options=?, links=?, status='open',"
                           " revision = revision + 1, updated_at = ? WHERE id = ?", f["type"], f["title"], f["ask"], f["body"],
                           f["recommendation"], json.dumps(f["options"]), json.dumps(f["links"]), t, live["id"])
                iid, result, status = live["id"], "reopened" if reopened else "updated", 200
            else:
                iid, result, status = "r" + secrets.token_hex(8), "created", 201
                self.s.run("INSERT INTO review_items(id, project, bot, key, type, title, ask, body, recommendation, options, links,"
                           " status, revision, previous_item_id, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,'open',1,?,?,?)",
                           iid, project, bot, key, f["type"], f["title"], f["ask"], f["body"], f["recommendation"],
                           json.dumps(f["options"]), json.dumps(f["links"]), latest["id"] if latest else None, t, t)
        out = {"item": self.shape(self.s.one("SELECT * FROM review_items WHERE id = ?", iid), who), "result": result}
        if latest and not live and latest["status"] == "rejected":
            out["warning"] = "the previous item for this key was rejected; this re-files it as a new item"
        return status, out

    # ---------------- read ----------------
    def shape(self, r, who=None):
        d = json.loads(r["decision"]) if r["decision"] else None
        return {"id": r["id"], "project": r["project"], "bot": r["bot"], "key": r["key"], "type": r["type"], "title": r["title"],
                "ask": r["ask"], "body": r["body"], "recommendation": r["recommendation"], "options": json.loads(r["options"] or "[]"),
                "links": json.loads(r["links"] or "[]"), "status": r["status"], "revision": r["revision"],
                "previous_item_id": r["previous_item_id"], "content_hash": row_hash(r), "decision": d,
                "delivery": {"state": r["delivery"], "bus_message_id": r["bus_message_id"], "error": r["delivery_error"],
                             "acked_at": r["acked_at"]} if d else None,
                "close_reason": r["close_reason"], "created_at": r["created_at"], "updated_at": r["updated_at"]}

    def list(self, who, qs):
        where, args = [], []
        status = qs.get("status") or "open"
        groups = {"open": ("open",), "changes_requested": ("changes_requested",), "pending": OPEN,
                  "decided": ("approved", "rejected", "changes_requested"), "answered": ("approved", "rejected", "changes_requested"),
                  "approved": ("approved",), "rejected": ("rejected",), "closed": ("closed",)}
        if status != "all":
            if status not in groups:
                raise ReviewError(400, "status must be one of " + ", ".join(list(groups) + ["all"]))
            where.append("status IN (%s)" % ",".join("?" * len(groups[status])))
            args += groups[status]
        if qs.get("acked") == "0":
            where.append("decision IS NOT NULL AND acked_at IS NULL")
        elif qs.get("acked") == "1":
            where.append("acked_at IS NOT NULL")
        for k in ("project", "key", "type"):
            if qs.get(k):
                where.append(f"{k} = ?")
                args.append(qs[k])
        ids = self.identities(who)
        if ids is not None:
            where.append("lower(bot) IN (%s)" % ",".join("?" * len(ids)))
            args += sorted(ids)
        elif qs.get("bot"):
            where.append("lower(bot) = lower(?)")
            args.append(qs["bot"])
        try:
            lim = max(1, min(int(qs.get("limit") or 200), LIST_MAX))
        except ValueError:
            lim = 200
        sql = "SELECT * FROM review_items" + (" WHERE " + " AND ".join(where) if where else "")
        total = self.s.one("SELECT COUNT(*) AS n FROM (" + sql + ")", *args)["n"]
        rows = self.s.q(sql + " ORDER BY updated_at DESC, rowid DESC LIMIT ?", *args, lim)
        return {"items": [self.shape(r, who) for r in rows], "total": total}

    def detail(self, who, item_id):
        return {"item": self.shape(self.own(who, item_id), who)}

    # ---------------- decide ----------------
    def decide(self, who, item_id, b):
        r = self.own(who, item_id)   # admin and chief only reach here (route roles)
        action = str(b.get("action") or "").strip().lower()
        if action not in ACTIONS:
            raise ReviewError(400, "action must be approve, changes or reject")
        seen = b.get("content_hash")
        if not isinstance(seen, str) or not seen:
            raise ReviewError(409, STALE_MSG + " (content_hash is required)", code="stale", item=self.shape(r, who))
        errors = []
        note = b.get("note") if b.get("note") is not None else b.get("feedback")
        edited = b.get("edited_text")
        option = b.get("option")
        for k, v, lim in (("note", note, 4000), ("edited_text", edited, 100000), ("option", option, 200)):
            if v is not None and (not isinstance(v, str) or len(v) > lim):
                errors.append(f"{k} must be text of at most {lim} characters")
        if errors:
            raise ReviewError(400, "; ".join(errors), errors=errors)
        rev = b.get("revision")
        if rev is not None and (not isinstance(rev, int) or isinstance(rev, bool)):
            raise ReviewError(400, "revision must be a whole number")
        by = "admin" if who["role"] == "admin" else f"chief:{who['bot']}"
        t = now()
        with self.s.tx():
            cur = self.s.one("SELECT * FROM review_items WHERE id = ?", r["id"])
            # The guard and the write share one transaction: the hash is compared with the item as it is at commit time.
            if row_hash(cur) != seen or (rev is not None and rev != cur["revision"]):
                raise ReviewError(409, STALE_MSG, code="stale", item=self.shape(cur, who))
            if cur["status"] != "open":
                raise ReviewError(409, f"this item is {cur['status'].replace('_', ' ')}, not open for a decision",
                                  code="not_open", item=self.shape(cur, who))
            labels = [o["label"] for o in json.loads(cur["options"] or "[]")]
            if option is not None and labels and option.strip() not in labels:
                raise ReviewError(400, "option must be one of the item's option labels: " + ", ".join(labels))
            decision = {"action": "request_changes" if ACTIONS[action] == "changes_requested" else action,
                        "option": option.strip() if option else None, "note": (note or "").strip() or None,
                        "edited_text": edited if edited not in (None, "") else None, "revision": cur["revision"],
                        "content_hash": seen, "by": by, "at": t}
            self.s.run("UPDATE review_items SET status = ?, decision = ?, decided_at = ?, delivery = 'pending', bus_message_id = NULL,"
                       " delivery_error = NULL, acked_at = NULL, updated_at = ? WHERE id = ?",
                       ACTIONS[action], json.dumps(decision), t, t, cur["id"])
        self.deliver(cur["id"])
        return {"item": self.shape(self.s.one("SELECT * FROM review_items WHERE id = ?", cur["id"]), who)}

    # ---------------- deliver ----------------
    def message(self, r, d, pointer_only=False):
        base = self.be.public_url.rstrip("/") if getattr(self.be, "public_url", "") else ""
        head = (f"[review-decision] item={r['id']} key={r['key']} project={r['project']} decision={d['action']} "
                f"revision={d['revision']}")
        tail = (f"\n\nFull record: GET {base}/review/items/{r['id']}\n"
                f"Ack when consumed: POST {base}/review/items/{r['id']}/ack {{\"revision\": {d['revision']}}}")
        if pointer_only:
            return head + "\n\nThe decision text could not go on the bus; read the full record." + tail
        parts = [head, f"Type: {r['type']} · decided by {d['by']}", f"Title: {r['title']}"]
        if d.get("option"):
            parts.append(f"Chosen option: {d['option']}")
        if d.get("note"):
            parts.append(f"Note:\n{d['note']}")
        if d.get("edited_text"):
            parts.append(f"Final text:\n{d['edited_text']}")
        body = "\n\n".join(parts) + tail
        if len(body) > 3700:
            return head + "\n\nThe decision is too long for a bus message; read the full record." + tail
        return body

    def deliver(self, item_id):
        """Send the decision to the filing bot's bus agent (any wake type). Without one, the decision waits in the bot's
        answered, unacked list. A refused message (for example a secret-shaped note) falls back to a pointer-only message."""
        r = self.s.one("SELECT * FROM review_items WHERE id = ?", item_id)
        d = json.loads(r["decision"])
        try:
            agent = self.be.bus.owner_agent(r["bot"], bus_only=False)
        except Exception as e:  # never lose the recorded decision over a lookup
            agent = None
            self.s.run("UPDATE review_items SET delivery = 'failed', delivery_error = ? WHERE id = ?", str(e)[:200], item_id)
            return
        if not agent:
            self.s.run("UPDATE review_items SET delivery = 'no_agent' WHERE id = ?", item_id)
            return
        meta = {"kind": "review_decision", "item_id": r["id"], "key": r["key"], "project": r["project"], "decision": d["action"],
                "revision": d["revision"], "option": d["option"]}
        err = None
        for pointer in (False, True):
            try:
                st, data = self.be.bus.send({"from": "agent-control", "to": agent["name"], "body": self.message(r, d, pointer),
                                             "thread": f"review:{r['id']}", "client_msg_id": f"review:{r['id']}:{d['revision']}"
                                             + (":p" if pointer else ""), "urgent": True, "reply_expected": False, "meta": meta})
            except Exception as e:
                st, data = 500, {"error": str(e)[:200]}
            if st in (200, 201):
                self.s.run("UPDATE review_items SET delivery = 'bus', bus_message_id = ? WHERE id = ?", data["id"], item_id)
                return
            err = f"{st}: {data.get('error')}"
            if st >= 500:
                break
        self.s.run("UPDATE review_items SET delivery = 'failed', delivery_error = ? WHERE id = ?", (err or "")[:300], item_id)

    def ack(self, who, item_id, b):
        r = self.own(who, item_id)
        if not r["decision"]:
            raise ReviewError(409, "this item has no decision to acknowledge")
        d = json.loads(r["decision"])
        rev = b.get("revision")
        if rev is not None and rev != d["revision"]:
            return {"ok": True, "stale": True, "decided_revision": d["revision"]}
        first = self.s.run("UPDATE review_items SET acked_at = ? WHERE id = ? AND acked_at IS NULL", now(), r["id"]).rowcount
        if first and r["bus_message_id"]:
            self.s.run("UPDATE bus_messages SET state = 'read', read_at = ?, delivered_at = COALESCE(delivered_at, ?)"
                       " WHERE id = ? AND read_at IS NULL", now(), now(), r["bus_message_id"])
        return {"ok": True, "first": bool(first), "decided_revision": d["revision"]}

    def close(self, who, item_id, b):
        r = self.own(who, item_id)
        if r["status"] not in OPEN:
            raise ReviewError(409, f"this item is {r['status'].replace('_', ' ')}; only an open item can be closed")
        reason = str(b.get("reason") or "withdrawn").strip()[:200]
        self.s.run("UPDATE review_items SET status = 'closed', close_reason = ?, updated_at = ? WHERE id = ?", reason, now(), r["id"])
        return {"item": self.shape(self.s.one("SELECT * FROM review_items WHERE id = ?", r["id"]), who)}
