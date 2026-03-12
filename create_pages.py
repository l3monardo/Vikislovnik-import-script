#!/usr/bin/env python3
"""
Interslavic Wiktionary Page Creator

Reads Interslavic words from a Google Sheet and creates Wiktionary pages
on the Wikimedia Incubator at incubator.wikimedia.org/wiki/Wt/isv/

Usage:
    python create_pages.py --dry-run --limit 5     # Preview 5 pages without publishing
    python create_pages.py --limit 10               # Create first 10 pages
    python create_pages.py                           # Create all pages
    python create_pages.py --start-from 100          # Skip first 100 rows, create the rest
"""

import argparse
import io
import json
import os
import subprocess
import sys
import time

import pandas as pd
import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

SHEET_ID = "1N79e_yVHDo-d026HljueuKJlAAdeELAiPzdFzdBuKbY"
SHEET_GID = "1987833874"  # 'words' tab
CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
    f"/export?format=csv&gid={SHEET_GID}"
)

WIKI_API = "https://incubator.wikimedia.org/w/api.php"
PAGE_PREFIX = "Wt/isv/"

EDIT_DELAY_SECONDS = 6  # seconds between edits — be kind to Wikimedia servers

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
NODE_SCRIPT = os.path.join(SCRIPT_DIR, "generate_tables.js")
BATCH_SIZE = 200  # words to process per Node.js call

# ---------------------------------------------------------------------------
# PART-OF-SPEECH MAPPING
# ---------------------------------------------------------------------------

# Maps the primary POS abbreviation to its ISV heading name.
# The sheet's partOfSpeech column can contain compound values like
# "v.tr. ipf." — we extract the primary POS and treat the rest as qualifiers.
POS_MAP = {
    "n.":      "Imennik srědnjego roda",        # neuter noun
    "m.anim.": "Živy imennik mužskogo roda",    # animate masculine noun
    "m.":      "Neživy imennik mužskogo roda",  # inanimate masculine noun
    "f.":      "Imennik ženskogo roda",          # feminine noun
    "adj.":    "Pridavnik",                      # adjective
    "adv.":    "Prislovnik",                     # adverb
    "v.":      "Glagol",                         # verb
    "conj.":   "Sveznik",                        # conjunction
    "prep.":   "Prědložnik",                     # preposition
    "intj.":   "Medžumetje",                     # interjection
    "pron.":   "Zaimennik",                      # pronoun
    "num.":    "Čislovnik",                      # numeral
    "prefix":  "Prědrastka",                     # prefix
}

# Sub-qualifier labels (shown as extra info in the article body)
QUALIFIER_MAP = {
    "ipf.":    "nesovršeny vid",    # imperfective
    "pf.":     "sovršeny vid",      # perfective
    "intr.":   "neprěhodny",        # intransitive
    "tr.":     "prěhodny",          # transitive
    "refl.":   "svratny",           # reflexive
    "aux.":    "pomočny",           # auxiliary
    "card.":   "kolikostny",        # cardinal
    "coll.":   "sborny",            # collective
    "fract.":  "ulomkovy",          # fractional
    "subst.":  "substantovany",     # substantivized
    "diff.":   "različajuči",       # differential
    "mult.":   "množiteljny",       # multiplicative
    "ord.":    "poredkovy",         # ordinal
    "pers.":   "osobny",            # personal
    "dem.":    "ukazateljny",       # demonstrative
    "indef.":  "neoznačiteljny",    # indefinite
    "rel.":    "relativny",         # relative
    "poss.":   "prisvojiteljny",    # possessive
    "int.":    "pytateljny",        # interrogative
    "neg.":    "odrěčny",           # negative
    "univ.":   "obobčiteljny",      # universal
    "pl.":     "jedino množina",    # only plural
    "sg.":     "jedino jednina",    # only singular
    "indecl.": "nesklanjajemy",     # indeclinable
}

# Language column → display name in the translations table
LANG_NAMES = {
    "en": "English (Anglijsky)",
    "ru": "Russky",
    "be": "Bělorusky",
    "uk": "Ukrainsky",
    "pl": "Poljsky",
    "cs": "Češsky",
    "sk": "Slovačsky",
    "sl": "Slovensky",
    "hr": "Hrvatsky",
    "sr": "Srbsky",
    "mk": "Makedonsky",
    "bg": "Blgarsky",
    "cu": "Crkověnoslovjansky",
    "de": "Němsky",
    "nl": "Niderlandsky",
    "eo": "Esperanto",
}

# Columns that contain translations (in the order they should appear)
TRANSLATION_COLS = list(LANG_NAMES.keys())


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def parse_pos(raw_pos: str):
    """
    Parse a partOfSpeech string like 'v.tr. ipf.' into:
      - primary POS heading (e.g. 'Glagol')
      - list of qualifier labels (e.g. ['prěhodny', 'nesovršeny vid'])
    """
    if not raw_pos or pd.isna(raw_pos):
        return "Slovo", []

    raw_pos = str(raw_pos).strip()
    tokens = raw_pos.replace("/", " ").split()

    primary_heading = None
    qualifiers = []

    # Try to match the longest token first (e.g. 'm.anim.' before 'm.')
    for token in tokens:
        t = token.strip().rstrip(",").rstrip(";")
        if not t:
            continue
        if primary_heading is None and t in POS_MAP:
            primary_heading = POS_MAP[t]
        elif t in QUALIFIER_MAP:
            qualifiers.append(QUALIFIER_MAP[t])
        elif primary_heading is None:
            # Try prefix matching: 'v.tr.' → check if starts with 'v.'
            for abbr in sorted(POS_MAP.keys(), key=len, reverse=True):
                if t.startswith(abbr.rstrip(".")):
                    primary_heading = POS_MAP[abbr]
                    # The rest might be a qualifier
                    remainder = t[len(abbr):]
                    if remainder and remainder in QUALIFIER_MAP:
                        qualifiers.append(QUALIFIER_MAP[remainder])
                    break

    if primary_heading is None:
        primary_heading = "Slovo"

    return primary_heading, qualifiers


# ---------------------------------------------------------------------------
# NODE.JS BRIDGE — DECLENSION & CONJUGATION
# ---------------------------------------------------------------------------

def generate_tables_batch(rows_data):
    """
    Call generate_tables.js with a batch of words.
    rows_data: list of dicts with keys: isv, addition, pos, type
    Returns: list of dicts with keys: word, tableType, data
    """
    if not rows_data:
        return []

    input_json = json.dumps(rows_data, ensure_ascii=False)
    try:
        result = subprocess.run(
            ["node", NODE_SCRIPT],
            input=input_json,
            capture_output=True,
            text=True,
            cwd=SCRIPT_DIR,
            timeout=120,
        )
        if result.returncode != 0:
            print(f"⚠️  Node.js error: {result.stderr.strip()}")
            return [None] * len(rows_data)
        return json.loads(result.stdout)
    except Exception as e:
        print(f"⚠️  Failed to call generate_tables.js: {e}")
        return [None] * len(rows_data)


def build_noun_declension_table(data, word, qual_str):
    """Format noun declension paradigm as styled Wikitext table."""
    if not data:
        return None
    cases = [
        ("Nominativ", "nom"),
        ("Akuzativ",  "acc"),
        ("Genitiv",   "gen"),
        ("Dativ",     "dat"),
        ("Instrumental", "ins"),
        ("Lokativ",   "loc"),
        ("Vokativ",   "voc"),
    ]
    
    header_qualifiers = f" <span style=\"color:#888;\">{qual_str}</span>" if qual_str else ""
    
    lines = [
        '{| class="wikitable" style="text-align:left; font-size:88%; border-collapse:collapse; border:none; width:60%;"',
        f'|+ style="font-size:105%; letter-spacing:0.08em; padding:10px 0 6px; color:#222; font-weight:normal; text-align:left;" | <span style="font-weight:bold;">{word}</span> &nbsp;·&nbsp;{header_qualifiers}',
        "|-",
        '! style="background:#333; color:#e8e8e8; font-weight:normal; font-size:80%; letter-spacing:0.06em; border:1px solid #333; padding:6px 12px; width:34%;" | Padež',
        '! style="background:#333; color:#e8e8e8; font-weight:normal; font-size:80%; letter-spacing:0.06em; border:1px solid #333; padding:6px 12px; width:33%;" | Jednina',
        '! style="background:#333; color:#e8e8e8; font-weight:normal; font-size:80%; letter-spacing:0.06em; border:1px solid #333; padding:6px 12px; width:33%;" | Množina',
    ]
    for label, key in cases:
        vals = data.get(key)
        if not vals:
            continue
        sg = vals[0] if vals[0] else "—"
        pl = vals[1] if len(vals) > 1 and vals[1] else "—"
        lines.append("|-")
        lines.append(f'! style="background:#fafafa; color:#555; font-weight:normal; font-size:82%; border:1px solid #e8e8e8; padding:5px 12px;" | {label}')
        lines.append(f'| style="border:1px solid #e8e8e8; padding:5px 12px; color:#222;" | {sg}')
        lines.append(f'| style="border:1px solid #e8e8e8; padding:5px 12px; color:#222;" | {pl}')
    lines.append("|}")
    return "\n".join(lines)


def build_adjective_declension_table(data, word, qual_str):
    """Format adjective declension paradigm as styled Wikitext table."""
    if not data:
        return None
    sg = data.get("singular", {})
    pl = data.get("plural", {})
    cases = [
        ("Nominativ", "nom"),
        ("Akuzativ",  "acc"),
        ("Genitiv",   "gen"),
        ("Dativ",     "dat"),
        ("Instrumental", "ins"),
        ("Lokativ",   "loc"),
    ]
    
    header_qualifiers = f" <span style=\"color:#888;\">{qual_str}</span>" if qual_str else ""
    
    lines = [
        '{| class="wikitable" style="text-align:left; font-size:88%; border-collapse:collapse; border:none; width:80%;"',
        f'|+ style="font-size:105%; letter-spacing:0.08em; padding:10px 0 6px; color:#222; font-weight:normal; text-align:left;" | <span style="font-weight:bold;">{word}</span> &nbsp;·&nbsp;{header_qualifiers}',
        "|-",
        '! style="background:#333; color:#e8e8e8; font-weight:normal; font-size:80%; letter-spacing:0.06em; border:1px solid #333; padding:6px 12px; width:25%;" | Padež',
        '! style="background:#333; color:#e8e8e8; font-weight:normal; font-size:80%; letter-spacing:0.06em; border:1px solid #333; padding:6px 12px; width:40%;" | Jednina (m./n./ž.)',
        '! style="background:#333; color:#e8e8e8; font-weight:normal; font-size:80%; letter-spacing:0.06em; border:1px solid #333; padding:6px 12px; width:35%;" | Množina (m.anim./ostale)',
    ]
    for label, key in cases:
        sg_vals = sg.get(key, [])
        pl_vals = pl.get(key, [])
        sg_str = " / ".join(sg_vals) if sg_vals else "—"
        pl_str = " / ".join(pl_vals) if pl_vals else "—"
        lines.append("|-")
        lines.append(f'! style="background:#fafafa; color:#555; font-weight:normal; font-size:82%; border:1px solid #e8e8e8; padding:5px 12px;" | {label}')
        lines.append(f'| style="border:1px solid #e8e8e8; padding:5px 12px; color:#222;" | {sg_str}')
        lines.append(f'| style="border:1px solid #e8e8e8; padding:5px 12px; color:#222;" | {pl_str}')
    lines.append("|}")
    return "\n".join(lines)


def build_verb_conjugation_table(data, word, qual_str):
    """Format verb conjugation paradigm as Wikitext table."""
    if not data:
        return None
    
    # Helper to safely get the correct person forms from array
    def get_form(arr, idx):
        return arr[idx] if len(arr) > idx and arr[idx] else "—"

    # For compound tenses (perfect, pluperfect, conditional), the array has 9 items:
    # 0=ja, 1=ty, 2=on-m, 3=on-f, 4=on-n, 5=my, 6=vy, 7=oni, 8=empty
    def get_3p_sg(arr):
        if len(arr) >= 5 and arr[2]:
            return f"{arr[2]}<br/>{arr[3]}<br/>{arr[4]}"
        elif len(arr) > 2 and arr[2]:
            return arr[2]
        return "—"
        
    def get_3p_pl(arr):
        if len(arr) >= 8 and arr[7]:
            return arr[7]
        return "—"

    present = data.get("present", [])
    imperfect = data.get("imperfect", [])
    future = data.get("future", [])
    perfect = data.get("perfect", [])
    pluperfect = data.get("pluperfect", [])
    conditional = data.get("conditional", [])

    inf = data.get("infinitive", "")
    imperative = data.get("imperative", "")
    prap = data.get("prap", "")
    prpp = data.get("prpp", "")
    pfap = data.get("pfap", "")
    pfpp = data.get("pfpp", "")
    gerund = data.get("gerund", "")

    header_qualifiers = f" <span style=\"color:#888;\">{qual_str}</span>" if qual_str else ""

    sections = []
    
    # Main conjugation table
    table = f"""{{| class="wikitable" style="text-align:center; font-size:88%; border-collapse:collapse; border:none; width:100%;"
|+ style="font-size:105%; letter-spacing:0.08em; padding:10px 0 6px; color:#222; font-weight:normal;" | <span style="font-weight:bold;">{word}</span> &nbsp;·&nbsp;{header_qualifiers}
|-
! style="background:#f7f7f7; color:#aaa; font-weight:normal; font-size:80%; letter-spacing:0.12em; text-transform:uppercase; border:1px solid #e8e8e8; padding:7px 10px;" rowspan="2" | &nbsp;
! colspan="6" style="background:#222; color:#fff; font-weight:normal; letter-spacing:0.06em; border:1px solid #222; padding:7px 14px;" | Vrěme
|-
! style="background:#333; color:#e8e8e8; font-weight:normal; font-size:80%; letter-spacing:0.06em; border:1px solid #333; padding:6px 10px;" | Nastojěče<br/><small style="color:#aaa;">present</small>
! style="background:#333; color:#e8e8e8; font-weight:normal; font-size:80%; letter-spacing:0.06em; border:1px solid #333; padding:6px 10px;" | Prosto minulo<br/><small style="color:#aaa;">simple past</small>
! style="background:#333; color:#e8e8e8; font-weight:normal; font-size:80%; letter-spacing:0.06em; border:1px solid #333; padding:6px 10px;" | Buduče<br/><small style="color:#aaa;">future</small>
! style="background:#666; color:#e8e8e8; font-weight:normal; font-size:80%; letter-spacing:0.06em; border:1px solid #666; padding:6px 10px;" | Perfekt<br/><small style="color:#bbb;">perfect</small>
! style="background:#666; color:#e8e8e8; font-weight:normal; font-size:80%; letter-spacing:0.06em; border:1px solid #666; padding:6px 10px;" | Pluskvamperfekt<br/><small style="color:#bbb;">pluperfect</small>
! style="background:#666; color:#e8e8e8; font-weight:normal; font-size:80%; letter-spacing:0.06em; border:1px solid #666; padding:6px 10px;" | Kondicional<br/><small style="color:#bbb;">conditional</small>
|-
! style="background:#f0f0f0; color:#999; font-weight:normal; font-size:75%; letter-spacing:0.14em; text-transform:uppercase; border:1px solid #e0e0e0; padding:4px;" colspan="7" | jednina
|-
! style="background:#fafafa; color:#444; font-weight:normal; font-size:82%; border:1px solid #e8e8e8; padding:5px 10px;" | 1. ''ja''
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#222;" | {get_form(present, 0)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#222;" | {get_form(imperfect, 0)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#222;" | {get_form(future, 0)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#555;" | {get_form(perfect, 0)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#555;" | {get_form(pluperfect, 0)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#555;" | {get_form(conditional, 0)}
|-
! style="background:#fafafa; color:#444; font-weight:normal; font-size:82%; border:1px solid #e8e8e8; padding:5px 10px;" | 2. ''ty''
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#222;" | {get_form(present, 1)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#222;" | {get_form(imperfect, 1)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#222;" | {get_form(future, 1)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#555;" | {get_form(perfect, 1)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#555;" | {get_form(pluperfect, 1)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#555;" | {get_form(conditional, 1)}
|-
! style="background:#fafafa; color:#444; font-weight:normal; font-size:82%; border:1px solid #e8e8e8; padding:5px 10px;" | 3. ''on/ona/ono''
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#222;" | {get_form(present, 2)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#222;" | {get_form(imperfect, 2)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#222;" | {get_form(future, 2)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#555; line-height:1.6;" | {get_3p_sg(perfect)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#555; line-height:1.6;" | {get_3p_sg(pluperfect)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#555; line-height:1.6;" | {get_3p_sg(conditional)}
|-
! style="background:#f0f0f0; color:#999; font-weight:normal; font-size:75%; letter-spacing:0.14em; text-transform:uppercase; border:1px solid #e0e0e0; padding:4px;" colspan="7" | množina
|-
! style="background:#fafafa; color:#444; font-weight:normal; font-size:82%; border:1px solid #e8e8e8; padding:5px 10px;" | 1. ''my''
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#222;" | {get_form(present, 3)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#222;" | {get_form(imperfect, 3)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#222;" | {get_form(future, 3)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#555;" | {get_form(perfect, 5)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#555;" | {get_form(pluperfect, 5)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#555;" | {get_form(conditional, 5)}
|-
! style="background:#fafafa; color:#444; font-weight:normal; font-size:82%; border:1px solid #e8e8e8; padding:5px 10px;" | 2. ''vy''
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#222;" | {get_form(present, 4)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#222;" | {get_form(imperfect, 4)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#222;" | {get_form(future, 4)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#555;" | {get_form(perfect, 6)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#555;" | {get_form(pluperfect, 6)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#555;" | {get_form(conditional, 6)}
|-
! style="background:#fafafa; color:#444; font-weight:normal; font-size:82%; border:1px solid #e8e8e8; padding:5px 10px;" | 3. ''oni/one''
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#222;" | {get_form(present, 5)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#222;" | {get_form(imperfect, 5)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#222;" | {get_form(future, 5)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#555;" | {get_3p_pl(perfect)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#555;" | {get_3p_pl(pluperfect)}
| style="border:1px solid #e8e8e8; padding:5px 10px; color:#555;" | {get_3p_pl(conditional)}
|}}"""
    sections.append(table)
    
    # Participles table
    participles_table = f"""<br/>

{{| class="wikitable" style="text-align:left; font-size:88%; border-collapse:collapse; border:none; width:60%;"
|+ style="font-size:95%; letter-spacing:0.08em; padding:8px 0 4px; color:#888; font-weight:normal; text-transform:uppercase;" | NEFINITNE FORMY I glagolno IME
|-
! style="background:#333; color:#e8e8e8; font-weight:normal; font-size:78%; letter-spacing:0.08em; border:1px solid #333; padding:6px 12px; width:55%;" | Forma
! style="background:#333; color:#e8e8e8; font-weight:normal; font-size:78%; letter-spacing:0.08em; border:1px solid #333; padding:6px 12px;" | &nbsp;
|-
! style="background:#fafafa; color:#555; font-weight:normal; font-size:82%; border:1px solid #e8e8e8; padding:5px 12px;" | Infinitiv
| style="border:1px solid #e8e8e8; padding:5px 12px; color:#222;" | {inf or "—"}
|-
! style="background:#fafafa; color:#555; font-weight:normal; font-size:82%; border:1px solid #e8e8e8; padding:5px 12px;" | Imperativ <small style="color:#aaa;">(imperative)</small>
| style="border:1px solid #e8e8e8; padding:5px 12px; color:#222;" | {imperative or "—"}
|-
! style="background:#fafafa; color:#555; font-weight:normal; font-size:82%; border:1px solid #e8e8e8; padding:5px 12px;" | Nast. aktiv. pričestje <small style="color:#aaa;">(pres. act. participle)</small>
| style="border:1px solid #e8e8e8; padding:5px 12px; color:#222;" | {prap or "—"}
|-
! style="background:#fafafa; color:#555; font-weight:normal; font-size:82%; border:1px solid #e8e8e8; padding:5px 12px;" | Nast. passiv. pričestje <small style="color:#aaa;">(pres. pass. participle)</small>
| style="border:1px solid #e8e8e8; padding:5px 12px; color:#222;" | {prpp or "—"}
|-
! style="background:#fafafa; color:#555; font-weight:normal; font-size:82%; border:1px solid #e8e8e8; padding:5px 12px;" | Prošlo aktiv. pričestje <small style="color:#aaa;">(past act. participle)</small>
| style="border:1px solid #e8e8e8; padding:5px 12px; color:#222;" | {pfap or "—"}
|-
! style="background:#fafafa; color:#555; font-weight:normal; font-size:82%; border:1px solid #e8e8e8; padding:5px 12px;" | Prošlo passiv. pričestje <small style="color:#aaa;">(past pass. participle)</small>
| style="border:1px solid #e8e8e8; padding:5px 12px; color:#222;" | {pfpp or "—"}
|-
! style="background:#fafafa; color:#555; font-weight:normal; font-size:82%; border:1px solid #e8e8e8; padding:5px 12px;" | Glagolno ime <small style="color:#aaa;">(verbal noun)</small>
| style="border:1px solid #e8e8e8; padding:5px 12px; color:#222;" | {gerund or "—"}
|}}"""
    sections.append(participles_table)

    return "\n".join(sections)


def build_numeral_declension_table(data, word, qual_str):
    """Format numeral declension paradigm as Wikitext table."""
    if not data:
        return None
    cases_data = data.get("cases")
    if not cases_data:
        return None
    columns = data.get("columns", ["wordForm"])
    case_order = [
        ("Nominativ", "nom"),
        ("Akuzativ",  "acc"),
        ("Genitiv",   "gen"),
        ("Dativ",     "dat"),
        ("Instrumental", "ins"),
        ("Lokativ",   "loc"),
    ]
    
    header_qualifiers = f" <span style=\"color:#888;\">{qual_str}</span>" if qual_str else ""
    
    col_width = 100 // (len(columns) + 1)
    
    lines = [
        '{| class="wikitable" style="text-align:left; font-size:88%; border-collapse:collapse; border:none; width:60%;"',
        f'|+ style="font-size:105%; letter-spacing:0.08em; padding:10px 0 6px; color:#222; font-weight:normal; text-align:left;" | <span style="font-weight:bold;">{word}</span> &nbsp;·&nbsp;{header_qualifiers}',
        "|-",
        f'! style="background:#333; color:#e8e8e8; font-weight:normal; font-size:80%; letter-spacing:0.06em; border:1px solid #333; padding:6px 12px; width:{col_width}%;" | Padež',
    ]
    
    for c in columns:
        lines.append(f'! style="background:#333; color:#e8e8e8; font-weight:normal; font-size:80%; letter-spacing:0.06em; border:1px solid #333; padding:6px 12px; width:{col_width}%;" | {c}')
        
    for label, key in case_order:
        vals = cases_data.get(key, [])
        if not vals:
            continue
        lines.append("|-")
        lines.append(f'! style="background:#fafafa; color:#555; font-weight:normal; font-size:82%; border:1px solid #e8e8e8; padding:5px 12px;" | {label}')
        for v in vals:
            cell_val = str(v) if v else "—"
            lines.append(f'| style="border:1px solid #e8e8e8; padding:5px 12px; color:#222;" | {cell_val}')
    lines.append("|}")
    return "\n".join(lines)


def build_pronoun_declension_table(data, word, qual_str):
    """Format pronoun declension paradigm as Wikitext table."""
    if not data:
        return None
    # Pronouns can have 'cases', 'casesSingular', 'casesPlural'
    cases_data = data.get("cases")
    cases_sg = data.get("casesSingular")
    cases_pl = data.get("casesPlural")

    if cases_data:
        return build_numeral_declension_table(data, word, qual_str)

    if cases_sg or cases_pl:
        case_order = [
            ("Nominativ", "nom"),
            ("Akuzativ",  "acc"),
            ("Genitiv",   "gen"),
            ("Dativ",     "dat"),
            ("Instrumental", "ins"),
            ("Lokativ",   "loc"),
        ]
        
        header_qualifiers = f" <span style=\"color:#888;\">{qual_str}</span>" if qual_str else ""
        
        sections = []
        if cases_sg:
            cols_sg = ["m.", "n.", "ž."] if any(len(v) >= 3 for v in cases_sg.values()) else ["m./n.", "ž."]
            col_width = 100 // (len(cols_sg) + 1)
            
            lines = [
                '{| class="wikitable" style="text-align:left; font-size:88%; border-collapse:collapse; border:none; width:60%;"',
                f'|+ style="font-size:105%; letter-spacing:0.08em; padding:10px 0 6px; color:#222; font-weight:normal; text-align:left;" | <span style="font-weight:bold;">{word}</span> &nbsp;·&nbsp;{header_qualifiers} <span style="color:#aaa;">(jednina)</span>',
                "|-",
                f'! style="background:#333; color:#e8e8e8; font-weight:normal; font-size:80%; letter-spacing:0.06em; border:1px solid #333; padding:6px 12px; width:{col_width}%;" | Padež',
            ]
            for c in cols_sg:
                lines.append(f'! style="background:#333; color:#e8e8e8; font-weight:normal; font-size:80%; letter-spacing:0.06em; border:1px solid #333; padding:6px 12px; width:{col_width}%;" | {c}')
                
            for label, key in case_order:
                vals = cases_sg.get(key, [])
                if not vals:
                    continue
                lines.append("|-")
                lines.append(f'! style="background:#fafafa; color:#555; font-weight:normal; font-size:82%; border:1px solid #e8e8e8; padding:5px 12px;" | {label}')
                for v in vals:
                    cell_val = str(v) if v else "—"
                    lines.append(f'| style="border:1px solid #e8e8e8; padding:5px 12px; color:#222;" | {cell_val}')
            lines.append("|}")
            sections.append("\n".join(lines))

        if cases_pl:
            cols_pl = list(set(len(v) for v in cases_pl.values() if v))
            max_cols = max(cols_pl) if cols_pl else 1
            col_headers = ["m.anim.", "ostale"] if max_cols >= 2 else ["množina"]
            col_width = 100 // (len(col_headers) + 1)
            
            lines = [
                '{| class="wikitable" style="text-align:left; font-size:88%; border-collapse:collapse; border:none; width:60%;"',
                f'|+ style="font-size:105%; letter-spacing:0.08em; padding:10px 0 6px; color:#222; font-weight:normal; text-align:left;" | <span style="font-weight:bold;">{word}</span>{header_qualifiers} <span style="color:#aaa;">(množina)</span>',
                "|-",
                f'! style="background:#333; color:#e8e8e8; font-weight:normal; font-size:80%; letter-spacing:0.06em; border:1px solid #333; padding:6px 12px; width:{col_width}%;" | Padež',
            ]
            for c in col_headers:
                lines.append(f'! style="background:#333; color:#e8e8e8; font-weight:normal; font-size:80%; letter-spacing:0.06em; border:1px solid #333; padding:6px 12px; width:{col_width}%;" | {c}')
                
            for label, key in case_order:
                vals = cases_pl.get(key, [])
                if not vals:
                    continue
                lines.append("|-")
                lines.append(f'! style="background:#fafafa; color:#555; font-weight:normal; font-size:82%; border:1px solid #e8e8e8; padding:5px 12px;" | {label}')
                for v in vals:
                    cell_val = str(v) if v else "—"
                    lines.append(f'| style="border:1px solid #e8e8e8; padding:5px 12px; color:#222;" | {cell_val}')
            lines.append("|}")
            sections.append("\n".join(lines))

        return "\n\n".join(sections) if sections else None

    return None


def format_grammar_table(table_result, qual_str):
    """Given a result from generate_tables.js, produce Wikitext section string."""
    if not table_result or not table_result.get("data"):
        return None

    table_type = table_result["tableType"]
    data = table_result["data"]
    word = table_result["word"]

    if table_type == "declension_noun":
        table_wikitext = build_noun_declension_table(data, word, qual_str)
        if table_wikitext:
            return f"=== Sklonjenje ===\n{table_wikitext}"
    elif table_type == "declension_adj":
        table_wikitext = build_adjective_declension_table(data, word, qual_str)
        if table_wikitext:
            return f"=== Sklonjenje ===\n{table_wikitext}"
    elif table_type == "declension_numeral":
        table_wikitext = build_numeral_declension_table(data, word, qual_str)
        if table_wikitext:
            return f"=== Sklonjenje ===\n{table_wikitext}"
    elif table_type == "declension_pronoun":
        table_wikitext = build_pronoun_declension_table(data, word, qual_str)
        if table_wikitext:
            return f"=== Sklonjenje ===\n{table_wikitext}"
    elif table_type == "conjugation":
        table_wikitext = build_verb_conjugation_table(data, word, qual_str)
        if table_wikitext:
            return f"=== Spreženje ===\n{table_wikitext}"

    return None


def build_translation_table(row):
    """Build a Wikitext translation table from a spreadsheet row."""
    lines = [
        '{| class="wikitable" style="text-align:left; font-size:88%; border-collapse:collapse; border:none; width:60%;"',
        "|-",
        '! style="background:#333; color:#e8e8e8; font-weight:normal; font-size:80%; letter-spacing:0.06em; border:1px solid #333; padding:6px 12px; width:40%;" | Jezyk',
        '! style="background:#333; color:#e8e8e8; font-weight:normal; font-size:80%; letter-spacing:0.06em; border:1px solid #333; padding:6px 12px; width:60%;" | Prěvod',
    ]

    for col in TRANSLATION_COLS:
        val = row.get(col, "")
        if pd.isna(val) or str(val).strip() == "":
            continue

        val = str(val).strip()
        is_machine = val.startswith("!")
        if is_machine:
            val = val[1:].strip()  # strip the leading '!'
            if not val:
                continue

        lang_name = LANG_NAMES.get(col, col)
        display = val
        if is_machine:
            display += " <small>''(mašinny prěvod)''</small>"

        lines.append("|-")
        lines.append(f'! style="background:#fafafa; color:#555; font-weight:normal; font-size:82%; border:1px solid #e8e8e8; padding:5px 12px;" | {lang_name}')
        lines.append(f'| style="border:1px solid #e8e8e8; padding:5px 12px; color:#222;" | {display}')

    lines.append("|}")
    return "\n".join(lines)


def build_categories(pos_heading):
    """
    Generate multiple hierarchical [[Category:]] tags for a given POS heading.

    For example, 'Neživy imennik mužskogo roda' generates:
      - [[Category:Wt/isv/Medžuslovjansky]]
      - [[Category:Wt/isv/Imennik]]
      - [[Category:Wt/isv/Neživy imennik]]
      - [[Category:Wt/isv/Mužskogo roda]]
      - [[Category:Wt/isv/Neživy imennik mužskogo roda]]
    """
    # Category definitions per POS heading
    CATEGORY_MAP = {
        "Imennik srědnjego roda": [
            "Medžuslovjansky", "Imennik", "Srědnjego roda",
            "Imennik srědnjego roda",
        ],
        "Živy imennik mužskogo roda": [
            "Medžuslovjansky", "Imennik", "Živy imennik",
            "Mužskogo roda", "Živy imennik mužskogo roda",
        ],
        "Neživy imennik mužskogo roda": [
            "Medžuslovjansky", "Imennik", "Neživy imennik",
            "Mužskogo roda", "Neživy imennik mužskogo roda",
        ],
        "Imennik ženskogo roda": [
            "Medžuslovjansky", "Imennik", "Ženskogo roda",
            "Imennik ženskogo roda",
        ],
        "Pridavnik": [
            "Medžuslovjansky", "Pridavnik",
        ],
        "Prislovnik": [
            "Medžuslovjansky", "Prislovnik",
        ],
        "Glagol": [
            "Medžuslovjansky", "Glagol",
        ],
        "Sveznik": [
            "Medžuslovjansky", "Sveznik",
        ],
        "Prědložnik": [
            "Medžuslovjansky", "Prědložnik",
        ],
        "Medžumetje": [
            "Medžuslovjansky", "Medžumetje",
        ],
        "Zaimennik": [
            "Medžuslovjansky", "Zaimennik",
        ],
        "Čislovnik": [
            "Medžuslovjansky", "Čislovnik",
        ],
        "Prědrastka": [
            "Medžuslovjansky", "Prědrastka",
        ],
    }

    cats = CATEGORY_MAP.get(pos_heading, ["Medžuslovjansky", pos_heading])
    # Deduplicate while preserving order
    seen = set()
    unique_cats = []
    for c in cats:
        if c not in seen:
            seen.add(c)
            unique_cats.append(c)
    return [f"[[Category:Wt/isv/{c}]]" for c in unique_cats]


def build_page_content(row, clean_word, grammar_section=None):
    """Generate full Wikitext for one word page."""
    raw_pos = row.get("partOfSpeech", "")
    en_meaning = row.get("en", "")
    addition = row.get("addition", "")
    using_example = row.get("using_example", "")

    pos_heading, qualifiers = parse_pos(raw_pos)

    # Build qualifier string
    qual_str = ""
    if qualifiers:
        qual_str = " ''(" + ", ".join(qualifiers) + ")''"

    sections = []

    # Main language header
    sections.append("== Medžuslovjansky ==")

    # Part of speech section
    sections.append("")
    sections.append("=== Morfologične svojstva ===")
    sections.append(f"''{pos_heading.lower()}{qual_str}''")
    sections.append("")
    sections.append(f"'''{clean_word}'''")

    # Addition info (if any)
    if addition and not pd.isna(addition) and str(addition).strip():
        sections.append(f": <small>{str(addition).strip()}</small>")

    # Usage example (if any)
    if using_example and not pd.isna(using_example) and str(using_example).strip() and str(using_example).strip() != "!":
        sections.append("")
        sections.append("=== Priměr ===")
        sections.append(f": ''{str(using_example).strip()}''")

    # Declension / Conjugation table
    if grammar_section:
        sections.append("")
        sections.append(grammar_section)

    # Translations table
    sections.append("")
    sections.append("=== Prěvody ===")
    sections.append(build_translation_table(row))

    # Categories — use [[Category:]] for Incubator engine compatibility
    if raw_pos and not pd.isna(raw_pos):
        sections.append("")
        categories = build_categories(pos_heading)
        sections.extend(categories)

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# MEDIAWIKI API
# ---------------------------------------------------------------------------

class WikiSession:
    """Minimal MediaWiki API session for creating pages."""

    def __init__(self, api_url, username, password):
        self.api = api_url
        self.session = requests.Session()
        
        # Configure retries to survive transient connection errors
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        retry_strategy = Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS", "POST"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        # Wikimedia requires a descriptive User-Agent for API access
        self.session.headers.update({
            "User-Agent": "ISVWiktionaryBot/1.0 (Interslavic Wiktionary page creator; contact: https://incubator.wikimedia.org/wiki/Wt/isv/)"
        })
        self._login(username, password)

    def _login(self, username, password):
        # Step 1: get login token
        r = self.session.get(self.api, params={
            "action": "query",
            "meta": "tokens",
            "type": "login",
            "format": "json",
        })
        r.raise_for_status()
        login_token = r.json()["query"]["tokens"]["logintoken"]

        # Step 2: log in
        r = self.session.post(self.api, data={
            "action": "login",
            "lgname": username,
            "lgpassword": password,
            "lgtoken": login_token,
            "format": "json",
        })
        r.raise_for_status()
        result = r.json()
        if result.get("login", {}).get("result") != "Success":
            raise RuntimeError(f"Login failed: {result}")
        print(f"✅ Logged in as {username}")

    def _get_csrf_token(self):
        r = self.session.get(self.api, params={
            "action": "query",
            "meta": "tokens",
            "format": "json",
        })
        r.raise_for_status()
        return r.json()["query"]["tokens"]["csrftoken"]

    def page_exists(self, title):
        r = self.session.get(self.api, params={
            "action": "query",
            "titles": title,
            "format": "json",
        })
        r.raise_for_status()
        pages = r.json()["query"]["pages"]
        return "-1" not in pages  # -1 means page doesn't exist

    def create_page(self, title, content, summary="Bot: Created word entry from ISV dictionary", overwrite=False):
        token = self._get_csrf_token()
        data = {
            "action": "edit",
            "title": title,
            "text": content,
            "summary": summary,
            "bot": "1",
            "token": token,
            "format": "json",
        }
        if not overwrite:
            data["createonly"] = "1"  # fail if page already exists
        r = self.session.post(self.api, data=data)
        r.raise_for_status()
        result = r.json()
        if "error" in result:
            return False, result["error"].get("info", str(result["error"]))
        return True, "created"

    def delete_page(self, title, reason="Cleanup: recreating with correct encoding"):
        token = self._get_csrf_token()
        r = self.session.post(self.api, data={
            "action": "delete",
            "title": title,
            "reason": reason,
            "token": token,
            "format": "json",
        })
        r.raise_for_status()
        result = r.json()
        if "error" in result:
            return False, result["error"].get("info", str(result["error"]))
        return True, "deleted"


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def fetch_data():
    """Download the Google Sheet as CSV and return a DataFrame."""
    print(f"📥 Downloading spreadsheet...")
    r = requests.get(CSV_URL)
    r.raise_for_status()
    # Google Sheets declares ISO-8859-1 but actually serves UTF-8.
    # Using r.content.decode('utf-8') ensures correct character handling.
    csv_text = r.content.decode('utf-8')
    df = pd.read_csv(io.StringIO(csv_text))
    print(f"   Found {len(df)} rows")
    return df


def main():
    parser = argparse.ArgumentParser(description="Create ISV Wiktionary pages from Google Sheet")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview generated Wikitext without publishing")
    parser.add_argument("--limit", type=int, default=0,
                        help="Maximum number of pages to create (0 = all)")
    parser.add_argument("--start-from", type=int, default=0,
                        help="Skip this many rows from the beginning")
    parser.add_argument("--output-dir", type=str, default="",
                        help="In dry-run mode, save pages to this directory")
    parser.add_argument("--overwrite", action="store_true",
                        help="Overwrite existing pages instead of skipping them")
    args = parser.parse_args()

    # Load data
    df = fetch_data()

    # Apply offset and limit
    if args.start_from > 0:
        df = df.iloc[args.start_from:]
        print(f"   Skipped first {args.start_from} rows, {len(df)} remaining")
    if args.limit > 0:
        df = df.head(args.limit)
        print(f"   Limited to {len(df)} rows")

    # Filter: must have an isv word
    df = df[df["isv"].notna() & (df["isv"].str.strip() != "")]
    print(f"   {len(df)} rows with valid ISV words")

    # Pre-generate declension/conjugation tables via Node.js
    print(f"📊 Generating declension/conjugation tables...")
    rows_for_node = []
    for idx, row in df.iterrows():
        rows_for_node.append({
            "isv": str(row["isv"]).strip(),
            "addition": str(row.get("addition", "")).strip() if not pd.isna(row.get("addition", "")) else "",
            "pos": str(row.get("partOfSpeech", "")).strip() if not pd.isna(row.get("partOfSpeech", "")) else "",
            "type": str(row.get("type", "")).strip() if not pd.isna(row.get("type", "")) else "",
        })

    # Process in batches
    all_table_results = []
    for i in range(0, len(rows_for_node), BATCH_SIZE):
        batch = rows_for_node[i:i + BATCH_SIZE]
        batch_results = generate_tables_batch(batch)
        all_table_results.extend(batch_results)
        print(f"   Processed {min(i + BATCH_SIZE, len(rows_for_node))}/{len(rows_for_node)} words")

    # Build grammar sections lookup
    grammar_sections = {}
    clean_words = {}
    for i, table_result in enumerate(all_table_results):
        if table_result:
            raw_word = rows_for_node[i]["isv"]
            raw_pos = rows_for_node[i]["pos"]
            _, qualifiers = parse_pos(raw_pos)
            qual_str = " · ".join(qualifiers) if qualifiers else ""
            
            clean_word = table_result.get("word", raw_word)
            clean_words[raw_word] = clean_word
            section = format_grammar_table(table_result, qual_str)
            if section:
                grammar_sections[raw_word] = section
    print(f"   Generated {len(grammar_sections)} grammar tables")

    # Setup wiki session (unless dry run)
    wiki = None
    if not args.dry_run:
        load_dotenv()
        username = os.getenv("WIKI_USERNAME")
        password = os.getenv("WIKI_PASSWORD")
        if not username or not password:
            print("❌ Error: Set WIKI_USERNAME and WIKI_PASSWORD in .env file")
            print("   See .env.example for the format")
            sys.exit(1)
        wiki = WikiSession(WIKI_API, username, password)

    # Output dir for dry-run
    if args.dry_run and args.output_dir:
        os.makedirs(args.output_dir, exist_ok=True)

    # Process rows
    created = 0
    skipped = 0
    errors = 0

    for idx, row in df.iterrows():
        raw_word = str(row["isv"]).strip()
        clean_word = clean_words.get(raw_word, raw_word)
        page_title = PAGE_PREFIX + clean_word
        grammar = grammar_sections.get(raw_word)
        content = build_page_content(row, clean_word, grammar_section=grammar)

        if args.dry_run:
            print(f"\n{'='*60}")
            print(f"📄 Page: {page_title}")
            print(f"{'='*60}")
            if args.output_dir:
                safe_name = clean_word.replace("/", "_").replace(" ", "_")
                filepath = os.path.join(args.output_dir, f"{safe_name}.wiki")
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)
                print(f"   Saved to {filepath}")
            else:
                print(content)
            created += 1
            continue

        # Live mode — create on wiki
        if wiki.page_exists(page_title) and not args.overwrite:
            print(f"⏭️  Skipped (exists): {page_title}")
            skipped += 1
            continue

        success, msg = wiki.create_page(page_title, content, overwrite=args.overwrite)
        if success:
            print(f"✅ Created: {page_title}")
            created += 1
        else:
            print(f"❌ Error creating {page_title}: {msg}")
            errors += 1

        # Rate limiting
        time.sleep(EDIT_DELAY_SECONDS)

    # Summary
    print(f"\n{'='*60}")
    print(f"📊 Summary:")
    print(f"   Created: {created}")
    print(f"   Skipped: {skipped}")
    print(f"   Errors:  {errors}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
