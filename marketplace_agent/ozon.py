"""Сбор данных с Ozon.

У Ozon нет открытого API каталога, а сайт защищён от ботов, поэтому используется
настоящий браузер (Playwright): открываем ozon.ru, получаем cookies антибот-защиты и
из контекста страницы запрашиваем тот же JSON, которым сайт рисует выдачу поиска.
"""
from __future__ import annotations

import json
import logging
import os
import re
from urllib.parse import quote

from .models import Product

log = logging.getLogger(__name__)

BASE = "https://www.ozon.ru"
ENTRYPOINT = "/api/entrypoint-api.bx/page/json/v2?url="
BLOCKS_PER_PAGE = 4

_NUM = r"(\d[\d\s  \xa0]*)"
RE_PRICE = re.compile(_NUM + r"\s*₽")
RE_RATING = re.compile(r"^\s*([1-5][.,]\d{1,2})\s*$")
RE_REVIEWS = re.compile(_NUM + r"\s*(?:отзыв|оценк)", re.I)
# Бейджи вида «2 тыс. купили», «Купили более 500 раз», «1000+ покупок»
RE_SALES = re.compile(
    r"(?:купили\s*(?:более|больше)?\s*" + _NUM + r"(тыс)?|" + _NUM + r"(тыс)?\.?\s*\+?\s*(?:купили|покупок|заказ))",
    re.I,
)
RE_SKU = re.compile(r"/product/[^/?]*?-?(\d{5,})/?")


def _to_int(text: str) -> int:
    return int(re.sub(r"\D", "", text) or 0)


def _texts(node) -> list[str]:
    """Все строковые значения узла JSON (порядок сохраняется)."""
    out: list[str] = []
    if isinstance(node, dict):
        for v in node.values():
            out.extend(_texts(v))
    elif isinstance(node, list):
        for v in node:
            out.extend(_texts(v))
    elif isinstance(node, str):
        out.append(node)
    return out


def _find_atom(main_state: list, atom_id: str | None = None, atom_type: str | None = None):
    for atom in main_state or []:
        if (atom_id and atom.get("id") == atom_id) or (atom_type and atom.get("type") == atom_type):
            return atom
    return None


def parse_item(item: dict, query: str = "") -> Product | None:
    link = ((item.get("action") or {}).get("link") or "").split("?")[0]
    sku = str(item.get("sku") or "")
    if not sku and (m := RE_SKU.search(link)):
        sku = m.group(1)
    main_state = item.get("mainState") or []

    name = ""
    name_atom = _find_atom(main_state, atom_id="name")
    if name_atom:
        # в новых версиях выдачи атом textDS содержит, кроме text, ещё и названия стилей
        text_atom = name_atom.get("textAtom") or name_atom.get("textDS") or {}
        name = (text_atom.get("text") or " ".join(_texts(text_atom or name_atom))).strip()

    prices: list[int] = []
    price_atom = _find_atom(main_state, atom_type="priceV2") or _find_atom(main_state, atom_type="priceWithTitle")
    for t in _texts(price_atom or main_state):
        if m := RE_PRICE.search(t):
            prices.append(_to_int(m.group(1)))
    prices = [p for p in prices if p > 0]

    rating, reviews, sales = None, 0, None
    for t in _texts(main_state):
        if rating is None and (m := RE_RATING.match(t)):
            rating = float(m.group(1).replace(",", "."))
        elif not reviews and (m := RE_REVIEWS.search(t)):
            reviews = _to_int(m.group(1))
        elif sales is None and (m := RE_SALES.search(t)):
            num, thousands = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            sales = _to_int(num) * (1000 if thousands else 1)

    if not sku or not name or not prices:
        return None
    price = min(prices)
    old = max(prices)
    return Product(
        marketplace="Ozon",
        sku=sku,
        name=name,
        url=BASE + link if link.startswith("/") else (link or f"{BASE}/product/{sku}/"),
        price=float(price),
        old_price=float(old) if old > price else None,
        rating=rating,
        reviews=reviews,
        sales=sales,
        sales_source="Ozon: бейдж «купили» в выдаче" if sales else "",
        query=query,
    )


def parse_search_page(page_json: dict, query: str = "") -> list[Product]:
    products: list[Product] = []
    for key, state in (page_json.get("widgetStates") or {}).items():
        if not key.startswith(("searchResultsV2", "tileGridDesktop", "tileGrid")):
            continue
        try:
            widget = json.loads(state) if isinstance(state, str) else state
        except ValueError:
            continue
        for item in widget.get("items") or []:
            if p := parse_item(item, query):
                products.append(p)
    return products


def _next_page(page_json: dict) -> str | None:
    for key, state in (page_json.get("widgetStates") or {}).items():
        if key.startswith("infiniteVirtualPaginator"):
            try:
                return (json.loads(state) if isinstance(state, str) else state).get("nextPage")
            except ValueError:
                return None
    return page_json.get("nextPage")


class OzonClient:
    def __init__(self, headless: bool = True, timeout_ms: int = 45000):
        self.headless = headless
        self.timeout_ms = timeout_ms

    def search(self, query: str, pages: int = 2) -> list[Product]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Для Ozon нужен Playwright: pip install playwright && playwright install chromium"
            ) from exc

        products: list[Product] = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled"],
                # свой Chromium вместо скачанного Playwright (например, в облачном окружении)
                executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None,
            )
            ctx = browser.new_context(
                locale="ru-RU",
                viewport={"width": 1366, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                ),
            )
            page = ctx.new_page()
            page.set_default_timeout(self.timeout_ms)
            # sorting=score — сортировка Ozon «по популярности»
            search_path = f"/search/?text={quote(query)}&from_global=true&sorting=score"
            page.goto(BASE + search_path, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)  # время на прохождение антибот-проверки

            # Ozon отдаёт выдачу блоками по 8 товаров; следующий блок — по ссылке nextPage
            # из пагинатора. Одна «страница» агента — несколько таких блоков.
            path = search_path
            for n in range(1, pages * BLOCKS_PER_PAGE + 1):
                data = page.evaluate(
                    """async (url) => {
                        const r = await fetch(url, {credentials: 'include'});
                        if (!r.ok) return {error: r.status};
                        return await r.json();
                    }""",
                    ENTRYPOINT + quote(path, safe=""),
                )
                if not data or data.get("error"):
                    log.warning("Ozon: страница %d не загружена (%s)", n, data and data.get("error"))
                    break
                found = parse_search_page(data, query)
                if not found:
                    break
                products.extend(found)
                path = _next_page(data)
                if not path:
                    break
                page.wait_for_timeout(1200)
            browser.close()
        if not products:
            raise RuntimeError(
                "Ozon не отдал выдачу (вероятно, сработала антибот-защита). "
                "Попробуйте запуск с --ozon-visible или позже."
            )
        return products
