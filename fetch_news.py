#!/usr/bin/env python3
"""
Fetch critical care medicine news from PubMed, journal RSS feeds,
and Open Evidence; then synthesize with Claude.
"""

import json
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.parse import urlencode
import re
import html

PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
OE_BASE = "https://api.openevidence.com"
SEARCH_WINDOW_DAYS = 4  # slightly wider than 72 h to avoid gaps

RSS_FEEDS = [
    {"name": "Critical Care Medicine (CCM)",
     "url": "https://journals.lww.com/ccmjournal/rss/mostpopular.xml"},
    {"name": "Intensive Care Medicine",
     "url": "https://link.springer.com/search.rss?search-within=Journal&facet-journal-id=134&query="},
    {"name": "CHEST Journal",
     "url": "https://journal.chestnet.org/rss/current.xml"},
    {"name": "NEJM",
     "url": "https://www.nejm.org/action/showFeed?jc=nejm&type=etoc&feed=rss"},
    {"name": "JAMA",
     "url": "https://jamanetwork.com/rss/site_3/67.xml"},
    {"name": "The Lancet",
     "url": "https://www.thelancet.com/rssfeed/lancet_current.xml"},
    {"name": "Annals of Internal Medicine",
     "url": "https://www.acpjournals.org/action/showFeed?type=etoc&feed=rss&jc=aim"},
    {"name": "American Journal of Respiratory & Critical Care Medicine",
     "url": "https://www.atsjournals.org/action/showFeed?type=etoc&feed=rss&jc=ajrccm"},
]

PUBMED_QUERIES = [
    'critical care[MeSH] AND ("last 4 days"[PDat])',
    'intensive care units[MeSH] AND ("last 4 days"[PDat])',
    '("mechanical ventilation"[Title/Abstract] OR "septic shock"[Title/Abstract] OR '
    '"ARDS"[Title/Abstract] OR "vasopressor"[Title/Abstract]) AND ("last 4 days"[PDat]) '
    'AND (Clinical Trial[pt] OR Review[pt] OR "Practice Guideline"[pt])',
    'critical illness[Title/Abstract] AND guideline[Title/Abstract] AND ("last 90 days"[PDat])',
]

OE_QUERIES = [
    "What are the most recent practice-changing guidelines and trials in critical care medicine?",
    "Latest evidence on mechanical ventilation strategies and weaning in ICU patients",
    "Recent updates on sepsis and septic shock management including vasopressors and fluid resuscitation",
    "New evidence on ARDS treatment including prone positioning, ECMO, and lung-protective ventilation",
    "Recent critical care trials on sedation, analgesia, delirium prevention, and ICU liberation",
]

CC_KEYWORDS = re.compile(
    r'\b(critical care|intensive care|ICU|mechanical ventilation|sepsis|septic shock|'
    r'ARDS|acute respiratory distress|vasopressor|ventilator|intubation|prone|'
    r'extubation|sedation|analgesia|delirium|renal replacement|CRRT|ECMO|'
    r'hemodynamic|resuscitation|fluid|norepinephrine|vasopressin|corticosteroid|'
    r'proning|weaning|critically ill|ventilat)\b',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def fetch_url(url, timeout=20, headers=None):
    h = {"User-Agent": "CriticalCareNewsFeed/1.0"}
    if headers:
        h.update(headers)
    try:
        req = Request(url, headers=h)
        with urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  [WARN] Failed to fetch {url[:80]}: {e}")
        return None


class APIAuthError(Exception):
    """Raised when a remote API returns 401 or 403."""


def post_json(url, payload, headers=None):
    import urllib.request
    h = {"Content-Type": "application/json", "User-Agent": "CriticalCareNewsFeed/1.0"}
    if headers:
        h.update(headers)
    try:
        data = json.dumps(payload).encode("utf-8")
        req = Request(url, data=data, headers=h, method="POST")
        with urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        # Surface auth failures distinctly so callers can stop immediately
        msg = str(e)
        if "401" in msg or "403" in msg or "Unauthorized" in msg or "Forbidden" in msg:
            raise APIAuthError(f"Authentication failed for {url}: {msg}")
        print(f"  [WARN] POST failed {url[:80]}: {e}")
        return None


# ---------------------------------------------------------------------------
# PubMed
# ---------------------------------------------------------------------------

def pubmed_search(query, retmax=20):
    params = urlencode({"db": "pubmed", "term": query, "retmax": retmax,
                        "retmode": "json", "sort": "pub_date"})
    data = fetch_url(f"{PUBMED_BASE}/esearch.fcgi?{params}")
    if not data:
        return []
    return json.loads(data).get("esearchresult", {}).get("idlist", [])


def pubmed_fetch(pmids):
    if not pmids:
        return []
    params = urlencode({"db": "pubmed", "id": ",".join(pmids),
                        "retmode": "xml", "rettype": "abstract"})
    data = fetch_url(f"{PUBMED_BASE}/efetch.fcgi?{params}")
    if not data:
        return []

    articles = []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []

    for art in root.findall(".//PubmedArticle"):
        try:
            pmid = art.findtext(".//PMID", "")
            title = art.findtext(".//ArticleTitle", "").strip()
            abstract = " ".join(t.text or "" for t in art.findall(".//AbstractText")).strip()
            journal = art.findtext(".//Journal/Title", "") or art.findtext(".//MedlineTA", "")
            pub_date_el = art.find(".//PubDate")
            year  = pub_date_el.findtext("Year", "")  if pub_date_el is not None else ""
            month = pub_date_el.findtext("Month", "") if pub_date_el is not None else ""
            day   = pub_date_el.findtext("Day", "")   if pub_date_el is not None else ""
            pub_date = " ".join(filter(None, [year, month, day]))

            authors_el = art.findall(".//Author")
            authors = []
            for a in authors_el[:3]:
                ln = a.findtext("LastName", "")
                fn = a.findtext("ForeName", "")
                if ln:
                    authors.append(f"{ln} {fn}".strip())
            if len(authors_el) > 3:
                authors.append("et al.")

            pub_types = [pt.text for pt in art.findall(".//PublicationType") if pt.text]

            articles.append({
                "id": f"pmid_{pmid}",
                "title": html.unescape(title),
                "abstract": html.unescape(abstract)[:1200],
                "source": journal,
                "authors": ", ".join(authors),
                "pub_types": pub_types,
                "date": pub_date,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "category": classify(title, abstract, pub_types),
            })
        except Exception as e:
            print(f"  [WARN] PubMed parse error: {e}")
    return articles


# ---------------------------------------------------------------------------
# RSS feeds
# ---------------------------------------------------------------------------

def parse_rss(xml_text, feed_name):
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"  [WARN] RSS parse error for {feed_name}: {e}")
        return items

    ns = {"atom": "http://www.w3.org/2005/Atom",
          "content": "http://purl.org/rss/1.0/modules/content/"}
    entries = root.findall(".//item") or root.findall(".//atom:entry", ns)
    cutoff = datetime.now(timezone.utc) - timedelta(days=SEARCH_WINDOW_DAYS)

    for entry in entries:
        title = (entry.findtext("title") or
                 entry.findtext("atom:title", namespaces=ns) or "").strip()
        link = (entry.findtext("link") or
                entry.findtext("atom:link", namespaces=ns) or "").strip()
        if not link:
            link_el = entry.find("atom:link", ns)
            if link_el is not None:
                link = link_el.get("href", "")
        desc = (entry.findtext("description") or
                entry.findtext("atom:summary", namespaces=ns) or
                entry.findtext("content:encoded", namespaces=ns) or "").strip()
        desc = re.sub(r"<[^>]+>", " ", desc)
        desc = re.sub(r"\s+", " ", desc).strip()[:1200]

        pub_date_str = (entry.findtext("pubDate") or
                        entry.findtext("atom:published", namespaces=ns) or
                        entry.findtext("atom:updated", namespaces=ns) or "")
        parsed_date = parse_date(pub_date_str)
        if parsed_date and parsed_date < cutoff:
            continue

        title_clean = html.unescape(re.sub(r"<[^>]+>", "", title))
        if not CC_KEYWORDS.search(title_clean) and not CC_KEYWORDS.search(desc):
            continue

        item_id = re.sub(r"[^a-z0-9]", "_", link.lower())[-60:]
        items.append({
            "id": f"rss_{item_id}",
            "title": title_clean,
            "abstract": html.unescape(desc),
            "source": feed_name,
            "authors": "",
            "pub_types": [],
            "date": parsed_date.strftime("%Y %b %d") if parsed_date else pub_date_str[:20],
            "url": link,
            "category": classify(title_clean, desc, []),
        })
    return items


# ---------------------------------------------------------------------------
# Open Evidence
# ---------------------------------------------------------------------------

def query_open_evidence(query, api_key):
    """Query the Open Evidence search API and return a result dict.
    Raises APIAuthError on 401/403. Returns None on other failures."""
    headers = {"Authorization": f"Bearer {api_key}"}
    for endpoint in ("/search", "/ask_question"):
        result = post_json(
            f"{OE_BASE}{endpoint}",
            {"query": query},
            headers=headers,
        )
        if result is not None:
            return result
    return None


def collect_open_evidence(api_key):
    """Run all OE queries. Returns (results, error_message).
    Stops immediately on auth failure."""
    results = []
    for q in OE_QUERIES:
        print(f"  OE query: {q[:70]}...")
        try:
            r = query_open_evidence(q, api_key)
        except APIAuthError as e:
            msg = "Invalid or expired Open Evidence API key. Update it in Settings."
            print(f"  [ERROR] {msg}\n  Detail: {e}")
            return [], msg
        if r:
            results.append({"query": q, "response": r})
        time.sleep(1)
    return results, None


def oe_results_to_text(oe_results):
    """Flatten OE results into a readable block for the Claude prompt."""
    lines = []
    for item in oe_results:
        lines.append(f"## Query: {item['query']}")
        r = item["response"]
        # OE may return answer/text/content/results depending on endpoint
        answer = (r.get("answer") or r.get("text") or r.get("content") or
                  r.get("response") or json.dumps(r, indent=2))
        if isinstance(answer, dict):
            answer = json.dumps(answer, indent=2)
        lines.append(str(answer)[:3000])
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude synthesis
# ---------------------------------------------------------------------------

SYNTHESIS_PROMPT = """You are a senior critical care physician and clinical informaticist.
Below are two data sources collected in the last 72 hours:

---
SOURCE A: PubMed + Journal RSS articles
{article_block}

---
SOURCE B: Open Evidence AI-synthesized responses to targeted clinical queries
{oe_block}

---
Your task:
1. Write a concise CLINICAL DIGEST (400-600 words) summarizing the most important and
   practice-relevant developments across both sources. Organize under these headings:
   - **Practice-Changing Highlights** (top 2-3 findings clinicians should act on)
   - **Guidelines & Consensus Updates**
   - **Key Trials & Research**
   - **Notable Discussions**
2. After the digest, output a JSON array called "highlights" listing the 5 most important
   article titles from SOURCE A (exact titles). Format:
   {"highlights": ["title1", "title2", ...]}

Respond with the digest text first, then the JSON on a new line prefixed with JSON_HIGHLIGHTS:
"""


def build_article_block(articles, max_articles=40):
    lines = []
    for a in articles[:max_articles]:
        lines.append(f"- [{a['category']}] {a['title']} ({a['source']}, {a['date']})")
        if a.get("abstract"):
            lines.append(f"  Abstract: {a['abstract'][:300]}")
    return "\n".join(lines)


def synthesize_with_claude(articles, oe_results, api_key):
    """Call Claude to produce a combined digest.
    Returns (digest, highlights, error_message)."""
    try:
        import anthropic
    except ImportError:
        return None, [], "anthropic Python package not installed on the runner."

    article_block = build_article_block(articles)
    oe_block = oe_results_to_text(oe_results) if oe_results else "(No Open Evidence results available)"
    prompt = SYNTHESIS_PROMPT.format(article_block=article_block, oe_block=oe_block)

    client = anthropic.Anthropic(api_key=api_key)
    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1800,
            messages=[{"role": "user", "content": prompt}],
        )
        full_text = message.content[0].text
    except Exception as e:
        msg = str(e)
        if "401" in msg or "403" in msg or "invalid" in msg.lower() or "authentication" in msg.lower():
            err = "Invalid or expired Anthropic API key. Update it in Settings."
        else:
            err = f"Claude API error: {msg}"
        print(f"  [ERROR] {err}")
        return None, [], err

    digest = full_text
    highlights = []
    marker = "JSON_HIGHLIGHTS:"
    if marker in full_text:
        parts = full_text.split(marker, 1)
        digest = parts[0].strip()
        try:
            highlights = json.loads(parts[1].strip()).get("highlights", [])
        except json.JSONDecodeError:
            pass

    return digest, highlights, None


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def parse_date(s):
    if not s:
        return None
    fmts = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d",
    ]
    for fmt in fmts:
        try:
            dt = datetime.strptime(s.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def classify(title, abstract, pub_types):
    text = (title + " " + abstract).lower()
    pt_lower = " ".join(pub_types).lower()
    if "guideline" in pt_lower or "guideline" in text or "consensus" in text or "recommendation" in text:
        return "Guideline / Consensus"
    if "randomized" in text or "rct" in text or "clinical trial" in pt_lower or "trial" in text:
        return "Clinical Trial"
    if "systematic review" in text or "meta-analysis" in text or "meta analysis" in text:
        return "Systematic Review / Meta-Analysis"
    if "review" in pt_lower or "review" in text:
        return "Review"
    if "case report" in pt_lower or "case series" in pt_lower:
        return "Case Report"
    return "Research Article"


def deduplicate(articles):
    seen, out = {}, []
    for a in articles:
        key = re.sub(r"[^a-z0-9]", "", a["title"].lower())[:80]
        if key and key not in seen:
            seen[key] = True
            out.append(a)
    return out


def sort_key(a):
    d = parse_date(a["date"])
    return d.isoformat() if d else "0000"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=== Critical Care Medicine News Fetcher ===")
    oe_api_key = os.environ.get("OPENEVIDENCE_API_KEY", "")
    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    all_articles = []

    # 1. PubMed
    print("\n[PubMed] Searching...")
    seen_ids: set = set()
    for q in PUBMED_QUERIES:
        print(f"  Query: {q[:70]}...")
        pmids = pubmed_search(q, retmax=25)
        new_ids = [p for p in pmids if p not in seen_ids]
        seen_ids.update(new_ids)
        all_articles.extend(pubmed_fetch(new_ids))
        time.sleep(0.4)
    print(f"  PubMed articles: {len(all_articles)}")

    # 2. RSS feeds
    print("\n[RSS] Fetching journal feeds...")
    for feed in RSS_FEEDS:
        print(f"  {feed['name']}...")
        xml = fetch_url(feed["url"])
        if xml:
            items = parse_rss(xml, feed["name"])
            all_articles.extend(items)
            print(f"    -> {len(items)} matching items")

    all_articles = deduplicate(all_articles)
    all_articles.sort(key=sort_key, reverse=True)
    print(f"\nTotal deduplicated articles: {len(all_articles)}")

    # 3. Open Evidence
    oe_results = []
    oe_error = None
    if oe_api_key:
        print("\n[Open Evidence] Querying...")
        oe_results, oe_error = collect_open_evidence(oe_api_key)
        if oe_error:
            print(f"  Stopped: {oe_error}")
        else:
            print(f"  {len(oe_results)} OE responses collected")
    else:
        print("\n[Open Evidence] OPENEVIDENCE_API_KEY not set — skipping")

    # 4. Claude synthesis
    digest = None
    highlights = []
    claude_error = None
    if anthropic_api_key:
        print("\n[Claude] Synthesizing combined digest...")
        digest, highlights, claude_error = synthesize_with_claude(
            all_articles, oe_results, anthropic_api_key
        )
        if claude_error:
            print(f"  Stopped: {claude_error}")
        elif digest:
            print("  Digest generated successfully")
    else:
        print("\n[Claude] ANTHROPIC_API_KEY not set — skipping synthesis")

    # Mark highlighted articles
    highlight_set = {re.sub(r"[^a-z0-9]", "", h.lower())[:80] for h in highlights}
    for a in all_articles:
        key = re.sub(r"[^a-z0-9]", "", a["title"].lower())[:80]
        a["highlighted"] = key in highlight_set

    feed = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "count": len(all_articles),
        "digest": digest,
        "oe_query_count": len(oe_results),
        "oe_error": oe_error,
        "claude_error": claude_error,
        "articles": all_articles,
    }

    with open("feed.json", "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)

    print(f"\nDone. {len(all_articles)} articles + digest written to feed.json")


if __name__ == "__main__":
    main()
