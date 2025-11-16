from __future__ import annotations

# Well-known papers for testing
KNOWN_PAPERS = [
    {
        "name": "attention_is_all_you_need",
        "title": "Attention Is All You Need",
        "authors": ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar", "Jakob Uszkoreit",
                    "Llion Jones", "Aidan N. Gomez", "Lukasz Kaiser", "Illia Polosukhin"],
        "first_author": "Vaswani",
        "year": 2017,
        "venue": "NeurIPS",
        "doi": "10.48550/arXiv.1706.03762",
        "arxiv_id": "1706.03762",
        "scholar_id": "2mSj3aYYXo0J",  # Scholar cluster ID
        "dblp_key": "conf/nips/VaswaniSPUJGKP17",
        "s2_id": "204e3073870fae3d05bcbc2f6a8e263d9b72e776",  # Semantic Scholar corpus ID
        "openalex_id": "W2964148859",  # OpenAlex work ID
        "pubmed_id": None,  # Not in PubMed (CS paper)
        "pmc_id": None,  # Not in PMC
    },
    {
        "name": "alexnet",
        "title": "ImageNet Classification with Deep Convolutional Neural Networks",
        "authors": ["Alex Krizhevsky", "Ilya Sutskever", "Geoffrey E. Hinton"],
        "first_author": "Krizhevsky",
        "year": 2012,
        "venue": "NeurIPS",
        "doi": "10.1145/3065386",
        "arxiv_id": None,  # Not on arXiv originally
        "scholar_id": "U0nTddPJCXcJ",
        "dblp_key": "conf/nips/KrizhevskySH12",
        "s2_id": "abd1c342495432171beb7ca8fd9551ef13cbd0ff",
        "openalex_id": "W2963159135",  # OpenAlex work ID
        "pubmed_id": None,  # Not in PubMed (CS paper)
        "pmc_id": None,  # Not in PMC
    },
    {
        "name": "bert",
        "title": "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
        "authors": ["Jacob Devlin", "Ming-Wei Chang", "Kenton Lee", "Kristina Toutanova"],
        "first_author": "Devlin",
        "year": 2019,
        "venue": "NAACL",
        "doi": "10.18653/v1/N19-1423",
        "arxiv_id": "1810.04805",
        "scholar_id": "df2b0e26d0599ce3e70df8a9da02e51594e0e992",
        "dblp_key": "conf/naacl/DevlinCLT19",
        "s2_id": "df2b0e26d0599ce3e70df8a9da02e51594e0e992",
        "openalex_id": "W2963403456",  # OpenAlex work ID
        "pubmed_id": None,  # Not in PubMed (CS paper)
        "pmc_id": None,  # Not in PMC
    },
    {
        "name": "resnet",
        "title": "Deep Residual Learning for Image Recognition",
        "authors": ["Kaiming He", "Xiangyu Zhang", "Shaoqing Ren", "Jian Sun"],
        "first_author": "He",
        "year": 2016,
        "venue": "CVPR",
        "doi": "10.1109/CVPR.2016.90",
        "arxiv_id": "1512.03385",
        "scholar_id": "1KiE26eQMfsJ",
        "dblp_key": "conf/cvpr/HeZRS16",
        "s2_id": "2c03df8b48bf3fa39054345bafabfeff15bfd11d",
        "openalex_id": "W2157791381",  # OpenAlex work ID
        "pubmed_id": None,  # Not in PubMed (CS paper)
        "pmc_id": None,  # Not in PMC
    },
    {
        "name": "crispr_cas9",
        "title": "A programmable dual-RNA-guided DNA endonuclease in adaptive bacterial immunity",
        "authors": ["Martin Jinek", "Krzysztof Chylinski", "Ines Fonfara", "Michael Hauer",
                    "Jennifer A. Doudna", "Emmanuelle Charpentier"],
        "first_author": "Jinek",
        "year": 2012,
        "venue": "Science",
        "doi": "10.1126/science.1225829",
        "arxiv_id": None,  # Not on arXiv (biomedical paper)
        "scholar_id": "KOYnGWpLN9IC",
        "dblp_key": None,  # Not in DBLP (biomedical)
        "s2_id": "13853cf7b0e556b6e7c0b419813ef7c45ce7d07a",
        "openalex_id": "W2129288563",  # OpenAlex work ID
        "pubmed_id": "22745249",  # PubMed ID
        "pmc_id": "PMC6286148",  # PubMed Central ID
    },
]

# Papers optimized for specific APIs (to ensure each API is actually tested)
API_SPECIFIC_PAPERS = {
    "semantic_scholar": {
        # This paper should be in S2
        "title": "Attention Is All You Need",
        "first_author": "Vaswani",
        "year": 2017,
    },
    "dblp": {
        # Donald Knuth's papers are well-established in DBLP
        "author_name": "Donald E. Knuth",
        "dblp_pid": "k/DonaldEKnuth",
        "min_year": 2000,
    },
    "openreview": {
        # Recent ICLR 2024 paper more likely to be in OpenReview
        "title": "Self-Rewarding Language Models",
        "first_author": "Yuan",
        "year": 2024,
    },
    "openalex": {
        # Well-known paper that should be in OpenAlex
        "title": "Attention Is All You Need",
        "first_author": "Vaswani",
        "year": 2017,
        "openalex_id": "W2964148859",
    },
    "pubmed": {
        # Famous CRISPR paper in PubMed
        "title": "A programmable dual-RNA-guided DNA endonuclease in adaptive bacterial immunity",
        "first_author": "Jinek",
        "year": 2012,
        "pubmed_id": "22745249",
    },
    "europepmc": {
        # Same CRISPR paper should be in Europe PMC
        "title": "A programmable dual-RNA-guided DNA endonuclease in adaptive bacterial immunity",
        "first_author": "Jinek",
        "year": 2012,
        "pmc_id": "PMC6286148",
    },
    "datacite": {
        # Example dataset DOI (Zenodo dataset)
        "title": "COVID-19 Open Research Dataset",
        "doi": "10.5281/zenodo.3715506",
        "year": 2020,
    },
    "orcid": {
        # Use a known public ORCID with accessible works
        "orcid_id": "0000-0002-1825-0097",
        "author_name": "Test User",
    },
}

# Test author with known publications (for integration tests)
TEST_AUTHOR = {
    "name": "Geoffrey Hinton",
    "email": "test@example.com",
    # Geoffrey Hinton's Google Scholar ID
    "scholar_id": "JicYPdAAAAAJ",
    "orcid": "",
    "dblp": "/h/GeoffreyEHinton",  # DBLP person ID
}

# Expected fields that should be present after enrichment
REQUIRED_FIELDS = ["title", "author", "year"]
OPTIONAL_FIELDS = ["journal", "booktitle", "doi", "url", "pages", "volume", "note"]

# API-specific configuration
API_CONFIGS = {
    "serpapi": {
        "required": True,
        "key_file": "keys/SerpAPI.key",
        "timeout": 30.0,
    },
    "semantic_scholar": {
        "required": False,
        "key_file": "keys/Semantic.key",
        "timeout": 15.0,
    },
    "crossref": {
        "required": False,
        "key_file": None,  # No auth required
        "timeout": 15.0,
    },
    "arxiv": {
        "required": False,
        "key_file": None,  # No auth required
        "timeout": 15.0,
    },
    "openreview": {
        "required": False,
        "key_file": "keys/OpenReview.key",
        "timeout": 15.0,
    },
    "dblp": {
        "required": False,
        "key_file": None,  # No auth required
        "timeout": 15.0,
    },
    "doi": {
        "required": False,
        "key_file": None,  # No auth required
        "timeout": 15.0,
    },
    "openalex": {
        "required": False,
        "key_file": None,  # No auth required
        "timeout": 15.0,
    },
    "pubmed": {
        "required": False,
        "key_file": None,  # No auth required
        "timeout": 15.0,
    },
    "europepmc": {
        "required": False,
        "key_file": None,  # No auth required
        "timeout": 15.0,
    },
    "datacite": {
        "required": False,
        "key_file": None,  # No auth required
        "timeout": 15.0,
    },
    "orcid": {
        "required": False,
        "key_file": None,  # No auth required
        "timeout": 15.0,
    },
}
