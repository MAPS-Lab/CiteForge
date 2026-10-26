from __future__ import annotations

from pathlib import Path

import pytest

from citeforge import bibtex_utils
from citeforge.citation_corrections import authoritative_correction, load_citation_corrections
from citeforge.models import Record
from citeforge.pipeline import article as article_mod
from citeforge.text_utils import format_author_dirname


def test_sbbd_record_is_generated_from_authoritative_source_metadata() -> None:
    baseline = {
        "type": "misc",
        "key": "Belizario2026:ReinforcementKidneyAllocation",
        "fields": {
            "title": (
                "Aprendizado por Reforco para Alocacao de Rins: Uma Abordagem Baseada em "
                "Policy Gradient com Dados Reais do Sistema Brasileiro de Transplantes"
            ),
            "author": "IV Belizario and D Teodoro and LGM Andrade and G Spadon and JF Rodrigues-Jr",
            "year": "2026",
        },
    }

    correction = authoritative_correction(baseline)

    assert correction is not None
    assert correction["source"] == "https://sol.sbc.org.br/index.php/sbbd/article/view/43973"
    assert correction["type"] == "inproceedings"
    fields = correction["fields"]
    assert fields["doi"] == "10.5753/sbbd.2026.249194"
    assert fields["booktitle"] == "Simpósio Brasileiro de Banco de Dados (SBBD)"
    assert fields["author"].startswith("Ivar V. Belizario and Douglas Teodoro")
    assert "note" not in fields


def test_patent_corrections_use_howpublished_instead_of_note() -> None:
    baseline = {
        "type": "misc",
        "key": "Haiqi2023:SurgicalAdverseEventDetection",
        "fields": {
            "title": "System and method for adverse event detection or severity estimation from surgical data",
            "author": "WEI Haiqi and TP Grantcharov and B Taati and Y Zhang and F Rudzicz and KL Yang",
            "year": "2023",
        },
    }

    correction = authoritative_correction(baseline)

    assert correction is not None
    fields = correction["fields"]
    assert fields["author"].startswith("Haiqi Wei and Teodor Pantchev Grantcharov")
    assert fields["howpublished"] == "US Patent 11,645,745"
    assert "note" not in fields


def test_correction_requires_exact_normalized_title_and_year() -> None:
    wrong_year = {
        "type": "misc",
        "key": "x",
        "fields": {
            "title": "A Systematic Review of Data Exhaust in IoT Devices",
            "author": "M Mellaty",
            "year": "2023",
        },
    }

    assert authoritative_correction(wrong_year) is None


def test_ieee_record_expands_initials_from_authoritative_metadata() -> None:
    baseline = {
        "type": "inproceedings",
        "key": "Teixeira2026:LOMADLocalAnomalyDetection",
        "fields": {
            "title": (
                "LOMAD: Local Anomaly Detection in Maritime Trajectories Using LSTM Prediction and Visual Analytics"
            ),
            "author": "MR Teixeira and G Spadon and CDG Linhares and A Soares",
            "year": "2026",
        },
    }

    correction = authoritative_correction(baseline)

    assert correction is not None
    assert correction["fields"]["author"] == (
        "Martim R. Teixeira and Gabriel Spadon and Claudio D. G. Linhares and Amilcar Soares"
    )


def test_authoritative_catalog_cannot_publish_note_fields() -> None:
    corrections = load_citation_corrections()

    assert corrections
    assert all("note" not in correction.fields for correction in corrections)


def test_pipeline_regenerates_sbbd_record_without_editing_bibtex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = Record("Gabriel Spadon", scholar_id="bfdGsGUAAAAJ")
    author_dir = tmp_path / format_author_dirname(rec.name, rec.scholar_id)
    author_dir.mkdir(parents=True)
    bib_path = author_dir / "Belizario2026-AprendizadoPor.bib"
    bib_path.write_text(
        "@misc{Belizario2026:ReinforcementKidneyAllocation,\n"
        "  title = {Aprendizado por Reforco para Alocacao de Rins: Uma Abordagem Baseada em "
        "Policy Gradient com Dados Reais do Sistema Brasileiro de Transplantes},\n"
        "  author = {IV Belizario and D Teodoro and LGM Andrade and G Spadon and JF Rodrigues-Jr},\n"
        "  year = {2026},\n"
        "  note = {Unenriched: no enrichment sources matched}\n"
        "}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(article_mod, "_phase2_search", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(article_mod, "process_validated_doi", lambda *_args, **_kwargs: True)

    written = article_mod.process_article(
        rec,
        {
            "title": (
                "Aprendizado por Reforco para Alocacao de Rins: Uma Abordagem Baseada em "
                "Policy Gradient com Dados Reais do Sistema Brasileiro de Transplantes"
            ),
            "authors": "IV Belizario and D Teodoro and LGM Andrade and G Spadon and JF Rodrigues-Jr",
            "year": 2026,
            "source": "existing_corpus",
        },
        None,
        str(tmp_path),
        None,
        None,
    )

    assert written == 1
    generated = bibtex_utils.parse_bibtex_to_dict(bib_path.read_text(encoding="utf-8"))
    assert generated is not None
    assert generated["type"] == "inproceedings"
    assert generated["fields"]["doi"] == "10.5753/sbbd.2026.249194"
    assert generated["fields"]["author"].startswith("Ivar V. Belizario and Douglas Teodoro")
    assert "note" not in generated["fields"]
