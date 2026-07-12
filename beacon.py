"""
Beacon
Local research IDE: file tree over any folder, tabbed editor with syntax
highlighting, embedded PDF reading, LaTeX compile with live preview, git
awareness per folder (GitHub, Overleaf, or local only), search in files,
quick open, a persistent PowerShell terminal on the project virtualenv,
and a Claude panel that sees the open file and the paper being read.

Stdlib only. The UI is served from workbench.html next to this script and
runs in an Edge app window or the default browser.
"""

import base64
import concurrent.futures
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Never let git block on an interactive terminal credential prompt (the app has
# no TTY): git fails fast instead of hanging. The GUI credential manager still
# works for the user's own commands in the real terminal.
os.environ["GIT_TERMINAL_PROMPT"] = "0"
CONFIG_FILE = os.path.join(SCRIPT_DIR, "workbench_config.json")
HTML_FILE = os.path.join(SCRIPT_DIR, "workbench.html")
PORT = 8347

# Application identity. Beacon is served locally, so the browser labels the
# installed app by its origin (127.0.0.1); there is no signed publisher. These
# constants carry the real author, version, and copyright for the software.
APP_NAME = "Beacon"
APP_VERSION = "1.0.0"
APP_AUTHOR = "Chandan Chaudhary"
APP_YEAR = "2026"

# Public static assets, served without a token because the browser fetches
# icons on its own and they expose no workspace data. favicon.ico is the
# multi-resolution icon the Windows taskbar and title bar use.
PUBLIC_ASSETS = {
    "/logo": ("Logo_icon.png", "image/png"),
    "/logo-64.png": ("logo-64.png", "image/png"),
    "/logo-180.png": ("logo-180.png", "image/png"),
    "/logo-192.png": ("logo-192.png", "image/png"),
    "/logo-512.png": ("logo-512.png", "image/png"),
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/sw.js": ("sw.js", "text/javascript"),
    "/vendor/xterm.js": ("vendor/xterm.js", "text/javascript"),
    "/vendor/xterm.css": ("vendor/xterm.css", "text/css"),
    "/vendor/xterm-addon-fit.js": ("vendor/xterm-addon-fit.js", "text/javascript"),
    "/vendor/marked.min.js": ("vendor/marked.min.js", "text/javascript"),
}

# A real interactive terminal needs a Windows pseudo console (ConPTY). pywinpty
# provides it. When it is not installed the app falls back to the line based
# terminal, so the feature degrades gracefully rather than breaking.
try:
    import winpty
    HAS_PTY = True
except Exception:
    HAS_PTY = False
ORIGIN = f"http://127.0.0.1:{PORT}"
# Only these Host headers are served. Rejecting everything else defeats DNS
# rebinding: a malicious site that repoints its hostname at 127.0.0.1 still
# sends its own domain as Host, so it never receives the page or the token.
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", "127.0.0.1", "localhost"}

# A per-launch secret. It is injected into the served page and required on
# every API and file request. A web page on another origin cannot read our
# HTML, so it cannot learn the token, which closes the cross-origin path that
# would otherwise let any site drive the terminal or compiler on this port.
SESSION_TOKEN = uuid.uuid4().hex

CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# Verified on this machine: --permission-mode manual denies out of scope
# mutating commands while the allowlist admits latexmk and python. Read only
# commands are auto approved by Claude Code itself and cannot be blocked.
CLAUDE_ALLOWED_TOOLS = "Read,Edit,Write,Grep,Glob,Bash(latexmk:*),Bash(python:*),Bash(pdflatex:*),Bash(bibtex:*)"
CLAUDE_MAX_BUDGET = "2.00"

MAX_TEXT_BYTES = 2_000_000
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".idea", ".vscode"}
ARTIFACT_EXTS = (".aux", ".fls", ".fdb_latexmk", ".synctex", ".synctex.gz", ".out",
                 ".bbl", ".blg", ".toc", ".lof", ".lot", ".pyc", ".nav", ".snm",
                 ".vrb", ".xdv", ".run.xml", ".bcf")
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
               ".svg": "image/svg+xml", ".tif": "image/tiff", ".tiff": "image/tiff"}

def default_python():
    """The interpreter that launched the server, which is the right default
    for running the user's code. Swap pythonw for python so console output is
    captured normally."""
    exe = sys.executable or "python"
    if exe.lower().endswith("pythonw.exe"):
        cand = exe[:-len("pythonw.exe")] + "python.exe"
        if os.path.isfile(cand):
            return cand
    return exe


DEFAULT_SETTINGS = {
    "theme": "dark",
    "accent": "clay",
    "compiler": "latexmk",
    "python_path": default_python(),
    "font_size": 12,
    "editor_font": "Cascadia Mono",
    "autosave": False,
    "auto_install": True,
    "ai_model": "claude:fable",
    "openai_key": "",
    "gemini_key": "",
    "xai_key": "",
    "writing_goal": 500,
}
ACCENTS = ("clay", "ocean", "forest", "violet", "gold")
EDITOR_FONTS = ("Cascadia Mono", "Cascadia Code", "Consolas", "JetBrains Mono",
                "Fira Code", "Courier New")

# Citation styles offered by the DOI content negotiation service (CSL names).
CITE_STYLES = {
    "bibtex": "BibTeX",
    "ieee": "IEEE",
    "vancouver": "Vancouver",
    "apa": "APA (7th)",
    "modern-language-association": "MLA",
    "chicago-author-date": "Chicago",
    "harvard-cite-them-right": "Harvard",
    "nature": "Nature",
}
CITE_UA = "ResearchWorkbench/1.0"


def clean_citation_text(text):
    """Crossref metadata and the CSL bibliography service emit HTML: italic
    tags around journal names and entities like &amp;. Strip the tags and
    decode the entities so the citation pastes as plain text."""
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def http_get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": CITE_UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def crossref_authors(doi):
    """Authoritative author list from the publisher's Crossref record. This is
    the ground truth registered with the DOI, so it corrects the occasional
    wrong name that an aggregator's automated disambiguation produces."""
    try:
        data = http_get_json(
            "https://api.crossref.org/works/" + quote(doi) + "?mailto=research-workbench@example.com",
            timeout=8)
    except Exception:
        return None
    names = []
    for a in data.get("message", {}).get("author", []) or []:
        given, family = a.get("given", ""), a.get("family", "")
        n = (given + " " + family).strip() or a.get("name", "")
        if n:
            names.append(clean_citation_text(n))
    return names or None


def enrich_authors_from_crossref(items):
    """Replace each item's author list with the authoritative Crossref list
    where a DOI exists. Runs in parallel with a time budget so the search
    stays responsive; items that do not resolve in time keep their original
    authors rather than blocking the response."""
    targets = [it for it in items if it.get("doi")]
    if not targets:
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(crossref_authors, it["doi"]): it for it in targets}
        done, not_done = concurrent.futures.wait(futs, timeout=9)
        for fut in done:
            it = futs[fut]
            try:
                names = fut.result()
            except Exception:
                names = None
            if names:
                it["authors_list"] = names
                it["authors"] = ", ".join(names[:5]) + (" et al." if len(names) > 5 else "")
                it["authoritative"] = True
        for fut in not_done:
            fut.cancel()


def reconstruct_abstract(inv):
    """OpenAlex returns abstracts as an inverted index (word -> positions).
    Rebuild the running text so the finder can show a snippet."""
    if not inv:
        return ""
    positions = {}
    for word, idxs in inv.items():
        for i in idxs:
            positions[i] = word
    return " ".join(positions[i] for i in sorted(positions))


# OpenAlex work types grouped into the finder's simple filter.
FIND_TYPE_FILTER = {
    "all": "",
    "article": "type:article",
    "book": "type:book|book-chapter|monograph|reference-book",
    "preprint": "type:preprint",
}

# Publisher recognition, and the major power-systems / electrical-engineering
# venues to highlight and boost in the research finder (that is the user's
# focus). IEEE and IET are electrical-engineering societies, so their journals
# rank as major; Elsevier and others count as major only for power/energy
# titles or the named venues below.
EE_PUBLISHERS = {
    "institute of electrical and electronics engineers": "IEEE",
    "ieee": "IEEE",
    "institution of engineering and technology": "IET",
    "elsevier": "Elsevier",
    "mdpi": "MDPI",
    "springer": "Springer",
    "wiley": "Wiley",
    "taylor": "Taylor & Francis",
    "cambridge": "Cambridge",
    "oxford": "Oxford",
}
MAJOR_EE_VENUES = (
    "ieee transactions on power", "ieee transactions on smart grid",
    "ieee transactions on energy", "ieee transactions on sustainable energy",
    "ieee transactions on industrial", "ieee transactions on industry",
    "ieee power", "ieee access", "ieee systems journal", "ieee electrification",
    "ieee open access journal of power", "ieee journal of emerging",
    "applied energy", "electric power systems research",
    "international journal of electrical power", "energy conversion and management",
    "renewable and sustainable energy", "renewable energy", "journal of energy storage",
    "sustainable energy", "electric power components", "energy reports",
    "iet generation", "iet renewable power", "iet power electronics",
    "iet electric power", "iet smart grid", "high voltage",
    "csee journal of power", "journal of modern power systems",
    "protection and control of modern power systems", "energies",
    "electric power", "power system", "power electronics", "smart grid",
)
EE_KEYWORDS = ("power", "energy", "electric", "grid", "renewable", "sustainable", "voltage")


def classify_publisher(venue, host_org):
    blob = (str(host_org) + " " + str(venue)).lower()
    for key, label in EE_PUBLISHERS.items():
        if key in blob:
            return label
    return ""


def is_major_ee(venue, publisher):
    v = (venue or "").lower()
    if publisher in ("IEEE", "IET"):
        return True
    if any(sub in v for sub in MAJOR_EE_VENUES):
        return True
    if publisher == "Elsevier" and any(k in v for k in EE_KEYWORDS):
        return True
    return False


# ---------- multi-source scholarly search ----------

_PREPRINT_HINTS = ("arxiv", "biorxiv", "medrxiv", "chemrxiv", "techrxiv",
                   "ssrn", "preprint", "research square", "authorea", "osf",
                   "hal", "repec working paper")


def is_preprint(it):
    """A record is treated as a preprint when its type or venue marks it as
    one, so peer-reviewed work can be ranked above it."""
    t = (it.get("type") or "").lower()
    if t in ("preprint", "posted-content"):
        return True
    v = (it.get("venue") or "").lower()
    return any(h in v for h in _PREPRINT_HINTS)


def coarse_type(t):
    """Reduce each source's type vocabulary to a small shared set so the Type
    filter behaves the same across OpenAlex, Crossref, and Semantic Scholar."""
    t = (t or "").lower()
    if "book" in t:
        return "book"
    if any(k in t for k in ("preprint", "posted-content", "arxiv")):
        return "preprint"
    if any(k in t for k in ("proceeding", "conference")):
        return "conference"
    if any(k in t for k in ("article", "journal", "paper", "review")):
        return "article"
    return "other"


_STOPWORDS = {"the", "a", "an", "of", "and", "or", "for", "in", "on", "to",
              "with", "by", "from", "as", "at", "is", "are", "using", "based",
              "via", "toward", "towards", "study", "analysis"}


def _tokens(s):
    return [w for w in re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split() if w]


def relevance_score(query, title, authors=""):
    """How well a record answers the query. An exact title match scores
    highest, the query appearing verbatim inside the title is next, and
    otherwise the fraction of query keywords found in the title or author list
    drives the score, so a mixed "title plus author" query still matches. This
    is the dominant ranking signal, so pasting a title surfaces that paper
    first, ahead of more-cited but less-relevant work."""
    q, t = _tokens(query), _tokens(title)
    if not q or not t:
        return 0.0
    qs, ts = " ".join(q), " ".join(t)
    if qs == ts:
        return 120.0
    score = 0.0
    if qs in ts:
        score += 55.0
    haystack = set(t) | set(_tokens(authors))
    qc = [w for w in q if w not in _STOPWORDS] or q
    coverage = sum(1 for w in qc if w in haystack) / len(qc)
    score += coverage * 40.0
    if coverage == 1.0:
        score += 10.0
    return score


def score_item(it, focus, query=""):
    """Overall rank. Query relevance dominates; peer-reviewed work, a citation
    record, and (under the EE focuses) a major power/EE venue add only small
    secondary weight, enough to order results of similar relevance without
    overturning a strong title or keyword match. Relevance order breaks ties
    in the caller."""
    s = relevance_score(query, it.get("title"), it.get("authors"))
    if not is_preprint(it):
        s += 1.0
    if it.get("doi"):
        s += 0.3
    c = it.get("cited_by") or 0
    s += 0.6 if c >= 100 else 0.4 if c >= 20 else 0.2 if c >= 5 else 0.0
    if focus in ("boost", "ee") and it.get("major"):
        s += 1.5
    return s


def _norm_title(t):
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())[:60]


def merge_items(results):
    """Merge per-source result lists, de-duplicating by DOI (or normalized
    title). When the same work appears in more than one source, the richer
    fields (abstract, citations, open-access link) are combined and every
    contributing source is recorded."""
    by_key, order = {}, []
    for source, items in results:
        for idx, it in enumerate(items):
            it["_srcrank"] = min(idx, it.get("_srcrank", 9999))
            srcs = it.get("sources") or set()
            srcs.add(source)
            it["sources"] = srcs
            key = (it.get("doi") or "").lower().strip() or _norm_title(it.get("title"))
            if not key:
                continue
            if key in by_key:
                base = by_key[key]
                base.setdefault("sources", set()).add(source)
                base["_srcrank"] = min(base.get("_srcrank", 9999), idx)
                for f in ("abstract", "oa_url", "venue", "url", "doi"):
                    if not base.get(f) and it.get(f):
                        base[f] = it[f]
                if (it.get("cited_by") or 0) > (base.get("cited_by") or 0):
                    base["cited_by"] = it["cited_by"]
                if it.get("major"):
                    base["major"] = True
                if not base.get("publisher") and it.get("publisher"):
                    base["publisher"] = it["publisher"]
                if not base.get("authors") and it.get("authors"):
                    base["authors"] = it["authors"]
                    base["authors_list"] = it.get("authors_list", [])
            else:
                by_key[key] = it
                order.append(key)
    merged = [by_key[k] for k in order]
    for it in merged:
        it["_rank"] = it.get("_srcrank", 9999)
    return merged


def _openalex_item(w):
    names = [a.get("author", {}).get("display_name", "")
             for a in (w.get("authorships") or []) if a.get("author")]
    names = [n for n in names if n]
    ploc = w.get("primary_location") or {}
    src = ploc.get("source") or {}
    venue = src.get("display_name", "") or ""
    host = src.get("host_organization_name", "") or ""
    oa = (w.get("open_access") or {}).get("oa_url") or (w.get("best_oa_location") or {}).get("pdf_url")
    doi = (w.get("doi") or "").replace("https://doi.org/", "")
    publisher = classify_publisher(venue, host)
    return {
        "doi": doi, "title": w.get("title") or w.get("display_name") or "",
        "authors": ", ".join(names[:5]) + (" et al." if len(names) > 5 else ""),
        "authors_list": names, "year": w.get("publication_year"),
        "venue": venue, "publisher": publisher,
        "major": is_major_ee(venue, publisher), "type": w.get("type", ""),
        "cited_by": w.get("cited_by_count", 0), "oa_url": oa,
        "url": ploc.get("landing_page_url") or (("https://doi.org/" + doi) if doi else oa) or w.get("id"),
        "abstract": (reconstruct_abstract(w.get("abstract_inverted_index")) or "")[:700],
    }


OPENALEX_SELECT = ("id,doi,title,display_name,publication_year,type,cited_by_count,"
                   "authorships,primary_location,best_oa_location,open_access,"
                   "abstract_inverted_index")


def openalex_search(query, wtype):
    url = ("https://api.openalex.org/works?per-page=40&mailto=research-workbench@example.com"
           "&select=" + OPENALEX_SELECT + "&search=" + quote(query))
    tf = FIND_TYPE_FILTER.get(wtype, "")
    if tf:
        url += "&filter=" + quote(tf, safe=":|")
    data = http_get_json(url, timeout=22)
    return [_openalex_item(w) for w in data.get("results", [])]


def _crossref_item(it):
    names = []
    for a in (it.get("author") or []):
        n = ((a.get("given", "") + " " + a.get("family", "")).strip() or a.get("name", ""))
        if n:
            names.append(clean_citation_text(n))
    try:
        year = it.get("issued", {}).get("date-parts", [[None]])[0][0]
    except (IndexError, TypeError):
        year = None
    doi = it.get("DOI", "")
    venue = clean_citation_text((it.get("container-title") or [""])[0])
    publisher = classify_publisher(venue, it.get("publisher", ""))
    return {
        "doi": doi, "title": clean_citation_text((it.get("title") or [""])[0]),
        "authors": ", ".join(names[:5]) + (" et al." if len(names) > 5 else ""),
        "authors_list": names, "year": year, "venue": venue,
        "publisher": publisher, "major": is_major_ee(venue, publisher),
        "type": it.get("type", ""), "cited_by": it.get("is-referenced-by-count", 0),
        "oa_url": None, "url": ("https://doi.org/" + doi) if doi else "",
        "abstract": "", "authoritative": True,
    }


def crossref_search(query, wtype):
    url = ("https://api.crossref.org/works?rows=25&select="
           "DOI,title,author,issued,container-title,type,is-referenced-by-count,publisher"
           "&query=" + quote(query))
    req = urllib.request.Request(url, headers={"User-Agent": CITE_UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    return [_crossref_item(it) for it in data.get("message", {}).get("items", [])]


DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.I)


def find_doi(query):
    m = DOI_RE.search(query or "")
    return m.group(0).rstrip(".,;)]") if m else None


def doi_lookup(doi):
    """Resolve a DOI directly to one record, trying OpenAlex first for its
    richer metadata and falling back to Crossref. Keyword search endpoints do
    not match a raw DOI, so this is what makes pasting a DOI work."""
    try:
        data = http_get_json("https://api.openalex.org/works/doi:" + quote(doi, safe="/")
                             + "?mailto=research-workbench@example.com&select=" + OPENALEX_SELECT,
                             timeout=12)
        if data and data.get("id"):
            it = _openalex_item(data)
            it["_via"] = "OpenAlex"
            return it
    except Exception:
        pass
    try:
        req = urllib.request.Request("https://api.crossref.org/works/" + quote(doi, safe="/"),
                                     headers={"User-Agent": CITE_UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=12) as r:
            msg = json.loads(r.read().decode("utf-8", "replace")).get("message", {})
        if msg and msg.get("DOI"):
            it = _crossref_item(msg)
            it["_via"] = "Crossref"
            return it
    except Exception:
        pass
    return None


def semanticscholar_search(query, wtype):
    fields = ("title,abstract,year,venue,authors,citationCount,publicationTypes,"
              "externalIds,openAccessPdf,publicationVenue")
    url = ("https://api.semanticscholar.org/graph/v1/paper/search?limit=25&fields="
           + fields + "&query=" + quote(query))
    data = http_get_json(url, timeout=18)
    items = []
    for w in (data.get("data") or []):
        ext = w.get("externalIds") or {}
        doi = (ext.get("DOI") or "").strip()
        names = [a.get("name", "") for a in (w.get("authors") or []) if a.get("name")]
        pv = w.get("publicationVenue") or {}
        venue = w.get("venue") or (pv.get("name") if isinstance(pv, dict) else "") or ""
        ptypes = w.get("publicationTypes") or []
        if "ArXiv" in ext or (venue or "").lower().startswith("arxiv"):
            wt = "preprint"
        elif any(p == "Conference" for p in ptypes):
            wt = "conference"
        elif any(p in ("Book", "BookSection") for p in ptypes):
            wt = "book"
        else:
            wt = "article"
        oa = (w.get("openAccessPdf") or {}) or {}
        publisher = classify_publisher(venue, "")
        arxiv = ext.get("ArXiv")
        items.append({
            "doi": doi, "title": w.get("title") or "",
            "authors": ", ".join(names[:5]) + (" et al." if len(names) > 5 else ""),
            "authors_list": names, "year": w.get("year"), "venue": venue,
            "publisher": publisher, "major": is_major_ee(venue, publisher),
            "type": wt, "cited_by": w.get("citationCount", 0), "oa_url": oa.get("url"),
            "url": ("https://doi.org/" + doi) if doi
                   else ("https://arxiv.org/abs/" + arxiv if arxiv else None),
            "abstract": (w.get("abstract") or "")[:700],
        })
    return items


FIND_SOURCES = {
    "openalex": [("OpenAlex", openalex_search)],
    "crossref": [("Crossref", crossref_search)],
    "semanticscholar": [("Semantic Scholar", semanticscholar_search)],
    "all": [("OpenAlex", openalex_search), ("Semantic Scholar", semanticscholar_search),
            ("Crossref", crossref_search)],
}

# Map source metadata to a BibTeX entry type when synthesizing an entry for a
# record that has no DOI (so no citation service can format it).
_BIBTYPE = {
    "article": "article", "journal-article": "article", "book": "book",
    "book-chapter": "incollection", "monograph": "book", "preprint": "misc",
    "dataset": "misc", "dissertation": "phdthesis", "proceedings-article": "inproceedings",
}


def prettify_bibtex(text):
    """The DOI service returns BibTeX as one dense line. Reformat it so the
    entry opens on its own line and every field sits on a separate indented
    line, which is how a .bib file is normally written."""
    text = text.strip()
    m = re.match(r"@(\w+)\s*\{\s*([^,]+),(.*)\}\s*$", text, re.DOTALL)
    if not m:
        return text
    etype, key, body = m.group(1), m.group(2).strip(), m.group(3)
    fields, buf, depth = [], "", 0
    for ch in body:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == "," and depth == 0:
            if buf.strip():
                fields.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        fields.append(buf.strip())
    lines = ["@" + etype + "{" + key + ","]
    for f in fields:
        if "=" in f:
            k, v = f.split("=", 1)
            lines.append("  " + k.strip() + " = " + v.strip() + ",")
        else:
            lines.append("  " + f + ",")
    lines.append("}")
    return "\n".join(lines)


def synth_bibtex(d):
    """Build a BibTeX entry from finder metadata for items lacking a DOI."""
    authors = d.get("authors") or []
    if isinstance(authors, str):
        authors = [a.strip() for a in authors.split(",") if a.strip()]
    year = str(d.get("year") or "")
    first_family = authors[0].split()[-1] if authors and authors[0].split() else "ref"
    key = re.sub(r"[^A-Za-z0-9]", "", first_family) + year or "ref"
    et = _BIBTYPE.get((d.get("type") or "").lower(), "misc")
    lines = ["@" + et + "{" + key + ","]
    lines.append("  title = {" + str(d.get("title", "")) + "},")
    if authors:
        lines.append("  author = {" + " and ".join(authors) + "},")
    if year:
        lines.append("  year = {" + year + "},")
    venue = d.get("venue")
    if venue:
        field = ("booktitle" if et in ("incollection", "inproceedings")
                 else "publisher" if et == "book" else "journal")
        lines.append("  " + field + " = {" + str(venue) + "},")
    if d.get("doi"):
        lines.append("  doi = {" + str(d["doi"]) + "},")
    if d.get("url"):
        lines.append("  url = {" + str(d["url"]) + "},")
    lines.append("}")
    return "\n".join(lines)

# {name} is replaced with the tex file name. All run in the file's folder.
COMPILERS = {
    "latexmk":  'latexmk -g -pdf -interaction=nonstopmode "{name}"',
    "pdflatex": 'pdflatex -interaction=nonstopmode "{name}"',
    "xelatex":  'latexmk -g -xelatex -interaction=nonstopmode "{name}"',
    "lualatex": 'latexmk -g -lualatex -interaction=nonstopmode "{name}"',
}

_lock = threading.Lock()
_cfg_lock = threading.RLock()   # guards all config read-modify-write cycles
_term_lock = threading.Lock()   # serializes terminal creation
_jobs = {}          # job_id -> {"lines": [dict], "done": bool, "rc": int}
_terminals = {}     # root -> {"proc": Popen, "lines": [str], "lock": Lock}
_git_cache = {}     # repo_root -> (timestamp, info)


# ---------- config ----------

def load_config():
    with _cfg_lock:
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except (ValueError, OSError):
            # A torn or corrupt file must not wipe the user's config. Keep the
            # last good copy if one exists so a mid-write crash is recoverable.
            try:
                with open(CONFIG_FILE + ".bak", "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}


def save_config(cfg):
    # Atomic replace so a reader never sees a half-written file, plus a .bak
    # of the prior good copy. Serialized with load_config through _cfg_lock so
    # concurrent handler and job threads cannot lose each other's updates.
    with _cfg_lock:
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(CONFIG_FILE):
            try:
                shutil.copyfile(CONFIG_FILE, CONFIG_FILE + ".bak")
            except OSError:
                pass
        os.replace(tmp, CONFIG_FILE)


def update_config(mutator):
    """Apply mutator(cfg) atomically under the config lock and persist it.
    Returns the saved config. All read-modify-write call sites use this so no
    two threads can clobber each other."""
    with _cfg_lock:
        cfg = load_config()
        result = mutator(cfg)
        save_config(cfg)
        return result if result is not None else cfg


def get_root():
    root = load_config().get("root", "")
    if root and os.path.isdir(root):
        return root
    return ""


def get_settings():
    merged = dict(DEFAULT_SETTINGS)
    merged.update(load_config().get("settings", {}))
    return merged


def job_env():
    """Environment for child jobs with the configured venv first on PATH,
    so `python` in the terminal and in latex shell escapes hits the venv."""
    env = os.environ.copy()
    py = get_settings().get("python_path", "")
    if py and os.path.isfile(py):
        scripts = os.path.dirname(py)
        env["PATH"] = scripts + os.pathsep + env.get("PATH", "")
        env["VIRTUAL_ENV"] = os.path.dirname(scripts)
    return env


# ---------- helpers ----------

def run_cmd(cmd, cwd, timeout=30):
    try:
        result = subprocess.run(
            cmd, cwd=cwd, shell=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=CREATION_FLAGS,
        )
        return result.returncode, (result.stdout or "") + (result.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "Command timed out."
    except OSError as e:
        return -1, str(e)


def path_allowed(path):
    """File APIs may only touch paths inside the configured workspace root.
    Uses commonpath so a drive root like D:\\ (which already ends in a
    separator) is handled correctly and prefix tricks like C:\\rootEVIL do
    not slip through."""
    root = get_root()
    if not root or not path:
        return False
    try:
        rp = os.path.normcase(os.path.realpath(path))
        rr = os.path.normcase(os.path.realpath(root))
        if rp == rr:
            return True
        return os.path.commonpath([rp, rr]) == rr
    except (OSError, ValueError):
        return False


def new_job():
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[job_id] = {"lines": [], "done": False, "rc": None,
                         "proc": None, "cancelled": False}
        if len(_jobs) > 40:
            for done_id in [k for k, v in _jobs.items() if v["done"]][:-20]:
                _jobs.pop(done_id, None)
    return job_id


def cancel_job(job_id):
    """Kill the process tree behind a running job so the user can stop a
    Claude task, a compile, or a run that is misbehaving."""
    with _lock:
        job = _jobs.get(job_id)
        proc = job.get("proc") if job else None
        if job is not None:
            job["cancelled"] = True
    if proc is not None and proc.poll() is None:
        kill_tree(proc)
        return True
    return False


def job_emit(job_id, kind, text):
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job["lines"].append({"kind": kind, "text": text})


def job_done(job_id, rc):
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job["rc"] = rc
            job["done"] = True


def kill_tree(proc):
    if os.name == "nt":
        subprocess.run(
            f"taskkill /T /F /PID {proc.pid}", shell=True,
            capture_output=True, creationflags=CREATION_FLAGS,
        )
    else:
        proc.kill()


def stream_job(job_id, cmd, cwd, timeout=None, stdin_text=None, line_filter=None):
    """Run cmd in a thread, push each output line into the job store."""

    def task():
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd, shell=True, env=job_env(),
                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=CREATION_FLAGS,
            )
        except OSError as e:
            job_emit(job_id, "error", str(e))
            job_done(job_id, -1)
            return
        with _lock:
            j = _jobs.get(job_id)
            if j is not None:
                j["proc"] = proc
                if j.get("cancelled"):
                    # Cancel arrived between new_job and Popen.
                    kill_tree(proc)
        rc = -1
        timed_out = []

        def on_timeout():
            timed_out.append(True)
            kill_tree(proc)

        timer = None
        try:
            # Feed stdin from its own thread. A large prompt can exceed the
            # pipe buffer, and writing it inline before reading stdout would
            # deadlock the child.
            if stdin_text is not None:
                def feed():
                    try:
                        proc.stdin.write(stdin_text)
                        proc.stdin.close()
                    except OSError:
                        pass
                threading.Thread(target=feed, daemon=True).start()
            if timeout:
                timer = threading.Timer(timeout, on_timeout)
                timer.daemon = True
                timer.start()
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                if not line:
                    continue
                # A raising line_filter must never abandon the job, or SSE
                # readers spin forever and the entry never gets evicted.
                try:
                    if line_filter:
                        line_filter(line)
                    else:
                        job_emit(job_id, "err" if line.startswith("!") else "out", line)
                except Exception as e:
                    job_emit(job_id, "error", f"output handler error: {e}")
        except Exception as e:
            job_emit(job_id, "error", str(e))
        finally:
            try:
                rc = proc.wait()
            except Exception:
                rc = -1
            if timer:
                timer.cancel()
            with _lock:
                was_cancelled = _jobs.get(job_id, {}).get("cancelled")
            if was_cancelled:
                job_emit(job_id, "info", "Stopped by user.")
                rc = -1
            elif timed_out:
                job_emit(job_id, "error", f"Timed out after {timeout} s, process tree killed.")
                rc = -1
            job_done(job_id, rc)

    threading.Thread(target=task, daemon=True).start()


def read_head(path, n=4000):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(n)
    except OSError:
        return ""


def resolve_main_tex(tex):
    """A section fragment pulled in by \\input has no \\documentclass and will
    not compile on its own. When the active file is such a fragment, find the
    real document in the workspace and build that instead. Returns
    (path_to_compile, note_or_None)."""
    if "\\documentclass" in read_head(tex):
        return tex, None
    root = get_root()
    if not root:
        return tex, None
    candidates = []
    for fp in walk_workspace(root):
        if not fp.lower().endswith(".tex"):
            continue
        head = read_head(fp)
        if "\\documentclass" in head and "\\begin{document}" in head:
            candidates.append(fp)
    if not candidates:
        return tex, None
    # Prefer a file literally named main.tex, then the shortest path.
    candidates.sort(key=lambda p: (os.path.basename(p).lower() != "main.tex", len(p)))
    chosen = candidates[0]
    note = (f"{os.path.basename(tex)} is a section fragment; compiling the main "
            f"document {os.path.relpath(chosen, root)} instead.")
    return chosen, note


def expand_tex_inputs(tex_path, seen=None, depth=0):
    """Inline the files pulled in by \\input and \\include so a paper split
    across many files counts as one document. Bounded in depth and guarded
    against cycles; only files inside the workspace are followed."""
    if seen is None:
        seen = set()
    real = os.path.normcase(os.path.realpath(tex_path))
    if depth > 15 or real in seen or not os.path.isfile(tex_path):
        return ""
    seen.add(real)
    base = os.path.dirname(tex_path)
    try:
        with open(tex_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    text = re.sub(r"(?<!\\)%.*", "", text)  # drop comments before following inputs

    def repl(m):
        name = m.group(1).strip()
        if not name:
            return " "
        cand = name if name.lower().endswith(".tex") else name + ".tex"
        child = os.path.normpath(os.path.join(base, cand))
        if not path_allowed(child):
            return " "
        return expand_tex_inputs(child, seen, depth + 1)

    return re.sub(r"\\(?:input|include)\s*\{([^}]*)\}", repl, text)


LATEX_SECT_LEVELS = {"part": 0, "chapter": 1, "section": 2, "subsection": 3,
                     "subsubsection": 4, "paragraph": 5, "subparagraph": 6}


def tex_outline(path, root, content=None, seen=None, depth=0):
    r"""Build a section outline for a LaTeX file, descending into \input and
    \include so a main file's outline includes the sections written in its
    section files. Each item carries its own file (workspace-relative) and the
    line within that file, so a click opens the right file at the right line."""
    if seen is None:
        seen = set()
    real = os.path.normcase(os.path.realpath(path))
    if depth > 20 or real in seen:
        return []
    seen.add(real)
    if content is None:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            return []
    base = os.path.dirname(path)
    rel = os.path.relpath(path, root) if path_allowed(path) else os.path.basename(path)
    sect_re = re.compile(r"^\s*\\(part|chapter|section|subsection|subsubsection"
                         r"|paragraph|subparagraph)\*?\s*\{(.+?)\}")
    inp_re = re.compile(r"\\(?:input|include)\s*\{([^}]*)\}")
    items = []
    for i, raw in enumerate(content.split("\n"), 1):
        line = re.sub(r"(?<!\\)%.*", "", raw)
        sm = sect_re.match(line)
        if sm:
            items.append({"file": rel, "line": i, "kind": "§",
                          "level": LATEX_SECT_LEVELS[sm.group(1)],
                          "name": sm.group(2).strip()})
        for im in inp_re.finditer(line):
            name = im.group(1).strip()
            if not name:
                continue
            cand = name if name.lower().endswith(".tex") else name + ".tex"
            child = os.path.normpath(os.path.join(base, cand))
            if path_allowed(child):
                items.extend(tex_outline(child, root, None, seen, depth + 1))
    return items


def latex_word_count(text):
    """Estimate word, character, figure/table, and equation counts of LaTeX
    source. This strips comments, math, and markup with regular expressions
    rather than parsing, so the word figure is an approximation for checking
    a draft against a journal limit, not an exact count."""
    m = re.search(r"\\begin\{document\}(.*)\\end\{document\}", text, re.DOTALL)
    body = m.group(1) if m else text
    body = re.sub(r"(?<!\\)%.*", "", body)
    n_floats = len(re.findall(r"\\begin\{(?:figure|table|wrapfigure|sidewaystable)\*?\}", body))
    n_display = 0
    for env in ("equation", "align", "gather", "multline", "eqnarray",
                "displaymath", "alignat", "flalign"):
        n_display += len(re.findall(r"\\begin\{" + env + r"\*?\}", body))
    strip_envs = ("equation", "align", "aligned", "gather", "multline",
                  "eqnarray", "displaymath", "alignat", "flalign", "math",
                  "figure", "table", "wrapfigure", "sidewaystable", "tabular",
                  "thebibliography", "lstlisting", "verbatim", "tikzpicture",
                  "minted")
    for env in strip_envs:
        body = re.sub(r"\\begin\{" + env + r"\*?\}.*?\\end\{" + env + r"\*?\}",
                      " ", body, flags=re.DOTALL)
    n_inline = len(re.findall(r"\\\(", body)) + body.count("$$") + \
        (len(re.findall(r"(?<!\\)\$", body)) - 2 * body.count("$$")) // 2
    body = re.sub(r"\\\[.*?\\\]", " ", body, flags=re.DOTALL)
    body = re.sub(r"\\\(.*?\\\)", " ", body, flags=re.DOTALL)
    body = re.sub(r"(?<!\\)\$\$.*?\$\$", " ", body, flags=re.DOTALL)
    body = re.sub(r"(?<!\\)\$.*?(?<!\\)\$", " ", body, flags=re.DOTALL)
    # commands whose braced argument carries no readable prose
    body = re.sub(r"\\(?:label|ref|eqref|cref|Cref|autoref|pageref|nameref|"
                  r"cite[a-zA-Z]*|includegraphics|input|include|bibliography|"
                  r"bibliographystyle|usepackage|documentclass|newcommand|"
                  r"renewcommand|providecommand|def|setlength|geometry|"
                  r"hypersetup|url|graphicspath)\s*(?:\[[^\]]*\])?(?:\{[^{}]*\})?",
                  " ", body)
    body = re.sub(r"\\[A-Za-z@]+\*?", " ", body)   # remaining control words
    body = re.sub(r"\\[^A-Za-z]", " ", body)       # control symbols: \\, \&, \%
    body = body.translate({ord(c): " " for c in "{}[]~^_"})
    words = re.findall(r"[0-9A-Za-zÀ-ɏ][0-9A-Za-zÀ-ɏ'\-]*", body)
    return {"words": len(words), "chars": sum(len(w) for w in words),
            "floats": n_floats, "display_math": n_display,
            "inline_math": max(0, n_inline)}


def _bib_split_fields(s):
    """Split the body of a BibTeX entry (after the citation key) into an
    ordered list of (field_name, value) pairs, respecting brace- and
    quote-delimited values so a comma inside a title does not split it."""
    fields = []
    i, n = 0, len(s)
    while i < n:
        while i < n and s[i] in " \t\r\n,":
            i += 1
        start = i
        while i < n and (s[i].isalnum() or s[i] in "_-+.:"):
            i += 1
        name = s[start:i].strip().lower()
        while i < n and s[i] in " \t\r\n":
            i += 1
        if i >= n or s[i] != "=":
            break
        i += 1
        while i < n and s[i] in " \t\r\n":
            i += 1
        if i < n and s[i] == "{":
            depth, vstart = 0, i
            while i < n:
                if s[i] == "{":
                    depth += 1
                elif s[i] == "}":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
            value = s[vstart:i]
        elif i < n and s[i] == '"':
            vstart = i
            i += 1
            while i < n and s[i] != '"':
                i += 1
            i += 1
            value = s[vstart:i]
        else:
            vstart = i
            while i < n and s[i] != ",":
                i += 1
            value = s[vstart:i].strip()
        if name:
            fields.append((name, value.strip()))
    return fields


def parse_bib(text):
    """Parse BibTeX into (preamble_chunks, entries). Each entry is a dict with
    type, key, and an ordered list of (field, value). @string/@preamble/@comment
    blocks are preserved verbatim. Returns None when the outer brace structure
    is unbalanced so a malformed file is never rewritten."""
    preamble, entries = [], []
    i, n = 0, len(text)
    while True:
        at = text.find("@", i)
        if at == -1:
            break
        j = at + 1
        while j < n and text[j].isalpha():
            j += 1
        etype = text[at + 1:j].lower()
        while j < n and text[j] in " \t\r\n":
            j += 1
        if j >= n or text[j] not in "{(":
            i = at + 1
            continue
        opener = text[j]
        openc, closec = ("{", "}") if opener == "{" else ("(", ")")
        depth, k = 0, j
        while k < n:
            c = text[k]
            if c == openc:
                depth += 1
            elif c == closec:
                depth -= 1
                if depth == 0:
                    break
            k += 1
        if k >= n or depth != 0:
            return None
        body = text[j + 1:k]
        raw = text[at:k + 1]
        i = k + 1
        if etype in ("string", "preamble", "comment"):
            preamble.append(raw)
            continue
        comma = body.find(",")
        if comma == -1:
            key, fields = body.strip(), []
        else:
            key, fields = body[:comma].strip(), _bib_split_fields(body[comma + 1:])
        entries.append({"type": etype, "key": key, "fields": fields})
    return preamble, entries


def format_bib_entry(e):
    """Render one parsed entry with lowercased field names aligned on the
    equals sign and two-space indentation."""
    width = max((len(name) for name, _ in e["fields"]), default=0)
    lines = ["@" + e["type"] + "{" + e["key"] + ","]
    for name, value in e["fields"]:
        lines.append("  " + name.ljust(width) + " = " + value + ",")
    lines.append("}")
    return "\n".join(lines)


# Line-level academic-prose checks. Each pattern flags a common weakness so a
# draft can be tightened before review; the checks are advisory, not absolute.
PROSE_CHECKS = [
    ("em-dash", re.compile(r"—|(?<!-)---(?!-)")),
    ("intensifier", re.compile(
        r"\b(?:very|quite|fairly|rather|really|extremely|vast|relatively|"
        r"remarkably|somewhat|arguably|clearly|obviously|drastically)\b", re.I)),
    ("hedge/filler", re.compile(
        r"\bit is important to note\b|\bin order to\b|\bit should be (?:noted|mentioned)\b|"
        r"\bto the best of (?:the authors'?|our) knowledge\b|"
        r"\bas far as (?:the authors|we) are aware\b|\bneedless to say\b", re.I)),
    ("overclaim", re.compile(
        r"\b(?:highly effective|is superior|prove[sd]? robustness|"
        r"state[- ]of[- ]the[- ]art)\b", re.I)),
    ("gerund clause", re.compile(
        r",\s+(?:including|providing|causing|resulting|confirming|enabling|"
        r"allowing|leading|yielding|producing)\b", re.I)),
    ("duplicated word", re.compile(r"\b([A-Za-z]+)\s+\1\b", re.I)),
    ("However start", re.compile(r"^\s*However,")),
    ("possible passive", re.compile(
        r"\b(?:is|are|was|were|be|been|being)\s+[a-z]+ed\b", re.I)),
    ("exclamation", re.compile(r"!")),
]


def prose_issues(text, limit=600):
    """Scan LaTeX prose line by line and return advisory writing issues as a
    list of {line, kind, text}. Comment content is ignored, and lines without
    at least two lowercase words are skipped so command markup is not flagged."""
    issues = []
    has_prose = re.compile(r"[a-z]{3,}\s+[a-z]{3,}")
    for i, raw in enumerate(text.split("\n"), 1):
        line = re.sub(r"(?<!\\)%.*", "", raw)
        if not has_prose.search(line):
            continue
        snippet = line.strip()[:160]
        for kind, rx in PROSE_CHECKS:
            if rx.search(line):
                issues.append({"line": i, "kind": kind, "text": snippet})
                if len(issues) >= limit:
                    return issues
    return issues


def parse_latex_log(text, main_name):
    r"""Pull errors and warnings with line numbers out of a LaTeX .log. The
    current input file is tracked through the log's parenthesis stack, so a
    problem inside an \input section is attributed to that section rather than
    the main document. File tokens are returned as written in the log; the
    caller resolves them to workspace paths."""
    file_exts = (".tex", ".sty", ".cls", ".def", ".cfg", ".fd", ".ltx",
                 ".aux", ".bbl", ".out", ".toc", ".clo")
    problems = []
    stack = [main_name]
    lines = text.split("\n")

    def cur():
        for f in reversed(stack):
            if f and f.lower().endswith(".tex"):
                return f
        return main_name

    for idx, line in enumerate(lines):
        j, n = 0, len(line)
        while j < n:
            c = line[j]
            if c == "(":
                k = j + 1
                while k < n and line[k] not in ' \t()[]{}"':
                    k += 1
                token = line[j + 1:k]
                is_file = ("/" in token or "\\" in token
                           or token.lower().endswith(file_exts))
                stack.append(token if is_file else "")
                j = k
                continue
            if c == ")" and len(stack) > 1:
                stack.pop()
            j += 1
        if line.startswith("!"):
            msg = line[1:].strip().rstrip(".")
            ln = 0
            for look in lines[idx + 1:idx + 8]:
                lm = re.match(r"l\.(\d+)", look)
                if lm:
                    ln = int(lm.group(1))
                    break
            if msg:
                problems.append({"f": cur(), "line": ln, "kind": "error", "text": msg[:200]})
            continue
        wm = re.search(r"(?:LaTeX|Package|Class)\b[^:]*Warning:\s*(.*)", line)
        if wm:
            lm = re.search(r"on input line (\d+)", line)
            problems.append({"f": cur(), "line": int(lm.group(1)) if lm else 0,
                             "kind": "warning", "text": wm.group(1).strip()[:200]})
            continue
        bm = re.match(r"(?:Overfull|Underfull) \\[hv]box.*?at lines? (\d+)", line)
        if bm:
            problems.append({"f": cur(), "line": int(bm.group(1)),
                             "kind": "warning", "text": line.strip()[:200]})
    return problems


def run_step(job_id, cmd, cwd, timeout, on_line=None):
    """Run one command to completion inside a job, streaming its output and
    honoring cancellation. Returns the exit code, or -1 on failure/timeout."""
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, shell=True, env=job_env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", creationflags=CREATION_FLAGS,
        )
    except OSError as e:
        job_emit(job_id, "error", str(e))
        return -1
    with _lock:
        j = _jobs.get(job_id)
        if j is not None:
            j["proc"] = proc
            if j.get("cancelled"):
                kill_tree(proc)
    timed_out = []

    def on_timeout():
        timed_out.append(True)
        kill_tree(proc)

    timer = threading.Timer(timeout, on_timeout) if timeout else None
    if timer:
        timer.daemon = True
        timer.start()
    try:
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            if not line:
                continue
            if on_line:
                on_line(line)
            else:
                job_emit(job_id, "err" if line.startswith("!") else "out", line)
    finally:
        rc = proc.wait()
        if timer:
            timer.cancel()
    with _lock:
        cancelled = _jobs.get(job_id, {}).get("cancelled")
    if cancelled:
        return -1
    if timed_out:
        job_emit(job_id, "error", f"Timed out after {timeout} s, process tree killed.")
        return -1
    return rc


def resolve_tex_package(missing):
    """Map a missing file like `physics` (physics.sty) to the TeX Live package
    that provides it. The package name often differs from the file name, so
    ask tlmgr which package ships the file."""
    rc, out = run_cmd(f'tlmgr search --global --file "/{missing}.sty"', SCRIPT_DIR, timeout=90)
    if rc != 0:
        rc, out = run_cmd(f'tlmgr search --global --file "/{missing}.cls"', SCRIPT_DIR, timeout=90)
    for line in out.splitlines():
        m = re.match(r"^(\S+):\s*$", line.strip())
        if m:
            return m.group(1)
    # Fall back to installing a package named after the file.
    return missing


def walk_workspace(root, max_files=6000):
    """Yield file paths under root with junk directories and artifacts skipped."""
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name.startswith("~$") or name.lower().endswith(ARTIFACT_EXTS):
                continue
            yield os.path.join(dirpath, name)
            count += 1
            if count >= max_files:
                return


# ---------- git ----------

def find_repo_root(path):
    p = os.path.realpath(path)
    if os.path.isfile(p):
        p = os.path.dirname(p)
    while True:
        if os.path.isdir(os.path.join(p, ".git")):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def classify_remote(url, name=""):
    low = url.lower()
    if "github.com" in low:
        return "github"
    if "overleaf.com" in low or "overleaf" in name.lower():
        return "overleaf"
    return "remote"


def git_info(path, fresh=False):
    repo = find_repo_root(path)
    if not repo:
        return {"repo": None}
    now = time.time()
    with _lock:
        cached = _git_cache.get(repo)
    if cached and not fresh and now - cached[0] < 3:
        return cached[1]
    rc, out = run_cmd("git status -sb", repo)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    branch, dirty, ahead, behind = "?", False, 0, 0
    if rc == 0 and lines and lines[0].startswith("## "):
        head = lines[0][3:]
        dirty = any(not ln.startswith(("warning:", "hint:")) for ln in lines[1:])
        m = re.search(r"ahead (\d+)", head)
        ahead = int(m.group(1)) if m else 0
        m = re.search(r"behind (\d+)", head)
        behind = int(m.group(1)) if m else 0
        if head.startswith("HEAD"):
            branch = "detached"
        elif head.startswith("No commits yet on "):
            branch = head[len("No commits yet on "):].strip() + " (new)"
        else:
            branch = head.split("...")[0].strip() or "?"
    remotes = {}
    rc, out = run_cmd("git remote -v", repo)
    if rc == 0:
        for ln in out.splitlines():
            parts = ln.split()
            if len(parts) >= 2:
                remotes[parts[0]] = classify_remote(parts[1], parts[0])
    info = {
        "repo": repo, "branch": branch, "dirty": dirty,
        "ahead": ahead, "behind": behind, "remotes": remotes,
        "sync": sorted(set(remotes.values())) if remotes else ["local"],
    }
    with _lock:
        _git_cache[repo] = (now, info)
    return info


# ---------- terminal ----------

WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_recv(rfile):
    """Read one WebSocket frame. Returns (opcode, payload_bytes) or (None, None)."""
    head = rfile.read(2)
    if len(head) < 2:
        return None, None
    b1, b2 = head[0], head[1]
    opcode = b1 & 0x0F
    masked = b2 & 0x80
    length = b2 & 0x7F
    if length == 126:
        length = int.from_bytes(rfile.read(2), "big")
    elif length == 127:
        length = int.from_bytes(rfile.read(8), "big")
    # A conforming client always masks, and no terminal input is this large.
    # Reject otherwise instead of allocating an attacker-chosen buffer.
    if not masked or length > 4_000_000:
        return None, None
    mask = rfile.read(4)
    payload = rfile.read(length) if length else b""
    payload = bytes(payload[i] ^ mask[i % 4] for i in range(length))
    return opcode, payload


def ws_send(wfile, lock, data, opcode=0x1):
    header = bytes([0x80 | opcode])
    n = len(data)
    if n < 126:
        header += bytes([n])
    elif n < 65536:
        header += bytes([126]) + n.to_bytes(2, "big")
    else:
        header += bytes([127]) + n.to_bytes(8, "big")
    with lock:
        wfile.write(header + data)
        wfile.flush()


def ws_pty_bridge(rfile, wfile, root):
    """Bridge a PowerShell pseudo console to a WebSocket. The browser's xterm.js
    sends keystrokes and resize events; the pty's output streams back, escape
    codes and all, so interactive tools such as claude and vim work inline."""
    pty = winpty.PtyProcess.spawn(
        "powershell.exe -NoLogo -NoProfile", cwd=root, dimensions=(24, 80))
    send_lock = threading.Lock()
    alive = [True]

    def pump_out():
        try:
            while alive[0] and pty.isalive():
                data = pty.read(65536)
                if not data:
                    break
                ws_send(wfile, send_lock, data.encode("utf-8", "replace"), 0x1)
        except (EOFError, OSError):
            pass
        finally:
            alive[0] = False
            try:
                ws_send(wfile, send_lock, b"", 0x8)
            except Exception:
                pass

    threading.Thread(target=pump_out, daemon=True).start()
    try:
        while alive[0]:
            opcode, payload = ws_recv(rfile)
            if opcode is None or opcode == 0x8:
                break
            if opcode == 0x9:            # ping
                ws_send(wfile, send_lock, payload, 0xA)
                continue
            if opcode not in (0x1, 0x2):
                continue
            msg = payload.decode("utf-8", "replace")
            if not msg:
                continue
            kind, rest = msg[0], msg[1:]
            if kind == "i":
                try:
                    pty.write(rest)
                except (EOFError, OSError):
                    break
            elif kind == "r":
                try:
                    cols, rows = rest.split(",")
                    pty.setwinsize(int(rows), int(cols))
                except Exception:
                    pass
    except (OSError, ConnectionError):
        pass
    finally:
        alive[0] = False
        try:
            pty.terminate(force=True)
        except Exception:
            pass


def get_terminal(root):
    # Serialize the whole check-then-spawn so two concurrent first requests
    # cannot each start a PowerShell and split the user's input and output
    # across two processes.
    with _term_lock:
        term = _terminals.get(root)
        if term and term["proc"].poll() is None:
            return term
        proc = subprocess.Popen(
            "powershell -NoLogo -NoProfile", cwd=root, shell=True, env=job_env(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=CREATION_FLAGS,
        )
        term = {"proc": proc, "lines": [], "lock": threading.Lock()}

        def reader():
            for line in proc.stdout:
                with term["lock"]:
                    term["lines"].append(line.rstrip("\r\n"))

        threading.Thread(target=reader, daemon=True).start()
        _terminals[root] = term
        return term


# ---------- claude ----------

# The assistant panel can use Claude through the local CLI (which can edit
# files) or a third party model through its HTTP API with the user's own key
# (chat only, no file access). provider:model is the id format.
AI_MODELS = [
    {"id": "claude:fable", "label": "Claude Fable 5", "provider": "claude", "edits": True},
    {"id": "claude:opus", "label": "Claude Opus 4.8", "provider": "claude", "edits": True},
    {"id": "claude:sonnet", "label": "Claude Sonnet 5", "provider": "claude", "edits": True},
    {"id": "claude:haiku", "label": "Claude Haiku 4.5", "provider": "claude", "edits": True},
    {"id": "openai:gpt-4o", "label": "ChatGPT (GPT-4o)", "provider": "openai", "edits": False},
    {"id": "openai:gpt-4o-mini", "label": "ChatGPT (GPT-4o mini)", "provider": "openai", "edits": False},
    {"id": "gemini:gemini-2.0-flash", "label": "Gemini 2.0 Flash", "provider": "gemini", "edits": False},
    {"id": "xai:grok-2-latest", "label": "Grok 2", "provider": "xai", "edits": False},
]
AI_KEY_SETTING = {"openai": "openai_key", "gemini": "gemini_key", "xai": "xai_key"}
AI_PROVIDERS = ("claude", "ollama", "openai", "gemini", "xai")
OLLAMA_URL = "http://localhost:11434"


def ollama_models():
    """List locally installed Ollama models. Ollama is a free, open source
    runtime for local models (Llama, Mistral, Qwen, and others) and needs no
    API key. Returns [] when Ollama is not running."""
    try:
        data = http_get_json(OLLAMA_URL + "/api/tags", timeout=3)
        return [m["name"] for m in data.get("models", []) if m.get("name")]
    except Exception:
        return []


def list_ai_models(settings):
    """Full model list for the picker: local free models first, then Claude
    (works through the CLI), then key-gated third party models."""
    models = []
    for name in ollama_models():
        models.append({"id": "ollama:" + name, "label": name + "  (local, free)",
                       "provider": "ollama", "edits": False, "ready": True, "free": True})
    for m in AI_MODELS:
        ready = m["provider"] == "claude" or bool(settings.get(AI_KEY_SETTING.get(m["provider"], "")))
        models.append({**m, "ready": ready, "free": False})
    return models


def build_ai_prompt(root, prompt, context, for_tools):
    parts = [
        "You are the assistant inside a research IDE. Work efficiently and "
        "precisely. The user is actively editing one file and their request "
        "almost always concerns that file."
    ]
    if for_tools:
        parts.append(
            "Prefer to act on the open file directly with the Edit tool, make "
            "the smallest change that satisfies the request, and do not read or "
            "search unrelated files unless the task clearly requires it.")
    else:
        parts.append(
            "You cannot edit files; answer the question and, when a change is "
            "needed, show the exact text to paste.")
    parts.append(f"Workspace: {root}")
    if context.get("file"):
        parts.append(f"Open file: {context['file']}")
    if context.get("selection"):
        parts.append("The user selected this text, most likely the subject of the "
                     "request:\n---\n" + context["selection"][:4000] + "\n---")
    content = context.get("content")
    if context.get("file") and isinstance(content, str) and len(content) <= 60000:
        parts.append("Current content of the open file:\n---\n" + content + "\n---")
    if context.get("pdf"):
        parts.append(f"The user is also reading this PDF: {context['pdf']}.")
    parts.append("User request: " + prompt)
    return "\n".join(parts)


def scrub_secret(text, secret):
    """Never let an API key appear in a message shown to the user."""
    return text.replace(secret, "***") if secret else text


def ai_http_chat(job_id, provider, model, full_prompt, key):
    """Single-shot call to a third party chat model over its REST API."""
    try:
        if provider == "ollama":
            payload = {"model": model, "stream": False,
                       "messages": [{"role": "user", "content": full_prompt}]}
            req = urllib.request.Request(
                OLLAMA_URL + "/api/chat", data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=300) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            text = data.get("message", {}).get("content", "")
        elif provider in ("openai", "xai"):
            base = "https://api.openai.com/v1" if provider == "openai" else "https://api.x.ai/v1"
            payload = {"model": model, "messages": [{"role": "user", "content": full_prompt}]}
            req = urllib.request.Request(
                base + "/chat/completions", data=json.dumps(payload).encode(),
                headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            text = data["choices"][0]["message"]["content"]
        elif provider == "gemini":
            # Pass the key as a header, never in the URL, so it cannot end up
            # in a logged or error-surfaced request line.
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent")
            payload = {"contents": [{"parts": [{"text": full_prompt}]}]}
            req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json",
                                                  "x-goog-api-key": key})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        else:
            job_emit(job_id, "error", "Unknown provider.")
            job_done(job_id, 1)
            return
        job_emit(job_id, "text", text)
        job_done(job_id, 0)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        job_emit(job_id, "error", scrub_secret(f"{provider} API error {e.code}: {body}", key))
        job_done(job_id, 1)
    except Exception as e:
        job_emit(job_id, "error", scrub_secret(f"{provider} request failed: {e}", key))
        job_done(job_id, 1)


def ai_chat(job_id, root, prompt, context, model_id):
    provider = model_id.split(":", 1)[0]
    model = model_id.split(":", 1)[1] if ":" in model_id else model_id
    if provider == "claude":
        claude_chat(job_id, root, prompt, context, model)
        return
    key = ""
    if provider in AI_KEY_SETTING:
        key = get_settings().get(AI_KEY_SETTING[provider], "")
        if not key:
            job_emit(job_id, "error",
                     f"No API key for {provider}. Add it in Settings, or use a Claude "
                     f"model (works without a key) or a local Ollama model (free).")
            job_done(job_id, 1)
            return
    # Ollama needs no key.
    full_prompt = build_ai_prompt(root, prompt, context, for_tools=False)
    threading.Thread(target=ai_http_chat,
                     args=(job_id, provider, model, full_prompt, key), daemon=True).start()


def claude_chat(job_id, root, prompt, context, model="fable"):
    cfg = load_config()
    session_id = cfg.get("claude_sessions", {}).get(root)
    full_prompt = build_ai_prompt(root, prompt, context, for_tools=True)

    cmd = (
        f'claude -p --output-format stream-json --verbose '
        f'--permission-mode manual --model {model} '
        f'--allowedTools "{CLAUDE_ALLOWED_TOOLS}" '
        f'--max-turns 25 --max-budget-usd {CLAUDE_MAX_BUDGET}'
    )
    if session_id:
        cmd += f" --resume {session_id}"

    def parse_line(line):
        try:
            ev = json.loads(line)
        except ValueError:
            return
        etype = ev.get("type")
        if etype == "system" and ev.get("subtype") == "init":
            sid = ev.get("session_id")
            if sid:
                def set_sid(cfg):
                    cfg.setdefault("claude_sessions", {})[root] = sid
                update_config(set_sid)
        elif etype == "assistant":
            for item in ev.get("message", {}).get("content", []):
                if item.get("type") == "text" and item.get("text", "").strip():
                    job_emit(job_id, "text", item["text"])
                elif item.get("type") == "tool_use":
                    name = item.get("name", "tool")
                    inp = item.get("input", {})
                    detail = (
                        inp.get("file_path") or inp.get("command")
                        or inp.get("pattern") or ""
                    )
                    detail = os.path.basename(detail) if name in ("Read", "Edit", "Write") else detail
                    job_emit(job_id, "tool", f"{name}  {str(detail)[:80]}")
        elif etype == "result":
            cost = ev.get("total_cost_usd")
            dur = ev.get("duration_ms")
            meta = []
            if dur:
                meta.append(f"{dur / 1000:.0f}s")
            if cost is not None:
                meta.append(f"${cost:.2f}")
            if ev.get("is_error"):
                job_emit(job_id, "error", str(ev.get("result", "Claude returned an error.")))
            if meta:
                job_emit(job_id, "meta", " · ".join(meta))

    stream_job(job_id, cmd, root, timeout=900, stdin_text=full_prompt, line_filter=parse_line)


# ---------- http ----------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

    def handle_ws_terminal(self, q):
        if not HAS_PTY:
            self._json({"error": "no pty available"}, 400)
            return
        origin = self.headers.get("Origin")
        if q.get("tok") != SESSION_TOKEN or (origin and origin != ORIGIN):
            self.send_response(403)
            self.end_headers()
            return
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self.send_response(400)
            self.end_headers()
            return
        accept = base64.b64encode(
            hashlib.sha1((key + WS_MAGIC).encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        root = get_root() or os.path.expanduser("~")
        try:
            ws_pty_bridge(self.rfile, self.wfile, root)
        except (OSError, ConnectionError):
            pass

    def _sse_send(self, obj):
        data = json.dumps(obj)
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}

    def _authorized(self, q, require_origin):
        """Guard every data endpoint. The token proves the request came from
        our own page. On state-changing requests also require a same-origin
        Origin header, so a cross-origin form or fetch is refused even if it
        somehow guessed the token."""
        token = self.headers.get("X-WB-Token") or q.get("tok", "")
        if token != SESSION_TOKEN:
            return False
        if require_origin:
            origin = self.headers.get("Origin")
            if origin and origin != ORIGIN:
                return False
        return True

    # ----- GET -----

    def _host_ok(self):
        return (self.headers.get("Host") or "").lower() in ALLOWED_HOSTS

    def do_GET(self):
        if not self._host_ok():
            self.send_response(403)
            self.end_headers()
            return
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        route = url.path
        try:
            if route == "/":
                with open(HTML_FILE, "r", encoding="utf-8") as f:
                    html = f.read().replace("__WB_TOKEN__", SESSION_TOKEN)
                self._bytes(html.encode("utf-8"), "text/html; charset=utf-8")
                return
            if route == "/ws/term":
                self.handle_ws_terminal(q)
                return
            if route in PUBLIC_ASSETS:
                fname, ctype = PUBLIC_ASSETS[route]
                fpath = os.path.join(SCRIPT_DIR, fname)
                if os.path.isfile(fpath):
                    with open(fpath, "rb") as f:
                        body = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "max-age=86400")
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self._json({"error": "not found"}, 404)
                return
            if not self._authorized(q, require_origin=False):
                self._json({"error": "unauthorized"}, 403)
                return
            if route == "/api/config":
                cfg = load_config()
                settings = get_settings()
                # Do not echo API key values into the page. Report only whether
                # each provider has a key configured.
                safe = dict(settings)
                safe["keys_set"] = {p: bool(settings.get(k)) for p, k in AI_KEY_SETTING.items()}
                for k in ("openai_key", "gemini_key", "xai_key"):
                    safe[k] = ""
                self._json({
                    "root": get_root(), "recents": cfg.get("recents", []),
                    "settings": safe,
                    "open_tabs": cfg.get("open_tabs", []),
                    "compilers": list(COMPILERS.keys()),
                    "pty": HAS_PTY,
                    "app": {"name": APP_NAME, "version": APP_VERSION,
                            "author": APP_AUTHOR, "year": APP_YEAR},
                })
            elif route == "/api/browse":
                self.api_browse(q)
            elif route == "/api/tree":
                self.api_tree(q)
            elif route == "/api/gitinfo":
                path = q.get("path", "")
                if not path_allowed(path):
                    self._json({"error": "outside workspace"}, 403)
                else:
                    self._json(git_info(path, fresh=q.get("fresh") == "1"))
            elif route == "/api/file":
                self.api_file_get(q)
            elif route == "/img":
                self.api_img(q)
            elif route == "/pdf":
                self.api_pdf(q)
            elif route == "/api/stream":
                self.api_stream(q)
            elif route == "/api/termstream":
                self.api_termstream(q)
            elif route == "/api/quickindex":
                self.api_quickindex()
            elif route == "/api/search":
                self.api_search(q)
            elif route == "/api/find/search":
                self.api_find_search(q)
            elif route == "/api/find/library":
                self.api_find_library(q)
            elif route == "/api/cite/search":
                self.api_cite_search(q)
            elif route == "/api/cite/format":
                self.api_cite_format(q)
            elif route == "/api/cite/styles":
                self._json({"styles": CITE_STYLES})
            elif route == "/api/ai/models":
                models = list_ai_models(get_settings())
                # Default to a free local model when one is available, else Claude.
                free = next((m["id"] for m in models if m.get("free")), None)
                self._json({"models": models, "default": free or "claude:fable"})
            else:
                self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    def api_browse(self, q):
        """Directory listing for the folder picker. Read only, any path."""
        path = q.get("path") or os.path.expanduser("~")
        if not os.path.isdir(path):
            self._json({"error": "not a directory"}, 400)
            return
        path = os.path.realpath(path)
        dirs = []
        try:
            for name in sorted(os.listdir(path), key=str.lower):
                full = os.path.join(path, name)
                if os.path.isdir(full) and not name.startswith(("$", ".")):
                    dirs.append(name)
        except PermissionError:
            pass
        home = os.path.expanduser("~")
        places = []
        for label, p in (("Home", home),
                         ("Desktop", os.path.join(home, "Desktop")),
                         ("Documents", os.path.join(home, "Documents")),
                         ("Downloads", os.path.join(home, "Downloads"))):
            if os.path.isdir(p):
                places.append({"label": label, "path": p})
        try:
            for name in sorted(os.listdir(home)):
                if name.startswith(("OneDrive", "Dropbox")) and os.path.isdir(os.path.join(home, name)):
                    places.append({"label": name, "path": os.path.join(home, name)})
        except OSError:
            pass
        drives = []
        if os.name == "nt":
            for letter in "CDEFGH":
                if os.path.exists(letter + ":\\"):
                    drives.append(letter + ":\\")
        parent = os.path.dirname(path)
        self._json({
            "path": path, "parent": parent if parent != path else None,
            "dirs": dirs, "drives": drives, "places": places,
        })

    def api_tree(self, q):
        path = q.get("path", "")
        if not path_allowed(path) or not os.path.isdir(path):
            self._json({"error": "outside workspace"}, 403)
            return
        entries = []
        try:
            names = sorted(os.listdir(path), key=str.lower)
        except PermissionError:
            names = []
        for name in names:
            if name in SKIP_DIRS or name.startswith((".", "~$")):
                continue
            full = os.path.join(path, name)
            if os.path.isdir(full):
                entries.append({
                    "name": name, "dir": True,
                    "repo": os.path.isdir(os.path.join(full, ".git")),
                })
        for name in names:
            if name.startswith("~$"):
                continue
            full = os.path.join(path, name)
            if os.path.isfile(full):
                if name.lower().endswith(ARTIFACT_EXTS):
                    continue
                ext = os.path.splitext(name)[1].lower()
                entries.append({"name": name, "dir": False, "ext": ext})
        self._json({"path": path, "entries": entries})

    def api_file_get(self, q):
        path = q.get("path", "")
        if not path_allowed(path) or not os.path.isfile(path):
            self._json({"error": "outside workspace"}, 403)
            return
        if os.path.getsize(path) > MAX_TEXT_BYTES:
            self._json({"error": "file too large for the editor"}, 400)
            return
        with open(path, "rb") as f:
            raw = f.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw.decode("cp1252")
            except UnicodeDecodeError:
                self._json({"error": "binary file"}, 400)
                return
        self._json({"content": text, "mtime": os.path.getmtime(path)})

    def api_pdf(self, q):
        path = q.get("path", "")
        if not path_allowed(path) or not os.path.isfile(path):
            self._json({"error": "outside workspace"}, 403)
            return
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", "inline")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def api_img(self, q):
        path = q.get("path", "")
        if not path_allowed(path) or not os.path.isfile(path):
            self._json({"error": "outside workspace"}, 403)
            return
        if os.path.getsize(path) > 30_000_000:
            self._json({"error": "image too large"}, 400)
            return
        ctype = IMAGE_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", "inline")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def api_stream(self, q):
        job_id = q.get("job", "")
        self._sse_start()
        sent = 0
        idle = 0
        while True:
            with _lock:
                job = _jobs.get(job_id)
                if job is None:
                    break
                lines = job["lines"][sent:]
                done = job["done"]
                rc = job["rc"]
            for item in lines:
                self._sse_send(item)
            sent += len(lines)
            if done:
                with _lock:
                    missing = _jobs.get(job_id, {}).get("missing")
                self._sse_send({"kind": "done", "rc": rc, "missing": missing})
                break
            # A heartbeat comment surfaces a client disconnect as a write
            # error, so an abandoned stream stops leaking its handler thread.
            idle = 0 if lines else idle + 1
            if idle >= 20:
                idle = 0
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
            time.sleep(0.15)

    def api_termstream(self, q):
        root = get_root()
        if not root:
            self._json({"error": "no workspace"}, 400)
            return
        term = get_terminal(root)
        self._sse_start()
        sent = int(q.get("from", 0))
        idle = 0
        while True:
            with term["lock"]:
                lines = term["lines"][sent:]
            for ln in lines:
                self._sse_send({"kind": "out", "text": ln, "seq": sent})
                sent += 1
            if term["proc"].poll() is not None:
                self._sse_send({"kind": "done", "rc": term["proc"].returncode})
                break
            idle = 0 if lines else idle + 1
            if idle >= 20:
                idle = 0
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
            time.sleep(0.15)

    def api_quickindex(self):
        root = get_root()
        if not root:
            self._json({"files": []})
            return
        files = []
        for full in walk_workspace(root):
            files.append(os.path.relpath(full, root))
        self._json({"files": files})

    def api_search(self, q):
        root = get_root()
        needle = (q.get("q") or "").strip()
        if not root or len(needle) < 2:
            self._json({"matches": [], "truncated": False})
            return
        low = needle.lower()
        matches = []
        truncated = False
        text_exts = (".tex", ".bib", ".py", ".md", ".txt", ".json", ".yaml", ".yml",
                     ".csv", ".cls", ".sty", ".bst", ".html", ".js", ".css", ".m",
                     ".r", ".ini", ".cfg", ".toml", ".bat", ".ps1")
        for full in walk_workspace(root):
            if not full.lower().endswith(text_exts):
                continue
            try:
                if os.path.getsize(full) > 1_500_000:
                    continue
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, 1):
                        if low in line.lower():
                            matches.append({
                                "file": os.path.relpath(full, root),
                                "line": lineno,
                                "text": line.strip()[:200],
                            })
                            if len(matches) >= 300:
                                truncated = True
                                break
            except OSError:
                continue
            if truncated:
                break
        self._json({"matches": matches, "truncated": truncated})

    # ----- POST -----

    def do_POST(self):
        if not self._host_ok():
            self._body()
            self.send_response(403)
            self.end_headers()
            return
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if not self._authorized(q, require_origin=True):
            self._body()
            self._json({"error": "unauthorized"}, 403)
            return
        body = self._body()
        route = url.path
        try:
            if route == "/api/root":
                self.api_root(body)
            elif route == "/api/file":
                self.api_file_post(body)
            elif route == "/api/compile":
                self.api_compile(body)
            elif route == "/api/export/docx":
                self.api_export_docx(body)
            elif route == "/api/wordcount":
                self.api_wordcount(body)
            elif route == "/api/clean":
                self.api_clean(body)
            elif route == "/api/format":
                self.api_format(body)
            elif route == "/api/export/md":
                self.api_export_md(body)
            elif route == "/api/bibtidy":
                self.api_bibtidy(body)
            elif route == "/api/citekeys":
                self.api_citekeys(body)
            elif route == "/api/citecheck":
                self.api_citecheck(body)
            elif route == "/api/labels":
                self.api_labels(body)
            elif route == "/api/todos":
                self.api_todos(body)
            elif route == "/api/prose":
                self.api_prose(body)
            elif route == "/api/outline":
                self.api_outline(body)
            elif route == "/api/refindex":
                self.api_refindex(body)
            elif route == "/api/compilelog":
                self.api_compilelog(body)
            elif route == "/api/writingstats":
                self.api_writingstats(body)
            elif route == "/api/gitop":
                self.api_gitop(body)
            elif route == "/api/git/connect":
                self.api_git_connect(body)
            elif route == "/api/app/update":
                self.api_app_update(body)
            elif route == "/api/run":
                self.api_run(body)
            elif route == "/api/term":
                self.api_term(body)
            elif route == "/api/claude":
                self.api_claude(body)
            elif route == "/api/claude/reset":
                root = get_root()
                update_config(lambda cfg: cfg.get("claude_sessions", {}).pop(root, None))
                self._json({"ok": True})
            elif route == "/api/settings":
                self.api_settings(body)
            elif route == "/api/tabs":
                tabs = [p for p in body.get("tabs", []) if isinstance(p, str)][:20]
                update_config(lambda cfg: cfg.__setitem__("open_tabs", tabs))
                self._json({"ok": True})
            elif route == "/api/fileop":
                self.api_fileop(body)
            elif route == "/api/texinstall":
                self.api_texinstall(body)
            elif route == "/api/pickdir":
                self.api_pickdir(body)
            elif route == "/api/cite/append":
                self.api_cite_append(body)
            elif route == "/api/find/bibtex":
                self.api_find_bibtex(body)
            elif route == "/api/openurl":
                self.api_openurl(body)
            elif route == "/api/job/cancel":
                self._json({"ok": cancel_job(body.get("job", ""))})
            elif route == "/api/extterm":
                self.api_extterm(body)
            elif route == "/api/reveal":
                path = body.get("path", "")
                if path_allowed(path):
                    os.startfile(path if os.path.isdir(path) else os.path.dirname(path))
                    self._json({"ok": True})
                else:
                    self._json({"error": "outside workspace"}, 403)
            else:
                self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    def api_root(self, body):
        path = body.get("path", "")
        if not os.path.isdir(path):
            self._json({"error": "not a directory"}, 400)
            return
        path = os.path.realpath(path)

        def switch(cfg):
            cfg["root"] = path
            cfg["recents"] = ([path] + [r for r in cfg.get("recents", []) if r != path])[:8]
            cfg["open_tabs"] = []
        update_config(switch)
        self._json({"ok": True, "root": path})

    def api_settings(self, body):
        settings = dict(get_settings())
        for key in DEFAULT_SETTINGS:
            if key in body:
                # A blank API key field means keep the existing key, so the
                # redacted value sent back by /api/config does not wipe it.
                if key in ("openai_key", "gemini_key", "xai_key") and not str(body[key]).strip():
                    continue
                settings[key] = body[key]
        if settings.get("compiler") not in COMPILERS:
            settings["compiler"] = "latexmk"
        if settings.get("accent") not in ACCENTS:
            settings["accent"] = "clay"
        if settings.get("editor_font") not in EDITOR_FONTS:
            settings["editor_font"] = "Cascadia Mono"
        if settings.get("theme") not in ("dark", "light"):
            settings["theme"] = "dark"
        try:
            settings["font_size"] = max(9, min(22, int(settings.get("font_size", 12))))
        except (TypeError, ValueError):
            settings["font_size"] = 12
        try:
            settings["writing_goal"] = max(0, min(100000, int(settings.get("writing_goal", 500))))
        except (TypeError, ValueError):
            settings["writing_goal"] = 500
        settings["autosave"] = bool(settings.get("autosave"))
        settings["auto_install"] = bool(settings.get("auto_install"))
        am = settings.get("ai_model") or ""
        if ":" not in am or am.split(":", 1)[0] not in AI_PROVIDERS:
            settings["ai_model"] = "claude:fable"
        for k in ("openai_key", "gemini_key", "xai_key"):
            settings[k] = str(settings.get(k, "") or "").strip()
        update_config(lambda cfg: cfg.__setitem__("settings", settings))
        # Redact keys in the response so they never sit in the page's memory.
        safe = dict(settings)
        safe["keys_set"] = {p: bool(settings.get(k)) for p, k in AI_KEY_SETTING.items()}
        for k in ("openai_key", "gemini_key", "xai_key"):
            safe[k] = ""
        self._json({"ok": True, "settings": safe})

    def api_file_post(self, body):
        path = body.get("path", "")
        if not path_allowed(path):
            self._json({"error": "outside workspace"}, 403)
            return
        content = body.get("content", "")
        # Optional optimistic-concurrency guard: the client sends the mtime it
        # last loaded, and a mismatch means Claude or another program wrote the
        # file underneath the editor. Report the conflict instead of clobbering.
        base = body.get("base_mtime")
        if base is not None and os.path.isfile(path):
            if abs(os.path.getmtime(path) - float(base)) > 0.001:
                self._json({"error": "conflict", "mtime": os.path.getmtime(path)}, 409)
                return
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        self._json({"ok": True, "mtime": os.path.getmtime(path)})

    def api_fileop(self, body):
        op = body.get("op", "")
        path = body.get("path", "")
        if not path_allowed(path):
            self._json({"error": "outside workspace"}, 403)
            return
        name = (body.get("name") or "").strip()
        bad = set('\\/:*?"<>|')
        if op in ("new_file", "new_folder", "rename"):
            if not name or any(c in bad for c in name):
                self._json({"error": "invalid name"}, 400)
                return
        try:
            if op == "new_file":
                target = os.path.join(path, name)
                if os.path.exists(target):
                    self._json({"error": "already exists"}, 400)
                    return
                with open(target, "x", encoding="utf-8") as f:
                    f.write("")
                self._json({"ok": True, "path": target})
            elif op == "new_folder":
                target = os.path.join(path, name)
                os.makedirs(target, exist_ok=False)
                self._json({"ok": True, "path": target})
            elif op == "rename":
                target = os.path.join(os.path.dirname(path), name)
                if os.path.exists(target):
                    self._json({"error": "already exists"}, 400)
                    return
                os.rename(path, target)
                self._json({"ok": True, "path": target})
            elif op == "delete":
                if os.path.normcase(os.path.realpath(path)) == os.path.normcase(os.path.realpath(get_root())):
                    self._json({"error": "will not delete the workspace root"}, 400)
                    return
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
                self._json({"ok": True})
            else:
                self._json({"error": "unknown op"}, 400)
        except FileExistsError:
            self._json({"error": "already exists"}, 400)
        except OSError as e:
            self._json({"error": str(e)}, 500)

    def api_compile(self, body):
        tex = body.get("path", "")
        if not path_allowed(tex) or not tex.lower().endswith(".tex"):
            self._json({"error": "select a .tex file"}, 400)
            return
        tex, note = resolve_main_tex(tex)
        name = os.path.basename(tex)
        if '"' in name:
            self._json({"error": "file name contains a quote"}, 400)
            return
        settings = get_settings()
        compiler = settings.get("compiler", "latexmk")
        template = COMPILERS.get(compiler, COMPILERS["latexmk"])
        auto = settings.get("auto_install", True)
        folder = os.path.dirname(tex)
        job_id = new_job()
        pdf = os.path.splitext(tex)[0] + ".pdf"

        def worker():
            if note:
                job_emit(job_id, "info", note)
            tried = set()
            rc = -1
            for _ in range(8):
                with _lock:
                    if _jobs.get(job_id, {}).get("cancelled"):
                        break
                missing = [None]

                def scan(line):
                    m = re.search(r"File [`']([^'\.]+)\.(?:sty|cls)' not found", line)
                    # Only accept a plain package name. A crafted \usepackage
                    # in an untrusted .tex could otherwise smuggle shell
                    # metacharacters into the tlmgr command below.
                    if m and re.fullmatch(r"[A-Za-z0-9._-]+", m.group(1)):
                        missing[0] = m.group(1)
                    job_emit(job_id, "err" if line.startswith("!") else "out", line)

                job_emit(job_id, "info", f"{compiler}: {name}")
                rc = run_step(job_id, template.format(name=name), folder, 600, scan)
                if rc == 0 or not missing[0]:
                    break
                miss = missing[0]
                if not auto or miss in tried:
                    if not auto:
                        with _lock:
                            j = _jobs.get(job_id)
                            if j is not None:
                                j["missing"] = miss
                    break
                tried.add(miss)
                job_emit(job_id, "info", f"Missing {miss}.sty; searching TeX Live for the package…")
                pkg = resolve_tex_package(miss)
                job_emit(job_id, "info", f"Installing package '{pkg}' with tlmgr…")
                irc = run_step(job_id, f"tlmgr install {pkg}", folder, 300)
                if irc != 0:
                    job_emit(job_id, "error",
                             f"Could not install '{pkg}'. Install manually: tlmgr install {pkg}")
                    with _lock:
                        j = _jobs.get(job_id)
                        if j is not None:
                            j["missing"] = miss
                    break
            job_done(job_id, rc)

        threading.Thread(target=worker, daemon=True).start()
        self._json({"job": job_id, "pdf": pdf})

    def api_texinstall(self, body):
        pkg = (body.get("pkg") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+", pkg):
            self._json({"error": "bad package name"}, 400)
            return
        job_id = new_job()
        job_emit(job_id, "info", f"tlmgr install {pkg}")
        stream_job(job_id, f"tlmgr install {pkg}", get_root() or SCRIPT_DIR, timeout=300)
        self._json({"job": job_id})

    def api_export_docx(self, body):
        """Convert a LaTeX document to Word (.docx) with Pandoc, which keeps
        the structure, headings, tables, equations, and citations rather than
        producing a lossy dump."""
        tex = body.get("path", "")
        if not path_allowed(tex) or not tex.lower().endswith(".tex"):
            self._json({"error": "select a .tex file"}, 400)
            return
        pandoc = find_pandoc()
        if not pandoc:
            self._json({"error": "Pandoc is not installed. Install it from pandoc.org "
                                 "(or 'winget install pandoc'), then try again."}, 400)
            return
        tex, note = resolve_main_tex(tex)
        name = os.path.basename(tex)
        if '"' in name:
            self._json({"error": "file name contains a quote"}, 400)
            return
        folder = os.path.dirname(tex)
        docx = os.path.splitext(name)[0] + ".docx"
        bib = next((f for f in os.listdir(folder) if f.lower().endswith(".bib")), None)
        cmd = f'"{pandoc}" "{name}" -o "{docx}" --citeproc --resource-path=.'
        if bib and '"' not in bib:
            cmd += f' --bibliography "{bib}"'
        job_id = new_job()
        if note:
            job_emit(job_id, "info", note)
        job_emit(job_id, "info", f"pandoc: {name} -> {docx}"
                 + (f" (with {bib})" if bib else ""))
        stream_job(job_id, cmd, folder, timeout=300)
        self._json({"job": job_id, "docx": os.path.join(folder, docx)})

    def api_wordcount(self, body):
        """Estimate the word count of a LaTeX document, following \\input and
        \\include so a multi-file paper counts as a whole."""
        tex = body.get("path", "")
        if not path_allowed(tex) or not tex.lower().endswith(".tex"):
            self._json({"error": "select a .tex file"}, 400)
            return
        tex, note = resolve_main_tex(tex)
        text = expand_tex_inputs(tex)
        if not text:
            self._json({"error": "could not read the document"}, 500)
            return
        stats = latex_word_count(text)
        stats.update({"ok": True, "file": os.path.basename(tex), "note": note})
        self._json(stats)

    def api_clean(self, body):
        """Remove regenerable build artifacts (LaTeX .aux/.bbl/.toc/…, a .log
        that sits beside a .tex of the same name, and .pyc) from the workspace
        so the tree stays readable. Source files are never touched."""
        root = get_root()
        if not root:
            self._json({"error": "open a workspace first"}, 400)
            return
        removed, freed = [], 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                low = name.lower()
                is_artifact = low.endswith(ARTIFACT_EXTS)
                is_tex_log = (low.endswith(".log")
                              and os.path.isfile(os.path.join(
                                  dirpath, os.path.splitext(name)[0] + ".tex")))
                if not (is_artifact or is_tex_log):
                    continue
                full = os.path.join(dirpath, name)
                if not path_allowed(full):
                    continue
                try:
                    size = os.path.getsize(full)
                    os.remove(full)
                    removed.append(os.path.relpath(full, root))
                    freed += size
                except OSError:
                    pass
        self._json({"ok": True, "count": len(removed), "bytes": freed,
                    "files": removed[:200]})

    def api_format(self, body):
        """Reformat a Python file with Ruff (preferred) or Black, whichever is
        installed in the configured environment."""
        path = body.get("path", "")
        if not path_allowed(path) or not path.lower().endswith(".py"):
            self._json({"error": "select a .py file"}, 400)
            return
        name = os.path.basename(path)
        if '"' in name:
            self._json({"error": "file name contains a quote"}, 400)
            return
        py = get_settings().get("python_path", "")
        if not py or not os.path.isfile(py):
            py = "python"
        folder = os.path.dirname(path)
        if run_cmd(f'"{py}" -m ruff --version', folder, timeout=20)[0] == 0:
            tool, cmd = "ruff", f'"{py}" -m ruff format "{name}"'
        elif run_cmd(f'"{py}" -m black --version', folder, timeout=20)[0] == 0:
            tool, cmd = "black", f'"{py}" -m black "{name}"'
        else:
            self._json({"error": "Neither Ruff nor Black is installed in the "
                                 "configured environment. Install one with "
                                 "'pip install ruff' (or 'pip install black'), "
                                 "then retry."}, 400)
            return
        job_id = new_job()
        job_emit(job_id, "info", f"{tool}: formatting {name}")
        stream_job(job_id, cmd, folder, timeout=120)
        self._json({"job": job_id, "tool": tool})

    def api_export_md(self, body):
        """Convert a Markdown file to Word (.docx) or PDF with Pandoc, wiring in
        a workspace .bib through citeproc when one is present."""
        md = body.get("path", "")
        to = (body.get("to") or "docx").lower()
        if to not in ("docx", "pdf"):
            self._json({"error": "unsupported target"}, 400)
            return
        if not path_allowed(md) or not md.lower().endswith((".md", ".markdown")):
            self._json({"error": "select a Markdown (.md) file"}, 400)
            return
        pandoc = find_pandoc()
        if not pandoc:
            self._json({"error": "Pandoc is not installed. Install it from "
                                 "pandoc.org (or 'winget install pandoc'), then "
                                 "try again."}, 400)
            return
        name = os.path.basename(md)
        if '"' in name:
            self._json({"error": "file name contains a quote"}, 400)
            return
        folder = os.path.dirname(md)
        out = os.path.splitext(name)[0] + "." + to
        bib = next((f for f in os.listdir(folder) if f.lower().endswith(".bib")), None)
        cmd = f'"{pandoc}" "{name}" -o "{out}" --standalone --resource-path=.'
        if bib and '"' not in bib:
            cmd += f' --citeproc --bibliography "{bib}"'
        job_id = new_job()
        job_emit(job_id, "info", f"pandoc: {name} -> {out}"
                 + (f" (with {bib})" if bib else ""))
        stream_job(job_id, cmd, folder, timeout=300)
        self._json({"job": job_id, "out": os.path.join(folder, out)})

    def api_bibtidy(self, body):
        """Normalize a .bib file: drop entries with duplicate citation keys,
        sort the rest by key, and align fields. The original is copied to a
        .bak first, and a file whose braces do not balance is left untouched."""
        path = body.get("path", "")
        if not path_allowed(path) or not path.lower().endswith(".bib"):
            self._json({"error": "select a .bib file"}, 400)
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            self._json({"error": str(e)}, 500)
            return
        parsed = parse_bib(text)
        if parsed is None:
            self._json({"error": "the .bib has unbalanced braces; not modified"}, 400)
            return
        preamble, entries = parsed
        if not entries:
            self._json({"error": "no BibTeX entries found"}, 400)
            return
        seen, kept, removed_keys, doi_map, dup_dois = set(), [], [], {}, []
        for e in entries:
            kl = e["key"].lower()
            if kl in seen:
                removed_keys.append(e["key"])
                continue
            seen.add(kl)
            kept.append(e)
            doi = ""
            for name, value in e["fields"]:
                if name == "doi":
                    doi = re.sub(r"[{}\"\s]", "", value).lower()
                    doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "")
                    break
            if doi:
                if doi in doi_map:
                    dup_dois.append([doi_map[doi], e["key"], doi])
                else:
                    doi_map[doi] = e["key"]
        kept.sort(key=lambda e: e["key"].lower())
        chunks = []
        if preamble:
            chunks.append("\n".join(preamble))
        chunks.append("\n\n".join(format_bib_entry(e) for e in kept))
        new_text = "\n\n".join(chunks).rstrip() + "\n"
        try:
            with open(path + ".bak", "w", encoding="utf-8") as f:
                f.write(text)
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_text)
        except OSError as e:
            self._json({"error": str(e)}, 500)
            return
        self._json({"ok": True, "entries_in": len(entries), "entries_out": len(kept),
                    "removed_keys": removed_keys, "dup_dois": dup_dois,
                    "backup": os.path.basename(path + ".bak")})

    def api_citekeys(self, body):
        """Return every citation key defined in the workspace .bib files, each
        with a short author/year/title label, for cite autocompletion."""
        root = get_root()
        if not root:
            self._json({"keys": []})
            return
        clean = lambda v: re.sub(r"[{}]", "", v).strip() if v else ""
        out, seen = [], set()
        for fp in walk_workspace(root):
            if not fp.lower().endswith(".bib"):
                continue
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    parsed = parse_bib(f.read())
            except OSError:
                continue
            if not parsed:
                continue
            for e in parsed[1]:
                k = e["key"]
                if not k or k in seen:
                    continue
                seen.add(k)
                fields = dict(e["fields"])
                author = clean(fields.get("author", ""))
                first = author.split(" and ")[0].split(",")[0].strip() if author else ""
                out.append({"key": k, "author": first,
                            "year": clean(fields.get("year", "")),
                            "title": clean(fields.get("title", ""))})
        out.sort(key=lambda x: x["key"].lower())
        self._json({"keys": out})

    def api_citecheck(self, body):
        r"""Cross-check the \cite keys used in a LaTeX document against the
        entries defined in the workspace .bib files, reporting citations with
        no matching entry and entries that are never cited."""
        tex = body.get("path", "")
        if not path_allowed(tex) or not tex.lower().endswith(".tex"):
            self._json({"error": "select a .tex file"}, 400)
            return
        tex, note = resolve_main_tex(tex)
        text = expand_tex_inputs(tex)
        if not text:
            self._json({"error": "could not read the document"}, 500)
            return
        used = set()
        cite_re = re.compile(r"\\[a-zA-Z]*cite[a-zA-Z]*\*?\s*(?:\[[^\]]*\])*\s*\{([^}]*)\}")
        for m in cite_re.finditer(text):
            for k in m.group(1).split(","):
                k = k.strip()
                if k:
                    used.add(k)
        defined = set()
        root = get_root()
        for fp in (walk_workspace(root) if root else []):
            if not fp.lower().endswith(".bib"):
                continue
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    parsed = parse_bib(f.read())
            except OSError:
                continue
            if parsed:
                for e in parsed[1]:
                    if e["key"]:
                        defined.add(e["key"])
        labels = set(m.group(1).strip()
                     for m in re.finditer(r"\\label\{([^}]+)\}", text))
        refs = set()
        ref_re = re.compile(r"\\(?:eqref|autoref|pageref|nameref|vref|labelcref|"
                            r"cpageref|cref|Cref|ref)\*?\s*\{([^}]*)\}")
        for m in ref_re.finditer(text):
            for k in m.group(1).split(","):
                k = k.strip()
                if k:
                    refs.add(k)
        self._json({"ok": True, "file": os.path.basename(tex), "note": note,
                    "used": len(used), "defined": len(defined),
                    "undefined": sorted(used - defined),
                    "uncited": sorted(defined - used),
                    "labels": len(labels), "refs": len(refs),
                    "undefined_refs": sorted(refs - labels),
                    "unused_labels": sorted(labels - refs)})

    def api_labels(self, body):
        r"""Return every \label defined across the workspace .tex files, for
        \ref/\eqref/\cref autocompletion."""
        root = get_root()
        if not root:
            self._json({"labels": []})
            return
        labels, seen = [], set()
        lab_re = re.compile(r"\\label\{([^}]+)\}")
        for fp in walk_workspace(root):
            if not fp.lower().endswith(".tex"):
                continue
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    text = re.sub(r"(?<!\\)%.*", "", f.read())
            except OSError:
                continue
            for m in lab_re.finditer(text):
                k = m.group(1).strip()
                if k and k not in seen:
                    seen.add(k)
                    labels.append(k)
        labels.sort(key=str.lower)
        self._json({"labels": labels})

    def api_todos(self, body):
        r"""List TODO/FIXME/XXX/HACK/BUG markers and \todo notes across the
        workspace source files, with file and line, for a jump-to task list."""
        root = get_root()
        if not root:
            self._json({"todos": []})
            return
        marker = re.compile(r"\b(?:TODO|FIXME|XXX|HACK|BUG)\b|\\(?:todo|fixme)\b")
        out = []
        for fp in walk_workspace(root):
            if not fp.lower().endswith((".tex", ".py", ".md", ".bib", ".txt")):
                continue
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if marker.search(line):
                            out.append({"file": os.path.relpath(fp, root),
                                        "line": i, "text": line.strip()[:200]})
                            if len(out) >= 500:
                                break
            except OSError:
                continue
            if len(out) >= 500:
                break
        self._json({"todos": out})

    def api_prose(self, body):
        """Run the advisory academic-prose checks over a LaTeX document and
        return the flagged lines."""
        tex = body.get("path", "")
        if not path_allowed(tex) or not tex.lower().endswith(".tex"):
            self._json({"error": "select a .tex file"}, 400)
            return
        try:
            with open(tex, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            self._json({"error": str(e)}, 500)
            return
        self._json({"ok": True, "file": os.path.basename(tex),
                    "issues": prose_issues(text)})

    def api_outline(self, body):
        r"""Return the section outline of a LaTeX file, following \input and
        \include into its section files so the whole document appears."""
        path = body.get("path", "")
        if not path_allowed(path) or not path.lower().endswith(".tex"):
            self._json({"error": "not a .tex file"}, 400)
            return
        content = body.get("content")
        if not isinstance(content, str):
            content = None
        root = get_root() or os.path.dirname(path)
        self._json({"ok": True, "items": tex_outline(path, root, content)})

    def api_compilelog(self, body):
        """Parse the .log from the last compile into a list of errors and
        warnings with file and line, for a clickable problems view."""
        tex = body.get("path", "")
        if not path_allowed(tex) or not tex.lower().endswith(".tex"):
            self._json({"error": "select a .tex file"}, 400)
            return
        tex, note = resolve_main_tex(tex)
        log_path = os.path.splitext(tex)[0] + ".log"
        if not os.path.isfile(log_path):
            self._json({"ok": True, "problems": [], "errors": 0, "warnings": 0})
            return
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                log = f.read()
        except OSError as e:
            self._json({"error": str(e)}, 500)
            return
        folder = os.path.dirname(tex)
        root = get_root() or folder
        main_rel = os.path.relpath(tex, root)
        seen, problems = set(), []
        for p in parse_latex_log(log, os.path.basename(tex)):
            rel = main_rel
            tok = p["f"]
            if tok:
                cand = os.path.normpath(os.path.join(folder, tok))
                if path_allowed(cand) and os.path.isfile(cand):
                    rel = os.path.relpath(cand, root)
            key = (rel, p["line"], p["text"])
            if key in seen:
                continue
            seen.add(key)
            problems.append({"file": rel, "line": p["line"] or 1,
                             "kind": p["kind"], "text": p["text"]})
        errors = sum(1 for p in problems if p["kind"] == "error")
        self._json({"ok": True, "note": note, "problems": problems[:300],
                    "errors": errors, "warnings": len(problems) - errors})

    def api_writingstats(self, body):
        """Record and report writing progress: the current total word count of
        the workspace .tex files, how many words have been added today against
        a daily goal, and a short recent history."""
        import datetime
        root = get_root()
        if not root:
            self._json({"error": "open a workspace first"}, 400)
            return
        total = 0
        for fp in walk_workspace(root):
            if fp.lower().endswith(".tex"):
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        total += latex_word_count(f.read())["words"]
                except OSError:
                    pass
        today = datetime.date.today().isoformat()

        def mut(cfg):
            ws = cfg.setdefault("writing_stats", {}).setdefault(root, {})
            day = ws.get(today)
            if day is None:
                ws[today] = {"baseline": total, "latest": total}
            else:
                if total < day["baseline"]:
                    day["baseline"] = total
                day["latest"] = total
            for old in sorted(ws)[:-60]:
                ws.pop(old, None)
            return dict(ws)

        ws = update_config(mut)
        goal = int(get_settings().get("writing_goal", 500) or 500)
        written = max(0, total - ws[today]["baseline"])
        history = [{"date": d,
                    "written": max(0, ws[d].get("latest", 0) - ws[d].get("baseline", 0))}
                   for d in sorted(ws)[-7:]]
        self._json({"ok": True, "total": total, "written_today": written,
                    "goal": goal, "history": history})

    def api_find_search(self, q):
        """Keyword search over scholarly works. A single source can be chosen,
        or "all" queries OpenAlex, Semantic Scholar, and Crossref together,
        de-duplicates by DOI or title, and ranks peer-reviewed work above
        preprints. Only fields returned by each source are shown; nothing is
        invented."""
        query = (q.get("q") or "").strip()
        if len(query) < 3:
            self._json({"items": []})
            return
        source = q.get("source", "all")
        wtype = q.get("type", "all")
        focus = q.get("focus", "")   # "ee" strict, "boost" EE first, else relevance
        # A pasted DOI never matches keyword search, so resolve it directly and
        # place that record first.
        doi = find_doi(query)
        doi_hit = doi_lookup(doi) if doi else None
        pure_doi = bool(doi) and re.sub(r"\s+", "", query).lower() == re.sub(r"\s+", "", doi).lower()
        results, errors = [], []
        if not (pure_doi and doi_hit):
            fetchers = FIND_SOURCES.get(source, FIND_SOURCES["all"])
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(fetchers)) as ex:
                futs = {ex.submit(fn, query, wtype): name for name, fn in fetchers}
                done, not_done = concurrent.futures.wait(futs, timeout=25)
                for fut in done:
                    name = futs[fut]
                    try:
                        results.append((name, fut.result() or []))
                    except Exception as e:
                        errors.append(f"{name}: {e}")
                for fut in not_done:
                    fut.cancel()
        if doi_hit:
            doi_hit["_doi_exact"] = True
            results.insert(0, (doi_hit.get("_via", "DOI"), [doi_hit]))
        if not results:
            self._json({"error": "search failed: " + ("; ".join(errors) or "no response")}, 502)
            return
        merged = merge_items(results)
        if wtype and wtype != "all":
            want = "article" if wtype == "article" else wtype
            merged = [it for it in merged if it.get("_doi_exact")
                      or coarse_type(it.get("type")) == want
                      or (wtype == "article" and coarse_type(it.get("type")) == "conference")]
        if focus == "ee":
            merged = [it for it in merged if it.get("_doi_exact") or it.get("major")]
        merged.sort(key=lambda it: (0 if it.get("_doi_exact") else 1,
                                    -round(score_item(it, focus, query), 1),
                                    it.get("_rank", 9999)))
        merged = merged[:20]
        enrich_authors_from_crossref(merged)
        searched = list(dict.fromkeys(name for name, _ in results))
        for it in merged:
            it["sources"] = sorted(it.get("sources", []))
            it["preprint"] = is_preprint(it)
            for k in ("_rank", "_srcrank", "_doi_exact", "_via"):
                it.pop(k, None)
        self._json({"items": merged, "source": source,
                    "searched": searched, "partial": errors or None})

    def _library_items(self):
        """Every paper already in the workspace .bib files, as finder records
        carrying their citation key, so the library can list what the project
        already cites."""
        root = get_root()
        items, seen = [], set()
        if not root:
            return items
        clean = lambda v: re.sub(r"[{}]", "", v).strip().strip('"').strip() if v else ""
        for fp in walk_workspace(root):
            if not fp.lower().endswith(".bib"):
                continue
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    parsed = parse_bib(f.read())
            except OSError:
                continue
            if not parsed:
                continue
            relbib = os.path.relpath(fp, root)
            for e in parsed[1]:
                key = e["key"]
                if not key or key.lower() in seen:
                    continue
                seen.add(key.lower())
                fld = {k: clean(v) for k, v in e["fields"]}
                names = [a.strip() for a in re.split(r"\s+and\s+", fld.get("author", ""))
                         if a.strip()]
                doi = fld.get("doi", "").replace("https://doi.org/", "").replace("http://doi.org/", "")
                venue = fld.get("journal") or fld.get("booktitle") or fld.get("publisher") or ""
                publisher = classify_publisher(venue, fld.get("publisher", ""))
                items.append({
                    "doi": doi, "title": fld.get("title", ""),
                    "authors": ", ".join(names[:5]) + (" et al." if len(names) > 5 else ""),
                    "authors_list": names, "year": fld.get("year", ""), "venue": venue,
                    "publisher": publisher, "major": is_major_ee(venue, publisher),
                    "type": e["type"], "cited_by": None, "oa_url": None,
                    "url": fld.get("url") or (("https://doi.org/" + doi) if doi else ""),
                    "abstract": fld.get("abstract", ""), "key": key,
                    "bibfile": relbib, "cited": True,
                })
        items.sort(key=lambda it: str(it.get("year") or ""), reverse=True)
        return items

    def api_find_library(self, q):
        """List the papers already cited in the workspace .bib files."""
        items = self._library_items()
        for it in items:
            it["preprint"] = is_preprint(it)
            it["sources"] = ["references.bib"]
        self._json({"items": items, "count": len(items)})

    def api_refindex(self, body):
        """Write a human-readable Markdown index of the cited papers to the
        workspace, so the project keeps a browsable list of its references with
        links to reach each paper later."""
        root = get_root()
        if not root:
            self._json({"error": "open a workspace first"}, 400)
            return
        items = self._library_items()
        if not items:
            self._json({"error": "no references found in the workspace .bib files"}, 400)
            return
        lines = ["# References index", "",
                 f"{len(items)} cited works in this project. Generated by Beacon.", ""]
        for it in items:
            meta = "  ".join(p for p in (it.get("authors"), str(it.get("year") or ""),
                                         it.get("venue")) if p)
            lines.append(f"- **{it['title'] or '(untitled)'}**  \n"
                         f"  `{it['key']}` — {meta}"
                         + (f"  \n  <{it['url']}>" if it.get("url") else ""))
        path = os.path.join(root, "references_index.md")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError as e:
            self._json({"error": str(e)}, 500)
            return
        self._json({"ok": True, "path": path, "count": len(items),
                    "name": os.path.basename(path)})

    def api_find_bibtex(self, body):
        """Return a BibTeX entry for a finder item. Uses the real DOI record
        when a DOI exists, otherwise synthesizes one from the metadata."""
        doi = (body.get("doi") or "").strip()
        if doi:
            try:
                req = urllib.request.Request(
                    "https://doi.org/" + quote(doi),
                    headers={"User-Agent": CITE_UA, "Accept": "application/x-bibtex; charset=utf-8"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    raw = r.read().decode("utf-8", "replace").strip()
                    self._json({"text": prettify_bibtex(raw)})
                    return
            except Exception:
                pass  # fall back to synthesis
        self._json({"text": synth_bibtex(body)})

    def api_cite_search(self, q):
        query = (q.get("q") or "").strip()
        if len(query) < 3:
            self._json({"items": []})
            return
        url = ("https://api.crossref.org/works?rows=12&select="
               "DOI,title,author,issued,container-title,type&query="
               + quote(query))
        try:
            req = urllib.request.Request(url, headers={"User-Agent": CITE_UA,
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            self._json({"error": f"lookup failed: {e}"}, 502)
            return
        items = []
        for it in data.get("message", {}).get("items", []):
            authors = it.get("author", []) or []
            names = []
            for a in authors:
                n = ((a.get("given", "") + " " + a.get("family", "")).strip()
                     or a.get("name", ""))
                if n:
                    names.append(clean_citation_text(n))
            author_str = ", ".join(names[:5]) + (" et al." if len(names) > 5 else "")
            try:
                year = it.get("issued", {}).get("date-parts", [[None]])[0][0]
            except (IndexError, TypeError):
                year = None
            doi = it.get("DOI", "")
            items.append({
                "doi": doi,
                "title": clean_citation_text((it.get("title") or [""])[0]),
                "authors": author_str,
                "authors_list": names,
                "year": year,
                "venue": clean_citation_text((it.get("container-title") or [""])[0]),
                "type": it.get("type", ""),
                "url": ("https://doi.org/" + doi) if doi else "",
                "authoritative": True,
            })
        self._json({"items": items})

    def api_cite_format(self, q):
        doi = (q.get("doi") or "").strip()
        style = (q.get("style") or "bibtex").strip()
        if not doi or style not in CITE_STYLES:
            self._json({"error": "bad doi or style"}, 400)
            return
        if style == "bibtex":
            accept = "application/x-bibtex; charset=utf-8"
        else:
            accept = f"text/x-bibliography; style={style}; locale=en-US"
        try:
            req = urllib.request.Request("https://doi.org/" + quote(doi),
                                         headers={"User-Agent": CITE_UA, "Accept": accept})
            with urllib.request.urlopen(req, timeout=20) as r:
                text = r.read().decode("utf-8", "replace").strip()
        except Exception as e:
            self._json({"error": f"format failed: {e}"}, 502)
            return
        # BibTeX is reformatted to one field per line; the CSL bibliography
        # output is HTML and needs its tags and entities removed.
        if style == "bibtex":
            text = prettify_bibtex(text)
        else:
            text = clean_citation_text(text)
        self._json({"text": text, "style": style})

    def api_cite_append(self, body):
        bibtex = body.get("bibtex", "")
        path = body.get("path", "")
        if not bibtex.strip():
            self._json({"error": "no bibtex"}, 400)
            return
        if not path:
            # Default to a references.bib at the workspace root.
            root = get_root()
            path = os.path.join(root, "references.bib") if root else ""
        if not path_allowed(path) or not path.lower().endswith(".bib"):
            self._json({"error": "choose a .bib file inside the workspace"}, 400)
            return
        # Skip if the cite key is already present, so repeated adds are safe.
        key_match = re.search(r"@\w+\{([^,]+),", bibtex)
        key = key_match.group(1).strip() if key_match else None
        if key and os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                if re.search(r"@\w+\{\s*" + re.escape(key) + r"\s*,", f.read()):
                    self._json({"ok": True, "path": path, "key": key, "duplicate": True})
                    return
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n" + bibtex.strip() + "\n")
        self._json({"ok": True, "path": path, "key": key})

    def api_openurl(self, body):
        url = (body.get("url") or "").strip()
        if not re.match(r"^https://[\w.\-]+/", url):
            self._json({"error": "only https urls are opened"}, 400)
            return
        webbrowser.open(url)
        self._json({"ok": True})

    def api_extterm(self, body):
        """Open a real external terminal in the workspace. The embedded
        terminal is a pipe, not a TTY, so interactive tools that need a
        terminal (claude, vim, and similar) must run in a real console."""
        root = get_root()
        if not root:
            self._json({"error": "no workspace"}, 400)
            return
        new_console = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
        try:
            subprocess.Popen(["wt.exe", "-d", root])   # Windows Terminal
            self._json({"ok": True, "term": "wt"})
            return
        except OSError:
            pass
        try:
            subprocess.Popen(
                ["powershell", "-NoExit", "-Command", "Set-Location -LiteralPath '" + root.replace("'", "''") + "'"],
                creationflags=new_console,
            )
            self._json({"ok": True, "term": "powershell"})
        except OSError as e:
            self._json({"error": str(e)}, 500)

    def api_pickdir(self, body):
        # Show the real Windows folder dialog in a short lived subprocess, so
        # the server thread is not tied to a Tk main loop. The chosen path is
        # printed on the last non empty stdout line.
        start = body.get("start") or get_root() or os.path.expanduser("~")
        helper = (
            "import tkinter, tkinter.filedialog as fd;"
            "r=tkinter.Tk();r.withdraw();r.attributes('-topmost',True);"
            "p=fd.askdirectory(title='Choose workspace folder',initialdir=%r);"
            "print(p or '')" % start
        )
        try:
            result = subprocess.run(
                [sys.executable, "-c", helper], capture_output=True, text=True,
                timeout=300, creationflags=CREATION_FLAGS,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            self._json({"error": str(e)}, 500)
            return
        lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
        path = lines[-1] if lines else ""
        if path and os.path.isdir(path):
            self._json({"path": os.path.realpath(path)})
        else:
            self._json({"path": None})   # user cancelled

    def api_git_connect(self, body):
        """Connect a folder to an Overleaf project or a GitHub repository so
        Sync pushes to it. Use Overleaf for a LaTeX/document folder and GitHub
        for a code/computational folder; a folder may have both. The token is
        embedded in the remote URL, kept only in the repo's local .git/config
        (never committed, never sent back to the page)."""
        path = body.get("path", "")
        provider = body.get("provider", "overleaf")
        url = (body.get("url") or "").strip()
        token = (body.get("token") or "").strip()
        remote = (body.get("remote") or "").strip()
        if not path_allowed(path):
            self._json({"error": "outside workspace"}, 403)
            return
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+", token):
            self._json({"error": "the token looks invalid (letters, digits, - . _ only)"}, 400)
            return
        if provider == "overleaf":
            m = re.search(r"git\.overleaf\.com/([A-Za-z0-9]+)", url) or re.fullmatch(r"([A-Za-z0-9]+)", url)
            if not m:
                self._json({"error": "enter your Overleaf git URL (https://git.overleaf.com/…) or project id"}, 400)
                return
            auth_url = f"https://git:{token}@git.overleaf.com/{m.group(1)}"
            remote = remote or "overleaf"
        elif provider == "github":
            m = re.search(r"github\.com[:/]+([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+?)(?:\.git)?/?$", url)
            if not m:
                self._json({"error": "enter your GitHub repo URL (https://github.com/owner/repo)"}, 400)
                return
            auth_url = f"https://{token}@github.com/{m.group(1)}/{m.group(2)}.git"
            remote = remote or "origin"
        else:
            self._json({"error": "unknown provider"}, 400)
            return
        if not re.fullmatch(r"[\w.\-]+", remote):
            self._json({"error": "bad remote name"}, 400)
            return
        repo = find_repo_root(path)
        if not repo:
            repo = path if os.path.isdir(path) else os.path.dirname(path)
            run_cmd("git init", repo)
        run_cmd(f"git remote remove {remote}", repo)   # ignore if absent
        rc, out = run_cmd(f'git remote add {remote} "{auth_url}"', repo)
        if rc != 0:
            self._json({"error": scrub_secret(out, token)[:200] or "could not add remote"}, 500)
            return
        # Verify the credentials without downloading anything.
        rc, out = run_cmd(f"git ls-remote --heads {remote}", repo, timeout=30)
        self._json({"ok": True, "verified": rc == 0, "repo": repo, "remote": remote,
                    "detail": "" if rc == 0 else scrub_secret(out, token)[:200]})

    def api_app_update(self, body):
        """Update Beacon itself. Beacon's own folder is a clone of its GitHub
        project, so an installed copy can pull changes that were pushed there.
        `action=check` reports how many updates are available; `action=update`
        fast-forwards to the latest (never clobbering local edits or config)."""
        action = body.get("action", "check")
        repo = SCRIPT_DIR
        rc, _ = run_cmd("git rev-parse --is-inside-work-tree", repo)
        if rc != 0:
            self._json({"error": "This copy of Beacon is not a git checkout, so it cannot update itself. Reinstall by cloning the repository, or download the latest release from GitHub."}, 400)
            return
        rc, branch = run_cmd("git rev-parse --abbrev-ref HEAD", repo)
        branch = branch.strip() or "main"
        rc_u, upstream = run_cmd("git rev-parse --abbrev-ref --symbolic-full-name @{u}", repo)
        upstream = upstream.strip() if rc_u == 0 else "origin/" + branch
        remote_name = upstream.split("/", 1)[0]
        # Fetch the latest refs without merging so nothing is changed yet.
        rc, out = run_cmd(f"git fetch --quiet {remote_name}", repo, timeout=60)
        if rc != 0:
            self._json({"error": "Could not reach the update server. " + out.strip()[:200]}, 502)
            return
        def _count(rng):
            rc, o = run_cmd(f"git rev-list --count {rng}", repo)
            try:
                return int(o.strip())
            except ValueError:
                return 0
        behind = _count(f"HEAD..{upstream}")
        ahead = _count(f"{upstream}..HEAD")
        _, cur = run_cmd("git rev-parse --short HEAD", repo)
        if action == "check":
            self._json({"ok": True, "behind": behind, "ahead": ahead,
                        "current": cur.strip(), "branch": branch, "upstream": upstream})
            return
        # action == "update": apply the fetched changes.
        if behind == 0:
            self._json({"ok": True, "updated": False, "behind": 0, "ahead": ahead,
                        "message": "Beacon is already up to date."})
            return
        # Fast-forward only: this refuses to run if the local copy has its own
        # commits or edits, so a user's changes are never overwritten.
        rc, out = run_cmd(f"git merge --ff-only {upstream}", repo, timeout=60)
        if rc != 0:
            self._json({"error": "This copy has local changes to Beacon's own files, so it cannot fast-forward. Stash or discard them, then update again. " + out.strip()[:200]}, 409)
            return
        _, newcur = run_cmd("git rev-parse --short HEAD", repo)
        self._json({"ok": True, "updated": True, "from": cur.strip(), "to": newcur.strip(),
                    "behind": behind, "message": "Updated to the latest version. Restart Beacon to apply the change."})

    def api_gitop(self, body):
        path = body.get("path", "")
        op = body.get("op", "")
        remote = body.get("remote", "origin")
        info = git_info(path) if path_allowed(path) else {"repo": None}
        if not info.get("repo"):
            self._json({"error": "no git repository here"}, 400)
            return
        if not re.fullmatch(r"[\w.\-]+", remote or ""):
            self._json({"error": "bad remote name"}, 400)
            return
        if op == "sync":
            # Push to every remote by default, so a repo with both a GitHub
            # origin and an Overleaf remote publishes to both. The caller may
            # pass a subset in "remotes" to publish to only one destination
            # (code to GitHub, documents to Overleaf) chosen at publish time.
            all_remotes = list(info.get("remotes", {}).keys())
            if not all_remotes:
                self._json({"error": "no remotes to sync"}, 400)
                return
            chosen = body.get("remotes")
            if chosen is None:
                remotes = all_remotes
            elif (isinstance(chosen, list) and chosen
                  and all(isinstance(r, str) and r in all_remotes for r in chosen)):
                remotes = chosen
            else:
                self._json({"error": "unknown remote in selection"}, 400)
                return
            if not all(re.fullmatch(r"[\w.\-]+", r) for r in remotes):
                self._json({"error": "a remote name is unsafe to push"}, 400)
                return
            parts = []
            # Commit local edits first. Saving a file does not commit it, and
            # push only sends commits, so without this an edited file never
            # reaches Overleaf. The commit is skipped when nothing is staged.
            if info.get("dirty"):
                msg = (body.get("message") or "Update via Beacon").strip()
                safe_msg = msg.replace('"', "'")
                parts.append("git add -A")
                parts.append(f'git diff --cached --quiet || git commit -m "{safe_msg}"')
            rc_u, _ = run_cmd("git rev-parse --abbrev-ref @{u}", info["repo"])
            if rc_u == 0:
                # Merge (not ff-only) so local commits and remote changes combine.
                parts.append("git pull --no-edit")
            parts += [f"git push {r}" for r in remotes]
            cmd = " && ".join(parts)
            label = "sync: commit + pull + push " + ", ".join(remotes)
        else:
            cmds = {
                "pull": "git pull",
                "push": f"git push {remote}",
                "fetch": "git fetch --all",
                "status": "git status",
                "log": "git log --oneline -15",
                "commit": "git add -A && git commit -F -",
            }
            if op not in cmds:
                self._json({"error": "unknown op"}, 400)
                return
            cmd = cmds[op]
            label = cmd.split(" && ")[-1]
        stdin_text = None
        if op == "commit":
            message = (body.get("message") or "").strip()
            if not message:
                self._json({"error": "empty commit message"}, 400)
                return
            stdin_text = message + "\n"
        job_id = new_job()
        job_emit(job_id, "info", f"{label}  ({os.path.basename(info['repo'])})")
        stream_job(job_id, cmd, info["repo"], timeout=120, stdin_text=stdin_text)
        self._json({"job": job_id})

    def api_run(self, body):
        path = body.get("path", "")
        low = path.lower()
        if not path_allowed(path) or not low.endswith((".py", ".ipynb")):
            self._json({"error": "select a .py or .ipynb file"}, 400)
            return
        name = os.path.basename(path)
        if '"' in name:
            self._json({"error": "file name contains a quote"}, 400)
            return
        py = get_settings().get("python_path", "")
        if not py or not os.path.isfile(py):
            py = "python"
        folder = os.path.dirname(path)
        if low.endswith(".ipynb"):
            if run_cmd(f'"{py}" -m jupyter --version', folder, timeout=20)[0] != 0:
                self._json({"error": "Jupyter is not installed in the configured "
                                     "environment. Install it with 'pip install "
                                     "jupyter nbconvert', then retry."}, 400)
                return
            cmd = (f'"{py}" -m jupyter nbconvert --to notebook --execute --inplace '
                   f'--ExecutePreprocessor.timeout=600 "{name}"')
            info = f"jupyter: executing {name} (outputs saved in place)"
        else:
            cmd = f'"{py}" "{name}"'
            info = f"{os.path.basename(py)} {name}"
        job_id = new_job()
        job_emit(job_id, "info", info)
        stream_job(job_id, cmd, folder, timeout=1800)
        self._json({"job": job_id})

    def api_term(self, body):
        root = get_root()
        if not root:
            self._json({"error": "no workspace"}, 400)
            return
        term = get_terminal(root)
        text = body.get("input", "")
        try:
            term["proc"].stdin.write(text + "\n")
            term["proc"].stdin.flush()
        except OSError:
            self._json({"error": "terminal is not running"}, 500)
            return
        self._json({"ok": True})

    def api_claude(self, body):
        root = get_root()
        if not root:
            self._json({"error": "no workspace"}, 400)
            return
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            self._json({"error": "empty prompt"}, 400)
            return
        model_id = body.get("model") or "claude:fable"
        # Accept any provider:model id from a known provider (Ollama model
        # names are dynamic, so an exact allowlist would reject them).
        if ":" not in model_id or model_id.split(":", 1)[0] not in AI_PROVIDERS:
            model_id = "claude:fable"
        job_id = new_job()
        ai_chat(job_id, root, prompt, body.get("context", {}), model_id)
        self._json({"job": job_id})


def main():
    addr = ("127.0.0.1", PORT)
    try:
        server = ThreadingHTTPServer(addr, Handler)
    except OSError:
        # Port already bound: assume a workbench is running, just open a window.
        if "--no-browser" not in sys.argv:
            open_window()
        return
    if "--no-browser" not in sys.argv:
        threading.Thread(target=open_window, daemon=True).start()
    print(f"Beacon {APP_VERSION} (c) {APP_YEAR} {APP_AUTHOR} - http://127.0.0.1:{PORT}")
    server.serve_forever()


def find_pandoc():
    """Locate pandoc.exe. winget installs it under LocalAppData, which is not
    always on PATH yet in the launching shell, so check known locations too."""
    import shutil
    p = shutil.which("pandoc")
    if p:
        return p
    for c in (r"%LocalAppData%\Pandoc\pandoc.exe",
              r"%ProgramFiles%\Pandoc\pandoc.exe",
              r"%ProgramData%\chocolatey\bin\pandoc.exe"):
        full = os.path.expandvars(c)
        if os.path.isfile(full):
            return full
    return None


def find_browser():
    for path in (
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    ):
        full = os.path.expandvars(path)
        if os.path.isfile(full):
            return full
    return None


# Bump this whenever the app icon changes. A new profile directory forces the
# browser to regenerate the taskbar icon from the current favicon instead of
# reusing a stale one cached from an earlier launch.
APP_PROFILE = "beacon-profile-v1"
OLD_PROFILES = ("research-workbench-app", "research-workbench-profile-v1",
                "research-workbench-profile-v2")


def open_window():
    time.sleep(0.4)
    url = f"http://127.0.0.1:{PORT}"
    browser = find_browser()
    # Launch the browser exe directly in app mode with a dedicated profile.
    # A fresh profile makes the window regenerate its Windows taskbar icon from
    # the current favicon; older profiles that cached a stale icon are removed.
    if browser:
        tmp = tempfile.gettempdir()
        for old in OLD_PROFILES:
            shutil.rmtree(os.path.join(tmp, old), ignore_errors=True)
        profile = os.path.join(tmp, APP_PROFILE)
        try:
            subprocess.Popen(
                [browser, f"--app={url}", f"--user-data-dir={profile}",
                 "--no-first-run", "--no-default-browser-check"],
                creationflags=CREATION_FLAGS,
            )
            return
        except OSError:
            pass
    webbrowser.open(url)


if __name__ == "__main__":
    main()
