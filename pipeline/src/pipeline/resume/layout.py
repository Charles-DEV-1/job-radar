"""Run-level placeholders retain the original DOCX package and formatting."""

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from docxtpl import DocxTemplate
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment
from lxml import etree

from pipeline.resume.schema import MasterResume, Tailoring

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
NS = {"w": W[1:-1]}
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
HEADINGS = {
    "summary": "summary",
    "profile": "summary",
    "professional summary": "summary",
    "skills": "skills",
    "technical skills": "skills",
    "core skills": "skills",
    "experience": "experience",
    "work experience": "experience",
    "professional experience": "experience",
    "employment": "experience",
    "projects": "projects",
    "personal projects": "projects",
    "education": "education",
    "certifications": "certifications",
}


class LayoutError(ValueError):
    pass


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def xml_root(blob: bytes) -> Any:
    if b"<!DOCTYPE" in blob or b"<!ENTITY" in blob:
        raise LayoutError("DOCX contains unsupported XML entity declarations")
    return etree.fromstring(
        blob, etree.XMLParser(resolve_entities=False, no_network=True)
    )


def stories(package: ZipFile) -> dict[str, Any]:
    return {
        name: xml_root(package.read(name))
        for name in package.namelist()
        if re.fullmatch(r"word/(?:document|header\d+|footer\d+)\.xml", name)
    }


def own_text_nodes(paragraph: Any) -> list[Any]:
    return [
        t
        for t in paragraph.iter(W + "t")
        if next(t.iterancestors(W + "p"), None) is paragraph
    ]


def paragraph_text(paragraph: Any) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        if next(node.iterancestors(W + "p"), None) is not paragraph:
            continue
        if node.tag == W + "t":
            parts.append(node.text or "")
        elif node.tag == W + "tab":
            parts.append("\t")
        elif node.tag in {W + "br", W + "cr"}:
            parts.append("\n")
    return "".join(parts)


def heading(text: str) -> str | None:
    return HEADINGS.get(text.strip().casefold().rstrip(":"))


def stable_id(prefix: str, position: int, text: str) -> str:
    return f"{prefix}-{position}-{hashlib.sha256(text.encode()).hexdigest()[:10]}"


def is_bullet(paragraph: Any, text: str) -> bool:
    style = paragraph.find(".//" + W + "pStyle")
    return bool(
        paragraph.find(".//" + W + "numPr") is not None
        or style is not None
        and "bullet" in style.get(W + "val", "").casefold()
        or re.match(r"^[•●▪\-]\s", text)
    )


def draft_master(original: Path) -> MasterResume:
    """Extract literal content only. Ambiguous headers are left for owner review."""
    with ZipFile(original) as package:
        roots = stories(package)
    paragraphs = list(roots["word/document.xml"].iter(W + "p"))
    data: dict[str, Any] = {
        "contact": {"name": "", "lines": []},
        "summary": "",
        "skills": [],
        "experience": [],
        "projects": [],
        "education": [],
        "certifications": [],
        "reviewed": False,
    }
    section = "contact"
    role: dict[str, Any] | None = None
    project: dict[str, Any] | None = None
    for position, paragraph in enumerate(paragraphs):
        text = paragraph_text(paragraph)
        if not text.strip():
            continue
        next_section = heading(text)
        if next_section:
            section, role, project = next_section, None, None
            continue
        if section == "contact":
            if not data["contact"]["name"]:
                data["contact"]["name"] = text
            else:
                data["contact"]["lines"].append(text)
        elif section == "summary":
            data["summary"] += ("\n" if data["summary"] else "") + text
        elif section == "skills":
            label, sep, values = text.partition(":")
            items = re.split(r"\s*[,;]\s*", values if sep else text)
            data["skills"].append(
                {"name": label if sep else "Skills", "items": items, "raw": text}
            )
        elif section == "experience":
            if role is None or not is_bullet(paragraph, text):
                pieces = re.split(r"\s*\|\s*|\t+", text)
                role = {
                    "id": stable_id("role", position, text),
                    "employer": pieces[0],
                    "title": pieces[1] if len(pieces) > 1 else "",
                    "location": pieces[2] if len(pieces) > 3 else None,
                    "dates": pieces[-1] if len(pieces) > 2 else "",
                    "heading": text,
                    "bullets": [],
                }
                data["experience"].append(role)
            else:
                role["bullets"].append(
                    {"id": stable_id("bullet", position, text), "text": text}
                )
        elif section == "projects":
            if project is None or not is_bullet(paragraph, text):
                project = {
                    "id": stable_id("project", position, text),
                    "name": text,
                    "bullets": [],
                }
                data["projects"].append(project)
            else:
                project["bullets"].append(
                    {"id": stable_id("bullet", position, text), "text": text}
                )
        elif section == "education":
            data["education"].append(
                {"institution": text, "degree": "", "dates": "", "raw": text}
            )
        elif section == "certifications":
            data["certifications"].append({"name": text, "raw": text})
    return MasterResume.model_validate(data)


def create_template(
    original: Path, template: Path, layout_path: Path, master: MasterResume
) -> dict[str, Any]:
    bindings: dict[str, list[dict[str, Any]]] = {}

    def bind(text: str, value: dict[str, Any]) -> None:
        if text:
            bindings.setdefault(text, []).append(value)

    summary_parts = master.summary.split("\n")
    for i, text in enumerate(summary_parts):
        bind(text, {"kind": "summary", "segment": i, "count": len(summary_parts)})
    for i, group in enumerate(master.skills):
        bind(
            group.raw or f"{group.name}: {', '.join(group.items)}",
            {"kind": "skills", "group": i},
        )
    for role in master.experience:
        for bullet in role.bullets:
            bind(bullet.text, {"kind": "bullet", "role": role.id, "id": bullet.id})
    blocks: list[dict[str, Any]] = []
    spans: list[dict[str, Any]] = []
    seen: set[str] = set()
    counter = 0
    with ZipFile(original) as package:
        roots = stories(package)
        original_text = " ".join(
            " ".join(paragraph_text(p).split()).casefold()
            for root in roots.values()
            for p in root.iter(W + "p")
        )
        original_text += " " + " ".join(
            rel.get("Target", "").casefold()
            for name in package.namelist()
            if name.endswith(".rels")
            for rel in xml_root(package.read(name))
            if rel.get("TargetMode") == "External"
        )
        immutable = [master.contact.name, *master.contact.lines, *master.contact.links]
        immutable += [
            master.contact.email or "",
            master.contact.phone or "",
            master.contact.location or "",
        ]
        immutable += [s for g in master.skills for s in g.items]
        for role in master.experience:
            immutable += [role.employer, role.title, role.dates, role.location or ""]
        for entry in master.education:
            immutable += [
                entry.institution,
                entry.degree,
                entry.dates,
                entry.location or "",
            ]
        for entry in master.certifications:
            immutable += [entry.name, entry.issuer or "", entry.dates or ""]
        for project in master.projects:
            immutable += [
                project.name,
                project.dates or "",
                project.description or "",
                project.url or "",
                *project.detail_lines,
                *[b.text for b in project.bullets],
            ]
        if any(
            " ".join(value.split()).casefold() not in original_text
            for value in immutable
            if value
        ):
            raise LayoutError(
                "Master facts disagree with original DOCX; correct inputs"
            )
        for part, root in roots.items():
            tree = root.getroottree()
            paragraphs = list(root.iter(W + "p"))
            original_texts = {p: paragraph_text(p) for p in paragraphs}
            for index, paragraph in enumerate(paragraphs):
                text = paragraph_text(paragraph)
                candidates = bindings.get(text, [])
                binding = candidates.pop(0) if candidates else {"kind": "static"}
                seen.add(str(binding))
                nodes = own_text_nodes(paragraph)
                runs: list[dict[str, str]] = []
                for node in nodes:
                    key = f"text_{counter:06d}"
                    counter += 1
                    runs.append({"key": key, "text": node.text or ""})
                    node.text = "{{ " + key + " }}"
                blocks.append(
                    {
                        "part": part,
                        "index": index,
                        "text": text,
                        "runs": runs,
                        "binding": binding,
                        "controls": bool(
                            paragraph.findall(".//" + W + "tab")
                            or paragraph.findall(".//" + W + "br")
                            or paragraph.findall(".//" + W + "hyperlink")
                        ),
                    }
                )
            if part == "word/document.xml":
                starts = []
                for project in master.projects:
                    matches = [
                        p
                        for p in paragraphs
                        if original_texts[p] == project.name
                        or original_texts[p].startswith(project.name + "\t")
                        or original_texts[p].startswith(project.name + "  ")
                    ]
                    if len(matches) == 1:
                        starts.append((project.id, matches[0]))
                for project_id, paragraph in starts:
                    parent = paragraph.getparent()
                    children = list(parent)
                    start = children.index(paragraph)
                    end = len(children)
                    for i in range(start + 1, len(children)):
                        if children[i].tag == W + "sectPr" or (
                            children[i].tag == W + "p"
                            and (
                                heading(original_texts.get(children[i], ""))
                                or any(children[i] is p for _, p in starts)
                            )
                        ):
                            end = i
                            break
                    spans.append(
                        {
                            "id": project_id,
                            "part": part,
                            "parent": tree.getpath(parent),
                            "start": start,
                            "end": end,
                        }
                    )
        # PDF reconstruction often wraps one fact into several paragraphs, or
        # puts several skill groups in one paragraph. Bind their literal regions
        # without moving runs, line breaks or surrounding text. Ambiguous matches
        # remain unbound and stop setup below.
        normalized: list[str] = []
        positions: list[tuple[int, int]] = []
        for i, block in enumerate(blocks):
            if block["part"] != "word/document.xml":
                continue
            for j, char in enumerate(block["text"] + " "):
                if char.isspace():
                    if normalized and normalized[-1] != " ":
                        normalized.append(" ")
                        positions.append((i, min(j, len(block["text"]))))
                else:
                    normalized.append(char)
                    positions.append((i, j))
        page_text = "".join(normalized)
        for fact, candidates in bindings.items():
            if not candidates:
                continue
            phrase = " ".join(fact.split())
            matches = list(re.finditer(re.escape(phrase), page_text))
            if len(matches) != 1 or len(candidates) != 1:
                continue
            match = matches[0]
            first, start = positions[match.start()]
            last, final = positions[match.end() - 1]
            pieces = [
                (
                    i,
                    start if i == first else 0,
                    final + 1 if i == last else len(blocks[i]["text"]),
                )
                for i in range(first, last + 1)
            ]
            if any(blocks[i]["binding"]["kind"] != "static" for i, _, _ in pieces):
                continue
            if any(
                a < region["end"] and b > region["start"]
                for i, a, b in pieces
                for region in blocks[i].get("regions", [])
            ):
                continue
            binding = candidates.pop()
            sizes = [b - a for _, a, b in pieces]
            for fragment, (i, a, b) in enumerate(pieces):
                blocks[i].setdefault("regions", []).append(
                    {
                        "start": a,
                        "end": b,
                        "fact": fact,
                        "binding": binding,
                        "fragment": fragment,
                        "sizes": sizes,
                    }
                )
        template.parent.mkdir(parents=True, exist_ok=True)
        with ZipFile(template, "w") as target:
            for item in package.infolist():
                blob = (
                    etree.tostring(
                        roots[item.filename],
                        xml_declaration=True,
                        encoding="UTF-8",
                        standalone=True,
                    )
                    if item.filename in roots
                    else package.read(item.filename)
                )
                target.writestr(item, blob)
    # Contact placeholders are filled only from the local master JSON. Existing
    # inline/hyperlink runs stay in their original positions and retain formatting.
    for block in blocks:
        if block["binding"]["kind"] != "static" or block.get("regions"):
            continue
        for field, index, value in _contact_fields(master):
            if value and block["text"] == value:
                block["binding"] = {"kind": "contact", "field": field, "index": index}
                break
    unbound = sum(len(values) for values in bindings.values())
    if unbound:
        template.unlink(missing_ok=True)
        raise LayoutError(
            "Editable fields could not be mapped to DOCX paragraphs; "
            "use literal paragraph text"
        )
    manifest = {
        "version": 1,
        "original_hash": digest(original),
        "template_hash": digest(template),
        "facts_hash": master.facts_hash(),
        "blocks": blocks,
        "project_spans": spans,
    }
    layout_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def load_layout(
    path: Path, master: MasterResume, original: Path, template: Path
) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        data.get("version") != 1
        or data.get("original_hash") != digest(original)
        or data.get("template_hash") != digest(template)
        or data.get("facts_hash") != master.facts_hash()
    ):
        raise LayoutError(
            "Master/template/layout changed; rerun resume.init with reviewed JSON"
        )
    return data


def _contact_fields(master: MasterResume) -> list[tuple[str, int | None, str]]:
    fields = []
    for field, value in master.contact.model_dump().items():
        if isinstance(value, str):
            fields.append((field, None, value))
        elif isinstance(value, list):
            fields.extend((field, i, text) for i, text in enumerate(value))
    return fields


def _replacement(
    block: dict[str, Any], master: MasterResume, edit: Tailoring
) -> str | None:
    if block.get("regions"):
        value = block["text"]
        for region in sorted(block["regions"], key=lambda r: r["start"], reverse=True):
            replacement = _replacement(
                {"binding": region["binding"], "text": region["fact"]}, master, edit
            )
            if replacement is None:
                raise LayoutError(
                    "Wrapped bullet selection needs manual paragraph mapping"
                )
            if " ".join(replacement.split()) == " ".join(region["fact"].split()):
                continue
            replacement = _region_fragments(replacement, region["sizes"])[
                region["fragment"]
            ]
            value = value[: region["start"]] + replacement + value[region["end"] :]
        return value
    binding = block["binding"]
    if binding["kind"] == "contact":
        value = getattr(master.contact, binding["field"])
        return value if binding["index"] is None else value[binding["index"]]
    if binding["kind"] == "summary":
        parts = edit.summary.split("\n")
        if len(parts) != binding["count"]:
            raise LayoutError("Summary paragraph count changed")
        return parts[binding["segment"]]
    if binding["kind"] == "skills":
        group = master.skills[binding["group"]]
        selected = [s for s in edit.skills_order if s in group.items]
        if selected == group.items:
            return block["text"]
        prefix, separator, _ = block["text"].partition(":")
        delimiter = "; " if ";" in block["text"] and "," not in block["text"] else ", "
        return (prefix + ": " if separator else "") + delimiter.join(selected)
    if binding["kind"] == "bullet":
        role = next(r for r in master.experience if r.id == binding["role"])
        tailored = next(r for r in edit.experience if r.id == role.id)
        position = [b.id for b in role.bullets].index(binding["id"])
        if position >= len(tailored.bullet_order):
            return None
        source_id = tailored.bullet_order[position]
        return next(b.text for b in tailored.bullets if b.id == source_id)
    # Backwards compatibility for contact paragraphs in existing templates.
    # Inline hyperlinks stay immutable in the hash-checked original package.
    for _, _, contact_value in _contact_fields(master):
        if contact_value and contact_value == block["text"]:
            return contact_value
    return block["text"]


def _region_fragments(text: str, sizes: list[int]) -> list[str]:
    if len(sizes) == 1:
        if len(text) > sizes[0]:
            raise LayoutError("Editable region exceeds its original slot")
        return [text]
    words = text.split()
    result: list[str] = []
    for size in sizes:
        chunk: list[str] = []
        while words and len(" ".join([*chunk, words[0]])) <= size:
            chunk.append(words.pop(0))
        result.append(" ".join(chunk))
    if words:
        raise LayoutError("Wrapped region cannot fit without splitting a word")
    return result


def _distribute(text: str, runs: list[dict[str, str]]) -> list[str]:
    lengths = [len(r["text"]) for r in runs]
    if not lengths or sum(lengths) == 0:
        if text:
            raise LayoutError("No existing text runs for editable content")
        return [""] * len(lengths)
    # Preserve each run/property/span; text boundaries move proportionally.
    result: list[str] = []
    position = 0
    for length in lengths[:-1]:
        end = position + min(length, max(0, len(text) - position))
        result.append(text[position:end])
        position = end
    return result + [text[position:]]


def render(
    master: MasterResume,
    edit: Tailoring,
    original: Path,
    template: Path,
    layout_path: Path,
    destination: Path,
) -> None:
    layout = load_layout(layout_path, master, original, template)
    context: dict[str, str] = {}
    removed: set[tuple[str, int]] = set()
    for block in layout["blocks"]:
        value = _replacement(block, master, edit)
        if value is None:
            removed.add((block["part"], block["index"]))
            value = ""
        if value == block["text"]:
            texts = [r["text"] for r in block["runs"]]
        else:
            if block["controls"] or len(value) > len(block["text"]):
                raise LayoutError(
                    "Editable paragraph exceeds its slot or has protected controls"
                )
            texts = _distribute(value, block["runs"])
        context.update(
            {r["key"]: text for r, text in zip(block["runs"], texts, strict=True)}
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    intermediate = destination.with_suffix(".rendering.docx")
    try:
        doc = DocxTemplate(template)
        doc.render(
            context,
            jinja_env=SandboxedEnvironment(undefined=StrictUndefined),
            autoescape=True,
        )
        doc.save(intermediate)
        with ZipFile(intermediate) as rendered, ZipFile(original) as source:
            roots = stories(rendered)
            paragraph_refs = {
                (part, index): paragraph
                for part, root in roots.items()
                for index, paragraph in enumerate(root.iter(W + "p"))
            }
            if edit.projects_order != [p.id for p in master.projects]:
                spans = layout["project_spans"]
                if (
                    len(spans) != len(master.projects)
                    or len({s["parent"] for s in spans}) != 1
                    or any(a["end"] != b["start"] for a, b in zip(spans, spans[1:]))
                ):
                    raise LayoutError(
                        "Project blocks are not contiguous; cannot reorder safely"
                    )
                root = roots["word/document.xml"]
                parent = root.xpath(spans[0]["parent"], namespaces=root.nsmap)[0]
                groups = {
                    s["id"]: [deepcopy(c) for c in list(parent)[s["start"] : s["end"]]]
                    for s in spans
                }
                start, end = spans[0]["start"], spans[-1]["end"]
                for child in list(parent)[start:end]:
                    parent.remove(child)
                for child in [c for pid in edit.projects_order for c in groups[pid]]:
                    parent.insert(start, child)
                    start += 1
            for reference in removed:
                paragraph = paragraph_refs[reference]
                if paragraph.getparent() is not None:
                    paragraph.getparent().remove(paragraph)
            with ZipFile(destination, "w") as target:
                for item in source.infolist():
                    blob = (
                        etree.tostring(
                            roots[item.filename],
                            xml_declaration=True,
                            encoding="UTF-8",
                            standalone=True,
                        )
                        if item.filename in roots
                        else source.read(item.filename)
                    )
                    target.writestr(item, blob)
    finally:
        intermediate.unlink(missing_ok=True)


def _structure(node: Any, include_text: bool) -> Any:
    attrs = sorted(
        (k, v)
        for k, v in node.attrib.items()
        if k != XML_SPACE and not k.startswith(W + "rsid")
    )
    return (
        node.tag,
        attrs,
        node.text if include_text else None,
        [_structure(child, include_text) for child in node],
    )


def compare(original: Path, rendered: Path, include_text: bool = True) -> list[str]:
    differences: list[str] = []
    with ZipFile(original) as first, ZipFile(rendered) as second:
        if set(first.namelist()) != set(second.namelist()):
            differences.append("- DOCX package members differ")
        for part in first.namelist():
            if part not in second.namelist():
                continue
            left, right = first.read(part), second.read(part)
            if part.endswith(".xml"):
                same = _structure(xml_root(left), include_text) == _structure(
                    xml_root(right), include_text
                )
            else:
                same = left == right
            if not same:
                differences.append(
                    f"- structure/style{'/text' if include_text else ''} differs: "
                    f"{part}"
                )
    return differences
