#!/usr/bin/env python3
"""Fetch critical care medicine news from PubMed and journal RSS feeds."""

import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.error import URLError
from urllib.parse import urlencode
import re
import html

PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
SEARCH_WINDOW_DAYS = 4  # slightly wider than 72h to avoid gaps

RSS_FEEDS = [
    {
        "name": "Critical Care Medicine (CCM)",
        "url": "https://journals.lww.com/ccmjournal/rss/mostpopular.xml",
    },
    {
        "name": "Intensive Care Medicine",
        "url": "https://link.springer.com/search.rss?search-within=Journal&facet-journal-id=134&query=",
    },
    {
        "name": "CHEST Journal",
        "url": "https://journal.chestnet.org/rss/current.xml",
    },
    {
        "name": "NEJM",
        "url": "https://www.nejm.org/action/showFeed?jc=nejm&type=etoc&feed=rss",
    },
    {
        "name": "JAMA",
        "url": "https://jamanetwork.com/rss/site_3/67.xml",
    },
    {
        "name": "The Lancet",
        "url": "https://www.thelancet.com/rssfeed/lancet_current.xml",
    },
    {
        "name": "Annals of Internal Medicine",
        "url": "https://www.acpjournals.org/action/showFeed?type=etoc&feed=rss&jc=aim",
    },
    {
        "name": "American Journal of Respiratory & Critical Care Medicine",
        "url": "https://www.atsjournals.org/action/showFeed?type=etoc&feed=rss&jc=ajrccm",
    },
]

PUBMED_QUERIES = [
    'critical care[MeSH] AND ("last 4 days"[PDat])',
    'intensive care units[MeSH] AND ("last 4 days"[PDat])',
    '("mechanical ventilation"[Title/Abstract] OR "septic shock"[Title/Abstract] OR "ARDS"[Title/Abstract] OR "vasopressor"[Title/Abstract]) AND ("last 4 days"[PDat]) AND (Clinical Trial[pt] OR Review[pt] OR "Practice Guideline"[pt])',
    'critical illness[Title/Abstract] AND guideline[Title/Abstract] AND ("last 90 days"[PDat])',
]

CC_KEYWORDS = re.compile(
    r'\b(critical care|intensive care|ICU|mechanical ventilation|sepsis|septic shock|'
    r'ARDS|acute respiratory distress|vasopressor|ventilator|intubation|prone|'
    r'extubation|sedation|analgesia|delirium|renal replacement|CRRT|ECMO|'
    r'hemodynamic|resuscitation|fluid|norepinephrine|vasopressin|corticosteroid|'
    r'proning|weaning|critically ill|ventilat)\b',
    re.IGNORECASE,
)


def fetch_url(url, timeout=15):
    try:
        req = Request(url, headers={"User-Agent": "CriticalCareNewsFeed/1.0"})
        with urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  [WARN] Failed to fetch {url}: {e}")
        return None


def pubmed_search(query, retmax=20):
    params = urlencode({"db": "pubmed", "term": query, "retmax": retmax,
                        "retmode": "json", "sort": "pub_date"})
    data = fetch_url(f"{PUBMED_BASE}/esearch.fcgi?{params}")
    if not data:
        return []
    ids = json.loads(data).get("esearchresult", {}).get("idlist", [])
    return ids


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
            abstract = " ".join(
                t.text or "" for t in art.findall(".//AbstractText")
            ).strip()
            journal = art.findtext(".//Journal/Title", "") or art.findtext(".//MedlineTA", "")
            pub_date_el = art.find(".//PubDate")
            year = pub_date_el.findtext("Year", "") if pub_date_el is not None else ""
            month = pub_date_el.findtext("Month", "") if pub_date_el is not None else ""
            day = pub_date_el.findtext("Day", "") if pub_date_el is not None else ""
            pub_date = " ".join(filter(None, [year, month, day]))

            authors_el = art.findall(".//Author")[:3]
            authors = []
            for a in authors_el:
                ln = a.findtext("LastName", "")
                fn = a.findtext("ForeName", "")
                if ln:
                    authors.append(f"{ln} {fn}".strip())
            if len(art.findall(".//Author")) > 3:
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
            print(f"  [WARN] parse error: {e}")
    return articles


def parse_rss(xml_text, feed_name):
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"  [WARN] RSS parse error for {feed_name}: {e}")
        return items

    ns = {"atom": "http://www.w3.org/2005/Atom",
          "content": "http://purl.org/rss/1.0/modules/content/"}

    # Handle both RSS 2.0 and Atom
    entries = root.findall(".//item") or root.findall(".//atom:entry", ns)
    cutoff = datetime.now(timezone.utc) - timedelta(days=SEARCH_WINDOW_DAYS)

    for entry in entries:
        title = (entry.findtext("title") or entry.findtext("atom:title", namespaces=ns) or "").strip()
        link = (entry.findtext("link") or entry.findtext("atom:link", namespaces=ns) or "").strip()
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
    seen_titles = {}
    out = []
    for a in articles:
        key = re.sub(r"[^a-z0-9]", "", a["title"].lower())[:80]
        if key and key not in seen_titles:
            seen_titles[key] = True
            out.append(a)
    return out


def main():
    print("=== Critical Care Medicine News Fetcher ===")
    all_articles = []

    # PubMed
    print("\n[PubMed] Searching...")
    seen_ids = set()
    for q in PUBMED_QUERIES:
        print(f"  Query: {q[:70]}...")
        pmids = pubmed_search(q, retmax=25)
        new_ids = [p for p in pmids if p not in seen_ids]
        seen_ids.update(new_ids)
        arts = pubmed_fetch(new_ids)
        all_articles.extend(arts)
        time.sleep(0.4)  # NCBI rate limit courtesy

    print(f"  PubMed articles: {len(all_articles)}")

    # RSS feeds
    print("\n[RSS] Fetching journal feeds...")
    for feed in RSS_FEEDS:
        print(f"  {feed['name']}...")
        xml = fetch_url(feed["url"])
        if xml:
            items = parse_rss(xml, feed["name"])
            all_articles.extend(items)
            print(f"    -> {len(items)} matching items")

    all_articles = deduplicate(all_articles)

    # Sort: newest first (articles without parsed dates go last)
    def sort_key(a):
        d = parse_date(a["date"])
        return d.isoformat() if d else "0000"

    all_articles.sort(key=sort_key, reverse=True)

    feed = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "count": len(all_articles),
        "articles": all_articles,
    }

    with open("feed.json", "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)

    print(f"\nDone. {len(all_articles)} articles written to feed.json")


if __name__ == "__main__":
    main()
