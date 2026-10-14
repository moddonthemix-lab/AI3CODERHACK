import io
import zipfile

from builder.documents import parse
from builder.sandbox import run_python
from builder.web import parse_edgar, parse_openalex_work


def test_sandbox_runs_analysis_but_blocks_escape(tmp_path):
    data = tmp_path / "t.csv"
    data.write_text("month,units\n1,10\n2,14\n3,19\n")
    ok = run_python("import csv,statistics\nu=[int(r['units']) for r in csv.DictReader(open('T1.csv'))]\n"
                    "print(statistics.mean(u))\nopen('out.txt','w').write('done')", tmp_path / "runs", {"T1.csv": data})
    assert ok["ok"] and ok["stdout"].strip() == "14.333333333333334" and ok["files"][0].endswith("out.txt")
    outside = tmp_path / "escaped.txt"
    bad = {
        "network": "import socket; socket.create_connection(('1.1.1.1', 80), timeout=2)",
        "write": f"open({str(outside)!r}, 'w').write('x')",
        "process": "import subprocess; subprocess.run(['echo', 'hi'])",
    }
    for name, code in bad.items():
        r = run_python(code, tmp_path / "runs", timeout=10)
        assert not r["ok"], name
    assert not outside.exists()
    for _ in range(3):  # used to race between the CPU limit and the clock limit
        r = run_python("while True: pass", tmp_path / "runs", timeout=2)
        assert not r["ok"] and ("timed out" in r["stderr"] or "exceeded" in r["stderr"])


def test_documents_tables_references_appendix():
    html = b"""<html><title>Report</title><body><h2>Results</h2><p>Revenue grew strongly in every region we measured.</p>
    <table><caption>Table 1: Revenue</caption><tr><th>Year</th><th>Rev</th></tr><tr><td>2024</td><td>1,200</td></tr></table>
    <table><tr><td>layout only</td></tr></table>
    <table class="navbox"><tr><td>Nav</td><td>links</td></tr><tr><td>More</td><td>links</td></tr></table>
    <h2>References</h2><p>doi:10.1038/s41586-020-2649-2. arXiv:2401.01234v2 and https://doi.org/10.1126/science.abc123.</p>
    <h2>Appendix A</h2><p>Robustness checks with an alternative sample of 500 firms are reported here.</p></body></html>"""
    d = parse(html, "r.html")
    assert len(d.tables) == 1 and d.tables[0].caption == "Table 1: Revenue" and d.tables[0].rows[1] == ["2024", "1,200"]
    assert d.tables[0].to_csv().splitlines()[0] == "Year,Rev"
    assert d.references == {"dois": ["10.1038/s41586-020-2649-2", "10.1126/science.abc123"], "arxiv": ["2401.01234"]}
    assert "500 firms" in d.appendices
    csv_doc = parse(b"a,b\n1,2\n", "x.csv")
    assert csv_doc.tables[0].shape == (2, 2)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:p><w:t>Hello docx world</w:t></w:p></w:document>")
    assert "Hello docx world" in parse(buf.getvalue(), "x.docx").text


def test_parse_openalex_work():
    w = {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1/abc", "display_name": "A study",
         "publication_date": "2024-02-01", "cited_by_count": 42, "type": "article",
         "abstract_inverted_index": {"Prices": [0], "rose": [1], "fast": [2]},
         "primary_location": {"source": {"display_name": "J. Econ"}}, "authorships": [{"author": {"display_name": "Ada"}}],
         "best_oa_location": {"pdf_url": "https://x.org/a.pdf"}, "referenced_works": ["https://openalex.org/W9"]}
    p = parse_openalex_work(w)
    assert p["snippet"] == "Prices rose fast" and p["url"] == "https://doi.org/10.1/abc" and p["tier"] == 1
    assert p["pdf_url"] == "https://x.org/a.pdf" and p["cited_by_count"] == 42 and p["venue"] == "J. Econ"
    r = parse_openalex_work({**w, "is_retracted": True})
    assert r["tier"] == 4 and "RETRACTED" in r["tier_label"]


def test_parse_edgar():
    data = {"hits": {"hits": [{"_id": "0001493152-18-010672:form10-k.htm", "_source": {
        "ciks": ["0001557668"], "display_names": ["BLUE EAGLE LITHIUM INC.  (CIK 0001557668)"], "form": "10-K",
        "file_date": "2018-07-31", "biz_locations": ["Henderson, NV"]}}]}}
    r = parse_edgar(data)[0]
    assert r["url"] == "https://www.sec.gov/Archives/edgar/data/1557668/000149315218010672/form10-k.htm"
    assert r["title"].startswith("BLUE EAGLE LITHIUM INC. — 10-K") and r["tier"] == 1 and r["published"] == "2018-07-31"
