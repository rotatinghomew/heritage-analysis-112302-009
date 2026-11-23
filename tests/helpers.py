"""测试辅助：在临时目录生成合成账册。"""

from __future__ import annotations

import csv
import tempfile
from pathlib import Path

PRIMARY_HEADER = [
    "shop", "owner", "week", "amount", "status", "open", "street", "phase",
    "detour", "spillover", "route", "notice", "trade_week", "settlement_week",
    "close_reason",
]


class Book:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.notices: list[dict] = []
        self.extras: dict[str, tuple[list[str], list[list]]] = {}

    def add(self, shop, owner, week, amount="", status="trade", open_="yes", street="",
            phase="", detour="", spillover="", route="", notice="", trade_week=None,
            settlement_week="", close_reason="") -> "Book":
        self.rows.append({
            "shop": shop, "owner": owner, "week": week, "amount": amount,
            "status": status, "open": open_, "street": street, "phase": phase,
            "detour": detour, "spillover": spillover, "route": route, "notice": notice,
            "trade_week": trade_week or week, "settlement_week": settlement_week,
            "close_reason": close_reason,
        })
        return self

    def series(self, shop, owner, weeks, amounts, **kwargs) -> "Book":
        for week, amount in zip(weeks, amounts):
            self.add(shop, owner, week, amount, **kwargs)
        return self

    def notice(self, notice_id, shop, owner, period, amount, status="issued", paid="no") -> "Book":
        self.notices.append({
            "notice": notice_id, "shop": shop, "owner": owner, "period": period,
            "amount": amount, "status": status, "paid": paid,
        })
        return self

    def extra(self, name: str, header: list[str], rows: list[list]) -> "Book":
        self.extras[name] = (header, rows)
        return self

    def write(self, directory: str | Path) -> Path:
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        with (root / "primary.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=PRIMARY_HEADER)
            writer.writeheader()
            writer.writerows(self.rows)
        if self.notices:
            with (root / "support-1.csv").open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(
                    stream, fieldnames=["notice", "shop", "owner", "period", "amount", "status", "paid"]
                )
                writer.writeheader()
                writer.writerows(self.notices)
        for name, (header, rows) in self.extras.items():
            with (root / name).open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(header)
                writer.writerows(rows)
        return root


class TempBook:
    """上下文管理器：生成临时账册目录。"""

    def __init__(self, book: Book):
        self.book = book
        self._tmp = tempfile.TemporaryDirectory()

    def __enter__(self) -> Path:
        return self.book.write(self._tmp.name)

    def __exit__(self, *exc) -> None:
        self._tmp.cleanup()


def stable_controls(book: Book, weeks=("W1", "W2", "W3", "W4", "W5"), phase="P1") -> Book:
    """两家不受影响、前两周起稳定的对照。"""
    for shop, owner, base in (("C1", "X1", 800), ("C2", "X2", 500)):
        for index, week in enumerate(weeks):
            book.add(
                shop, owner, week, base, street="B", phase="pre" if index < 3 else phase,
                detour="" if index < 3 else "no",
            )
    return book
