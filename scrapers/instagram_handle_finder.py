"""Instagram handle discovery — multi-strategy, confidence-weighted.

Discovery pipeline (all strategies run in parallel):
  S1. Instagram search API  — IG's own /users/search/ endpoint (top signal)
  S2. Website deep-scan     — parse <a> tags, footer, Shopify/theme JS globals
  S3. DDG search            — targeted site:instagram.com queries
  S4. Link-in-bio scrape    — Linktree/Beacons.ai if surfaced by S3
  S5. Pattern fallback      — generated variants, last resort, confidence = "guess"

Confidence levels:
  "confirmed"  — website source or IG search top result + domain cross-match  (0.95)
  "high"       — IG search top result + brand name match                       (0.85)
  "medium"     — validated profile, partial name match                         (0.70)
  "low"        — validated profile exists, no name match                       (0.50)
  "guess"      — pattern-only, not API-validated                               (0.30)
"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse, quote

import httpx

_IG_BLOCKLIST = frozenset({
    "p", "reel", "reels", "explore", "accounts", "stories", "tv",
    "about", "help", "legal", "blog", "press", "developers", "direct",
    "privacy", "safety", "terms", "lite", "download", "create",
})

_IG_SEARCH_API  = "https://i.instagram.com/api/v1/users/search/?q={q}&count=10"
_IG_PROFILE_API = "https://i.instagram.com/api/v1/users/web_profile_info/?username={}"

_MOBILE_HEADERS = {
    "User-Agent": "Instagram 219.0.0.12.117 Android",
    "X-IG-App-ID": "936619743392459",
    "Accept": "*/*",
    "Accept-Language": "en-US",
}
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def _valid_handle(h: str) -> bool:
    h = h.strip("._").lower()
    return bool(h) and h not in _IG_BLOCKLIST and 2 <= len(h) <= 30

def _clean(h: str) -> str:
    return h.strip("._@").lower()

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())

def _brand_words(brand_name: str) -> list[str]:
    return [w for w in re.split(r"\W+", brand_name.lower()) if len(w) > 2]

def _domain_from_url(url: str) -> str:
    if not url:
        return ""
    return urlparse(url).netloc.replace("www.", "").lower()


# ── S1: Instagram search API ───────────────────────────────────────────────────

async def _strategy_ig_search(
    brand_name: str,
    client: httpx.AsyncClient,
) -> list[dict]:
    """Query Instagram's own /users/search/ — returns real ranked accounts.

    Returns list of candidate dicts:
      {handle, full_name, followers, is_verified, source, search_rank}
    """
    # Try brand name as-is + a cleaned slug variant
    queries: list[str] = [brand_name]
    slug = re.sub(r"[^a-z0-9 ]", " ", brand_name).strip()
    if slug.lower() != brand_name.lower():
        queries.append(slug)

    seen: set[str] = set()
    results: list[dict] = []

    for query in queries:
        try:
            url = _IG_SEARCH_API.format(q=quote(query))
            r = await client.get(url, headers=_MOBILE_HEADERS, timeout=8, follow_redirects=True)
            if r.status_code != 200:
                continue
            users = r.json().get("users", [])
            for rank, user in enumerate(users):
                handle = (user.get("username") or "").strip()
                if not handle or not _valid_handle(handle):
                    continue
                key = _clean(handle)
                if key in seen:
                    continue
                seen.add(key)
                results.append({
                    "handle":      handle,
                    "full_name":   user.get("full_name", "") or "",
                    "followers":   user.get("follower_count") or 0,
                    "is_verified": bool(user.get("is_verified")),
                    "source":      "ig_search",
                    "search_rank": rank,  # 0 = top result
                })
        except Exception:
            continue

    return results


# ── S2: Website deep-scan ──────────────────────────────────────────────────────

_IG_HREF_RE  = re.compile(r'href=["\']https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]{2,30})/?["\']', re.I)
_IG_PLAIN_RE = re.compile(r'instagram\.com/([A-Za-z0-9_.]{2,30})/?(?:["\'\s>\)\]]|$)', re.I)
# JS bundle URLs: <script src="..."> — prefer chunk/main/app bundles, skip tiny inline scripts
_JS_SRC_RE   = re.compile(r'<script[^>]+src=["\']([^"\']+\.js[^"\']*)["\']', re.I)


def _extract_ig_handles(text: str, seen: set[str]) -> list[str]:
    """Pull IG handles from any text (HTML or JS). Updates seen in-place."""
    found: list[str] = []
    for m in _IG_HREF_RE.finditer(text):
        h = _clean(m.group(1))
        if _valid_handle(h) and h not in seen:
            seen.add(h); found.append(m.group(1))
    for m in _IG_PLAIN_RE.finditer(text):
        h = _clean(m.group(1))
        if _valid_handle(h) and h not in seen:
            seen.add(h); found.append(m.group(1))
    return found


async def _strategy_website(
    website_url: str,
    client: httpx.AsyncClient,
) -> list[str]:
    """Scrape brand's own website for Instagram handles.

    Pass 1: homepage + common sub-pages via httpx.
    Pass 2: if nothing found, fetch up to 3 JS bundles linked from homepage
            (catches React/Next.js SPAs where social links live in the bundle).
    """
    if not website_url:
        return []

    base = website_url.rstrip("/")
    pages = [
        base,
        f"{base}/about",
        f"{base}/about-us",
        f"{base}/contact",
        f"{base}/contact-us",
        f"{base}/pages/about",
        f"{base}/pages/contact",
    ]

    async def _fetch(url: str) -> str:
        try:
            r = await client.get(url, headers=_BROWSER_HEADERS, timeout=8, follow_redirects=True)
            return r.text if r.status_code == 200 else ""
        except Exception:
            return ""

    pages_html = await asyncio.gather(*[_fetch(p) for p in pages])
    homepage_html = pages_html[0]  # keep for JS bundle extraction

    seen: set[str] = set()
    handles: list[str] = []

    for html in pages_html:
        if html:
            handles.extend(_extract_ig_handles(html, seen))

    # ── Pass 2: JS bundle scan (React/Next.js SPAs) ────────────────────────────
    # Fires when HTML scan found nothing. Scans up to 12 bundles, short-circuits
    # on first hit. Skips known infra bundles (polyfills/webpack/framework/runtime)
    # that never contain social links.
    _INFRA_SKIP = re.compile(
        r"(polyfill|webpack|runtime|framework|commons|sentry|analytics"
        r"|gtm|beacon|recaptcha|hotjar|intercom|crisp|drift|hubspot)",
        re.I,
    )
    if not handles and homepage_html:
        from urllib.parse import urlparse as _up
        _p = _up(website_url)
        _netloc_base = f"{_p.scheme}://{_p.netloc}"

        bundle_urls: list[str] = []
        for m in _JS_SRC_RE.finditer(homepage_html):
            src = m.group(1)
            if src.startswith("//"): src = "https:" + src
            elif src.startswith("/"): src = _netloc_base + src
            elif not src.startswith("http"): src = website_url.rstrip("/") + "/" + src
            if not _INFRA_SKIP.search(src):
                bundle_urls.append(src)

        # Scan bundles one at a time; stop as soon as we find handles
        for bundle_url in bundle_urls[:12]:
            js = await _fetch(bundle_url)
            if js:
                found = _extract_ig_handles(js, seen)
                if found:
                    handles.extend(found)
                    break

    return handles


# ── S3: DDG search ─────────────────────────────────────────────────────────────

def _extract_handles_and_linktrees(results: list[dict]) -> tuple[list[str], list[str]]:
    """Pull IG handles and link-in-bio URLs from DDG search results."""
    handles: list[str] = []
    linktrees: list[str] = []

    for r in results:
        url     = r.get("url", "")
        snippet = r.get("snippet", "") + " " + r.get("title", "")

        # Direct instagram.com URL
        m = re.search(r"instagram\.com/([A-Za-z0-9_.]{2,30})/?(?:\?|$|/|\s)", url)
        if m and _valid_handle(m.group(1)):
            handles.append(m.group(1))

        # @mention in snippet
        for m in re.finditer(r"@([A-Za-z0-9_.]{2,30})", snippet):
            if _valid_handle(m.group(1)):
                handles.append(m.group(1))

        # instagram.com mention in snippet
        for m in re.finditer(r"instagram\.com/([A-Za-z0-9_.]{2,30})", snippet):
            if _valid_handle(m.group(1)):
                handles.append(m.group(1))

        # Linktree / Beacons.ai
        if "linktr.ee/" in url or "beacons.ai/" in url:
            linktrees.append(url)
        for m in re.finditer(r"(https?://(?:linktr\.ee|beacons\.ai)/[A-Za-z0-9_.]{2,40})", snippet):
            linktrees.append(m.group(1))

    return handles, linktrees


async def _strategy_ddg(
    brand_name: str,
    website_url: str,
    search_agent,
) -> tuple[list[str], list[str]]:
    """DDG search — returns (handles, linktree_urls)."""
    if search_agent is None:
        return [], []

    domain = _domain_from_url(website_url)
    queries = [
        f'site:instagram.com "{brand_name}"',
        f'{brand_name} official instagram',
        f'"{brand_name}" instagram india fashion',
    ]
    if domain:
        queries.append(f'instagram {domain}')

    async def _run(q: str) -> tuple[list[str], list[str]]:
        try:
            results = await asyncio.to_thread(search_agent.search, q, 8)
            return _extract_handles_and_linktrees(results)
        except Exception:
            return [], []

    batches = await asyncio.gather(*[_run(q) for q in queries])
    handles, linktrees = [], []
    seen_h: set[str] = set()
    seen_l: set[str] = set()
    for hs, ls in batches:
        for h in hs:
            k = _clean(h)
            if k not in seen_h:
                seen_h.add(k)
                handles.append(h)
        for l in ls:
            if l not in seen_l:
                seen_l.add(l)
                linktrees.append(l)
    return handles, linktrees


# ── S4: Link-in-bio scrape ─────────────────────────────────────────────────────

async def _strategy_linktree(
    urls: list[str],
    client: httpx.AsyncClient,
) -> list[str]:
    """Scrape Linktree / Beacons.ai pages for embedded Instagram links."""
    if not urls:
        return []

    _IG_RE = re.compile(r'instagram\.com/([A-Za-z0-9_.]{2,30})/?', re.I)

    async def _scrape(url: str) -> list[str]:
        try:
            r = await client.get(url, headers=_BROWSER_HEADERS, timeout=8, follow_redirects=True)
            if r.status_code != 200:
                return []
            found = []
            for m in _IG_RE.finditer(r.text):
                h = _clean(m.group(1))
                if _valid_handle(h):
                    found.append(m.group(1))
            return found
        except Exception:
            return []

    batches = await asyncio.gather(*[_scrape(u) for u in urls[:5]])
    seen: set[str] = set()
    handles: list[str] = []
    for batch in batches:
        for h in batch:
            k = _clean(h)
            if k not in seen:
                seen.add(k)
                handles.append(h)
    return handles


# ── S5: Pattern fallback ───────────────────────────────────────────────────────

def _candidates_from_brand(brand_name: str) -> list[str]:
    """Last-resort handle variants from brand name string alone."""
    name = brand_name.lower().strip()
    noise = re.compile(
        r"\b(india|official|brand|shop|store|the|by|pvt|ltd|private|limited)\b"
    )
    name_clean = re.sub(r"\s+", " ", noise.sub("", name)).strip()
    slug      = re.sub(r"[^a-z0-9]", "", name_clean)
    slug_full = re.sub(r"[^a-z0-9]", "", name)
    words     = re.sub(r"[^a-z0-9 ]", " ", name_clean).split()

    seen: set[str] = set()
    result: list[str] = []

    def add(*handles: str) -> None:
        for h in handles:
            h = h.strip("._").lower()
            if h and h not in seen and _valid_handle(h):
                seen.add(h)
                result.append(h)

    add(slug, f"{slug}_in", f"{slug}india", f"{slug}_india", f"the{slug}")
    if len(words) >= 2:
        add("_".join(words), ".".join(words))
    add(f"{slug}.official", f"{slug}_official", f"{slug}official",
        f"shop{slug}", f"{slug}.india", f"{slug}store")
    if slug_full != slug:
        add(slug_full, f"{slug_full}_in")

    return result


# ── Profile validation ─────────────────────────────────────────────────────────

async def _validate_profile(handle: str, client: httpx.AsyncClient) -> dict | None:
    """Lightweight profile fetch — returns partial profile or None if not found."""
    try:
        r = await client.get(
            _IG_PROFILE_API.format(_clean(handle)),
            headers=_MOBILE_HEADERS, timeout=8, follow_redirects=True,
        )
        if r.status_code != 200:
            return None
        user = r.json().get("data", {}).get("user") or {}
        if not user:
            return None
        return {
            "followers":    user.get("edge_followed_by", {}).get("count") or 0,
            "posts_count":  user.get("edge_owner_to_timeline_media", {}).get("count") or 0,
            "bio":          user.get("biography", "") or "",
            "external_url": user.get("external_url", "") or "",
            "full_name":    user.get("full_name", "") or "",
            "is_verified":  bool(user.get("is_verified")),
        }
    except Exception:
        return None


# ── Scoring ────────────────────────────────────────────────────────────────────

def _score_candidate(
    handle: str,
    profile: dict,
    brand_name: str,
    website_url: str,
    source: str,
    search_rank: int | None = None,
) -> tuple[str, float]:
    """Return (confidence_label, score)."""
    bio          = (profile.get("bio") or "").lower()
    external_url = (profile.get("external_url") or "").lower()
    full_name    = (profile.get("full_name") or "").lower()
    followers    = profile.get("followers") or 0
    posts_count  = profile.get("posts_count") or 0
    domain       = _domain_from_url(website_url)
    brand_lower  = brand_name.lower()
    brand_slug   = _slug(brand_name)

    score = 0.0

    # Domain cross-match (strongest signal)
    if domain and (domain in external_url or domain in bio):
        score += 0.50

    # Brand name in full_name or bio
    words = _brand_words(brand_name)
    hits  = sum(1 for w in words if w in full_name or w in bio)
    if hits:
        score += 0.20 * min(hits / max(len(words), 1), 1.0)

    # Brand slug in handle
    if brand_slug in _slug(handle):
        score += 0.15

    # Real account (not ghost)
    if followers > 500 or posts_count > 5:
        score += 0.10

    # Verified badge bonus
    if profile.get("is_verified"):
        score += 0.05

    # Source tier bonus
    if source == "website":
        score += 0.20
    elif source == "ig_search":
        # Top IG search result is a strong signal — decay by rank
        rank_bonus = max(0.0, 0.15 - (search_rank or 0) * 0.02)
        score += rank_bonus
    elif source == "linktree":
        score += 0.12
    elif source == "ddg":
        score += 0.05

    # Confidence label
    if score >= 0.65 or source == "website":
        return "confirmed", score
    if score >= 0.40:
        return "high", score
    if followers > 0 or posts_count > 0:
        return "medium", score
    return "low", score


# ── Main entry point ───────────────────────────────────────────────────────────

async def discover_handle(
    brand_name: str,
    website_url: str,
    search_agent,
) -> tuple[str | None, str]:
    """Find the Instagram handle for a brand. Never raises.

    Returns (handle, confidence):
      "confirmed" | "high" | "medium" | "low" | "guess" | "not_found"

    Strategy (parallel):
      S1 Instagram search API → real ranked accounts from IG itself
      S2 Website deep-scan   → parse <a> tags, JS globals, footer
      S3 DDG search          → site:instagram.com queries
      S4 Linktree scrape     → if S3 surfaces linktr.ee / beacons.ai URLs
      S5 Pattern fallback    → last resort, confidence capped at "guess"
    """
    print(f"  [ig_finder] Discovering handle for '{brand_name}'…", flush=True)

    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:

        # ── Phase 1: run S1 + S2 + S3 in parallel ─────────────────────────────
        ig_search_task  = _strategy_ig_search(brand_name, client)
        website_task    = _strategy_website(website_url, client)
        ddg_task        = _strategy_ddg(brand_name, website_url, search_agent)

        ig_results, website_handles, (ddg_handles, linktree_urls) = await asyncio.gather(
            ig_search_task, website_task, ddg_task,
        )

        # ── Phase 2: S4 linktree scrape (depends on S3 output) ─────────────────
        linktree_handles = await _strategy_linktree(linktree_urls, client)

        # ── Phase 3: build unified candidate list ──────────────────────────────
        # Ordered by source trust: website > ig_search > linktree > ddg > pattern
        # ig_search candidates carry partial profile data — skip re-validation
        ig_search_map: dict[str, dict] = {
            _clean(c["handle"]): c for c in ig_results
        }

        # Non-ig_search candidates need profile validation
        seen_keys: set[str] = set(_clean(h) for h in [c["handle"] for c in ig_results])
        to_validate: list[tuple[str, str]] = []  # (handle, source)

        for h in website_handles:
            k = _clean(h)
            if k not in seen_keys:
                seen_keys.add(k)
                to_validate.append((h, "website"))

        for h in linktree_handles:
            k = _clean(h)
            if k not in seen_keys:
                seen_keys.add(k)
                to_validate.append((h, "linktree"))

        for h in ddg_handles:
            k = _clean(h)
            if k not in seen_keys:
                seen_keys.add(k)
                to_validate.append((h, "ddg"))

        # Pattern fallback — only if no other candidates found
        if not ig_results and not to_validate:
            for h in _candidates_from_brand(brand_name):
                k = _clean(h)
                if k not in seen_keys:
                    seen_keys.add(k)
                    to_validate.append((h, "pattern"))
            print(f"  [ig_finder] No real candidates — falling back to {len(to_validate)} patterns", flush=True)

        # ── Phase 4: validate non-ig_search candidates ─────────────────────────
        _sem = asyncio.Semaphore(5)

        async def _val(handle: str, source: str) -> tuple[str, str, dict | None]:
            async with _sem:
                profile = await _validate_profile(handle, client)
            return handle, source, profile

        validated = await asyncio.gather(
            *[_val(h, s) for h, s in to_validate[:8]],
            return_exceptions=True,
        )

        # ── Phase 5: score all candidates ──────────────────────────────────────
        best_handle:     str | None = None
        best_confidence: str        = "not_found"
        best_score:      float      = -1.0

        # Score ig_search candidates (profile data embedded in search result)
        for c in ig_results:
            profile = {
                "followers":    c["followers"],
                "posts_count":  0,
                "bio":          "",
                "external_url": "",
                "full_name":    c["full_name"],
                "is_verified":  c["is_verified"],
            }
            confidence, score = _score_candidate(
                c["handle"], profile, brand_name, website_url,
                source="ig_search", search_rank=c["search_rank"],
            )
            print(
                f"  [ig_finder] @{c['handle']} (ig_search rank={c['search_rank']}) "
                f"→ {confidence} (score={score:.2f}, followers={c['followers']:,})",
                flush=True,
            )
            if score > best_score:
                best_score, best_handle, best_confidence = score, c["handle"], confidence

        # Junk handles that commonly appear in DDG results but are never brand accounts
        _JUNK_HANDLES = frozenset({
            "walmart", "amazon", "flipkart", "myntra", "snapdeal", "ajio",
            "meesho", "nykaa", "purplle", "bigbasket", "blinkit", "zepto",
            "instagram", "facebook", "youtube", "twitter", "linkedin",
        })

        # Source trust weights for unvalidated fallback (when IG API is rate-limited)
        _SOURCE_TRUST = {"website": 0.50, "linktree": 0.25, "ddg": 0.08}

        # Score validated candidates — stash unvalidated for Phase 6
        unvalidated: list[tuple[str, str]] = []  # (handle, source) where profile=None

        for item in validated:
            if isinstance(item, Exception):
                continue
            handle, source, profile = item

            if profile is None:
                if source != "pattern" and _clean(handle) not in _JUNK_HANDLES:
                    unvalidated.append((handle, source))
                continue

            confidence, score = _score_candidate(
                handle, profile, brand_name, website_url, source=source,
            )
            print(
                f"  [ig_finder] @{handle} ({source}) → {confidence} "
                f"(score={score:.2f}, followers={profile.get('followers', 0):,})",
                flush=True,
            )
            if score > best_score:
                best_score, best_handle, best_confidence = score, handle, confidence
                if source == "website":
                    best_confidence = "confirmed"

        if best_handle and best_score >= 0.40:
            print(f"  [ig_finder] Winner: @{best_handle} ({best_confidence})", flush=True)
            return best_handle, best_confidence

        # ── Phase 6: validation failed OR low-confidence — slug + source scoring on unvalidated ───
        # Also fires when best Phase 5 score < 0.40 (e.g. ghost account matched only by slug + rank).
        # Fires when IG API is rate-limited; avoids falling to pure pattern guesses.
        # Scoring: source trust + slug proximity. Prefer website > linktree > ddg.
        # Penalise handles where brand slug is buried far inside other words.
        # Squatter pattern: handle is brand slug padded with underscores/dots (e.g. bewakoof_____)
        _SQUATTER_RE = re.compile(r'^([a-z0-9]+)[_.]{2,}$|^[_.]{2,}([a-z0-9]+)$', re.I)

        def _unvalidated_score(handle: str, source: str) -> float:
            brand_slug = _slug(brand_name)
            h_slug     = _slug(handle)   # strips non-alphanumeric
            h_lower    = handle.lower()
            trust      = _SOURCE_TRUST.get(source, 0.05)

            # Hard penalise squatter handles (brand + trailing junk underscores)
            if _SQUATTER_RE.match(h_lower):
                return 0.0

            if h_slug == brand_slug and h_lower == brand_slug:
                slug_sc = 0.60          # true exact match (no extra chars in raw handle)
            elif h_slug == brand_slug:
                slug_sc = 0.45          # slug matches but handle has dots/underscores suffix
            elif h_slug.startswith(brand_slug) and len(h_slug) - len(brand_slug) <= 5:
                slug_sc = 0.50          # brand_slug + short suffix (_in, india …)
            elif brand_slug in h_slug:
                extra   = len(h_slug) - len(brand_slug)
                slug_sc = max(0.20, 0.40 - extra * 0.015)
            elif any(w in h_slug for w in _brand_words(brand_name)):
                slug_sc = 0.18
            else:
                slug_sc = 0.0
            return trust + slug_sc

        if unvalidated:
            scored = sorted(
                unvalidated,
                key=lambda hs: _unvalidated_score(hs[0], hs[1]),
                reverse=True,
            )
            best_h, best_src = scored[0]
            sc = _unvalidated_score(best_h, best_src)
            if sc >= 0.30:  # minimum bar — at least brand slug present
                conf = "medium" if sc >= 0.55 else "low"
                # If Phase 5 had a low-confidence validated result, compare scores.
                # Unvalidated website source with high slug match beats a ghost account.
                if best_handle and sc < best_score:
                    print(
                        f"  [ig_finder] Phase 5 winner @{best_handle} (score={best_score:.2f}) "
                        f"beats unvalidated @{best_h} (score={sc:.2f})",
                        flush=True,
                    )
                    return best_handle, best_confidence
                print(
                    f"  [ig_finder] Unvalidated fallback: @{best_h} ({best_src}, "
                    f"{conf}, score={sc:.2f})",
                    flush=True,
                )
                return best_h, conf

        # Phase 6 had nothing useful — return Phase 5 low-confidence result if any
        if best_handle:
            print(f"  [ig_finder] Winner (low-conf): @{best_handle} ({best_confidence})", flush=True)
            return best_handle, best_confidence

        # ── Phase 7: nothing — pattern slug guess ──────────────────────────────
        patterns = _candidates_from_brand(brand_name)
        if patterns:
            guess = patterns[0]
            print(f"  [ig_finder] All strategies failed — pattern guess: @{guess}", flush=True)
            return guess, "guess"

    print(f"  [ig_finder] No handle found for '{brand_name}'", flush=True)
    return None, "not_found"
