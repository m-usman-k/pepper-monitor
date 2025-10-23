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
    discount: Optional[str]
    store: Optional[str]
    image: Optional[str]
    code: Optional[str]
    description: Optional[str]


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
        if not (deal.image and deal.price and deal.store):
            detail = await self._fetch_detail_fields(deal.url)
            if detail:
                deal.image = deal.image or detail.get("image")
                deal.price = deal.price or detail.get("price")
                deal.discount = deal.discount or detail.get("discount")
                deal.store = deal.store or detail.get("store")
                deal.code = deal.code or detail.get("code")
                deal.description = deal.description or detail.get("description")
        return deal

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
        # Discount
        discount_el = soup.select_one(".badge--deal, span[class*='discount'], .thread-discount")
        if discount_el:
            data["discount"] = discount_el.get_text(strip=True)
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
        discount_el = card.select_one("span[class*='discount'], .badge--deal")
        discount = discount_el.get_text(strip=True) if discount_el else None
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
        if any(v is None for v in (price, discount, store, image, code)):
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
                    discount = discount or thread.get('discount') or thread.get('discountText')
                    code = code or thread.get('voucherCode') or thread.get('code')
                # Some components may provide explicit image props
                img = props.get('image') if isinstance(props, dict) else None
                if isinstance(img, dict):
                    image = image or img.get('src') or img.get('url')

        if not href:
            return None
        # Prefer stable numeric id from href if available
        m = re.search(r"-(\d+)(?:[#/?].*)?$", href)
        unique_id = m.group(1) if m else href
        return Deal(
            unique_id=unique_id,
            url=href,
            title=title,
            price=price,
            discount=discount,
            store=store,
            image=image,
            code=code,
            description=description,
        )
