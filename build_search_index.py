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
import yt_dlp

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

# External resource pages to scrape for cross-references
EXTERNAL_RESOURCE_PAGES = [
    # DNNK egen vidensbank — primær kilde
    ("DNNK Rapporter",         "https://www.dnnk.dk/vidensbank2/",                              "dnnk.dk"),
    ("DNNK Rapporter side 2",  "https://www.dnnk.dk/vidensbank2/page/2/",                       "dnnk.dk"),
    ("DNNK Rapporter side 3",  "https://www.dnnk.dk/vidensbank2/page/3/",                       "dnnk.dk"),
    ("DNNK Rapporter side 4",  "https://www.dnnk.dk/vidensbank2/page/4/",                       "dnnk.dk"),
    ("DNNK Rapporter side 5",  "https://www.dnnk.dk/vidensbank2/page/5/",                       "dnnk.dk"),
    ("DNNK Rapporter side 6",  "https://www.dnnk.dk/vidensbank2/page/6/",                       "dnnk.dk"),
    ("DNNK Horizon & LIFE",    "https://www.dnnk.dk/horizon-og-life/",                          "dnnk.dk"),
    ("DNNK Nyheder",           "https://www.dnnk.dk/nyheder/",                                  "dnnk.dk"),
    # Klimatilpasning.dk — sekundær
    ("Klimatilpasning.dk",     "https://www.klimatilpasning.dk/publikationer/",                 "klimatilpasning.dk"),
    ("Klimatilpasning vejl.",  "https://www.klimatilpasning.dk/kommuner-og-forsyning/proces-og-vejledning/", "klimatilpasning.dk"),
]

# URL-patterns og titler der ALDRIG må optræde som ressource
EXCLUDE_PATTERNS = [
    r"/privatlivspolitik",
    r"/kontakt",
    r"/cookies",
    r"/login",
    r"/medlem",
    r"/wp-admin",
    r"/wp-content",
    r"/wp-login",
    r"/feed",
    r"#",
    r"/page/\d+/?$",     # selve paginerings-links
    r"/\d+-\d+/?$",      # numerisk junk som /8293-2
]
EXCLUDE_TITLE_KEYWORDS = [
    "privatlivspolitik", "cookies", "kontakt os", "log ind",
    "tilmeld", "nyhedsbrev", "om os", "medlemskab",
]

def is_junk_resource(url: str, title: str) -> bool:
    """Filter out navigation/junk URLs and titles"""
    url_low = url.lower()
    title_low = title.lower()
    for pat in EXCLUDE_PATTERNS:
        if re.search(pat, url_low):
            return True
    for kw in EXCLUDE_TITLE_KEYWORDS:
        if kw in title_low:
            return True
    if len(title) < 15 or len(title) > 200:
        return True
    return False

def detect_resource_type(url: str, title: str) -> str:
    """Detect what type of resource this is for icon display"""
    u = url.lower(); t = title.lower()
    if any(k in u for k in ["/horizon", "/life", "interreg"]): return "🌐 EU-projekt"
    if any(k in t for k in ["håndbog", "haandbog", "guide", "vejledning"]): return "📋 Vejledning"
    if any(k in t for k in ["værktøj", "vaerktoj", "atlas", "kort"]):       return "🔧 Værktøj"
    if any(k in t for k in ["case", "kommune", "projekt"]):                 return "📍 Case"
    if any(k in t for k in ["lov", "bekendtg", "direktiv", "paragraf"]):    return "⚖️ Lovgivning"
    if any(k in t for k in ["rapport", "hvidbog", "white paper", "analyse"]): return "📊 Rapport"
    return "📄 Dokument"

HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DNNK-indexer/1.0)"}

DNNK_YOUTUBE_CHANNEL = "https://www.youtube.com/@dnnk-detnationalenetvrkfor946/videos"

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
    """
    Robust similarity that handles missing æ/ø/å in filenames.
    Uses three strategies and returns the best score:
    1. Standard normalization
    2. ASCII normalization (æ→ae, ø→oe, å→aa) + remove spaces
    3. Consonant-only comparison (most robust against missing vowels)
    """
    def norm_std(s):
        s = s.lower()
        s = re.sub(r"[^a-z0-9æøå ]", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    def norm_ascii(s):
        s = s.lower()
        s = s.replace("æ", "ae").replace("ø", "oe").replace("å", "aa")
        return re.sub(r"[^a-z0-9]", "", s)

    def consonants(s):
        s = norm_ascii(s)
        return "".join(c for c in s if c.isalpha() and c not in "aeiou")

    sim1 = SequenceMatcher(None, norm_std(a), norm_std(b)).ratio()
    sim2 = SequenceMatcher(None, norm_ascii(a), norm_ascii(b)).ratio()
    sim3 = SequenceMatcher(None, consonants(a), consonants(b)).ratio()
    return max(sim1, sim2, sim3)

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

def fetch_youtube_channel() -> list[dict]:
    """
    Fetch all videos from DNNK's YouTube channel using yt-dlp.
    Returns list of {title, youtube_id, youtube_url}.
    """
    try:
        print("  Henter videoer fra DNNK YouTube-kanal …")
        ydl_opts = {
            "quiet": True,
            "extract_flat": True,
            "playlist_items": "1-500",
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(DNNK_YOUTUBE_CHANNEL, download=False)
            videos = []
            for entry in (info or {}).get("entries", []):
                vid_id = entry.get("id", "")
                title  = entry.get("title", "")
                if vid_id and title:
                    raw_date = entry.get("upload_date", "") or ""
                upload_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}" if len(raw_date) == 8 else None
                videos.append({
                        "title":       title,
                        "youtube_id":  vid_id,
                        "youtube_url": f"https://youtube.com/watch?v={vid_id}",
                        "upload_date": upload_date,
                    })
            print(f"  Fandt {len(videos)} videoer på YouTube-kanalen")
            return videos
    except Exception as exc:
        print(f"  Warning – YouTube-kanal ikke tilgængelig: {exc}")
        return []

def scrape_dnnk_events() -> list[dict]:
    """
    Scrape all DNNK category pages.
    dnnk.dk lists webinars directly with YouTube links and dates —
    there are no separate /event/ subpages.
    Returns list of {title, category, event_url, youtube_id, youtube_url, date}.
    """
    events = []

    for cat_name, cat_url in DNNK_CATEGORY_PAGES:
        try:
            print(f"  Scraping {cat_url} …")
            resp = requests.get(cat_url, headers=HTTP_HEADERS, timeout=15)
            soup = BeautifulSoup(resp.text, "html.parser")

            # Remove boilerplate
            for tag in soup.find_all(["nav", "header", "footer", "script", "style"]):
                tag.decompose()

            # Strategy: find every YouTube link, then extract title and date from context
            seen_yt = set()
            for yt_a in soup.find_all("a", href=re.compile(r"youtu")):
                yt_id = extract_youtube_id(yt_a["href"])
                if not yt_id or yt_id in seen_yt:
                    continue
                seen_yt.add(yt_id)

                # Walk up to find a container with a title
                title = ""
                date = None
                parent = yt_a.find_parent(["li", "tr", "article", "div", "section"])
                for _ in range(4):  # max 4 levels up
                    if not parent:
                        break
                    # Look for heading or bold text as title
                    for tag in ["h1", "h2", "h3", "h4", "strong", "b"]:
                        el = parent.find(tag)
                        if el:
                            t = el.get_text(strip=True)
                            if len(t) > 8 and t != yt_a.get_text(strip=True):
                                title = t
                                break
                    # Look for date in DD.MM.YYYY format
                    if not date:
                        m = re.search(r"\d{2}\.\d{2}\.\d{4}", parent.get_text())
                        if m:
                            raw = m.group(0)
                            parts = raw.split(".")
                            date = f"{parts[2]}-{parts[1]}-{parts[0]}"
                    if title:
                        break
                    parent = parent.find_parent(["li", "tr", "article", "div", "section"])

                # Fallback: use link text if no heading found
                if not title:
                    title = yt_a.get_text(strip=True)
                if not title or len(title) < 5:
                    continue

                events.append({
                    "title":       title,
                    "category":    cat_name,
                    "event_url":   cat_url,   # no individual event page — use category URL
                    "youtube_id":  yt_id,
                    "youtube_url": f"https://youtube.com/watch?v={yt_id}",
                    "date":        date,
                })

            # Also check iframes (embedded players)
            for iframe in soup.find_all("iframe", src=re.compile(r"youtu")):
                yt_id = extract_youtube_id(iframe.get("src", ""))
                if not yt_id or yt_id in seen_yt:
                    continue
                seen_yt.add(yt_id)
                parent = iframe.find_parent(["li", "tr", "article", "div"])
                title = ""
                date = None
                if parent:
                    for tag in ["h1", "h2", "h3", "h4", "strong"]:
                        el = parent.find(tag)
                        if el:
                            title = el.get_text(strip=True)
                            break
                    m = re.search(r"\d{2}\.\d{2}\.\d{4}", parent.get_text())
                    if m:
                        parts = m.group(0).split(".")
                        date = f"{parts[2]}-{parts[1]}-{parts[0]}"
                if not title:
                    continue
                events.append({
                    "title":       title,
                    "category":    cat_name,
                    "event_url":   cat_url,
                    "youtube_id":  yt_id,
                    "youtube_url": f"https://youtube.com/watch?v={yt_id}",
                    "date":        date,
                })

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

def generate_summary(client: anthropic.Anthropic, title: str, content: str, description: str | None = None) -> dict:
    excerpt = content[:5000]
    invitation_block = (
        f"Invitationstekst fra dnnk.dk (verificeret af DNNK, brug som primær kilde):\n{description}\n\n"
        if description else ""
    )
    for attempt in range(3):
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=600,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Webinar titel (kan have manglende æ/ø/å): {title}\n\n"
                            f"{invitation_block}"
                            f"Transskription (uddrag, brug som supplement):\n{excerpt}\n\n"
                            "Svar KUN med valid JSON – ingen forklaring:\n"
                            '{"corrected_title": "Korrekt dansk titel med æ/ø/å",\n'
                            ' "summary": "2-3 sætninger om indhold og vigtigste pointer (dansk)",\n'
                            ' "keywords": ["nøgleord1", "nøgleord2", ...]}\n\n'
                            "Krav:\n"
                            "- corrected_title: Ret manglende eller forkerte æ/ø/å i titlen baseret på kontekst. "
                            "Behold titlen uændret hvis den allerede er korrekt.\n"
                            "- summary: Basér primært på invitationsteksten hvis den findes. "
                            "Supplér med pointer fra transskriptionen som invitationen ikke dækker.\n"
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
        except (anthropic.RateLimitError, anthropic.APIStatusError) as e:
            if hasattr(e, 'status_code') and e.status_code not in (429, 529):
                raise
            wait = 60 * (attempt + 1)
            print(f"    Rate limit – venter {wait}s …")
            time.sleep(wait)
        except Exception as exc:
            print(f"    Warning – AI generation failed: {exc}")
            break
    return {"summary": "", "keywords": []}

# ── External resources ────────────────────────────────────────────────────────

def scrape_external_resources() -> list[dict]:
    """
    Scrape DNNK rapporter og klimatilpasning.dk publikationer/vejledninger.
    Returns list of {title, url, source}.
    """
    resources = []

    for label, base_url, source in EXTERNAL_RESOURCE_PAGES:
        try:
            print(f"  Scraping ekstern kilde: {base_url} …")
            page_url = base_url
            pages_scraped = 0
            while page_url and pages_scraped < 8:
                resp = requests.get(page_url, headers=HTTP_HEADERS, timeout=15)
                soup = BeautifulSoup(resp.text, "html.parser")

                # Find article/resource links — try multiple selectors
                found = 0
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    title = a.get_text(strip=True)
                    if not title or len(title) < 10:
                        continue
                    # Skip navigation/boilerplate links
                    if any(skip in title.lower() for skip in ["log ind", "søg", "menu", "læs mere", "detaljer", "→", "←"]):
                        continue
                    if not href.startswith("http"):
                        href = "https://www." + source + href if href.startswith("/") else href
                    # Only keep links on same domain
                    if source not in href:
                        continue
                    # Skip category/index pages — only individual resources
                    if href.rstrip("/") == base_url.rstrip("/"):
                        continue
                    if is_junk_resource(href, title): continue
                    resources.append({"title": title, "url": href, "source": label, "type": detect_resource_type(href, title)})
                    found += 1

                # Follow pagination
                next_link = soup.find("a", string=re.compile(r"→|næste|next", re.I))
                if next_link and next_link.get("href") and found > 0:
                    next_href = next_link["href"]
                    if not next_href.startswith("http"):
                        next_href = "https://www." + source + next_href
                    page_url = next_href if next_href != page_url else None
                else:
                    page_url = None
                pages_scraped += 1
                time.sleep(0.5)

        except Exception as exc:
            print(f"  Warning – kunne ikke scrape {base_url}: {exc}")

    # Deduplicate by URL
    seen = set()
    unique = []
    for r in resources:
        if r["url"] not in seen:
            seen.add(r["url"])
            unique.append(r)

    print(f"  Fandt {len(unique)} eksterne ressourcer")
    return unique


def keyword_overlap(kw_list: list[str], text: str) -> int:
    """Count how many keywords from kw_list appear in text (case-insensitive)."""
    text_lower = text.lower()
    return sum(1 for kw in kw_list if kw.lower() in text_lower)


def find_related_resources(keywords: list[str], title: str, resources: list[dict], top_n: int = 3) -> list[dict]:
    """Find top_n external resources matching by weighted keyword + title overlap.
    Requires at least 2 overlapping keywords for inclusion."""
    if not keywords or not resources:
        return []
    scored = []
    title_low = title.lower()
    for r in resources:
        kw_score = keyword_overlap(keywords, r["title"]) * 3  # nøgleord vægter 3x
        # Bonus hvis ressourcetitlen indeholder et stort ord fra webinartitlen
        title_words = [w for w in title_low.split() if len(w) > 5]
        title_bonus = sum(1 for w in title_words if w in r["title"].lower())
        total = kw_score + title_bonus
        if kw_score >= 6:   # mindst 2 nøgleords-overlap (2 × 3 = 6)
            scored.append((total, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:top_n]]


def compute_related_webinars(index: list[dict], top_n: int = 3) -> None:
    """
    For each entry in index, find top_n related webinars by keyword overlap.
    Mutates index in place by adding 'related_webinars' key.
    """
    for entry in index:
        kw = entry.get("keywords", [])
        if not kw:
            entry["related_webinars"] = []
            continue
        scored = []
        for other in index:
            if other["filename"] == entry["filename"]:
                continue
            other_kw = other.get("keywords", [])
            other_text = " ".join(other_kw) + " " + other.get("title", "")
            score = keyword_overlap(kw, other_text)
            if score >= 2:  # require at least 2 overlapping keywords
                scored.append((score, {"title": other["title"], "filename": other["filename"], "youtube_url": other.get("youtube_url"), "dnnk_url": other.get("dnnk_url")}))
        scored.sort(key=lambda x: -x[0])
        entry["related_webinars"] = [r for _, r in scored[:top_n]]


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

    print("Henter DNNK YouTube-kanal …")
    youtube_videos = fetch_youtube_channel()

    print("Scraping dnnk.dk for event metadata …")
    dnnk_events = scrape_dnnk_events()

    print("Scraping eksterne ressourcer (vidensbank, klimatilpasning.dk) …")
    ext_resources = scrape_external_resources()

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
            if match_confidence >= 0.45 and matched.get("title"):
                title = matched["title"]
            print(f"  → matched ({match_confidence:.2f}): {matched['title'][:60]}")
            if event_url:
                description = get_event_description(event_url)
                time.sleep(0.5)

        # Fallback 1: match against YouTube channel video titles
        if not youtube_id and youtube_videos:
            best_yt_score = 0.0
            best_yt = None
            for vid in youtube_videos:
                score = title_similarity(title, vid["title"])
                if score > best_yt_score:
                    best_yt_score = score
                    best_yt = vid
            if best_yt and best_yt_score >= 0.45:
                youtube_id  = best_yt["youtube_id"]
                youtube_url = best_yt["youtube_url"]
                if not date and best_yt.get("upload_date"):
                    date = best_yt["upload_date"]
                print(f"  → YouTube match ({best_yt_score:.2f}): {best_yt['title'][:60]}")

        # Fallback 2: extract YouTube ID directly from transcript
        if not youtube_id:
            yt_m = re.search(
                r"(?:youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{11})", content
            )
            if yt_m:
                youtube_id = yt_m.group(1)
                youtube_url = f"https://youtube.com/watch?v={youtube_id}"

        # Generate AI summary
        print("  → generating summary …")
        ai = generate_summary(client, title, content, description)
        if ai.get("corrected_title") and len(ai["corrected_title"]) > 5:
            title = ai["corrected_title"]

        related_resources = find_related_resources(ai.get("keywords", []), title, ext_resources)

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
                "related_resources": related_resources,
                "related_webinars": [],  # filled in after all entries are built
            }
        )

        time.sleep(0.3)

        # Gem løbende hver 10. entry så vi ikke mister arbejde ved fejl
        if len(index) % 10 == 0:
            with open("search-index.json", "w", encoding="utf-8") as f:
                json.dump(index, f, ensure_ascii=False, indent=2)

    # Drop ressourcer der matcher 40%+ af alle entries (for generiske til at være nyttige)
    print("Filtrerer for generiske ressourcer …")
    resource_counts = {}
    for entry in index:
        for r in entry.get("related_resources", []):
            resource_counts[r["url"]] = resource_counts.get(r["url"], 0) + 1
    threshold = len(index) * 0.40
    too_common = {url for url, cnt in resource_counts.items() if cnt > threshold}
    if too_common:
        print(f"  Fjerner {len(too_common)} for generiske ressourcer:")
        for url in too_common:
            print(f"    - {url} (matchede {resource_counts[url]} entries)")
    for entry in index:
        entry["related_resources"] = [r for r in entry.get("related_resources", []) if r["url"] not in too_common]

    # Compute related webinars across entire index
    print("Beregner krydsreferencer mellem webinarer …")
    compute_related_webinars(index)

    # Sort chronologically by folder, then title
    index.sort(key=lambda x: (x.get("folder", ""), x.get("title", "")))

    with open("search-index.json", "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    print(f"\nDone. {len(index)} entries written to search-index.json")


if __name__ == "__main__":
    build_index()
