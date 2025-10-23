import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional

import aiohttp
from urllib.parse import urlparse, urljoin
import bs4
from bs4 import BeautifulSoup

from config import REQUEST_TIMEOUT_SECONDS, USER_AGENT, MAX_CONCURRENT_REQUESTS
from proxies import ProxyProvider

logger = logging.getLogger(__name__)


@dataclass
class Deal:
    unique_id: str
    url: str
    title: str
    price: Optional[str]
    old_price: Optional[str]
    discount: Optional[str]
    store: Optional[str]
    image: Optional[str]
    code: Optional[str]
    description: Optional[str]
    store_url: Optional[str]


class PepperScraper:
    def __init__(self):
        self.proxy_provider = ProxyProvider()
        self._sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async def fetch_latest(self, page_url: str) -> Optional[Deal]:
        url = self._normalize_url(page_url)
        html = await self._fetch_html(url)
        if not html:
            return None
        deal = self._parse_latest(html)
        if deal is None:
            return None
        # If key fields are missing (common on listing pages), enrich from detail page
        needs_enrich = not (getattr(deal, "image", None) and getattr(deal, "price", None) and getattr(deal, "store", None) and getattr(deal, "store_url", None))
        if needs_enrich:
            detail = await self._fetch_detail_fields(deal.url)
            if detail:
                deal.image = deal.image or detail.get("image")
                deal.price = deal.price or detail.get("price")
                deal.old_price = getattr(deal, "old_price", None) or detail.get("old_price")
                deal.discount = deal.discount or detail.get("discount")
                deal.store = deal.store or detail.get("store")
                deal.code = deal.code or detail.get("code")
                deal.description = deal.description or detail.get("description")
                if detail.get("store_url"):
                    deal.store_url = detail.get("store_url")
        return deal

    async def _resolve_visit_link(self, visit_url: str) -> Optional[str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        }
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        proxy = await self.proxy_provider.get_proxy_url()
        async with self._sem:
            for attempt, use_proxy in enumerate([True, False]):
                try:
                    chosen_proxy = proxy if (use_proxy and proxy) else None
                    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                        async with session.get(visit_url, proxy=chosen_proxy, ssl=False, allow_redirects=False) as resp:
                            loc = resp.headers.get("Location")
                            if loc:
                                return self._normalize_url(loc)
                except Exception:
                    logger.exception("Visit resolve error for %s (attempt %d)", visit_url, attempt + 1)
                    continue
        return None

    def _normalize_url(self, url: str) -> str:
        if not url:
            return url
        u = url.strip()
        # Protocol-relative
        if u.startswith("//"):
            return "https:" + u
        parsed = urlparse(u)
        if parsed.scheme in ("http", "https"):
            return u
        # Missing scheme but contains domain
        if "pepper.pl" in u and not parsed.scheme:
            return "https://" + u
        # Treat as relative path to Pepper base
        base = "https://www.pepper.pl/"
        return urljoin(base, u.lstrip('/'))

    def _price_to_float(self, text: Optional[str]) -> Optional[float]:
        if not text:
            return None
        t = str(text)
        # Remove currency and spaces, unify decimal
        t = t.replace('zł', '').replace('PLN', '').replace(' ', '').replace('\xa0', '')
        t = t.replace(',', '.')
        # Keep only digits and dot
        import re as _re
        m = _re.search(r"(\d+(?:\.\d+)?)", t)
        return float(m.group(1)) if m else None

    def _format_percent(self, old_v: Optional[float], new_v: Optional[float]) -> Optional[str]:
        if not old_v or not new_v or old_v <= 0:
            return None
        pct = round((1.0 - (new_v / old_v)) * 100)
        return f"-{int(pct)}%"

    async def _fetch_html(self, url: str) -> Optional[str]:
        # Use more realistic headers; Pepper sometimes tailors content by locale and UA
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        proxy = await self.proxy_provider.get_proxy_url()
        async with self._sem:
            # Try with proxy (if any), then retry once without proxy
            for attempt, use_proxy in enumerate([True, False]):
                try:
                    chosen_proxy = proxy if (use_proxy and proxy) else None
                    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                        async with session.get(url, proxy=chosen_proxy, ssl=False) as resp:
                            if resp.status != 200:
                                logger.warning("HTTP %s for %s (attempt %d)", resp.status, url, attempt + 1)
                                continue
                            return await resp.text()
                except Exception:
                    logger.exception("HTTP error for %s (attempt %d)", url, attempt + 1)
                    continue
        return None

    async def _fetch_detail_fields(self, url: str) -> Optional[dict]:
        html = await self._fetch_html(self._normalize_url(url))
        if not html:
            return None
        try:
            soup = BeautifulSoup(html, "lxml")
        except bs4.FeatureNotFound:
            soup = BeautifulSoup(html, "html.parser")
        data: dict = {}
        # Image: prefer OpenGraph
        og = soup.select_one("meta[property='og:image'][content]")
        if og and og.get("content"):
            data["image"] = self._normalize_url(og.get("content"))
        img_el = soup.select_one("img[src]")
        if not data.get("image") and img_el:
            src = img_el.get("src")
            if src:
                data["image"] = self._normalize_url(src)
        # Price
        price_el = soup.select_one(".thread-price, span[class*='price'], [itemprop='price'], meta[itemprop='price'][content]")
        if price_el:
            data["price"] = price_el.get_text(strip=True) if price_el.name != "meta" else price_el.get("content")
        # Old price (strikethrough)
        old_el = soup.select_one(
            ".thread-old-price, .price--old, .price-old, .text--del, del, s, .strikethrough, .was-price"
        )
        if old_el:
            data["old_price"] = old_el.get_text(strip=True)
        # Discount
        discount_el = soup.select_one(".badge--deal, span[class*='discount'], .thread-discount, .label--contrast--danger, .label--contrast")
        if discount_el:
            data["discount"] = discount_el.get_text(strip=True)
        if not data.get("discount"):
            import re as _re
            for el in soup.select(".thread-price span, .thread-price div, span, .label, .badge"):
                t = el.get_text(strip=True)
                m = _re.search(r"-?\d{1,3}%", t or "")
                if m:
                    data["discount"] = m.group(0)
                    break
        # Store
        store_el = soup.select_one(".cept-merchant-link, span[class*='merchant'], .thread-title--merchant")
        if store_el:
            data["store"] = store_el.get_text(strip=True)
        # Code
        code_el = soup.select_one("code, .voucher-code, span[class*='code']")
        if code_el:
            data["code"] = code_el.get_text(strip=True)
        # Description
        desc_el = soup.select_one(".userHtml-content, .cept-description-container, .thread--text, article p")
        if desc_el:
            data["description"] = desc_el.get_text(strip=True)
        # Direct store link via pepper visit endpoint
        visit_el = soup.select_one("a[href*='/visit/']")
        if visit_el and visit_el.get("href"):
            visit_url = self._normalize_url(visit_el.get("href"))
            data["store_url"] = visit_url
            resolved = await self._resolve_visit_link(visit_url)
            if resolved:
                data["store_url"] = resolved
        return data or None

    def _parse_latest(self, html: str) -> Optional[Deal]:
        # Use lxml if installed; otherwise fallback to built-in parser
        try:
            soup = BeautifulSoup(html, "lxml")
        except bs4.FeatureNotFound:
            soup = BeautifulSoup(html, "html.parser")
        # Prefer the first visible thread card structure found in testing.html
        card = (
            soup.select_one("article.thread[id^='thread_']")
            or soup.select_one("article.thread")
            or soup.select_one("article[data-thread-id]")
            or soup.select_one("article[data-t]")
            or soup.select_one("div.thread")
            or soup.select_one("article")
        )
        # Fallback to any pepper anchor
        if not card:
            a = (
                soup.select_one("a.thread-link[href]")
                or soup.select_one("a[href*='pepper.pl']")
                or soup.find("a", href=True)
            )
            if not a:
                return None
            href = a.get("href")
            title = a.get_text(strip=True)
            return Deal(
                unique_id=href,
                url=href,
                title=title or "New deal",
                price=None,
                discount=None,
                store=None,
                image=None,
                code=None,
                description=None,
            )
        # Extract key fields
        link_el = (
            card.select_one("a.thread-link[href]")
            or card.select_one("strong.thread-title a[href]")
            or card.select_one("a[href]")
        )
        href = link_el.get("href") if link_el else None
        title_el = (
            card.select_one("strong.thread-title a")
            or card.select_one("h2, h3")
            or link_el
        )
        title = title_el.get_text(strip=True) if title_el else "New deal"
        # Price and discount heuristics
        price_el = card.select_one("span[class*='price'], .thread-price, .threadListCard-price")
        price = price_el.get_text(strip=True) if price_el else None
        old_price = None
        discount_el = card.select_one("span[class*='discount'], .badge--deal, .label--contrast--danger, .label--contrast")
        discount = discount_el.get_text(strip=True) if discount_el else None
        if not old_price:
            old_el = card.select_one("del, s, .price--old, .thread-old-price, .text--del")
            if old_el:
                old_price = old_el.get_text(strip=True)
        if not discount:
            # Fallback: scan for a percentage token near price block
            import re as _re
            for el in card.select(".threadListCard-price span, .threadListCard-price div, span, .label, .badge"):
                t = el.get_text(strip=True)
                if t and _re.search(r"-?\d{1,3}%", t):
                    discount = _re.search(r"-?\d{1,3}%", t).group(0)
                    break
        store_el = card.select_one("span[class*='merchant'], .cept-merchant-link, .thread-title--merchant")
        store = store_el.get_text(strip=True) if store_el else None
        img_el = card.select_one("img[src]")
        image = img_el.get("src") if img_el else None
        if image and not image.startswith("http"):
            image = self._normalize_url(image)
        # Promo code often appears as code tag or span with 'code'
        code_el = card.select_one("code, .voucher-code, span[class*='code']")
        code = code_el.get_text(strip=True) if code_el else None
        desc_el = card.select_one(".userHtml-content, .cept-description-container, p, .thread--text")
        description = desc_el.get_text(strip=True) if desc_el else None

        # Attempt to enrich from data-vue3 JSON props in listing card
        store_url = None
        next_best_price_str = None
        price_discount_numeric = None
        if any(v is None for v in (price, discount, store, image, code)) or True:
            import json
            for el in card.select('[data-vue3]'):
                raw = el.get('data-vue3')
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                # Common shapes: {"name":"ThreadMainListItemNormalizer","props":{"thread":{...}}}
                props = obj.get('props') or {}
                thread = props.get('thread') or {}
                if thread:
                    # Merchant/store normalization
                    merchant_val = thread.get('merchant') or thread.get('merchantInfo') or props.get('merchant')
                    if isinstance(merchant_val, dict):
                        store = store or merchant_val.get('merchantName') or merchant_val.get('name') or merchant_val.get('merchantUrlName')
                    elif isinstance(merchant_val, str):
                        store = store or merchant_val
                    store = store or thread.get('retailerName')
                    # price fields vary; try typical keys
                    price = price or thread.get('priceString') or thread.get('price') or thread.get('priceText')
                    old_price = (
                        old_price
                        or thread.get('oldPriceString')
                        or thread.get('oldPrice')
                        or thread.get('previousPrice')
                        or thread.get('priceBeforeString')
                        or thread.get('priceBefore')
                        or thread.get('strikethroughPrice')
                    )
                    # Next best/original price provided by listing
                    next_best_price_str = (
                        next_best_price_str
                        or thread.get('displayNextBestPrice')
                        or thread.get('nextBestPrice')
                    )
                    # Numeric discount value if present
                    price_discount_numeric = price_discount_numeric or thread.get('priceDiscount')
                    # Discount can be numeric or text
                    discount = (
                        discount
                        or thread.get('discountText')
                        or thread.get('discount')
                        or thread.get('discountPercent')
                        or thread.get('discountPercentage')
                        or thread.get('savingsPercentage')
                        or thread.get('percentOff')
                    )
                    code = code or thread.get('voucherCode') or thread.get('code')
                # Some components may provide explicit image props
                img = props.get('image') if isinstance(props, dict) else None
                if isinstance(img, dict):
                    image = image or img.get('src') or img.get('url')
                # Main button component contains direct visit link
                if obj.get('name') == 'ThreadListItemMainButton':
                    link = props.get('link') if isinstance(props, dict) else None
                    if link:
                        store_url = self._normalize_url(link)

        if not href:
            return None
        # Prefer stable numeric id from href if available
        m = re.search(r"-(\d+)(?:[#/?].*)?$", href)
        unique_id = m.group(1) if m else href
        # Compute missing old_price/discount from nextBestPrice and numeric discount
        if not old_price and next_best_price_str:
            old_price = next_best_price_str
        if not discount:
            # If we have price and old price, compute percentage
            new_v = self._price_to_float(price)
            old_v = self._price_to_float(old_price)
            computed = self._format_percent(old_v, new_v)
            if computed:
                discount = computed
            elif price_discount_numeric is not None:
                try:
                    discount = f"-{int(round(float(price_discount_numeric)))}%"
                except Exception:
                    pass
        return Deal(
            unique_id=unique_id,
            url=href,
            title=title,
            price=price,
            old_price=old_price,
            discount=discount,
            store=store,
            image=image,
            code=code,
            description=description,
            store_url=store_url,
        )
