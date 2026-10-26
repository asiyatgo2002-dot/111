"""Готовые категории для исследования: набор подкатегорий с поисковыми запросами.

Цена/качество сравнивается внутри подкатегории (полотенце не сравнивается по цене с
одеялом), а продажи — по всей категории.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Subcategory:
    name: str
    queries: tuple[str, ...]
    # Начала названий, по которым отсекаются аксессуары и товары не той подкатегории
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class Category:
    key: str
    title: str
    aliases: tuple[str, ...]
    subcategories: tuple[Subcategory, ...]
    # Начала названий, отсекаемые во всех подкатегориях
    common_exclude: tuple[str, ...] = ()
    # Сколько товаров одной подкатегории максимум попадает в общий топ (0 — без ограничения)
    max_per_subcategory: int = 4
    extra: dict = field(default_factory=dict)


HOME_TEXTILE = Category(
    key="home-textile",
    title="Домашний текстиль",
    aliases=("домашний текстиль", "текстиль для дома", "home textile", "home-textile"),
    subcategories=(
        Subcategory("Постельное бельё", ("постельное белье",),
                    ("наволочк", "простын", "пододеяльник", "резинк")),
        Subcategory("Полотенца", ("полотенца махровые", "набор полотенец"), ("сушилк", "кухонн")),
        Subcategory("Пледы", ("плед",), ("чехол", "подставк", "плед для кошек", "плед для собак")),
        Subcategory("Подушки", ("подушка для сна",),
                    ("наволочк", "чехол", "подушка декоративн", "подушка для беременных")),
        Subcategory("Одеяла", ("одеяло",), ("пододеяльник", "чехол", "конверт")),
        Subcategory("Шторы", ("шторы для комнаты", "тюль"),
                    ("зажим", "подхват", "кольц", "лента", "грузик")),
        Subcategory("Покрывала", ("покрывало на кровать",), ("чехол",)),
        Subcategory("Наматрасники", ("наматрасник",), ("матрас", "простын")),
        Subcategory("Кухонный текстиль", ("полотенца кухонные", "скатерть"), ("прищеп", "скатерть одноразов")),
    ),
    common_exclude=("держател", "крючок", "крючки", "вешалк", "карниз", "кронштейн", "вакуумн", "органайзер"),
)

CATEGORIES = {HOME_TEXTILE.key: HOME_TEXTILE}


def find_category(text: str | None) -> Category | None:
    """Ищет категорию по ключу (`home-textile`) или по названию («домашний текстиль»)."""
    if not text:
        return None
    norm = text.strip().lower().replace("ё", "е")
    for cat in CATEGORIES.values():
        if norm == cat.key or norm in cat.aliases:
            return cat
    return None
