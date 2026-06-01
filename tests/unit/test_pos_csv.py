"""POS CSV parser contracts."""

from __future__ import annotations

from pathlib import Path

from services.pos.csv_replay import parse_csv


def test_parses_brigade_style_headers(tmp_path: Path) -> None:
    p = tmp_path / "x.csv"
    p.write_text(
        "Invoice Date,Invoice Number,Total Amount,Item Count,Payment Mode,Salesperson Id\n"
        "10/04/2026 20:01,ML0426KAP0001358,274.36,1,UPI,1178\n"
        "10/04/2026 20:05,ML0426KAP0001359,499.00,2,CARD,971\n"
    )
    rows = parse_csv(p)
    assert len(rows) == 2
    assert rows[0]["invoice"] == "ML0426KAP0001358"
    assert rows[0]["total"] == 274.36
    assert rows[0]["items"] == 1
    assert rows[0]["mode"] == "UPI"
    assert rows[0]["salesperson"] == "1178"
    assert rows[0]["ts"] < rows[1]["ts"]


def test_skips_unparseable_rows(tmp_path: Path) -> None:
    p = tmp_path / "x.csv"
    p.write_text(
        "Invoice Date,Invoice Number,Total Amount\n"
        "10/04/2026 20:01,INV1,123.45\n"
        ",INV2,99.00\n"             # missing ts → skip
        "10/04/2026 20:03,INV3,not_a_number\n"  # bad total → skip
        "10/04/2026 20:05,INV4,500.00\n"
    )
    rows = parse_csv(p)
    assert [r["invoice"] for r in rows] == ["INV1", "INV4"]


def test_sorts_by_timestamp(tmp_path: Path) -> None:
    p = tmp_path / "x.csv"
    p.write_text(
        "Invoice Date,Invoice Number,Total Amount\n"
        "10/04/2026 20:05,B,1.00\n"
        "10/04/2026 20:01,A,1.00\n"
        "10/04/2026 20:03,C,1.00\n"
    )
    rows = parse_csv(p)
    assert [r["invoice"] for r in rows] == ["A", "C", "B"]
