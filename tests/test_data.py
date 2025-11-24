from __future__ import annotations

# Well-known papers for testing, including complex edge cases
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
        "scholar_id": "2mSj3aYYXo0J",
        "dblp_key": "conf/nips/VaswaniSPUJGKP17",
        "s2_id": "204e3073870fae3d05bcbc2f6a8e263d9b72e776",
        "openalex_id": "W2964148859",
        "pubmed_id": None,
        "pmc_id": None,
    },
    {
        "name": "alexnet",
        "title": "ImageNet Classification with Deep Convolutional Neural Networks",
        "authors": ["Alex Krizhevsky", "Ilya Sutskever", "Geoffrey E. Hinton"],
        "first_author": "Krizhevsky",
        "year": 2012,
        "venue": "NeurIPS",
        "doi": "10.1145/3065386",
        "arxiv_id": None,
        "scholar_id": "U0nTddPJCXcJ",
        "dblp_key": "conf/nips/KrizhevskySH12",
        "s2_id": "abd1c342495432171beb7ca8fd9551ef13cbd0ff",
        "openalex_id": "W2963159135",
        "pubmed_id": None,
        "pmc_id": None,
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
        "openalex_id": "W2963403456",
        "pubmed_id": None,
        "pmc_id": None,
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
        "openalex_id": "W2157791381",
        "pubmed_id": None,
        "pmc_id": None,
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
        "arxiv_id": None,
        "scholar_id": "KOYnGWpLN9IC",
        "dblp_key": None,
        "s2_id": "13853cf7b0e556b6e7c0b419813ef7c45ce7d07a",
        "openalex_id": "W2129288563",
        "pubmed_id": "22745249",
        "pmc_id": "PMC6286148",
    },
    # --- COMPLEX / EDGE CASE PAPERS ---
    {
        "name": "higgs_boson",
        "title": "Observation of a new particle in the search for the Standard Model Higgs boson with the ATLAS detector at the LHC",
        "authors": ["Georges Aad", "Brad Abbott", "J. Abdallah", "et al."],  # 2932 authors in reality
        "first_author": "Aad",
        "year": 2012,
        "venue": "Physics Letters B",
        "doi": "10.1016/j.physletb.2012.08.020",
        "arxiv_id": "1207.7214",
        "scholar_id": "8k8ZJ-4AAAAJ",
        "dblp_key": None,
        "s2_id": "547565d7663435b54558936022139c74257743c5",
        "openalex_id": "W2089136360",
        "pubmed_id": None,
        "pmc_id": None,
        "notes": "Extreme author count test case",
    },
    {
        "name": "alphafold",
        "title": "Highly accurate protein structure prediction with AlphaFold",
        "authors": ["John Jumper", "Richard Evans", "Alexander Pritzel", "Tim Green", "Michael Figurnov",
                    "Olaf Ronneberger", "Kathryn Tunyasuvunakool", "Russ Bates", "Augustin Žídek",
                    "Anna Potapenko", "Alex Bridgland", "Clemens Meyer", "Simon A. A. Kohl",
                    "Andrew J. Ballard", "Andrew Cowie", "Bernardino Romera-Paredes", "Stanislav Nikolov",
                    "Rishub Jain", "Jonas Adler", "Trevor Back", "Stig Petersen", "David Reiman",
                    "Ellen Clancy", "Michal Zielinski", "Martin Steinegger", "Michalina Pacholska",
                    "Tamish Berghammer", "Sebastian Bodenstein", "David Silver", "Oriol Vinyals",
                    "Andrew W. Senior", "Koray Kavukcuoglu", "Pushmeet Kohli", "Demis Hassabis"],
        "first_author": "Jumper",
        "year": 2021,
        "venue": "Nature",
        "doi": "10.1038/s41586-021-03819-2",
        "arxiv_id": None,
        "scholar_id": "UeHAZ0UAAAAJ",
        "dblp_key": "journals/nature/JumperEPGFBRTBR21",
        "s2_id": "15c8d726213a5b1c5570e15f61727c56262092ee",
        "openalex_id": "W3181579737",
        "pubmed_id": "34265844",
        "pmc_id": "PMC8371605",
        "notes": "Large author list with complex affiliations (in reality)",
    },
    {
        "name": "latex_title",
        "title": "On the $\\sqrt{2}$ approximation and $\\pi$ estimation",
        "authors": ["Test Mathematician"],
        "first_author": "Mathematician",
        "year": 2020,
        "venue": "Journal of Testing",
        "doi": "10.1234/latex.test",
        "arxiv_id": None,
        "scholar_id": None,
        "dblp_key": None,
        "s2_id": None,
        "openalex_id": None,
        "pubmed_id": None,
        "pmc_id": None,
        "notes": "LaTeX math in title test case",
    },
    {
        "name": "unicode_authors",
        "title": "International Collaboration Study",
        "authors": ["Jürgen Müller", "François Dubois", "Åsa Sørensen", "José María González"],
        "first_author": "Müller",
        "year": 2022,
        "venue": "Global Science",
        "doi": "10.1234/unicode.test",
        "arxiv_id": None,
        "scholar_id": None,
        "dblp_key": None,
        "s2_id": None,
        "openalex_id": None,
        "pubmed_id": None,
        "pmc_id": None,
        "notes": "Unicode/Accented characters test case",
    },
    {
        "name": "long_title",
        "title": "A very long title that goes on and on to test the buffer limits and similarity matching algorithms of the system to ensure that it does not crash or produce incorrect results when faced with an unusually verbose publication title that might occur in certain fields like medicine or humanities where titles can be descriptive paragraphs",
        "authors": ["Verbose Author"],
        "first_author": "Author",
        "year": 2023,
        "venue": "Journal of Verbosity",
        "doi": "10.1234/long.test",
        "arxiv_id": None,
        "scholar_id": None,
        "dblp_key": None,
        "s2_id": None,
        "openalex_id": None,
        "pubmed_id": None,
        "pmc_id": None,
        "notes": "Long title test case (>300 chars)",
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
