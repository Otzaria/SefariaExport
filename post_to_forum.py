#!/usr/bin/env python3
"""Post the changelog diff to the Otzaria forum as a Hebrew post (gated, non-fatal)."""
import argparse
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Jerusalem")


def truthy(val, default=True):
    if val is None:
        return default
    return val.strip().lower() not in ("false", "0", "no", "off", "")


def load_json(path, fallback):
    if path and os.path.isfile(path) and os.path.getsize(path) > 0:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return fallback


def heb_date():
    try:
        from pyluach import dates
        return dates.HebrewDate.from_pydate(datetime.now(tz=TZ).date()).hebrew_date_string()
    except Exception:
        return datetime.now(tz=TZ).strftime("%Y-%m-%d")


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
    args = ap.parse_args()

    diff = load_json(args.diff_json, None)
    if not diff:
        print(f"⏭️  No diff file at {args.diff_json} — skipping.")
        return 0
    if not diff.get("has_baseline"):
        print("⏭️  Initial release (no baseline) — skipping forum post.")
        return 0

    date = heb_date()
    tag = args.tag or diff.get("new_tag", "")

    changes_text, has_changes = build_changes_post(diff, date)
    new_books_text, has_new_books = build_new_books_post(diff, date)

    if not (has_changes or has_new_books):
        print("⏭️  No book changes — skipping forum post.")
        return 0

    repo = os.getenv("GITHUB_REPOSITORY")
    footer = f"\n[להורדת העדכון](https://github.com/{repo}/releases/tag/{tag})\n" if (repo and tag) else ""

    posts = [
        ("שינויים בספרים", args.topic, changes_text + footer),
        ("ספרים חדשים", args.new_books_topic, new_books_text + footer),
    ]

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
        for label, topic_id, text in posts:
            try:
                resp = client.send_post(text, topic_id)
                print(f"✅ Posted to forum topic {topic_id} ({label}): {str(resp)[:200]}")
            except Exception as e:  # non-fatal
                print(f"⚠️  Forum post failed for {label} (non-fatal): {e}")
    finally:
        try:
            client.logout()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
