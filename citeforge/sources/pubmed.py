import json
from collections.abc import Sequence
from itertools import batched

from citeforge.http import HOSTS, Http, ParseError, Result
from citeforge.ids import split_id, to_id
from citeforge.model import Fetch

HOST = HOSTS["eutils.ncbi.nlm.nih.gov"]
CHUNK = 200


def parse_summaries(body: bytes) -> dict[str, str]:
    """Read an esummary JSON body into pmid: id -> doi: id for every summary that lists a valid DOI."""
    try:
        doc = json.loads(body)
    except ValueError as e:
        raise ParseError(f"esummary: {e}") from e
    result = doc.get("result") if isinstance(doc, dict) else None
    if not isinstance(result, dict) or not isinstance(result.get("uids"), list):
        raise ParseError("esummary: no result")
    out: dict[str, str] = {}
    for uid in result["uids"]:
        summary = result.get(uid) if isinstance(uid, str) else None
        pmid = to_id("pmid", uid) if isinstance(uid, str) else None
        if pmid is None or not isinstance(summary, dict) or not isinstance(summary.get("articleids"), list):
            continue
        dois = {
            doi
            for a in summary["articleids"]
            if isinstance(a, dict) and a.get("idtype") == "doi" and isinstance(a.get("value"), str)
            if (doi := to_id("doi", a["value"])) and split_id(doi)[0] == "doi"
        }
        if len(dois) == 1:
            out[pmid] = dois.pop()
    return out


def dois(http: Http, ids: Sequence[str]) -> Result[dict[str, str]]:
    """Map pmid: ids to the DOI PubMed lists for them, 200 per call; a chunk that failed leaves its ids out."""
    wanted = sorted({i for i in ids if split_id(i)[0] == "pmid"})
    found: dict[str, str] = {}
    reason = "ok"
    for chunk in batched(wanted, CHUNK, strict=False):
        params = {"db": "pubmed", "retmode": "json", "id": ",".join(split_id(i)[1] for i in chunk)}
        reply = http.get(HOST, "/entrez/eutils/esummary.fcgi", params)
        if reply.fetch is not Fetch.OK:
            reason = reply.reason
            continue
        try:
            found |= parse_summaries(reply.body)
        except ParseError:
            reason = "parse_error"
    return Result(Fetch.OK if reason == "ok" else Fetch.FAILED, found, reason)
