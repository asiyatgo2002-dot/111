"""Ранжирование товаров: продажи + соотношение цена/качество."""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median

from .models import Product


@dataclass
class ScoringConfig:
    min_rating: float = 4.5
    min_reviews: int = 30
    # Доля покупателей, оставляющих отзыв — для оценки продаж, когда маркетплейс их не показывает
    review_rate: float = 0.05
    # Байесовское сглаживание рейтинга: «виртуальные» отзывы со средней оценкой
    prior_reviews: int = 50
    # Товары дешевле этой доли от медианы обычно аксессуары/не тот товар
    min_price_ratio: float = 0.15
    w_sales: float = 0.45
    w_value: float = 0.35
    w_quality: float = 0.20


def fill_sales_estimates(products: list[Product], cfg: ScoringConfig) -> None:
    for p in products:
        if not p.sales and p.reviews:
            p.sales = int(round(p.reviews / cfg.review_rate))
            p.sales_source = f"оценка: отзывы ÷ {cfg.review_rate:.0%}"


def dedupe(products: list[Product]) -> list[Product]:
    seen: dict[tuple[str, str], Product] = {}
    for p in products:
        seen.setdefault(p.key, p)
    return list(seen.values())


def filter_candidates(products: list[Product], cfg: ScoringConfig) -> list[Product]:
    ok = [
        p for p in products
        if p.price > 0 and (p.rating or 0) >= cfg.min_rating and p.reviews >= cfg.min_reviews
    ]
    if ok:
        med = median(p.price for p in ok)
        ok = [p for p in ok if p.price >= med * cfg.min_price_ratio]
    return ok


def _minmax(values: list[float]) -> list[float]:
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [1.0] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def score(products: list[Product], cfg: ScoringConfig) -> list[Product]:
    """Считает баллы 0..100 и возвращает товары по убыванию итогового балла."""
    if not products:
        return []
    mean_rating = sum(p.rating or 0 for p in products) / len(products)
    m = cfg.prior_reviews
    # Байесовский рейтинг: 4.9 по 10 отзывам весит меньше, чем 4.8 по 10 000
    bayes = [((p.rating or 0) * p.reviews + mean_rating * m) / (p.reviews + m) for p in products]
    quality = _minmax(bayes)

    # Цена/качество: качество на рубль; цена логарифмируется, чтобы дешёвое не побеждало всегда
    med_price = median(p.price for p in products)
    value_raw = [q_b / (1 + math.log(p.price / med_price + 1)) for p, q_b in zip(products, bayes)]
    value = _minmax(value_raw)

    sales = _minmax([math.log1p(p.sales or 0) for p in products])

    total_w = cfg.w_sales + cfg.w_value + cfg.w_quality
    for p, s, v, q in zip(products, sales, value, quality):
        p.sales_score = round(s * 100, 1)
        p.value_score = round(v * 100, 1)
        p.quality_score = round(q * 100, 1)
        p.total_score = round(
            (cfg.w_sales * s + cfg.w_value * v + cfg.w_quality * q) / total_w * 100, 1
        )
    return sorted(products, key=lambda p: p.total_score, reverse=True)
