"""Conservative author names from native first-page line geometry."""

import re
import unicodedata

_ORGANIZATION = re.compile(
    r"\b(?:universit\w*|institut\w*|department|school|college|laborator\w*|labs?|"
    r"research|corp(?:oration)?|inc|llc|ltd|company|google|microsoft|deepmind)\b", re.I)

def names_from_byline(text: str) -> list[str]:
    from app.services import metadata_service as policy

    text = unicodedata.normalize("NFKC", text)
    if "@" in text or _ORGANIZATION.search(text) or re.search(r"https?://|corresponding|contribut|copyright", text, re.I):
        return []
    text = re.sub(r"\b(?:(?:graduate student|senior|life|student)\s+)?(?:member|fellow)\s*,?\s*IEEE\b", "", text, flags=re.I)
    text = re.sub(r"[0-9*\u2020\u2021\u00a7\u00b6\u2217\u204e\u2709]+(?:\s+[0-9*\u2020\u2021\u00a7\u00b6\u2217\u204e\u2709]+)*", "|", text)
    candidates = re.split(r"\s*(?:[|,;&]|\band\b)\s*", text, flags=re.I)
    names = []
    for candidate in candidates:
        candidate = candidate.strip(" .;:-")
        words = candidate.split()
        if not 2 <= len(words) <= 6:
            continue
        if any(word.casefold().strip(".") in policy._NAME_STOPWORDS for word in words):
            continue
        valid = True
        for word in words:
            letters = [character for character in word if character.isalpha()]
            if not letters or not (letters[0].isupper() or word.casefold() in policy._NAME_PARTICLES):
                valid = False
                break
            if any(not (character.isalpha() or character in "-.'\u2019") for character in word):
                valid = False
                break
        if valid:
            names.append(candidate)
    return names


def extract_byline(content):
    from app.services import metadata_service as policy

    horizontal = [line for line in content["lines"] if abs(line["dir"][1]) < .2]
    titles = [line for line in horizontal if line["bbox"][1] < content["height"] * .45
              and policy._looks_like_title(line["text"]) and len(line["text"]) >= 12]
    if not titles:
        return []
    size = max(line["size"] for line in titles)
    bottom = max(line["bbox"][3] for line in titles if line["size"] >= size * .92)
    endings = [line["bbox"][1] for line in horizontal if line["bbox"][1] > bottom
               and re.match(r"^(?:(?:[IVX]+|\d+)[.)]?\s+)?(?:abstract|introduction|keywords|index terms)\b", line["text"], re.I)]
    stop = min(endings + [content["height"] * .65])
    lines = sorted((line for line in horizontal if bottom < line["bbox"][1] < stop),
                   key=lambda line: (round(line["bbox"][1] / 3), line["bbox"][0]))
    names = []
    seen = set()
    for line in lines:
        for name in names_from_byline(line["text"]):
            if name.casefold() not in seen:
                names.append(name)
                seen.add(name.casefold())
    return names