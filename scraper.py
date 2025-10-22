import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional

import aiohttp
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
        html = await self._fetch_html(page_url)
        if not html:
            return None
        return self._parse_latest(html)

    async def _fetch_html(self, url: str) -> Optional[str]:
        headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        proxy = await self.proxy_provider.get_proxy_url()
        async with self._sem:
            try:
                async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                    async with session.get(url, proxy=proxy, ssl=False) as resp:
                        if resp.status != 200:
                            logger.warning("HTTP %s for %s", resp.status, url)
                            return None
                        return await resp.text()
            except Exception:
                logger.exception("HTTP error for %s", url)
                return None

    def _parse_latest(self, html: str) -> Optional[Deal]:
        soup = BeautifulSoup(html, "lxml")
        # Try to find the first deal card. Pepper often uses <article> with classes like 'thread' or data attributes
        card = (
            soup.select_one("article.thread")
            or soup.select_one("article[data-thread-id]")
            or soup.select_one("article[data-t]")
            or soup.select_one("div.thread")
            or soup.select_one("article")
        )
        # Fallback: look for first anchor pointing to /kupony/ or /promocje/ etc.
        if not card:
            a = soup.select_one("a[href*='pepper.pl']") or soup.find("a", href=True)
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
        # Extract basic fields with resilient selectors
        # URL
        a = card.select_one("a[href]")
        href = a.get("href") if a else None
        title_el = card.select_one("h2, h3, a")
        title = title_el.get_text(strip=True) if title_el else "New deal"
        # Price and discount heuristics
        price_el = card.select_one("span[class*='price'], .thread-price")
        price = price_el.get_text(strip=True) if price_el else None
        discount_el = card.select_one("span[class*='discount'], .badge--deal")
        discount = discount_el.get_text(strip=True) if discount_el else None
        store_el = card.select_one("span[class*='merchant'], .cept-merchant-link, .thread-title--merchant")
        store = store_el.get_text(strip=True) if store_el else None
        img_el = card.select_one("img[src]")
        image = img_el.get("src") if img_el else None
        # Promo code often appears as code tag or span with 'code'
        code_el = card.select_one("code, .voucher-code, span[class*='code']")
        code = code_el.get_text(strip=True) if code_el else None
        desc_el = card.select_one("p, .cept-description-container, .thread--text")
        description = desc_el.get_text(strip=True) if desc_el else None

        if not href:
            return None
        unique_id = href
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
