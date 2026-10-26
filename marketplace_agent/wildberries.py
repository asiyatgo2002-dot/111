"""Сбор данных с Wildberries через публичные API витрины (те же, что использует сайт)."""
from __future__ import annotations

import logging
import time

import requests

from .models import Product

log = logging.getLogger(__name__)

SEARCH_URL = "https://search.wb.ru/exactmatch/ru/common/v14/search"
# Количество заказов по артикулу (этот счётчик показывается на карточке товара)
ORDERS_URL = "https://product-order-qnt.wildberries.ru/v2/by-nm/"
PRODUCT_URL = "https://www.wildberries.ru/catalog/{nm}/detail.aspx"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.wildberries.ru",
    "Referer": "https://www.wildberries.ru/",
}


class WildberriesClient:
    def __init__(self, dest: str = "-1257786", timeout: float = 20, delay: float = 0.7):
        # dest — регион доставки (по умолчанию Москва), от него зависят цены и наличие
        self.dest = dest
        self.timeout = timeout
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def _get_json(self, url: str, params: dict, retries: int = 3):
        for attempt in range(1, retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                if resp.status_code == 429:
                    raise requests.HTTPError("429 Too Many Requests")
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, ValueError) as exc:
                if attempt == retries:
                    raise
                log.debug("WB %s: попытка %d не удалась (%s)", url, attempt, exc)
                time.sleep(self.delay * 2 ** attempt)

    def search(self, query: str, pages: int = 2) -> list[Product]:
        """Поиск с сортировкой «по популярности» — WB ставит наверх самые продаваемые товары."""
        products: list[Product] = []
        for page in range(1, pages + 1):
            params = {
                "ab_testing": "false",
                "appType": 1,
                "curr": "rub",
                "dest": self.dest,
                "lang": "ru",
                "page": page,
                "query": query,
                "resultset": "catalog",
                "sort": "popular",
                "spp": 30,
            }
            data = self._get_json(SEARCH_URL, params)
            raw = (data.get("data") or {}).get("products") or data.get("products") or []
            if not raw:
                break
            products.extend(p for p in (parse_product(r, query) for r in raw) if p)
            time.sleep(self.delay)
        self._attach_orders(products)
        return products

    def _attach_orders(self, products: list[Product], batch: int = 50) -> None:
        for i in range(0, len(products), batch):
            chunk = products[i : i + batch]
            try:
                data = self._get_json(ORDERS_URL, {"nm": ",".join(p.sku for p in chunk)}, retries=2)
            except (requests.RequestException, ValueError) as exc:
                log.warning("WB: не удалось получить число заказов (%s), будет оценка по отзывам", exc)
                return
            orders = parse_orders(data)
            for p in chunk:
                if orders.get(p.sku):
                    p.sales = orders[p.sku]
                    p.sales_source = "WB: заказы (счётчик карточки)"
            time.sleep(self.delay)


def _price_rub(raw: dict) -> tuple[float | None, float | None]:
    """Возвращает (цена, цена до скидки) в рублях. WB отдаёт цены в копейках."""
    for size in raw.get("sizes") or []:
        price = size.get("price") or {}
        if price.get("product"):
            return price["product"] / 100, (price.get("basic") or 0) / 100 or None
    if raw.get("salePriceU"):
        return raw["salePriceU"] / 100, (raw.get("priceU") or 0) / 100 or None
    return None, None


def parse_product(raw: dict, query: str = "") -> Product | None:
    price, old_price = _price_rub(raw)
    if not raw.get("id") or not price:
        return None  # нет в наличии / нет цены
    rating = raw.get("reviewRating") or raw.get("nmReviewRating") or raw.get("rating")
    return Product(
        marketplace="Wildberries",
        sku=str(raw["id"]),
        name=raw.get("name", "").strip(),
        brand=raw.get("brand", "").strip(),
        url=PRODUCT_URL.format(nm=raw["id"]),
        price=price,
        old_price=old_price,
        rating=float(rating) if rating else None,
        reviews=int(raw.get("feedbacks") or raw.get("nmFeedbacks") or 0),
        query=query,
    )


def parse_orders(data) -> dict[str, int]:
    items = data if isinstance(data, list) else (data or {}).get("data") or []
    return {str(i.get("nmId")): int(i.get("qnt") or 0) for i in items if i.get("nmId")}
