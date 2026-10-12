"""Robots wildcard/end-anchor rules with longest-match and Allow tie-breaking."""

import math
import re
from urllib.parse import quote, urlsplit
from urllib.robotparser import RobotFileParser


def normalized_path(path: str) -> str:
    # Decode only unreserved ASCII; encoded slashes keep their meaning.
    def unreserved(match: re.Match[str]) -> str:
        value = chr(int(match[0][1:], 16))
        return (
            value
            if value.isascii() and (value.isalnum() or value in "-._~")
            else match[0].upper()
        )

    return quote(re.sub(r"%[0-9a-fA-F]{2}", unreserved, path), safe="/%?&=:+@,;!$*~.-_")


class RobotsPolicy(RobotFileParser):
    def __init__(self, url: str = "") -> None:
        super().__init__(url)
        self.groups: list[tuple[list[str], list[tuple[str, bool]]]] = []
        self.intervals: list[float] = []

    def parse(self, lines: list[str]) -> None:
        super().parse(lines)
        agents: list[str] = []
        rules: list[tuple[str, bool]] = []
        intervals: list[float] = []
        directives = False

        def finish() -> None:
            nonlocal agents, rules, directives, intervals
            if agents:
                self.groups.append((agents, rules))
                self.intervals.append(max([1, *intervals]))
            agents, rules, directives = [], [], False
            intervals = []

        self.groups = []
        self.intervals = []
        for raw in lines:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            key, separator, value = line.partition(":")
            if not separator:
                continue
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                if directives:
                    finish()
                agents.append(value.lower())
            elif agents:
                directives = True
                if key in {"allow", "disallow"} and value:
                    rules.append((value, key == "allow"))
                if key in {"crawl-delay", "request-rate"}:
                    try:
                        if key == "crawl-delay":
                            interval = float(value)
                        else:
                            requests, seconds = value.split("/", 1)
                            interval = float(seconds) / float(requests)
                        if math.isfinite(interval) and interval > 0:
                            intervals.append(interval)
                    except (ValueError, ZeroDivisionError):
                        continue
        finish()

    def can_fetch(self, useragent: str, url: str) -> bool:
        if self.disallow_all or not self.last_checked:
            return False
        if self.allow_all:
            return True
        product = useragent.split("/", 1)[0].lower()
        named = [
            (
                max((len(a) for a in agents if a != "*" and a in product), default=0),
                rules,
            )
            for agents, rules in self.groups
        ]
        specificity = max((score for score, _ in named), default=0)
        selected = (
            [rules for score, rules in named if score == specificity]
            if specificity
            else [rules for agents, rules in self.groups if "*" in agents]
        )
        parts = urlsplit(url)
        target = normalized_path(
            (parts.path or "/") + ("?" + parts.query if parts.query else "")
        )
        matches: list[tuple[int, bool]] = []
        for rules in selected:
            for path, allow in rules:
                anchored = path.endswith("$")
                path = normalized_path(path[:-1] if anchored else path)
                pattern = "^" + ".*".join(re.escape(part) for part in path.split("*"))
                if re.search(pattern + ("$" if anchored else ""), target):
                    matches.append((len(path.replace("*", "")), allow))
        return max(matches)[1] if matches else True

    def polite_interval(self, useragent: str) -> float:
        product = useragent.split("/", 1)[0].lower()
        scores = [
            max((len(a) for a in agents if a != "*" and a in product), default=0)
            for agents, _ in self.groups
        ]
        specificity = max(scores, default=0)
        selected = [
            i
            for i, (agents, _) in enumerate(self.groups)
            if (scores[i] == specificity if specificity else "*" in agents)
        ]
        return max([1, *(self.intervals[i] for i in selected)])
