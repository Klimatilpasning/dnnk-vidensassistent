#!/usr/bin/env python3
"""Eksportér nye transskriptioner til manuel resumé-behandling — uden API-kald.

Finder de filer i transskriptions-repoet der endnu ikke har en entry i
search-index.json, laver al matching (titel, kategori, dato, dnnk.dk-event,
YouTube-link) og skriver én opgavefil pr. webinar i manuel_koe/tekster/.

Arbejdsgang:
  1. python manuel_eksport.py
  2. Åbn en fil i manuel_koe/tekster/, indsæt hele indholdet i en Claude-samtale
  3. Gem svaret (JSON) i manuel_koe/svar/<samme navn>.json
  4. python manuel_import.py

Env:  GITHUB_TOKEN  (valgfri, men anbefalet — undgår GitHub rate limit)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata

import requests

import build_search_index as bsi

# Scriptet køres lokalt på Windows, hvor konsollen er cp1252 og vælter på de
# pile og æ/ø/å som build_search_index printer (i CI er stdout UTF-8).
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

KOE_DIR = "manuel_koe"
TEKST_DIR = os.path.join(KOE_DIR, "tekster")
SVAR_DIR = os.path.join(KOE_DIR, "svar")
OPGAVER_FIL = os.path.join(KOE_DIR, "opgaver.json")

SVAR_SKABELON = {
    "corrected_title": "",
    "summary": "",
    "keywords": [],
    "speakers": [],
    "places": [],
}


def slug(title: str) -> str:
    s = title.replace("æ", "ae").replace("ø", "oe").replace("å", "aa")
    s = s.replace("Æ", "Ae").replace("Ø", "Oe").replace("Å", "Aa")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")
    return (s[:40] or "uden_titel").lower()


def load_index() -> list[dict]:
    if not os.path.exists("search-index.json"):
        return []
    with open("search-index.json", encoding="utf-8-sig") as f:
        return json.load(f)


def load_opgaver() -> dict:
    if not os.path.exists(OPGAVER_FIL):
        return {}
    with open(OPGAVER_FIL, encoding="utf-8") as f:
        return json.load(f)


def opgave_tekst(navn: str, meta: dict) -> str:
    """Den fil brugeren kopierer ind i en Claude-samtale."""
    prompt = bsi.summary_prompt(
        meta["title"], meta["content"], meta["description"],
        doc_type="pdf" if meta["is_pdf"] else "webinar",
    )
    return (
        f"# Opgave: {navn}\n\n"
        f"Kategori: {meta['category']} · Dato: {meta['date'] or 'ukendt'} · "
        f"Type: {'PDF-dokument' if meta['is_pdf'] else 'webinar'}\n"
        f"Kilde: {meta['dnnk_url'] or meta['source_url'] or meta['youtube_url'] or '—'}\n\n"
        "Kopiér ALT under stregen ind i en Claude-samtale. Gem svaret (kun JSON-objektet) som\n"
        f"`{os.path.join(SVAR_DIR, navn + '.json')}` og kør derefter `python manuel_import.py`.\n\n"
        "---\n\n"
        f"{prompt}\n"
    )


def main() -> int:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--antal", type=int, default=0,
                    help="eksportér højst N filer (0 = alle nye)")
    ap.add_argument("--alle", action="store_true",
                    help="eksportér også opgaver der allerede har et svar liggende")
    args = ap.parse_args()

    os.makedirs(TEKST_DIR, exist_ok=True)
    os.makedirs(SVAR_DIR, exist_ok=True)

    index = load_index()
    indekseret = {e.get("path") or e["filename"] for e in index} | {e["filename"] for e in index}
    print(f"Indeks: {len(index)} entries")

    print("Henter filliste fra GitHub …")
    alle_filer = bsi.get_all_transcription_files()
    nye = [f for f in alle_filer
           if f["path"] not in indekseret and f["filename"] not in indekseret]
    print(f"  {len(alle_filer)} filer i alt, {len(nye)} uden resumé")

    if not alle_filer:
        print("FEJL: ingen filer hentet fra GitHub — tjek netværk/GITHUB_TOKEN.")
        return 1
    if not nye:
        print("Intet at eksportere — alle filer har allerede en entry i indekset.")
        return 0

    opgaver = load_opgaver()
    if not args.alle:
        besvaret = {navn for navn in opgaver
                    if os.path.exists(os.path.join(SVAR_DIR, navn + ".json"))
                    and os.path.getsize(os.path.join(SVAR_DIR, navn + ".json")) > 0}
        kendte_stier = {o["path"] for navn, o in opgaver.items() if navn in besvaret}
        nye = [f for f in nye if f["path"] not in kendte_stier]

    if args.antal:
        nye = nye[:args.antal]

    print("Scraper dnnk.dk for event-metadata …")
    dnnk_events = bsi.scrape_dnnk_events()
    if not dnnk_events:
        print("  ADVARSEL: 0 events fra dnnk.dk — titler/datoer bliver dårligere.")

    print("Henter DNNK YouTube-kanal …")
    youtube_videos = bsi.fetch_youtube_channel()
    if not youtube_videos:
        print("  ADVARSEL: ingen YouTube-videoer hentet (mangler yt-dlp?) — "
              "entries kan ende uden 'se webinaret'-link. Installér med: pip install yt-dlp")

    skrevet = 0
    for i, file_info in enumerate(nye):
        filename = file_info["filename"]
        print(f"[{i+1}/{len(nye)}] {bsi.decode_filename(filename)[:70]} …")

        try:
            resp = requests.get(file_info["raw_url"], timeout=15)
            resp.raise_for_status()
            resp.encoding = "utf-8"
            content = resp.text
        except Exception as exc:
            print(f"  Fejl ved hentning: {exc}")
            continue

        meta = bsi.prepare_entry_metadata(file_info, content, dnnk_events, youtube_videos)
        navn = f"{len(opgaver) + skrevet + 1:03d}_{slug(meta['title'])}"

        with open(os.path.join(TEKST_DIR, navn + ".md"), "w", encoding="utf-8") as f:
            f.write(opgave_tekst(navn, meta))

        svar_sti = os.path.join(SVAR_DIR, navn + ".json")
        if not os.path.exists(svar_sti):
            with open(svar_sti, "w", encoding="utf-8") as f:
                json.dump(SVAR_SKABELON, f, ensure_ascii=False, indent=2)

        # content gemmes ikke i manifestet — teksten står i opgavefilen, og
        # manifestet skal kunne læses som et overblik over køen
        opgaver[navn] = {k: v for k, v in meta.items() if k != "content"}
        skrevet += 1

    with open(OPGAVER_FIL, "w", encoding="utf-8") as f:
        json.dump(opgaver, f, ensure_ascii=False, indent=2)

    print(f"\n{skrevet} opgaver skrevet til {TEKST_DIR}/")
    print(f"Udfyld svarene i {SVAR_DIR}/ og kør: python manuel_import.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
