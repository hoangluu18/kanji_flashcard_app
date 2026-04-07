from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import Kanji, KanjiCard

logger = get_logger(__name__)


@dataclass(frozen=True)
class CatalogCard:
    card_index: int
    card_img_path: Path


@dataclass(frozen=True)
class CatalogKanji:
    kanji_id: int
    page: int
    header_img_path: Path
    cards: list[CatalogCard]


class CatalogError(RuntimeError):
    pass


def _resolve_asset_path(raw_path: str, assets_root: Path) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return (assets_root / path).resolve()


def load_catalog(cards_json_path: Path, assets_root: Path) -> dict[int, CatalogKanji]:
    if not cards_json_path.exists():
        raise CatalogError(f"cards.json not found: {cards_json_path}")

    try:
        raw_items = json.loads(cards_json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CatalogError(f"Invalid JSON file: {cards_json_path}") from exc

    if not isinstance(raw_items, list):
        raise CatalogError("cards.json must be a JSON array")

    grouped: dict[int, dict] = {}
    for idx, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            raise CatalogError(f"cards.json item #{idx} is not an object")

        kanji_id = int(item.get("kanji", item.get("page", 0)))
        if kanji_id <= 0:
            raise CatalogError(f"cards.json item #{idx} missing kanji/page > 0")

        card_index = int(item.get("card_index", item.get("id", 0)))
        card_img = item.get("card_img")
        header_img = item.get("header_img")
        if not card_img or not header_img:
            raise CatalogError(f"cards.json item #{idx} missing card_img/header_img")

        card_path = _resolve_asset_path(str(card_img), assets_root)
        header_path = _resolve_asset_path(str(header_img), assets_root)

        entry = grouped.setdefault(
            kanji_id,
            {
                "page": int(item.get("page", kanji_id)),
                "header_img_path": header_path,
                "cards": [],
                "seen_cards": set(),
            },
        )

        if entry["header_img_path"] != header_path:
            logger.warning(
                "Multiple headers found for kanji %s. Using first: %s",
                kanji_id,
                entry["header_img_path"],
            )

        if card_index not in entry["seen_cards"]:
            entry["cards"].append(CatalogCard(card_index=card_index, card_img_path=card_path))
            entry["seen_cards"].add(card_index)

    catalog: dict[int, CatalogKanji] = {}
    for kanji_id, info in grouped.items():
        cards = sorted(info["cards"], key=lambda c: c.card_index)
        catalog[kanji_id] = CatalogKanji(
            kanji_id=kanji_id,
            page=info["page"],
            header_img_path=info["header_img_path"],
            cards=cards,
        )

    return dict(sorted(catalog.items(), key=lambda kv: kv[0]))


def seed_catalog(session: Session, catalog: dict[int, CatalogKanji]) -> None:
    # Upsert catalog rows so user SRS state remains intact across reseeds.
    existing_kanji = {
        row.id: row for row in session.scalars(select(Kanji).order_by(Kanji.id.asc())).all()
    }

    for kanji_id, item in catalog.items():
        row = existing_kanji.get(kanji_id)
        if row is None:
            row = Kanji(
                id=kanji_id,
                page=item.page,
                number=kanji_id,
                header_img_path=str(item.header_img_path),
                header_file_id=None,
                viet_name=None,
            )
            session.add(row)
        else:
            row.page = item.page
            row.number = kanji_id
            row.header_img_path = str(item.header_img_path)

    session.flush()

    for kanji_id, item in catalog.items():
        existing_cards = session.scalars(
            select(KanjiCard)
            .where(KanjiCard.kanji_id == kanji_id)
            .order_by(KanjiCard.card_order.asc(), KanjiCard.id.asc())
        ).all()

        cached_file_id_by_path = {
            card_row.card_img_path: card_row.card_file_id for card_row in existing_cards
        }

        for card_row in existing_cards:
            session.delete(card_row)

        session.flush()

        for order, card in enumerate(item.cards, start=1):
            card_path = str(card.card_img_path)
            session.add(
                KanjiCard(
                    kanji_id=kanji_id,
                    card_img_path=card_path,
                    card_file_id=cached_file_id_by_path.get(card_path),
                    card_order=order,
                )
            )

    session.flush()


def count_catalog_rows(session: Session) -> tuple[int, int]:
    kanji_count = session.scalar(select(func.count()).select_from(Kanji))
    card_count = session.scalar(select(func.count()).select_from(KanjiCard))
    return int(kanji_count or 0), int(card_count or 0)
