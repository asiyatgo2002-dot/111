"""ИИ-часть агента на Claude: планирует поисковые запросы, отсеивает нерелевантные
товары (аксессуары, подделки под запрос) и пишет выводы по итоговому топу.

Работает только при наличии ANTHROPIC_API_KEY; без ключа агент использует эвристики.
"""
from __future__ import annotations

import json
import logging
import os

from .models import Product

log = logging.getLogger(__name__)

MODEL = os.environ.get("MARKETPLACE_AGENT_MODEL", "claude-opus-5-5")


class Analyst:
    def __init__(self, model: str = MODEL):
        import anthropic

        self.client = anthropic.Anthropic()
        self.model = model

    @classmethod
    def maybe_create(cls) -> "Analyst | None":
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            return None
        try:
            return cls()
        except ImportError:
            log.warning("Пакет anthropic не установлен — ИИ-анализ отключён")
            return None

    def _ask_json(self, system: str, prompt: str, schema: dict, effort: str = "low") -> dict:
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason in ("refusal", "max_tokens"):
            raise RuntimeError(f"Claude не вернул ответ (stop_reason={response.stop_reason})")
        text = "".join(b.text for b in response.content if b.type == "text")
        return json.loads(text)

    def plan_queries(self, topic: str, limit: int = 3) -> list[str]:
        schema = {
            "type": "object",
            "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
            "required": ["queries"],
            "additionalProperties": False,
        }
        data = self._ask_json(
            "Ты аналитик маркетплейсов Ozon и Wildberries.",
            f"Пользователь ищет лучшие по цене/качеству и самые продаваемые товары по теме: «{topic}».\n"
            f"Составь до {limit} коротких поисковых запросов на русском, как их вводят покупатели "
            "в строку поиска маркетплейса. Первый запрос — сама тема в естественной форме. "
            "Не добавляй бренды, если их нет в теме.",
            schema,
        )
        queries = [q.strip() for q in data["queries"] if q.strip()][:limit]
        return queries or [topic]

    def filter_relevant(self, topic: str, products: list[Product]) -> list[Product]:
        if not products:
            return products
        listing = "\n".join(
            f"{i}. [{p.marketplace}] {p.name} | {p.brand} | {p.price:.0f} ₽" for i, p in enumerate(products)
        )
        schema = {
            "type": "object",
            "properties": {"relevant": {"type": "array", "items": {"type": "integer"}}},
            "required": ["relevant"],
            "additionalProperties": False,
        }
        data = self._ask_json(
            "Ты аналитик маркетплейсов. Отбираешь товары, которые действительно соответствуют запросу.",
            f"Тема: «{topic}».\nНиже список товаров из поисковой выдачи. Верни номера тех, что являются "
            "именно искомым товаром. Исключи аксессуары, запчасти, чехлы, комплектующие и товары другой "
            "категории, попавшие в выдачу по рекламе.\n\n" + listing,
            schema,
        )
        keep = {i for i in data["relevant"] if 0 <= i < len(products)}
        return [p for i, p in enumerate(products) if i in keep]

    def summarize(self, topic: str, top: list[Product]) -> tuple[str, list[str]]:
        listing = "\n".join(
            f"{i + 1}. [{p.marketplace}] {p.name} ({p.brand}) — {p.price:.0f} ₽, рейтинг {p.rating}, "
            f"отзывов {p.reviews}, продаж ≈{p.sales} ({p.sales_source}), балл {p.total_score}"
            for i, p in enumerate(top)
        )
        schema = {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "comments": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary", "comments"],
            "additionalProperties": False,
        }
        data = self._ask_json(
            "Ты аналитик маркетплейсов. Пиши по-русски, по делу, без воды. Опирайся только на данные.",
            f"Тема исследования: «{topic}». Итоговый топ по продажам и соотношению цена/качество:\n\n"
            f"{listing}\n\n"
            "1) summary — вывод на 4–7 предложений: ценовой диапазон лидеров, где выгоднее (Ozon или WB), "
            "лучший выбор в бюджетном и среднем сегменте, на что обратить внимание.\n"
            f"2) comments — ровно {len(top)} коротких комментариев (до 15 слов), по одному на каждый товар "
            "в том же порядке: чем он выделяется.",
            schema,
            effort="medium",
        )
        comments = (data["comments"] + [""] * len(top))[: len(top)]
        return data["summary"], comments
