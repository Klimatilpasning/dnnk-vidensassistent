#!/usr/bin/env python3
"""Læs manuelt udfyldte resuméer ind i search-index.json — uden API-kald.

Modstykket til manuel_eksport.py: parrer hvert svar i manuel_koe/svar/ med
metadataene i manuel_koe/opgaver.json, bygger entries præcis som det natlige
job ville have gjort, og skriver search-index.json.

  python manuel_import.py           # opdaterer search-index.json lokalt
  python manuel_import.py --push    # committer og pusher også til GitHub

Env:  GITHUB_TOKEN  (valgfri)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

import build_search_index as bsi

# Se manuel_eksport.py: Windows-konsollen er cp1252 og vælter ellers på pile/æøå.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

KOE_DIR = "manuel_koe"
SVAR_DIR = os.path.join(KOE_DIR, "svar")
OPGAVER_FIL = os.path.join(KOE_DIR, "opgaver.json")

PAAKRAEVEDE_META = ("filename", "path", "folder", "is_pdf", "title", "category")


def laes_svar(sti: str) -> tuple[dict | None, str]:
    """Returnerer (svar, fejl). svar er None hvis filen ikke kan bruges."""
    try:
        with open(sti, encoding="utf-8-sig") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        return None, f"ugyldig JSON ({exc})"
    except OSError as exc:
        return None, f"kunne ikke læses ({exc})"

    if not isinstance(data, dict):
        return None, "svaret skal være et JSON-objekt"

    summary = (data.get("summary") or "").strip()
    if not summary:
        return None, "tomt 'summary'-felt (ikke udfyldt endnu)"

    keywords = data.get("keywords") or []
    if not isinstance(keywords, list) or not keywords:
        return None, "'keywords' mangler eller er tom"

    for felt in ("speakers", "places"):
        if felt in data and not isinstance(data[felt], list):
            return None, f"'{felt}' skal være en liste"

    return data, ""


def main() -> int:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--push", action="store_true",
                    help="commit og push search-index.json til GitHub bagefter")
    args = ap.parse_args()

    if not os.path.exists(OPGAVER_FIL):
        print(f"Ingen {OPGAVER_FIL} — kør manuel_eksport.py først.")
        return 1
    with open(OPGAVER_FIL, encoding="utf-8") as f:
        opgaver = json.load(f)

    if os.path.exists("search-index.json"):
        with open("search-index.json", encoding="utf-8-sig") as f:
            index = json.load(f)
    else:
        index = []
    indekseret = {e.get("path") or e["filename"] for e in index} | {e["filename"] for e in index}
    print(f"Indeks: {len(index)} entries · kø: {len(opgaver)} opgaver")

    klar: list[tuple[str, dict, dict]] = []
    afventer = sprunget = 0
    for navn, meta in opgaver.items():
        if any(k not in meta for k in PAAKRAEVEDE_META):
            print(f"  FEJL {navn}: opgaven mangler metadata — eksportér den igen")
            continue
        if meta["path"] in indekseret or meta["filename"] in indekseret:
            sprunget += 1
            continue

        sti = os.path.join(SVAR_DIR, navn + ".json")
        if not os.path.exists(sti):
            afventer += 1
            continue

        svar, fejl = laes_svar(sti)
        if svar is None:
            if "ikke udfyldt" in fejl:
                afventer += 1
            else:
                print(f"  FEJL {navn}: {fejl}")
            continue
        klar.append((navn, meta, svar))

    print(f"  {len(klar)} klar · {afventer} afventer svar · {sprunget} allerede i indekset")
    if not klar:
        print("Intet at importere.")
        return 0

    print("Scraper eksterne ressourcer (vidensbank, klimatilpasning.dk) …")
    ext_resources = bsi.scrape_external_resources()

    for navn, meta, svar in klar:
        meta = dict(meta)
        meta.setdefault("date", None)
        meta.setdefault("source_url", None)
        meta.setdefault("dnnk_url", None)
        meta.setdefault("youtube_id", None)
        meta.setdefault("youtube_url", None)
        meta.setdefault("description", None)
        meta.setdefault("match_confidence", None)
        entry = bsi.build_entry(meta, svar, ext_resources)
        index.append(entry)
        print(f"  + {entry['title'][:70]}")
        opgaver[navn]["importeret"] = True

    print("Filtrerer for generiske ressourcer …")
    bsi.drop_generic_resources(index)

    print("Beregner krydsreferencer mellem webinarer …")
    bsi.compute_related_webinars(index)

    index.sort(key=lambda x: (x.get("folder", ""), x.get("title", "")))
    bsi.save_index(index)
    with open(OPGAVER_FIL, "w", encoding="utf-8") as f:
        json.dump(opgaver, f, ensure_ascii=False, indent=2)

    print(f"\n{len(klar)} entries tilføjet. search-index.json har nu {len(index)} entries.")

    if args.push:
        subprocess.run(["git", "add", "search-index.json", OPGAVER_FIL], check=True)
        subprocess.run(["git", "commit", "-m",
                        f"chore: {len(klar)} manuelle resuméer indlæst [skip ci]"], check=True)
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=True)
        subprocess.run(["git", "push"], check=True)
        print("Pushet til GitHub — siden opdateres om et par minutter.")
    else:
        print("Kør disse for at lægge det op (eller brug --push):")
        print(f"  git add search-index.json {OPGAVER_FIL}")
        print(f'  git commit -m "chore: {len(klar)} manuelle resuméer indlæst [skip ci]"')
        print("  git push")
    return 0


if __name__ == "__main__":
    sys.exit(main())
