"""Full-document parsing: text, tables, sections, appendices and references.

With the ``docs`` extra installed, Scholar uses IBM's Docling — layout-aware
PDF/DOCX/PPTX/XLSX parsing that recovers real tables. Without it he falls
back to the standard library (HTML tables, CSV, DOCX text) and pypdf.

A parsed ``Document`` gives him what a careful reader looks for: the tables
(saved as CSV so the sandbox can check the numbers), the appendices, and the
reference list (DOIs/arXiv ids he can follow back to the original studies).
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>{}|\\^`\[\]]+)", re.I)
ARXIV_RE = re.compile(r"\barXiv:\s?(\d{4}\.\d{4,5})(v\d+)?", re.I)


@dataclass
class Table:
    rows: list[list[str]]
    caption: str = ""

    @property
    def shape(self) -> tuple[int, int]:
        return len(self.rows), max((len(r) for r in self.rows), default=0)

    def to_csv(self) -> str:
        buf = io.StringIO()
        csv.writer(buf).writerows(self.rows)
        return buf.getvalue()

    def to_markdown(self, max_rows: int = 25) -> str:
        if not self.rows:
            return ""
        width = self.shape[1]
        rows = [r + [""] * (width - len(r)) for r in self.rows[:max_rows]]
        esc = lambda c: str(c).replace("|", "/").replace("\n", " ")  # noqa: E731
        out = ["| " + " | ".join(esc(c) for c in rows[0]) + " |", "|" + "---|" * width]
        out += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows[1:]]
        if len(self.rows) > max_rows:
            out.append(f"| … {len(self.rows) - max_rows} more rows |")
        return "\n".join(out)


@dataclass
class Document:
    text: str
    title: str = ""
    published: str = ""
    description: str = ""
    tables: list[Table] = field(default_factory=list)
    parser: str = "text"

    @property
    def sections(self) -> list[tuple[str, str]]:
        """(heading, body) pairs from markdown-style headings."""
        parts = re.split(r"^(#{1,4} .+)$", self.text, flags=re.M)
        out = [("", parts[0].strip())] if parts[0].strip() else []
        for i in range(1, len(parts) - 1, 2):
            out.append((parts[i].lstrip("# ").strip(), parts[i + 1].strip()))
        return out

    def section(self, *names: str) -> str:
        """Text of sections whose heading starts with any of ``names`` (case-insensitive)."""
        want = tuple(n.lower() for n in names)
        return "\n\n".join(body for head, body in self.sections if head.lower().startswith(want))

    @property
    def appendices(self) -> str:
        return self.section("appendix", "appendices", "supplementary", "annex", "exhibit")

    @property
    def references(self) -> dict[str, list[str]]:
        """DOIs and arXiv ids cited — preferring the reference list when there is one."""
        refs = self.section("references", "bibliography", "works cited", "literature cited") or self.text
        dois = []
        for d in DOI_RE.findall(refs):
            d = d.rstrip(".,;:)").lower()
            if d not in dois:
                dois.append(d)
        arxiv = list(dict.fromkeys(m[0] for m in ARXIV_RE.findall(refs)))
        return {"dois": dois[:200], "arxiv": arxiv[:100]}


# ------------------------------------------------------------------ HTML
class _HTML(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside",
            "iframe", "button", "select", "template"}
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr",
             "table", "section", "article", "blockquote", "pre", "dd", "dt", "figcaption"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.in_title = False
        self.title = ""
        self.meta: dict[str, str] = {}
        self.tables: list[Table] = []
        self._tables: list[dict] = []  # stack for nested tables
        self._cell: list[str] | None = None
        self._caption: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "title":
            self.in_title = True
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content"):
                self.meta[key] = a["content"]
        elif tag == "table":
            cls = (a.get("class") or "").lower()
            junk = any(k in cls for k in ("navbox", "infobox", "sidebar", "metadata", "ambox", "vertical-navbox",
                                          "mw-collapsible", "nowraplinks", "toc"))
            self._tables.append({"rows": [], "row": None, "caption": "", "junk": junk})
        elif tag == "tr" and self._tables:
            self._tables[-1]["row"] = []
        elif tag == "caption" and self._tables:
            self._caption = []
        elif tag in ("td", "th") and self._tables:
            self._cell = []
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag in ("h1", "h2", "h3", "h4") and not self.skip:
            self.parts.append("#" * min(int(tag[1]), 3) + " ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag == "title":
            self.in_title = False
        elif tag in ("td", "th") and self._tables and self._cell is not None:
            t = self._tables[-1]
            if t["row"] is None:
                t["row"] = []
            t["row"].append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._tables and self._tables[-1]["row"] is not None:
            t = self._tables[-1]
            if any(c for c in t["row"]):
                t["rows"].append(t["row"])
            t["row"] = None
        elif tag == "caption" and self._tables and self._caption is not None:
            self._tables[-1]["caption"] = " ".join("".join(self._caption).split())
            self._caption = None
        elif tag == "table" and self._tables:
            t = self._tables.pop()
            table = Table(t["rows"], t["caption"])
            r, c = table.shape
            if r >= 2 and c >= 2 and not t.get("junk"):  # skip layout and navigation tables
                self.tables.append(table)
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
            return
        if self.skip:
            return
        self.parts.append(data)
        if self._cell is not None:
            self._cell.append(data)
        if self._caption is not None:
            self._caption.append(data)


def parse_html(html: str) -> Document:
    p = _HTML()
    p.feed(html)
    text = "".join(p.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    lines = [ln.strip() for ln in text.split("\n")]
    # Drop menu crumbs: very short lines that aren't headings.
    text = "\n".join(ln for ln in lines if len(ln) > 30 or ln.startswith("#") or not ln).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    m = p.meta
    return Document(
        text=text,
        title=unescape(m.get("og:title") or p.title).strip(),
        published=(m.get("article:published_time") or m.get("date") or m.get("dc.date")
                   or m.get("citation_publication_date") or "")[:10],
        description=m.get("description") or m.get("og:description") or "",
        tables=p.tables, parser="html",
    )


# ------------------------------------------------------------------ others
def _parse_csv(data: bytes) -> Document:
    text = data.decode("utf-8-sig", "replace")
    rows = list(csv.reader(io.StringIO(text)))
    t = Table(rows)
    return Document(text=t.to_markdown(50), tables=[t], parser="csv")


def _parse_docx(data: bytes) -> Document:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    return Document(text=unescape(re.sub(r"<[^>]+>", "", xml)).strip(), parser="docx-basic")


def _parse_pdf_basic(data: bytes) -> Document:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise DocumentError("reading PDFs needs an extra: pip install -e '.[docs]' (best) or '.[pdf]'") from e
    reader = PdfReader(io.BytesIO(data))
    text = "\n\n".join((p.extract_text() or "") for p in reader.pages[:300])
    title = (reader.metadata.title if reader.metadata else "") or ""
    return Document(text=text, title=str(title), parser="pypdf")


class DocumentError(RuntimeError):
    pass


_docling_converter = None


def docling_available() -> bool:
    try:
        import docling.document_converter  # noqa: F401
        return True
    except ImportError:
        return False


def _parse_docling(data: bytes, name: str) -> Document:
    global _docling_converter
    from docling.datamodel.base_models import DocumentStream
    from docling.document_converter import DocumentConverter

    if _docling_converter is None:
        _docling_converter = DocumentConverter()
    res = _docling_converter.convert(DocumentStream(name=name, stream=io.BytesIO(data)))
    doc = res.document
    tables = []
    for t in doc.tables:
        try:
            df = t.export_to_dataframe(doc=doc)
        except TypeError:  # older Docling
            df = t.export_to_dataframe()
        rows = [[str(c) for c in df.columns]] + [[str(v) for v in r] for r in df.itertuples(index=False)]
        try:
            caption = t.caption_text(doc)
        except Exception:  # noqa: BLE001 — caption is a nicety
            caption = ""
        tables.append(Table(rows, caption or ""))
    title = ""
    for item in getattr(doc, "texts", [])[:20]:
        if str(getattr(item, "label", "")).endswith("title"):
            title = item.text
            break
    md = doc.export_to_markdown()
    if not title:
        m = re.search(r"^#{1,2} (.+)$", md, re.M)
        title = m.group(1).strip() if m else ""
    return Document(text=md, title=title, tables=tables, parser="docling")


def kind_of(name: str, content_type: str = "", head: bytes = b"") -> str:
    name, ct = name.lower(), content_type.lower()
    if head[:5] == b"%PDF-" or "pdf" in ct or name.endswith(".pdf"):
        return "pdf"
    if name.endswith((".docx",)) or "wordprocessingml" in ct:
        return "docx"
    if name.endswith((".pptx",)) or "presentationml" in ct:
        return "pptx"
    if name.endswith((".xlsx",)) or "spreadsheetml" in ct:
        return "xlsx"
    if name.endswith(".csv") or "text/csv" in ct:
        return "csv"
    if "html" in ct or name.endswith((".html", ".htm")) or head.lstrip()[:1] == b"<":
        return "html"
    return "text"


def parse(data: bytes, name: str = "document", content_type: str = "") -> Document:
    kind = kind_of(name, content_type, data[:16])
    charset = (re.search(r"charset=([\w-]+)", content_type) or [None, "utf-8"])[1]
    if kind == "html":
        return parse_html(data.decode(charset, "replace"))
    if kind == "csv":
        return _parse_csv(data)
    if kind in ("pdf", "docx", "pptx", "xlsx") and docling_available():
        suffix = "." + kind
        try:
            return _parse_docling(data, name if name.lower().endswith(suffix) else name + suffix)
        except Exception as e:  # noqa: BLE001 — fall back rather than lose the document
            if kind not in ("pdf", "docx"):
                raise DocumentError(f"Docling could not parse {name}: {e}") from e
    if kind == "pdf":
        return _parse_pdf_basic(data)
    if kind == "docx":
        return _parse_docx(data)
    if kind in ("pptx", "xlsx"):
        raise DocumentError(f"{kind.upper()} files need Docling: pip install -e '.[docs]'")
    return Document(text=data.decode(charset, "replace"), parser="text")


def parse_file(path: str | Path) -> Document:
    path = Path(path)
    doc = parse(path.read_bytes(), path.name)
    if not doc.title:
        doc.title = path.stem.replace("_", " ").replace("-", " ")
    return doc


def chunk(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
    """Split on paragraph boundaries into ~size-character chunks with a little overlap."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        while len(p) > size:  # very long paragraph: hard split
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(p[:size])
            p = p[size - overlap:]
        if len(cur) + len(p) + 2 > size and cur:
            chunks.append(cur)
            cur = cur[-overlap:] + "\n\n" + p if overlap else p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks
