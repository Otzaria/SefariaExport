#!/usr/bin/env python3
"""Post the changelog diff to the Otzaria forum as a Hebrew post (gated, non-fatal)."""
import argparse
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Jerusalem")

# The forum enforces a per-user delay between posts (currently 1s), so the
# threads are written one after another with room to spare rather than in a
# tight loop — the tight loop silently lost every second post.
DEFAULT_POST_DELAY_SECONDS = 4.0
DEFAULT_POST_ATTEMPTS = 4


def truthy(val, default=True):
    if val is None:
        return default
    return val.strip().lower() not in ("false", "0", "no", "off", "")


def load_json(path, fallback):
    if path and os.path.isfile(path) and os.path.getsize(path) > 0:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return fallback


def heb_date(day=None):
    """Hebrew date string for `day` (default: today in Israel).

    An explicit day matters when re-publishing a post for an older release:
    the post must carry that release's date, not the day it was resent.
    """
    day = day or datetime.now(tz=TZ).date()
    try:
        from pyluach import dates
        return dates.HebrewDate.from_pydate(day).hebrew_date_string()
    except Exception:
        return day.strftime("%Y-%m-%d")


def he_of(book, key="he"):
    """Hebrew title with an English fallback."""
    return book.get(key) or book.get("en") or book.get("new_en") or ""


def bullets(lines):
    return "\n".join(f"* {x}" for x in lines)


def rename_lines(he_renamed, en_renamed):
    """Old→new name pairs (Hebrew) from both Hebrew-title and English-title renames."""
    pairs = [(b["old_he"], b["new_he"]) for b in he_renamed
             if b.get("old_he") and b.get("new_he")]
    pairs += [(b["old_he"], b["new_he"]) for b in en_renamed
              if b.get("old_he") and b.get("new_he") and b["old_he"] != b["new_he"]]
    return [f"«{old}» שונה ל־«{new}»" for old, new in sorted(set(pairs))]


def move_lines(moved):
    return [f"{he_of(b)} — הועבר מ־{b['old_category']} אל {b['new_category']}"
            for b in moved]


def version_lines(versions):
    """Pasteable `book | versionTitle` lines for new editions (for black_versions.txt)."""
    out = []
    for v in sorted(versions, key=lambda v: (v.get("book_he") or v.get("book_en") or "",
                                             v.get("version") or "")):
        book = v.get("book_he") or v.get("book_en") or ""
        mark = "" if v.get("exact", True) else "  ⁇"
        out.append(f"{book} | {v.get('version', '')}{mark}")
    return out


def build_changes_post(diff, date):
    """'שינויים בספרים' post: renames, moves, content updates, removals."""
    books = diff.get("books", {})
    removed = sorted({he_of(b) for b in books.get("removed", [])})
    content = sorted({he_of(b) for b in books.get("content_changed", [])})
    renamed = rename_lines(books.get("he_renamed", []), books.get("en_renamed", []))
    moved = sorted(move_lines(books.get("moved", [])))

    has_content = bool(renamed or moved or content or removed)

    parts = [f"# עדכון ספריית ספריא (Sefaria) — שינויים בספרים\n", f"**עדכון {date}**\n"]
    if renamed:
        parts.append(f"\n## שינויי שם (שם קודם ⟶ שם חדש):\n{bullets(renamed)}\n")
    if moved:
        parts.append(f"\n## ספרים שהועברו:\n{bullets(moved)}\n")
    if content:
        parts.append(f"\n## עודכנו/תוקנו הספרים הבאים:\n{bullets(content)}\n")
    if removed:
        parts.append(f"\n## הוסרו הספרים הבאים:\n{bullets(removed)}\n")
    if not has_content:
        parts.append("\nאין שינויים בספרים קיימים בעדכון זה.\n")
    return "".join(parts), has_content


def build_new_books_post(diff, date):
    """'ספרים חדשים' post: newly added books and new editions/versions."""
    books = diff.get("books", {})
    added = sorted({he_of(b) for b in books.get("added", [])})
    versions = version_lines(diff.get("versions", {}).get("added", []))

    has_content = bool(added or versions)

    parts = [f"# עדכון ספריית ספריא (Sefaria) — ספרים חדשים\n", f"**עדכון {date}**\n"]
    if added:
        parts.append(f"\n## ספרים חדשים:\n{bullets(added)}\n")
    if versions:
        # Pasteable `book | versionTitle` lines for triage into black_versions.txt.
        block = "\n".join(versions)
        parts.append(f"\n## גרסאות (מהדורות) חדשות:\n```\n{block}\n```\n")
    if not has_content:
        parts.append("\nאין ספרים חדשים בעדכון זה.\n")
    return "".join(parts), has_content


def positive_number(name, default):
    """A positive float from env var `name`, or `default` if unset/unusable."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        print(f"⚠️  {name}={raw!r} is not a number — using {default}.")
        return default
    if value <= 0:
        print(f"⚠️  {name}={raw!r} is not positive — using {default}.")
        return default
    return value


def send_posts(client, posts, delay=None, attempts=None):
    """Send every post, spaced out; return the labels that never landed.

    Two rules earn their keep here:

      * posts are spaced by `delay`, and a refusal that names the forum's
        post-rate window is retried with a growing backoff.  Without this the
        second post of the run is rejected every single time.
      * a transport error is NOT retried.  The post may well have been created
        before the connection broke, and a blind retry would duplicate it in
        the thread — a duplicate is worse than a reported miss.
    """
    from otzaria_forum import ForumPostError

    delay = delay if delay is not None else positive_number(
        "FORUM_POST_DELAY_SECONDS", DEFAULT_POST_DELAY_SECONDS)
    attempts = int(attempts if attempts is not None else positive_number(
        "FORUM_POST_ATTEMPTS", DEFAULT_POST_ATTEMPTS))
    failed = []

    for index, (label, topic_id, text) in enumerate(posts):
        if index:
            time.sleep(delay)
        wait = delay
        for attempt in range(1, attempts + 1):
            try:
                resp = client.send_post(text, topic_id)
            except ForumPostError as exc:
                if exc.retryable and attempt < attempts:
                    print(f"⏳ Forum post-rate refusal for {label} (topic {topic_id}), "
                          f"attempt {attempt}/{attempts}: {exc.message} — "
                          f"retrying in {wait:g}s", flush=True)
                    time.sleep(wait)
                    wait *= 2
                    continue
                print(f"❌ Forum post NOT created for {label} (topic {topic_id}) "
                      f"after {attempt} attempt(s): {exc}")
                failed.append(label)
                break
            except Exception as exc:  # transport, or anything else unexpected
                print(f"❌ Forum post for {label} (topic {topic_id}) failed with an "
                      f"undetermined outcome and was NOT retried "
                      f"(it may or may not exist): {exc!r}")
                failed.append(label)
                break
            url = (resp.get("response") or {}).get("url") or ""
            print(f"✅ Posted to forum topic {topic_id} ({label}) {url}".rstrip())
            break

    return failed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("diff_json")
    ap.add_argument("--titles", default="", help="current release titles.json (unused; kept for compat)")
    ap.add_argument("--prev-titles", default="", help="previous release titles.json (unused; kept for compat)")
    ap.add_argument("--topic", type=int, default=int(os.getenv("FORUM_TOPIC_ID", "1617")),
                    help="topic id for the 'changes to existing books' thread")
    ap.add_argument("--new-books-topic", type=int,
                    default=int(os.getenv("FORUM_NEW_BOOKS_TOPIC_ID", "1994")),
                    help="topic id for the 'new books' thread")
    ap.add_argument("--tag", default="")
    ap.add_argument("--only", choices=("both", "changes", "new-books"), default="both",
                    help="which thread(s) to write; 'new-books' re-publishes only that post")
    ap.add_argument("--as-of", dest="as_of", default="",
                    help="YYYY-MM-DD to date the post by (default today); use the original "
                         "release date when re-publishing a post that never landed")
    args = ap.parse_args()

    as_of = None
    if args.as_of:
        as_of = datetime.strptime(args.as_of, "%Y-%m-%d").date()

    diff = load_json(args.diff_json, None)
    if not diff:
        print(f"⏭️  No diff file at {args.diff_json} — skipping.")
        return 0
    if not diff.get("has_baseline"):
        print("⏭️  Initial release (no baseline) — skipping forum post.")
        return 0

    date = heb_date(as_of)
    tag = args.tag or diff.get("new_tag", "")

    changes_text, has_changes = build_changes_post(diff, date)
    new_books_text, has_new_books = build_new_books_post(diff, date)

    if not (has_changes or has_new_books):
        print("⏭️  No book changes — skipping forum post.")
        return 0

    repo = os.getenv("GITHUB_REPOSITORY")
    footer = f"\n[להורדת העדכון](https://github.com/{repo}/releases/tag/{tag})\n" if (repo and tag) else ""

    posts = []
    if args.only in ("both", "changes"):
        posts.append(("שינויים בספרים", args.topic, changes_text + footer))
    if args.only in ("both", "new-books"):
        posts.append(("ספרים חדשים", args.new_books_topic, new_books_text + footer))

    # A re-publish asks for one specific thread; writing "nothing new" into it
    # would add noise to the very thread the miss was reported against.
    if args.only == "new-books" and not has_new_books:
        print("⏭️  Nothing new in this diff — not writing to the new-books thread.")
        return 0
    if args.only == "changes" and not has_changes:
        print("⏭️  No changes in this diff — not writing to the changes thread.")
        return 0

    for label, topic_id, text in posts:
        print(f"----- forum post ({label}, topic {topic_id}) -----")
        print(text)
        print("----------------------")

    # Content is always printed above; only the send is gated.
    if not truthy(os.getenv("POST_TO_FORUM")):
        print("⏭️  POST_TO_FORUM disabled — not sending (dry run).")
        return 0
    username = os.getenv("USER_NAME")
    password = os.getenv("PASSWORD")
    if not username or not password:
        print("⏭️  USER_NAME / PASSWORD not set — not sending.")
        return 0

    from otzaria_forum import OtzariaForumClient
    client = OtzariaForumClient(username.strip().replace(" ", "+"), password.strip())
    try:
        client.login()
        failed = send_posts(client, posts)
    finally:
        try:
            client.logout()
        except Exception:
            pass

    if failed:
        # The step is `continue-on-error`, so this never blocks a release — but
        # a lost post now shows up red instead of hiding behind a ✅.
        print(f"❌ {len(failed)} of {len(posts)} forum post(s) never landed: "
              f"{', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
