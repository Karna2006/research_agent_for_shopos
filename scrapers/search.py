"""DuckDuckGo search wrapper — structured results, never crashes the pipeline."""
import re
from ddgs import DDGS


def _safe_text(query: str, max_results: int) -> list[dict]:
    try:
        return DDGS().text(query, max_results=max_results) or []
    except Exception:
        return []


def _safe_news(query: str, timelimit: str, max_results: int) -> list[dict]:
    try:
        return DDGS().news(query, timelimit=timelimit, max_results=max_results) or []
    except Exception:
        return []


class SearchAgent:
    def search(self, query: str, max_results: int = 5) -> list[dict]:
        """Organic text search → [{title, url, snippet}]."""
        raw = _safe_text(query, max_results)
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "snippet": r.get("body", ""),
            }
            for r in raw
        ]

    def search_news(self, query: str, days: int = 90) -> list[dict]:
        """Recent news about a brand → [{title, url, snippet, published}].

        DuckDuckGo news API accepts timelimit: 'd' (day), 'w' (week), 'm' (month).
        We map days → the closest supported timelimit.
        """
        if days <= 1:
            timelimit = "d"
        elif days <= 7:
            timelimit = "w"
        else:
            timelimit = "m"

        raw = _safe_news(query, timelimit=timelimit, max_results=10)
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("body", ""),
                "published": r.get("date", ""),
                "source": r.get("source", ""),
            }
            for r in raw
        ]

    def find_competitors(self, brand_name: str, category: str) -> list[dict]:
        """Search for top competitors of a brand in a given category.

        Runs two complementary queries and deduplicates by domain.
        Returns [{title, url, snippet}].
        """
        queries = [
            f"top competitors of {brand_name} {category} brand India",
            f"alternatives to {brand_name} {category} online store",
        ]
        seen_domains: set[str] = set()
        competitors: list[dict] = []

        for q in queries:
            for result in self.search(q, max_results=8):
                url = result.get("url", "")
                domain = _extract_domain(url)
                brand_domain = _extract_domain(brand_name)
                # Skip results that point back to the brand itself or already seen
                if domain and domain not in seen_domains and brand_name.lower() not in domain:
                    seen_domains.add(domain)
                    competitors.append(result)

        return competitors[:10]


def _extract_domain(url: str) -> str:
    """Pull the bare domain from a URL or brand name string."""
    match = re.search(r"(?:https?://)?(?:www\.)?([^/?\s]+)", url.lower())
    return match.group(1) if match else url.lower().strip()
