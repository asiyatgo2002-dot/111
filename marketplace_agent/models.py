from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Product:
    marketplace: str  # "Wildberries" | "Ozon"
    sku: str
    name: str
    url: str
    price: float  # цена для покупателя, ₽
    brand: str = ""
    old_price: float | None = None  # цена до скидки, ₽
    rating: float | None = None  # 0..5
    reviews: int = 0
    sales: int | None = None  # продажи/заказы
    sales_source: str = ""  # откуда взято значение продаж
    query: str = ""
    subcategory: str = ""  # подкатегория для категорийных исследований

    # заполняются при ранжировании
    quality_score: float = 0.0
    value_score: float = 0.0
    sales_score: float = 0.0
    total_score: float = 0.0
    comment: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def discount_pct(self) -> float | None:
        if self.old_price and self.old_price > self.price:
            return round((1 - self.price / self.old_price) * 100, 1)
        return None

    @property
    def key(self) -> tuple[str, str]:
        return (self.marketplace, self.sku)
