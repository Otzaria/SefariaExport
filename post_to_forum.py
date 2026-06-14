#!/usr/bin/env python3
"""Post the changelog diff to the Otzaria forum as a Hebrew post (gated, non-fatal)."""
import argparse
import json
import os
import sys
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


def en_title(label):
    """Book label is a category path like 'Halakhah/Ben Ish Hai'."""
    return label.split("/")[-1]


def norm_en(label):
    """English title for rename-matching, with a trailing ' (Hebrew)' marker dropped."""
    en = en_title(label).strip()
    low = en.lower()
    if low.endswith("(hebrew)"):
        en = en[: low.rfind("(hebrew)")].strip()
    return en.lower()


def he_name(label, titles_primary, titles_fallback):
    en = en_title(label)
    return titles_primary.get(en) or titles_fallback.get(en) or en


def name_set(labels, titles_primary, titles_fallback):
    return {he_name(l, titles_primary, titles_fallback) for l in labels}


def bullets(names):
    return "\n".join(f"* {n}" for n in sorted(names))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("diff_json")
    ap.add_argument("--titles", default="", help="current release titles.json")
    ap.add_argument("--prev-titles", default="", help="previous release titles.json")
    ap.add_argument("--topic", type=int, default=int(os.getenv("FORUM_TOPIC_ID", "20")))
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    diff = load_json(args.diff_json, None)
    if not diff:
        print(f"⏭️  No diff file at {args.diff_json} — skipping.")
        return 0
    if not diff.get("has_baseline"):
        print("⏭️  Initial release (no baseline) — skipping forum post.")
        return 0

    books = diff.get("books", {})
    added, changed, removed = books.get("added", []), books.get("changed", []), books.get("removed", [])
    if not (added or changed or removed):
        print("⏭️  No book changes — skipping forum post.")
        return 0

    titles_cur = load_json(args.titles, {})
    titles_prev = load_json(args.prev_titles, {})
    # Removed books exist only in the previous export -> use previous titles.
    cur_primary, cur_fallback = titles_cur, titles_prev
    rm_primary, rm_fallback = titles_prev, titles_cur

    # Pair English-only renames by normalized English title (not heTitle, which
    # may collide across distinct books) and drop matched pairs from both lists.
    rm_by_norm = {}
    for l in removed:
        rm_by_norm.setdefault(norm_en(l), []).append(l)
    renamed_added, renamed_removed = set(), set()
    for l in added:
        bucket = rm_by_norm.get(norm_en(l))
        if bucket:
            renamed_added.add(l)
            renamed_removed.add(bucket.pop())

    added_names = name_set([l for l in added if l not in renamed_added], cur_primary, cur_fallback)
    changed_names = name_set(changed, cur_primary, cur_fallback)
    removed_names = name_set([l for l in removed if l not in renamed_removed], rm_primary, rm_fallback)

    if not (added_names or changed_names or removed_names):
        print("⏭️  Only renames (no catalog change) — skipping.")
        return 0

    date = heb_date()
    tag = args.tag or diff.get("new_tag", "")
    parts = [f"# עדכון ספריית ספריא (Sefaria)\n", f"**עדכון {date}**\n"]
    if added_names:
        parts.append(f"\n## התווספו הספרים הבאים:\n{bullets(added_names)}\n")
    if changed_names:
        parts.append(f"\n## עודכנו/תוקנו הספרים הבאים:\n{bullets(changed_names)}\n")
    if removed_names:
        parts.append(f"\n## הוסרו הספרים הבאים:\n{bullets(removed_names)}\n")

    repo = os.getenv("GITHUB_REPOSITORY")
    if repo and tag:
        parts.append(f"\n[להורדת העדכון](https://github.com/{repo}/releases/tag/{tag})\n")

    content = "".join(parts)
    print("----- forum post -----")
    print(content)
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
        resp = client.send_post(content, args.topic)
        print(f"✅ Posted to forum topic {args.topic}: {str(resp)[:200]}")
    except Exception as e:  # non-fatal
        print(f"⚠️  Forum post failed (non-fatal): {e}")
    finally:
        try:
            client.logout()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
