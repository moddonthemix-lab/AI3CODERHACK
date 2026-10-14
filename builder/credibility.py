"""Source credibility tiers.

A plain, predictable heuristic based on where a source lives. It is a
starting point, not a verdict: a tier-1 domain can still be wrong and a blog
can be right — which is why findings need corroboration (see research.py).

    1  primary / peer-reviewed / official
    2  reputable secondary (major press, reference works, preprints)
    3  general web (companies, blogs, trade sites, Wikipedia)
    4  unvetted (social media, forums, anonymous and .onion sites)
"""

from __future__ import annotations

from urllib.parse import urlparse

TIER_LABELS = {
    1: "primary / peer-reviewed / official",
    2: "reputable secondary",
    3: "general web",
    4: "unvetted — corroborate before trusting",
}

_TIER1 = {
    "who.int", "un.org", "europa.eu", "oecd.org", "worldbank.org", "imf.org", "bis.org",
    "doi.org", "nature.com", "science.org", "sciencedirect.com", "springer.com", "link.springer.com",
    "wiley.com", "onlinelibrary.wiley.com", "thelancet.com", "nejm.org", "jamanetwork.com",
    "bmj.com", "cell.com", "plos.org", "journals.plos.org", "pnas.org", "ieee.org",
    "ieeexplore.ieee.org", "acm.org", "dl.acm.org", "pubmed.ncbi.nlm.nih.gov", "cochranelibrary.com",
    "nist.gov", "rfc-editor.org", "ietf.org", "w3.org", "iso.org",
}
_TIER1_SUFFIXES = (".gov", ".mil", ".edu", ".int", ".gov.uk", ".ac.uk", ".gc.ca", ".gov.au", ".edu.au")

_TIER2 = {
    "arxiv.org", "biorxiv.org", "medrxiv.org", "ssrn.com", "papers.ssrn.com", "semanticscholar.org",
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "npr.org", "pbs.org", "economist.com",
    "ft.com", "wsj.com", "nytimes.com", "washingtonpost.com", "theguardian.com", "bloomberg.com",
    "cnbc.com", "forbes.com", "hbr.org", "mckinsey.com", "pewresearch.org", "gallup.com",
    "britannica.com", "statista.com", "nationalgeographic.com", "scientificamerican.com",
    "newscientist.com", "arstechnica.com", "wired.com", "techcrunch.com", "theverge.com",
    "investopedia.com", "morningstar.com", "nasdaq.com", "crunchbase.com", "owasp.org",
    "developer.mozilla.org", "docs.python.org",
}

_TIER4 = {
    "reddit.com", "quora.com", "x.com", "twitter.com", "facebook.com", "instagram.com",
    "tiktok.com", "youtube.com", "pinterest.com", "4chan.org", "answers.com", "fandom.com",
    "telegram.org", "t.me", "discord.com", "threads.net", "linkedin.com",
}


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host.removeprefix("www.").removeprefix("m.")


def _matches(host: str, names: set[str]) -> bool:
    return any(host == n or host.endswith("." + n) for n in names)


def tier(url: str) -> int:
    host = domain_of(url)
    if not host:
        return 4
    if host.endswith(".onion"):
        return 4
    if _matches(host, _TIER4):
        return 4
    if _matches(host, _TIER1) or host.endswith(_TIER1_SUFFIXES):
        return 1
    if _matches(host, _TIER2):
        return 2
    return 3


def rate(url: str) -> dict:
    t = tier(url)
    return {"domain": domain_of(url), "tier": t, "tier_label": TIER_LABELS[t]}
