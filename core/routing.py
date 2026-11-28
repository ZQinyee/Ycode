from __future__ import annotations

import re

from .storage import Store


FOLLOWUP = re.compile(
    r"继续|接着|刚才|之前|上次|后来|接下来|那[个件篇次部项趟段]|怎么样了?|叫(?:啥|什么)|是谁"
    r"|\b(?:continue|resume|follow[- ]up|earlier|previous(?:ly)?|last time|what happened to)\b",
    re.I,
)
WORDS = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]+")
STOP_WORDS = {"of", "in", "to", "is", "it", "the", "was", "and", "did", "on", "that", "this", "please"}


def search_terms(text: str) -> set[str]:
    terms = set()
    for word in WORDS.findall(text.casefold()):
        if word.isascii():
            if len(word) > 1 and word not in STOP_WORDS:
                terms.add(word)
        else:
            terms.update(word[index:index + 2] for index in range(len(word) - 1))
    return terms


def excerpt(text: str, hits: set[str]) -> str:
    folded = text.casefold()
    start = max(0, min(folded.index(word) for word in hits) - 40)
    return text[start:start + 160].strip()


def history_match(task: dict | None, query: set[str]) -> tuple[int, str]:
    best = (0, "")
    if task:
        for message in reversed(task["history"]):
            text = message.get("content")
            if message["role"] not in ("user", "assistant") or not text:
                continue
            folded = text.casefold()
            hits = {word for word in query if word in folded}
            if len(hits) > best[0]:
                best = len(hits), excerpt(text, hits)
                if hits == query:
                    break
    return best


def routing_candidates(store: Store, request: str, cards: list[dict]) -> list[dict]:
    query = search_terms(FOLLOWUP.sub(" ", request))
    if not query:
        return cards[:20]

    matches = []
    for card in cards:
        metadata = "\n".join((card["title"], card["goal"], *card["artifacts"]))
        primary = query & search_terms(metadata)
        secondary = (query - primary) & search_terms(card["summary"])
        evidence = excerpt(card["summary"], secondary) if secondary else ""
        matches.append((card, query - primary - secondary, 2 * len(primary) + len(secondary), evidence))

    scan_history = FOLLOWUP.search(request) and all(missing for _, missing, _, _ in matches)
    ranked = []
    for card, missing, score, evidence in matches:
        if scan_history:
            count, snippet = history_match(store.load_task(card["id"]), missing)
            score += count
            evidence = snippet or evidence
        if score:
            ranked.append((score, {**card, "match": evidence} if evidence else card))
    ranked.sort(key=lambda match: match[0], reverse=True)
    candidates = [card for _, card in ranked[:12]] + cards[:20]
    unique = {}
    for card in candidates:
        unique.setdefault(card["id"], card)
    return list(unique.values())[:20]
