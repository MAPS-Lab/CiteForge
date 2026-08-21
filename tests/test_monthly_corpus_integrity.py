from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPADON = ROOT / "output" / "Spadon (bfdGsGUAAAAJ)"
A2I2 = ROOT / "output" / "a2i2"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_kidney_paper_uses_authoritative_sbbd_metadata() -> None:
    path = SPADON / "Belizario2026-AprendizadoPor.bib"
    content = _read(path)

    assert content.startswith("@inproceedings{Belizario2026:ReinforcementKidneyAllocation,")
    assert "Refor{\\c{c}}o" in content
    assert "Aloca{\\c{c}}{\\~a}o" in content
    assert (
        "author = {Ivar V. Belizario and Douglas Teodoro and Lu{\\'i}s G. M. Andrade "
        "and Gabriel Spadon and Jose F. Rodrigues-Jr}" in content
    )
    assert "booktitle = {Simp{\\'o}sio Brasileiro de Banco de Dados (SBBD)}" in content
    assert "pages = {238-249}" in content
    assert "publisher = {SBC}" in content
    assert "issn = {2763-8979}" in content
    assert "doi = {10.5753/sbbd.2026.249194}" in content
    assert content == _read(A2I2 / path.name)


def test_changed_author_records_keep_authoritative_name_casing() -> None:
    haiqi = _read(ROOT / "output" / "Rudzicz (elXOB1sAAAAJ)" / "Wei2023-SystemMethod.bib")
    mellaty = _read(ROOT / "output" / "Zincir-Heywood (F9nG0F4AAAAJ)" / "Mellaty2022-SystematicReview.bib")

    assert "author = {Haiqi Wei and Teodor Pantchev Grantcharov and Babak Taati" in haiqi
    assert "and Yichen Zhang and Frank Rudzicz and Kevin Lee Yang}" in haiqi
    assert "author = {Mahdieh Mellaty and Srinivas Sampalli and Nur Zincir-Heywood" in mellaty
    assert "and Kevin de Snayer and Terri Dougall}" in mellaty
    assert "note = {Manuscript}" in mellaty
    assert haiqi == _read(A2I2 / "Wei2023-SystemMethod.bib")
    assert mellaty == _read(A2I2 / "Mellaty2022-SystematicReview.bib")


def test_changed_records_use_complete_author_names_when_sources_resolve_them() -> None:
    teixeira = _read(SPADON / "Teixeira2026-LOMADLocal.bib")
    castiglione = _read(ROOT / "output" / "Wu (IdBlVPUAAAAJ)" / "Castiglione2024-SystemMethod.bib")
    asadi = _read(ROOT / "output" / "Brandt (OMA_KjcAAAAJ)" / "Asadi2020-WhatS.bib")

    assert "author = {Martim R. Teixeira and Gabriel Spadon and Claudio D. G. Linhares and Amilcar Soares}" in teixeira
    assert (
        "author = {Giuseppe Marcello Antonio Castiglione and Weiguang Ding and "
        "Sayedmasoud Hashemi Amroabadi and Ga Wu and Christopher "
        "C{\\^o}t{\\'e} Srinivasa}" in castiglione
    )
    assert "author = {M Asadi and A Brandt and RHC Moir" in asadi
    assert teixeira == _read(A2I2 / "Teixeira2026-LOMADLocal.bib")
    assert castiglione == _read(A2I2 / "Castiglione2024-SystemMethod.bib")
