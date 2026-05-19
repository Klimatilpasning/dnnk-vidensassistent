#!/usr/bin/env python3
"""
Build search-index.json for DNNK Vidensassistent.

For each transcription:
  - Decodes title and category from filename
  - Matches against dnnk.dk event pages (title, description, date, speakers)
  - Finds YouTube URL via dnnk.dk category pages
  - Generates AI summary + keywords via Claude Haiku
  - Only processes NEW files (skips already-indexed ones)

Run:  python build_search_index.py
Env:  ANTHROPIC_API_KEY  (required)
      GITHUB_TOKEN        (optional but recommended to avoid rate limits)
"""

import anthropic
import json
import os
import re
import time
import requests
from bs4 import BeautifulSoup
from difflib import SequenceMatcher
from urllib.parse import unquote, quote

# ── Config ────────────────────────────────────────────────────────────────────

TRANSCRIPTIONS_REPO = "klimatilpasning/dnnk-transcriptor"
BASE_FOLDER = ".github/workflows/transcritranscriptions"

DNNK_CATEGORY_PAGES = [
    ("Godmorgen med DNNK", "https://www.dnnk.dk/god-morgen-med-dnnk/"),
    ("Tech Talk",          "https://www.dnnk.dk/tech-talks/"),
    ("Jura",               "https://www.dnnk.dk/jura-i-klimatilpasning/"),
    ("Masterclass",        "https://www.dnnk.dk/dnnk-masterclass/"),
    ("Konferencer",        "https://www.dnnk.dk/optagelser-fra-konferencer-og-temadage/"),
    ("Øvrige",             "https://www.dnnk.dk/ovrige-optagelser/"),
]

HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DNNK-indexer/1.0)"}

# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_youtube_id(url: str) -> str | None:
    if not url:
        return None
    m = re.search(
        r"(?:youtube\.com/(?:watch\?v=|embed/)|youtu\.be/)([a-zA-Z0-9_-]{11})",
        url,
    )
    return m.group(1) if m else None


def decode_filename(filename: str) -> str:
    """'Godmorgen_med_DNNK__Aarhus__erfaringer.mp3.txt' → clean title."""
    name = filename
    for ext in (".mp3.txt", ".aac.txt", ".wav.txt", ".txt"):
        if name.endswith(ext):
            name = name[: -len(ext)]
            break
    name = unquote(name)
    name = re.sub(r"_{2,}", ": ", name)   # double underscores → colon
    name = name.replace("_", " ")
    name = re.sub(r"\s+", " ", name).strip()
    return name


def detect_category(filename: str) -> str:
    f = filename.lower()
    if "godmorgen" in f:
        return "Godmorgen med DNNK"
    if "tech" in f and "talk" in f:
        return "Tech Talk"
    if "masterclass" in f:
        return "Masterclass"
    if "jura" in f:
        return "Jura"
    return "Øvrige"


def title_similarity(a: str, b: str) -> float:
    def norm(s):
        s = s.lower()
        s = re.sub(r"[^a-z0-9æøå ]", " ", s)
        return re.sub(r"\s+", " ", s).strip()
    return SequenceMatcher(None, norm(a), norm(b)).ratio()

# ── GitHub ────────────────────────────────────────────────────────────────────

def get_github_headers() -> dict:
    h = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def get_all_transcription_files() -> list[dict]:
    url = (
        f"https://api.github.com/repos/{TRANSCRIPTIONS_REPO}"
        "/git/trees/main?recursive=1"
    )
    resp = requests.get(url, headers=get_github_headers(), timeout=30)
    resp.raise_for_status()
    tree = resp.json().get("tree", [])

    files = []
    for item in tree:
        path = item["path"]
        if (
            path.startswith(BASE_FOLDER)
            and path.endswith(".txt")
            and item["type"] == "blob"
        ):
            filename = os.path.basename(path)
            folder = os.path.basename(os.path.dirname(path))
            safe_path = quote(path, safe="/")
            files.append(
                {
                    "path": path,
                    "filename": filename,
                    "folder": folder,
                    "raw_url": (
                        f"https://raw.githubusercontent.com"
                        f"/{TRANSCRIPTIONS_REPO}/main/{safe_path}"
                    ),
                }
            )
    return files

# ── DNNK scraping ─────────────────────────────────────────────────────────────

def scrape_dnnk_events() -> list[dict]:
    """
    Scrape all DNNK category pages.
    Returns list of {title, category, event_url, youtube_id, youtube_url, date}.
    """
    events = []

    for cat_name, cat_url in DNNK_CATEGORY_PAGES:
        try:
            print(f"  Scraping {cat_url} …")
            resp = requests.get(cat_url, headers=HTTP_HEADERS, timeout=15)
            soup = BeautifulSoup(resp.text, "html.parser")

            # Find all links to event pages
            for a in soup.find_all("a", href=re.compile(r"/event/")):
                event_url = a["href"]
                if not event_url.startswith("http"):
                    event_url = "https://www.dnnk.dk" + event_url

                title = a.get_text(strip=True)
                if not title:
                    continue

                # Look for nearby YouTube link
                parent = a.find_parent(["li", "div", "article", "tr"])
                yt_id = None
                date = None
                if parent:
                    for yt_a in parent.find_all("a", href=re.compile(r"youtu")):
                        yt_id = extract_youtube_id(yt_a["href"])
                        if yt_id:
                            break
                    for yt_i in parent.find_all("iframe", src=re.compile(r"youtu")):
                        yt_id = yt_id or extract_youtube_id(yt_i.get("src", ""))
                    date_el = parent.find(
                        string=re.compile(r"\d{2}\.\d{2}\.\d{4}")
                    )
                    if date_el:
                        m = re.search(r"\d{2}\.\d{2}\.\d{4}", str(date_el))
                        date = m.group(0) if m else None

                events.append(
                    {
                        "title": title,
                        "category": cat_name,
                        "event_url": event_url,
                        "youtube_id": yt_id,
                        "youtube_url": (
                            f"https://youtube.com/watch?v={yt_id}" if yt_id else None
                        ),
                        "date": date,
                    }
                )
            time.sleep(0.5)
        except Exception as exc:
            print(f"  Warning – could not scrape {cat_url}: {exc}")

    print(f"  Found {len(events)} events on dnnk.dk")
    return events


def get_event_description(event_url: str) -> str | None:
    """Fetch invitationstekst from a dnnk.dk/event/... page."""
    if not event_url:
        return None
    try:
        resp = requests.get(event_url, headers=HTTP_HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove boilerplate
        for tag in soup.find_all(["nav", "header", "footer", "script", "style"]):
            tag.decompose()

        # Try common content containers
        for selector in [
            {"class": re.compile(r"entry-content|post-content|event-description", re.I)},
            {"class": re.compile(r"content", re.I)},
        ]:
            el = soup.find("div", **selector)
            if el:
                text = el.get_text(separator=" ", strip=True)
                text = re.sub(r"\s+", " ", text)
                return text[:600].strip()
    except Exception:
        pass
    return None


def find_best_event(title: str, events: list[dict]) -> dict | None:
    best_score = 0.0
    best = None
    for ev in events:
        if not ev.get("title"):
            continue
        score = title_similarity(title, ev["title"])
        if score > best_score:
            best_score = score
            best = ev
    return best if best_score >= 0.45 else None

# ── AI summary ────────────────────────────────────────────────────────────────

def generate_summary(client: anthropic.Anthropic, title: str, content: str) -> dict:
    excerpt = content[:5000]
    for attempt in range(3):
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Webinar: {title}\n\n"
                            f"Transskription (uddrag):\n{excerpt}\n\n"
                            "Svar KUN med valid JSON – ingen forklaring:\n"
                            '{"summary": "2-3 sætninger om indhold og vigtigste pointer (dansk)",\n'
                            ' "keywords": ["nøgleord1", "nøgleord2", ...]}\n\n'
                            "Krav:\n"
                            "- summary: 2-3 sætninger på dansk\n"
                            "- keywords: 8-12 ord (emner, steder, teknologier, metoder, aktører)"
                        ),
                    }
                ],
            )
            raw = resp.content[0].text.strip()
            raw = re.sub(r"^```json\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            return json.loads(raw)
        except anthropic.RateLimitError:
            wait = 60 * (attempt + 1)
            print(f"    Rate limit – venter {wait}s …")
            time.sleep(wait)
        except Exception as exc:
            print(f"    Warning – AI generation failed: {exc}")
            break
    return {"summary": "", "keywords": []}

# ── Main ──────────────────────────────────────────────────────────────────────

def load_existing_index() -> list[dict]:
    rebuild = os.environ.get("REBUILD", "").lower() in ("1", "true", "yes")
    if rebuild:
        print("  REBUILD=true – starter forfra")
        return []
    if os.path.exists("search-index.json"):
        with open("search-index.json", encoding="utf-8") as f:
            return json.load(f)
    return []


def build_index():
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

    print("Loading existing index …")
    existing = load_existing_index()
    existing_map = {e["filename"]: e for e in existing}
    print(f"  {len(existing)} entries already indexed")

    print("Fetching transcription file list from GitHub …")
    all_files = get_all_transcription_files()
    new_files = [f for f in all_files if f["filename"] not in existing_map]
    print(f"  {len(all_files)} total files, {len(new_files)} new")

    if not new_files:
        print("Nothing new to process.")
        return

    print("Scraping dnnk.dk for event metadata …")
    dnnk_events = scrape_dnnk_events()

    index = list(existing)

    for i, file_info in enumerate(new_files):
        filename = file_info["filename"]
        title = decode_filename(filename)
        category = detect_category(filename)

        print(f"[{i+1}/{len(new_files)}] {title[:70]} …")

        # Fetch transcription text
        try:
            resp = requests.get(file_info["raw_url"], timeout=15)
            content = resp.text
        except Exception as exc:
            print(f"  Error fetching content: {exc}")
            continue

        # Match with dnnk.dk event
        matched = find_best_event(title, dnnk_events)
        event_url = youtube_id = youtube_url = description = date = None

        if matched:
            event_url = matched.get("event_url")
            youtube_id = matched.get("youtube_id")
            youtube_url = matched.get("youtube_url")
            date = matched.get("date")
            # Use DNNK title when match is confident — fixes æ/ø/å lost in filename encoding
            match_confidence = title_similarity(title, matched["title"])
            if match_confidence >= 0.55 and matched.get("title"):
                title = matched["title"]
            print(f"  → matched ({match_confidence:.2f}): {matched['title'][:60]}")
            if event_url:
                description = get_event_description(event_url)
                time.sleep(0.5)

        # Fallback: extract YouTube ID directly from transcript
        if not youtube_id:
            yt_m = re.search(
                r"(?:youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{11})", content
            )
            if yt_m:
                youtube_id = yt_m.group(1)
                youtube_url = f"https://youtube.com/watch?v={youtube_id}"

        # Generate AI summary
        print("  → generating summary …")
        ai = generate_summary(client, title, content)

        index.append(
            {
                "filename": filename,
                "path": file_info["path"],
                "folder": file_info["folder"],
                "title": title,
                "category": category,
                "date": date,
                "summary": ai.get("summary", ""),
                "keywords": ai.get("keywords", []),
                "youtube_id": youtube_id,
                "youtube_url": youtube_url,
                "dnnk_url": event_url,
                "description": description,
            }
        )

        time.sleep(0.3)

    # Sort chronologically by folder, then title
    index.sort(key=lambda x: (x.get("folder", ""), x.get("title", "")))

    with open("search-index.json", "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    print(f"\nDone. {len(index)} entries written to search-index.json")


if __name__ == "__main__":
    build_index()
