#!/usr/bin/env python3
"""Daily: reply publicly to recent 5-star guest reviews.

1. Reads the "All Reviews Log" boards in the USA and Canada monday.com
   workspaces (filled by the Review-Automation sync two hours earlier) and
   picks items with Overall Rating 5, Host Responded unchecked, and a Review
   Date within the last MAX_AGE_DAYS days.
2. Checks each one against Hospitable: the review must still have no host
   response and must still be open for a reply on the platform.
3. Has Claude draft a short, personal reply from the *public* review text
   only (private feedback is never shown to the model, so it can't leak).
4. Posts the reply through Hospitable, then ticks "Host Responded" on the
   monday item and adds the posted text as an update on that item.

Environment:
  HOSPITABLE_API_TOKEN  Hospitable personal access token with write access (required)
  MONDAY_API_TOKEN      monday.com API token (required)
  ANTHROPIC_API_KEY     Claude API key (required)
  ANTHROPIC_WORKSPACE_ID  workspace for the key; needed only if the key isn't scoped to one
  DRY_RUN               "true" to draft replies without posting or changing monday
  MAX_AGE_DAYS          only reply to reviews this recent (default 30)
  MAX_PER_RUN           cap on replies per run (default 40)
"""

import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import anthropic

HOSPITABLE_API = "https://public.api.hospitable.com/v2"
MONDAY_API = "https://api.monday.com/v2"
MODEL = "claude-opus-5-5"

# All Reviews Log boards, one per workspace
REVIEW_BOARDS = {
    18435520124: "USA Automated (Trial)",
    18435476757: "Canada Automated(Trial)",
}

RESPONSE_LIMIT = 1000  # Hospitable / Airbnb maximum
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS") or 30)
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN") or 40)

SYSTEM_PROMPT = """You write public replies to 5-star guest reviews for Instastay, a short-term rental host.

The reply is posted publicly under the guest's review on Airbnb/VRBO, where future guests read it.

Write the reply so that it:
- Thanks the guest by first name.
- Picks up one or two specific things the guest mentioned, in your own words, so it clearly isn't a template. If the review has no text, keep it to a warm two-sentence thank-you that mentions the stay.
- Is written in first person as the host ("I", "we" for the team is fine), warm and natural, never gushing or salesy.
- Ends by welcoming them back.
- Is 2 to 4 sentences and under 450 characters.

Do not:
- Use emojis, hashtags, exclamation marks in every sentence, or quotation marks around the reply.
- Mention discounts, refunds, promotions, or any promise about the property.
- Mention or hint at private feedback, problems, ratings, or star counts.
- Sign off with a name or add a greeting line like "Dear".

Reply with the response text only."""


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------- HTTP

class HTTPStatusError(RuntimeError):
    def __init__(self, status, url, body):
        super().__init__(f"HTTP {status} from {url}: {body[:500]}")
        self.status = status


def http_json(url, *, headers, body=None, method=None, retries=4):
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method or ("POST" if data else "GET"))
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = int(e.headers.get("Retry-After") or 2 ** (attempt + 1))
                log(f"  HTTP {e.code} from {url}, retrying in {wait}s")
                time.sleep(wait)
                continue
            raise HTTPStatusError(e.code, url, e.read().decode()) from e
        except urllib.error.URLError:
            if attempt < retries:
                time.sleep(2 ** (attempt + 1))
                continue
            raise


def hospitable(path, params=None, body=None):
    url = f"{HOSPITABLE_API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Authorization": f"Bearer {os.environ['HOSPITABLE_API_TOKEN']}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    return http_json(url, headers=headers, body=body)


def monday(query, variables=None):
    resp = http_json(MONDAY_API, headers={
        "Authorization": os.environ["MONDAY_API_TOKEN"],
        "Content-Type": "application/json",
        "API-Version": "2024-10",
    }, body={"query": query, "variables": variables or {}})
    if resp.get("errors"):
        raise RuntimeError(f"monday API error: {json.dumps(resp['errors'])[:1000]}")
    return resp["data"]


# ------------------------------------------------------------------------- monday

COLUMN_TITLES = ["Property", "Review Date", "Overall Rating", "Host Responded", "Review ID"]


def board_columns(board_id):
    data = monday("query($id:[ID!]){boards(ids:$id){columns{id title}}}", {"id": [board_id]})
    cols = {c["title"]: c["id"] for c in data["boards"][0]["columns"]}
    missing = [t for t in COLUMN_TITLES if t not in cols]
    if missing:
        raise RuntimeError(f"Board {board_id} is missing columns: {missing}")
    return cols


def board_items(board_id, column_ids):
    query = """query($id:[ID!],$cols:[String!]){boards(ids:$id){items_page(limit:500){cursor
      items{id name column_values(ids:$cols){id text}}}}}"""
    next_query = """query($cursor:String!,$cols:[String!]){next_items_page(limit:500,cursor:$cursor){cursor
      items{id name column_values(ids:$cols){id text}}}}"""
    page = monday(query, {"id": [board_id], "cols": column_ids})["boards"][0]["items_page"]
    while True:
        yield from page["items"]
        if not page.get("cursor"):
            return
        page = monday(next_query, {"cursor": page["cursor"], "cols": column_ids})["next_items_page"]


def is_candidate(fields, today, max_age_days):
    """A board row qualifies when it's a 5-star review, unanswered, and recent enough."""
    try:
        rating = float(fields.get("Overall Rating") or 0)
        reviewed = dt.date.fromisoformat((fields.get("Review Date") or "")[:10])
    except ValueError:
        return False
    return (
        rating == 5
        and fields.get("Host Responded") != "v"
        and bool((fields.get("Review ID") or "").strip())
        and (today - reviewed).days <= max_age_days
    )


def five_star_candidates(today):
    out = []
    for board_id, label in REVIEW_BOARDS.items():
        cols = board_columns(board_id)
        by_id = {cols[t]: t for t in COLUMN_TITLES}
        for item in board_items(board_id, list(by_id)):
            fields = {by_id[cv["id"]]: cv["text"] for cv in item["column_values"]}
            if is_candidate(fields, today, MAX_AGE_DAYS):
                out.append({
                    "board_id": board_id, "workspace": label, "item_id": item["id"],
                    "guest": item["name"], "property": fields.get("Property") or "",
                    "review_id": fields["Review ID"].strip(), "date": fields["Review Date"][:10],
                    "responded_col": cols["Host Responded"],
                })
    out.sort(key=lambda c: c["date"], reverse=True)
    return out


def mark_responded(candidate, response_text):
    monday(
        """mutation($board:ID!,$item:ID!,$vals:JSON!){
          change_multiple_column_values(board_id:$board,item_id:$item,column_values:$vals){id}}""",
        {"board": candidate["board_id"], "item": candidate["item_id"],
         "vals": json.dumps({candidate["responded_col"]: {"checked": "true"}})},
    )
    if response_text:
        monday(
            "mutation($item:ID!,$body:String!){create_update(item_id:$item,body:$body){id}}",
            {"item": candidate["item_id"], "body": f"<p><b>Auto-reply posted via Hospitable:</b></p><p>{response_text}</p>"},
        )


# --------------------------------------------------------------------- hospitable

def recent_hospitable_reviews(since):
    """review id -> {review, property} for every review posted on or after `since`."""
    reviews, properties = {}, []
    page = 1
    while True:
        resp = hospitable("/properties", {"page": page, "per_page": 100})
        properties += resp.get("data", [])
        if page >= (resp.get("meta") or {}).get("last_page", page):
            break
        page += 1

    for prop in properties:
        page = 1
        while True:
            resp = hospitable(f"/properties/{prop['id']}/reviews",
                              {"page": page, "per_page": 50, "include": "guest"})
            batch = resp.get("data", [])
            for r in batch:
                if (r.get("reviewed_at") or "")[:10] >= since.isoformat():
                    reviews[r["id"]] = {"review": r, "property": prop}
            # Reviews come back newest first; stop once a page reaches past the window.
            oldest = min(((r.get("reviewed_at") or "")[:10] for r in batch), default="")
            if not batch or oldest < since.isoformat() or page >= (resp.get("meta") or {}).get("last_page", page):
                break
            page += 1
    return reviews, properties


def post_response(review_id, text):
    return hospitable(f"/reviews/{review_id}/response", body={"response": text})


def style_examples(reviews, limit=4):
    """A few replies the host already wrote, so drafts match their voice."""
    examples = []
    for entry in sorted(reviews.values(), key=lambda e: e["review"].get("reviewed_at") or "", reverse=True):
        public = entry["review"].get("public") or {}
        if public.get("response") and public.get("review"):
            examples.append(public["response"].strip())
        if len(examples) >= limit:
            break
    return examples


# ------------------------------------------------------------------------- Claude

def build_prompt(review, property_name, examples):
    guest = review.get("guest") or {}
    first_name = (guest.get("first_name") or "").strip() or "there"
    public_text = ((review.get("public") or {}).get("review") or "").strip()
    parts = [
        f"Guest first name: {first_name}",
        f"Property: {property_name}",
        f"Platform: {review.get('platform') or 'airbnb'}",
        "",
        "<public_review>",
        public_text or "(The guest left 5 stars without writing a review.)",
        "</public_review>",
    ]
    if examples:
        parts += ["", "Replies the host has written before, for tone and length only (don't copy them):"]
        parts += [f"<example>{e}</example>" for e in examples]
    return "\n".join(parts)


def clean_reply(text):
    text = text.strip().strip('"“”').strip()
    if len(text) > RESPONSE_LIMIT:
        cut = text[:RESPONSE_LIMIT]
        text = cut[: cut.rfind(".") + 1] or cut
    return text


def draft_reply(client, review, property_name, examples):
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_prompt(review, property_name, examples)}],
    )
    if response.stop_reason == "refusal":
        return None
    text = "".join(b.text for b in response.content if b.type == "text")
    return clean_reply(text) or None


# --------------------------------------------------------------------------- main

def annotate(title, lines, chunk_chars=3500, max_chunks=9):
    """Post the report as run annotations (GitHub cuts each one at ~4 KB, so split it)."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    chunks, current = [], ""
    for line in lines:
        if current and len(current) + len(line) + 1 > chunk_chars:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current:
        chunks.append(current)
    for i, chunk in enumerate(chunks[:max_chunks], 1):
        body = chunk.rstrip().replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        suffix = f" ({i}/{len(chunks)})" if len(chunks) > 1 else ""
        log(f"::notice title={title}{suffix}::{body}")


def main():
    for var in ("HOSPITABLE_API_TOKEN", "MONDAY_API_TOKEN", "ANTHROPIC_API_KEY"):
        if not os.environ.get(var):
            sys.exit(f"Missing required environment variable {var}")
    if DRY_RUN:
        log("DRY RUN: replies are drafted but not posted, and monday is not changed")

    today = dt.datetime.now(dt.timezone.utc).date()
    candidates = five_star_candidates(today)
    log(f"{len(candidates)} unanswered 5-star reviews from the last {MAX_AGE_DAYS} days on monday")

    reviews, _ = recent_hospitable_reviews(today - dt.timedelta(days=MAX_AGE_DAYS + 1))
    examples = style_examples(reviews)
    # Org-level API keys (not scoped to a workspace) must name the workspace on every request.
    workspace_id = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    client = anthropic.Anthropic(
        default_headers={"anthropic-workspace-id": workspace_id} if workspace_id else None,
    )

    stats = {"posted": 0, "already": 0, "closed": 0, "missing": 0, "failed": 0}
    report = []
    for c in candidates:
        if stats["posted"] >= MAX_PER_RUN:
            report.append(f"Stopped at MAX_PER_RUN={MAX_PER_RUN}; the rest will be picked up tomorrow")
            break
        label = f"{c['workspace']} | {c['property']} | {c['guest']} | {c['date']}"
        entry = reviews.get(c["review_id"])
        if not entry:
            stats["missing"] += 1
            report.append(f"NOT FOUND in Hospitable: {label}")
            continue
        review = entry["review"]
        if (review.get("public") or {}).get("response") or review.get("responded_at"):
            stats["already"] += 1
            if not DRY_RUN:
                mark_responded(c, None)
            continue
        if review.get("can_respond") is False:
            stats["closed"] += 1
            report.append(f"REPLY WINDOW CLOSED: {label}")
            continue

        reply = draft_reply(client, review, entry["property"].get("name") or c["property"], examples)
        if not reply:
            stats["failed"] += 1
            report.append(f"NO DRAFT (model declined): {label}")
            continue

        if DRY_RUN:
            stats["posted"] += 1
            report.append(f"DRAFT {label}\n    {reply}")
            log(f"~ {label}\n    {reply}")
            continue

        try:
            post_response(c["review_id"], reply)
        except HTTPStatusError as e:
            stats["failed"] += 1
            report.append(f"POST FAILED ({e.status}): {label}")
            log(f"! {e}")
            if e.status in (401, 403, 404, 405):
                # Wrong endpoint or token without write access: every other post would fail the same way.
                annotate("Review replies FAILED", report)
                raise
            continue
        stats["posted"] += 1
        report.append(f"POSTED {label}\n    {reply}")
        log(f"+ {label}\n    {reply}")
        mark_responded(c, reply)

    summary = (f"{'Drafted' if DRY_RUN else 'Posted'}: {stats['posted']}, already answered: {stats['already']}, "
               f"reply window closed: {stats['closed']}, not found: {stats['missing']}, failed: {stats['failed']}")
    log(f"\nDone. {summary}")
    annotate(f"Review replies{' (dry run)' if DRY_RUN else ''}", [summary, *report])
    if stats["failed"] and not stats["posted"]:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # surface the reason on the run page, not just in the log
        if os.environ.get("GITHUB_ACTIONS") and not (isinstance(exc, SystemExit) and exc.code in (0, None)):
            import traceback
            detail = traceback.format_exc()[-3000:]
            print("::error title=Review replies crashed::" + detail.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"), flush=True)
        raise
