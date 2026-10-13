"""Deterministic section selection; original database descriptions are never edited."""

import re
from dataclasses import dataclass

# Exact heading aliases, including common ATS wording. Unknown prose is retained
# conservatively and receives the lowest budget priority. No model call is used.
HEADINGS: dict[str, tuple[str, int]] = {}
for category, priority, aliases in (
    ("title", 5, ("title", "job title", "position")),
    (
        "requirements",
        4,
        (
            "requirements",
            "job requirements",
            "technical requirements",
            "minimum requirements",
            "basic requirements",
            "required skills",
            "skills",
            "what we are looking for",
            "what we're looking for",
            "what you bring",
            "what you will bring",
            "what you'll bring",
            "who you are",
            "what we look for",
        ),
    ),
    (
        "qualifications",
        4,
        (
            "qualifications",
            "minimum qualifications",
            "basic qualifications",
            "required qualifications",
            "preferred qualifications",
            "nice to have",
            "nice to haves",
            "bonus points",
            "your qualifications",
            "experience and qualifications",
        ),
    ),
    (
        "tech_stack",
        4,
        (
            "tech stack",
            "technology stack",
            "technical skills",
            "technologies",
            "our stack",
            "tools and technologies",
        ),
    ),
    (
        "responsibilities",
        3,
        (
            "responsibilities",
            "key responsibilities",
            "job responsibilities",
            "your responsibilities",
            "duties",
            "duties and responsibilities",
            "what you will do",
            "what you'll do",
            "what you’ll do",
            "what you will be doing",
            "what you'll be doing",
            "what you’ll be doing",
            "what you do",
            "your role",
            "in this role",
        ),
    ),
    (
        "overview",
        1,
        (
            "description",
            "job description",
            "role description",
            "role overview",
            "the role",
            "about the role",
            "about this role",
            "the opportunity",
        ),
    ),
    (
        "drop",
        -1,
        (
            "about us",
            "about the company",
            "company overview",
            "who we are",
            "our mission",
            "our values",
            "benefits",
            "perks",
            "perks and benefits",
            "compensation and benefits",
            "what we offer",
            "why join us",
            "equal opportunity",
            "equal opportunity employer",
            "equal employment opportunity",
            "diversity and inclusion",
            "application process",
            "application procedure",
            "how to apply",
            "recruitment process",
            "hiring process",
            "interview process",
        ),
    ),
):
    for alias in aliases:
        HEADINGS[alias] = (category, priority)


@dataclass
class Section:
    text: str
    priority: int


@dataclass
class Description:
    sections: list[Section]
    original_chars: int
    dropped_sections: int = 0
    truncated: bool = False

    @property
    def text(self) -> str:
        return " ".join(section.text for section in self.sections if section.text)

    def trim(self, chars: int) -> bool:
        """Remove tails of the lowest-value sections first, preserving prefixes."""
        available = [i for i, section in enumerate(self.sections) if section.text]
        if not available:
            return False
        index = min(available, key=lambda i: (self.sections[i].priority, -i))
        section = self.sections[index]
        remaining = max(0, len(section.text) - max(1, chars))
        prefix = section.text[:remaining]
        # Preserve complete words where possible, but bound unbroken long input too.
        if (
            remaining < len(section.text)
            and prefix
            and not section.text[remaining].isspace()
        ):
            prefix = prefix.rsplit(" ", 1)[0] if " " in prefix else ""
        section.text = prefix.rstrip()
        self.truncated = True
        return True

    def warning(self) -> str | None:
        if not self.dropped_sections and not self.truncated:
            return None
        return (
            f"Job description compacted: {self.original_chars} -> "
            f"{len(self.text)} characters; "
            f"{self.dropped_sections} boilerplate sections dropped; "
            f"budget truncation={'yes' if self.truncated else 'no'}"
        )


def heading(line: str) -> tuple[str, int] | None:
    label = re.sub(r"^(?:#{1,6}\s*|\d+[.)]\s*)", "", line.strip())
    label = label.strip("* _:-–—").casefold().replace("’", "'")
    label = " ".join(label.split())
    if label in HEADINGS:
        return HEADINGS[label]
    if re.match(r"(?:we are|[\w &.-]+ is) an equal[- ]opportunity\b", label):
        return "drop", -1
    return None


def compact_description(text: str, company: str = "") -> Description:
    result = Description([], len(text))
    current: list[str] = []
    priority = 0

    def flush() -> None:
        if current and priority >= 0:
            result.sections.append(
                Section(" ".join(" ".join(current).split()), priority)
            )
        current.clear()

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Inline "Requirements: Python, SQL" and standalone Markdown/ATS headings.
        prefix, colon, _ = line.partition(":")
        found = heading(prefix if colon else line)
        if (
            company
            and prefix.strip().strip("#* ").casefold() == "about " + company.casefold()
        ):
            found = ("drop", -1)
        if found:
            flush()
            category, priority = found
            if category == "drop":
                result.dropped_sections += 1
            current.append(line)
        elif (line.endswith(":") and len(line) < 100) or line.startswith("# "):
            flush()
            priority = 0
            current.append(line)
        else:
            current.append(line)
    flush()
    return result
