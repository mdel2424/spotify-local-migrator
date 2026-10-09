import re
import unicodedata
from collections import Counter

from ..models import LocalTrack
from .models import PreparedTrack

APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", chr(96): "'", "＇": "'"})
EXTENSION = re.compile(r"\.(mp3|flac|wav|m4a|aac|ogg|opus|aiff|wma)$", re.I)
COLLABORATION = re.compile(r"\s+(?:x|&|and|feat\.?|ft\.?|featuring)\s+|[,，]\s*", re.I)
PRODUCTION = re.compile(r"\b(?:prod(?:uced)?(?:\s+by)?(?:[.:]\s*|\s+)|p\.\s*)", re.I)
PROMOTION = re.compile(
    r"\*?(?:music\s*)?vid(?:eo)?\s*(?:link\s*)?in\s*(?:the\s*)?(?:desc|disc)\w*\*?", re.I
)
BRACKETS = re.compile(r"\([^()]*\)|\[[^\[\]]*\]")
VERSION = re.compile(
    r"^(?:\d{4}\s+)?(live|remix|instrumental|acoustic|demo|remaster(?:ed)?|"
    r"radio\s+edit|sped[\s-]*up|slowed(?:\s+(?:down|and\s+reverb))?|"
    r"extended(?:\s+(?:mix|version))?|(?:.*\s+)?version)\b",
    re.I,
)
FEATURE = re.compile(r"\b(?:feat\.?|ft\.?|featuring)\s+(.+)$", re.I)


def unicode_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).translate(APOSTROPHES).strip()


def normalize(value: str | None) -> str:
    value = unicode_text(value or "").casefold().replace("&", " and ")
    value = value.replace("'", "")
    value = "".join(character if character.isalnum() else " " for character in value)
    return " ".join(value.split())


def split_artists(value: str, known: list[str] | None = None) -> list[str]:
    value = unicode_text(value)
    if any(normalize(value) == normalize(name) for name in known or []):
        return [value]
    return [part.strip(" @") for part in COLLABORATION.split(value) if part.strip(" @")]


def unique_names(values: list[str]) -> list[str]:
    result: dict[str, str] = {}
    for value in values:
        if normalize(value):
            result.setdefault(normalize(value), value)
    return list(result.values())


def infer_artists(tracks: list[LocalTrack]) -> list[str]:
    """Repeated tags are evidence; an uploader tag on one song is not."""
    votes: Counter[str] = Counter()
    names: dict[str, str] = {}
    for track in tracks:
        seen = set()
        for tag in track.artists:
            for artist in split_artists(tag):
                key = normalize(artist)
                if key and key not in seen:
                    seen.add(key)
                    votes[key] += 1
                    names.setdefault(key, artist)
    if not votes:
        return []
    key, count = votes.most_common(1)[0]
    if count >= 5 and count / max(1, len(tracks)) >= 0.35:
        return [names[key]]
    return []


def infer_producers(tracks: list[LocalTrack], configured: list[str]) -> list[str]:
    producers = list(configured)
    for track in tracks:
        title = unicode_text(track.title or "")
        for bracket in BRACKETS.findall(title):
            content = bracket[1:-1]
            marker = PRODUCTION.search(content)
            if marker:
                content = content[marker.end() :]
                producers.extend(re.split(r"\s*[,+&/]\s*|\s+x\s+", content))
        marker = PRODUCTION.search(BRACKETS.sub("", title))
        if marker:
            content = BRACKETS.sub("", title)[marker.end() :]
            producers.extend(re.split(r"\s*[,+&/]\s*|\s+x\s+", content))
    return unique_names([name.strip(" @*.") for name in producers if name.strip(" @*.")])


def version_label(content: str) -> str | None:
    content = unicode_text(content).casefold()
    match = VERSION.match(content)
    if not match:
        if re.search(r"\bremix$", content):
            return normalize(content)
        return None
    label = match.group(1)
    if label.startswith("remaster"):
        years = re.findall(r"\b(?:19|20)\d{2}\b", content)
        return "remaster" + (":" + years[0] if years else "")
    if label.startswith("live"):
        suffix = normalize(content[len("live") :])
        return "live" + (":" + suffix if suffix else "")
    if label.endswith("version") or label == "remix":
        return normalize(content)
    if label.startswith("slowed"):
        return "slowed"
    return normalize(label)


def extract_versions(title: str) -> tuple[str, list[str]]:
    """Only formatted annotations qualify; 'Long Live Twxn' is a song title."""
    versions = []

    def bracket(match: re.Match[str]) -> str:
        label = version_label(match.group()[1:-1])
        if label:
            versions.append(label)
            return " "
        if FEATURE.match(match.group()[1:-1].strip()):
            return " "
        return match.group()

    core = BRACKETS.sub(bracket, unicode_text(title))
    suffix = re.search(r"\s+[-–—]\s+(.+)$", core)
    if suffix:
        label = version_label(suffix.group(1))
        if label:
            versions.append(label)
            core = core[: suffix.start()]
    return normalize(core), sorted(set(versions))


def artist_title_prefixes(
    title: str, known: list[str] | None = None
) -> list[tuple[list[str], str, str]]:
    """Possible Artist - Title interpretations, not yet trusted artist evidence."""
    separators = list(re.finditer(r"\s+[-–—]\s+", title))
    result = []
    start = 0
    for separator in separators:
        block = title[start : separator.start()].strip()
        song = title[separator.end() :].strip()
        start = separator.end()
        if not block or not song or version_label(song):
            continue
        artists = unique_names(split_artists(block, known))
        if artists and all(len(artist) <= 80 for artist in artists):
            result.append((artists, song, title[: separator.end()].strip()))
    return result


def corroborate_title_artist(
    prepared: PreparedTrack, catalogue_artists: list[str]
) -> PreparedTrack:
    """Use an embedded artist only when Spotify corroborates that literal credit.

    Keep ordinary hyphenated titles unchanged when no prefix artist agrees.
    Title credits can outweigh unrelated uploader tags, but explicit features
    and version annotations remain required evidence.
    """
    if prepared.artist_source == "title":
        return prepared
    catalogue_keys = {normalize(artist) for artist in catalogue_artists}
    for artists, title, prefix in artist_title_prefixes(prepared.title, catalogue_artists):
        if not catalogue_keys.intersection(normalize(artist) for artist in artists):
            continue
        # A prefix can itself contain features, with additional features after
        # the song title. Retain both sets rather than dropping the second one.
        _, original_versions = extract_versions(title)
        featured = list(prepared.featured_artists)
        feature = FEATURE.search(title)
        if feature:
            credit = feature.group(1)
            version_suffix = re.search(r"\s+[-–—]\s+(.+)$", credit)
            if version_suffix and version_label(version_suffix.group(1)):
                credit = credit[: version_suffix.start()]
            featured.extend(split_artists(credit, catalogue_artists))
            title = title[: feature.start()].strip()
        featured = unique_names(featured)
        all_artists = unique_names(artists + featured)
        artist_keys = {normalize(artist) for artist in all_artists}
        ignored = unique_names(
            prepared.ignored_artist_tags
            + [
                artist
                for artist in prepared.primary_artists
                if prepared.artist_source == "metadata" and normalize(artist) not in artist_keys
            ]
        )
        warnings = [
            warning
            for warning in prepared.warnings
            if warning
            not in (
                "No reliable artist evidence; automatic selection is disabled.",
                "Filename-like title provides weak identity evidence.",
            )
        ]
        if ignored:
            warnings.append("Artist tags differ from title/playlist artist evidence.")
        core, versions = extract_versions(title)
        versions = sorted(set(prepared.qualifiers + original_versions + versions))
        filename_like = bool(re.fullmatch(r"(?:track|audio|unknown|untitled)\s*\d*", core))
        if filename_like:
            warnings.append("Filename-like title provides weak identity evidence.")
        return prepared.model_copy(
            update={
                "title": title,
                "core_title": core,
                "qualifiers": versions,
                "artists": all_artists,
                "primary_artists": artists,
                "featured_artists": featured,
                "artist_source": "title",
                "filename_like": filename_like,
                "ignored_artist_tags": ignored,
                "removed_annotations": prepared.removed_annotations + [prefix],
                "warnings": list(dict.fromkeys(warnings)),
            }
        )
    return prepared


def prepare_track(
    track: LocalTrack,
    expected_artists: list[str],
    producer_names: list[str],
    *,
    expected_source: str = "inferred",
    uploader_names: list[str] | None = None,
) -> PreparedTrack:
    title = unicode_text(track.title or "")
    removed: list[str] = []
    ignored: list[str] = []
    warnings: list[str] = []
    featured: list[str] = []
    primary: list[str] = []
    source = "missing"
    producer_keys = {normalize(name) for name in producer_names}
    uploader_keys = {normalize(name) for name in uploader_names or []}
    known = expected_artists

    # Artist anchors allow "Uploader - Artist x Guest - Song". Arbitrary "A - B"
    # is not split without a known artist; this preserves titles with hyphens.
    for artist in sorted(known, key=len, reverse=True):
        pattern = re.compile(r"(?<!\w)" + re.escape(unicode_text(artist)) + r"(?!\w)", re.I)
        match = pattern.search(title)
        if not match:
            continue
        prefix_end = re.match(
            r"(?:(?:\s+(?:x|&|feat\.?|ft\.?|featuring)\s+)[^-–—()\[\]]+)?\s*[-–—]\s*",
            title[match.end() :],
            re.I,
        )
        if prefix_end:
            end = match.end() + prefix_end.end()
            before_artist = title[: match.start()]
            prefix_start = 0
            last_separator = re.search(r".*[-–—]\s*", before_artist)
            if last_separator:
                prefix_start = last_separator.end()
                removed.append(title[:prefix_start].strip())
            artist_block = title[prefix_start:end].rstrip(" -–—")
            primary = split_artists(artist_block, known)
            title = title[end:]
            source = "title"
            break
        if match.start() == 0 and title[match.end() :].startswith(" "):
            primary = [artist]
            removed.append(title[: match.end()])
            title = title[match.end() :].strip()
            source = "title"
            break

    def clean_bracket(match: re.Match[str]) -> str:
        content = match.group()[1:-1].strip()
        feature = FEATURE.match(content)
        if feature:
            featured.extend(split_artists(feature.group(1), known))
            removed.append(match.group())
            return " "
        if version_label(content):
            return match.group()
        credit_names = re.split(r"\s*[,+&/]\s*|\s+x\s+", content)
        if (
            PRODUCTION.search(content)
            or "exclusive" in content.casefold()
            or re.search(r"\bdj\w*\b|slump audios|shoku radio|^dir\.", content, re.I)
            or bool(re.fullmatch(r"(?:@\w+[\s,+&]*)+", content))
            or (
                credit_names
                and any(normalize(name.strip(" @")) in producer_keys for name in credit_names)
                and all(len(normalize(name)) < 40 for name in credit_names)
            )
        ):
            removed.append(match.group())
            return " "
        return match.group()

    title = BRACKETS.sub(clean_bracket, title)
    feature = FEATURE.search(title)
    if feature and not any(FEATURE.search(prefix) for _, _, prefix in artist_title_prefixes(title)):
        featured.extend(split_artists(feature.group(1), known))
        removed.append(title[feature.start() :])
        title = title[: feature.start()]
    marker = PRODUCTION.search(title)
    if marker:
        removed.append(title[marker.start() :])
        title = title[: marker.start()]
    promotion = PROMOTION.search(title)
    if promotion:
        removed.append(title[promotion.start() :])
        title = title[: promotion.start()]
    for producer in sorted(producer_names, key=len, reverse=True):
        if len(normalize(producer)) < 4:
            continue
        suffix = re.search(r"\s+" + re.escape(unicode_text(producer)) + r"\s*$", title, re.I)
        if suffix:
            removed.append(title[suffix.start() :])
            title = title[: suffix.start()]
            break

    had_extension = bool(EXTENSION.search(title))
    title = EXTENSION.sub("", title)
    if had_extension or "_" in title:
        title = re.sub(r"^\d{1,3}[\s._-]+", "", title)
    title = " ".join(title.replace("_", " ").strip(" -–—").split())
    filename_like = bool(re.fullmatch(r"(?:track|audio|unknown|untitled)\s*\d*", normalize(title)))
    if filename_like:
        warnings.append("Filename-like title provides weak identity evidence.")

    tags = unique_names([name for tag in track.artists for name in split_artists(tag, known)])
    if primary:
        for tag in tags:
            if normalize(tag) not in {normalize(name) for name in primary + featured}:
                ignored.append(tag)
    elif known:
        reliable = [tag for tag in tags if normalize(tag) in {normalize(name) for name in known}]
        primary = reliable or list(known)
        source = "metadata" if reliable else expected_source
        ignored = [tag for tag in tags if tag not in reliable]
    else:
        primary = [
            tag
            for tag in tags
            if normalize(tag) not in uploader_keys | producer_keys
            and not re.search(r"\bdj\b|exclusive|radio|@\w|archive", tag, re.I)
        ]
        ignored = [tag for tag in tags if tag not in primary]
        source = "metadata" if primary else "missing"

    all_artists = unique_names(primary + featured)
    if ignored:
        warnings.append("Artist tags differ from title/playlist artist evidence.")
    if track.duration_ms is None:
        warnings.append("Duration missing; automatic selection is disabled.")
    if not all_artists:
        warnings.append("No reliable artist evidence; automatic selection is disabled.")
    core, versions = extract_versions(title)
    if not core:
        warnings.append("No usable title remains after cleanup.")
    return PreparedTrack(
        title=title,
        core_title=core,
        artists=all_artists,
        primary_artists=unique_names(primary),
        featured_artists=unique_names(featured),
        artist_source=source,
        qualifiers=versions,
        removed_annotations=removed,
        ignored_artist_tags=ignored,
        warnings=warnings,
        filename_like=filename_like,
    )
