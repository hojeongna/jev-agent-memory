"""Query extraction and a tiny dependency-free tokenizer (English + Korean).

Korean handling: trailing particles (josa) are stripped from each word so that
"회의가" / "회의를" both become "회의". English handling: lower-cased, common stopwords
removed, simple possessive stripping. Anything else passes through as \\w+ words.
"""
from __future__ import annotations

import re

# Only this much of a prompt is searched for <channel> blocks (a hook must stay fast whatever
# it is handed). Messages are far shorter; a longer prompt keeps the blocks in its first part.
MAX_PARSE_CHARS = 256 * 1024
_OPEN, _CLOSE = "<channel", "</channel>"


def channel_blocks(raw: str) -> list[tuple[str, str]]:
    """[(attributes, body)] of every `<channel ...>body</channel>` block, in order, within the
    first MAX_PARSE_CHARS characters. Linear time: plain substring search, each part of the
    text is scanned once (a regex with a lazy `.*?` took quadratic time on input with many
    opening tags and no closing tag). Same matches as `<channel(?:\\s[^>]*)?>(.*?)</channel>`:
    `<channel` must be followed by whitespace or `>`, so `<channel-note>` is not a block; the
    attribute text has its leading whitespace stripped."""
    s = (raw or "")[:MAX_PARSE_CHARS]
    out, pos = [], 0
    while True:
        i = s.find(_OPEN, pos)
        if i == -1:
            break
        nxt = s[i + len(_OPEN):i + len(_OPEN) + 1]
        if nxt and nxt not in " \t\r\n>":
            pos = i + 1  # "<channels>", "<channel-note>": not a channel block
            continue
        j = s.find(">", i + len(_OPEN))
        if j == -1:
            break
        end = s.find(_CLOSE, j + 1)
        if end == -1:
            break  # no closing tag after this one, so none after any later opening tag either
        out.append((s[i + len(_OPEN):j].strip(), s[j + 1:end]))
        pos = end + len(_CLOSE)
    return out

JOSA = sorted([
    "이라서", "이라도", "이라면", "에게서", "으로서", "으로써",
    "에서", "으로", "까지", "부터", "한테", "보다", "이랑", "라서",
    "은", "는", "이", "가", "을", "를", "에", "와", "과", "도", "만", "랑", "의", "나", "야",
], key=len, reverse=True)
JOSA_RE = re.compile("(" + "|".join(JOSA) + ")$")
HANGUL_RE = re.compile(r"[가-힣]")

STOPWORDS_KO = {
    "근데", "지금", "진짜", "그냥", "정말", "완전", "좀", "아까", "방금", "이제",
    "오늘", "어제", "내일", "이거", "그거", "저거", "여기", "거기", "저기",
    "이렇게", "그렇게", "저렇게", "뭔가", "약간", "그래서", "그러니까", "그러면",
    "싶어서", "싶은데", "같아서", "같은데", "말이야", "말야", "해줄래", "해줘",
}

STOPWORDS_EN = {
    "a", "an", "the", "and", "or", "but", "if", "then", "so", "of", "to", "in", "on", "at",
    "for", "with", "about", "from", "by", "as", "is", "are", "was", "were", "be", "been",
    "am", "do", "does", "did", "doing", "have", "has", "had", "it", "its", "this", "that",
    "these", "those", "there", "here", "what", "which", "who", "whom", "how", "why", "when",
    "where", "can", "could", "would", "should", "will", "shall", "may", "might", "must",
    "you", "your", "we", "our", "they", "them", "their", "he", "she", "his", "her", "me",
    "my", "mine", "not", "no", "yes", "just", "now", "today", "yesterday", "tomorrow",
    "really", "very", "some", "any", "all", "also", "too", "again", "still", "like",
    "get", "got", "let", "lets", "please", "thanks", "thank", "ok", "okay", "hey", "hi",
    "into", "out", "up", "down", "over", "than", "more", "most", "much", "many", "one",
    "i", "im", "ive", "id", "dont", "didnt", "doesnt", "cant", "wont", "isnt", "whats",
    "thing", "things", "stuff", "gonna", "wanna", "kind", "sort", "maybe", "right",
}

WORD_RE = re.compile(r"[\w가-힣'\u2019]+", re.UNICODE)


def message_blocks(raw: str) -> list[str]:
    """Bodies of all Claude Code <channel ...> blocks in a prompt, in order (non-empty only).
    One prompt can carry several when messages arrived while the agent was busy."""
    return [b.strip() for _, b in channel_blocks(raw) if b.strip()]


def message_text(raw: str) -> str:
    """Strip Claude Code <channel ...> wrappers. Several blocks are joined with newlines in
    the order they appear; a prompt without a block is returned as is (cut to
    MAX_PARSE_CHARS)."""
    raw = (raw or "").strip()
    blocks = channel_blocks(raw)
    if not blocks:
        return raw[:MAX_PARSE_CHARS]
    return "\n".join(b.strip() for _, b in blocks if b.strip())


def extract_query(raw: str, max_len: int = 100) -> str:
    """Keyword query (first `max_len` chars after cleanup). With several <channel> blocks the
    newest (last) message comes first, so its words are not crowded out by older ones."""
    blocks = message_blocks(raw)
    raw = " ".join(reversed(blocks)) if len(blocks) > 1 else message_text(raw)
    raw = raw[: max(4 * max_len, 4096)]  # only the start becomes the query anyway
    raw = re.sub(r"https?://\S+", " ", raw)
    raw = re.sub(r"[`*_#>\[\]\(\)]+", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw[:max_len]


MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
          "august": 8, "september": 9, "october": 10, "november": 11, "december": 12}
DATE_TOKEN = "date:"  # pseudo token "date:2" = a numeric m/d date in month 2 ("2/8", "02/15")
_VOWELS = "aeiou"


def stem_english(w: str) -> str:
    """Very light English normalisation for rare-word spelling matches (the match is a
    substring test, so a short stem finds both forms): museums->museum, galleries->galler,
    dishes->dish, visited->visit, visiting->visit, stopped->stop. No real stemming: words
    ending in ss/us/is, words under 5 letters and stems under 4 letters are left alone."""
    if len(w) < 5 or not w.isascii() or not w.isalpha():
        return w
    out = w
    if w.endswith("ies"):
        out = w[:-3]  # galleries -> galler (a prefix of gallery and galleries)
    elif w.endswith(("sses", "xes", "zes", "ches", "shes")):
        out = w[:-2]
    elif w.endswith("s") and not w.endswith(("ss", "us", "is")):
        out = w[:-1]
    elif w.endswith("ied"):
        out = w[:-3]
    elif w.endswith("ed") or w.endswith("ing"):
        stem = w[:-2] if w.endswith("ed") else w[:-3]
        if len(stem) >= 4 and any(c in _VOWELS for c in stem):
            if stem[-1] == stem[-2] and stem[-1] not in _VOWELS + "lsz":
                stem = stem[:-1]  # stopped -> stop
            out = stem
    return out if len(out) >= 4 else w


def normalize_word(w: str, english: bool = False) -> str:
    if HANGUL_RE.search(w):
        return JOSA_RE.sub("", w)  # Korean never goes through the English rules
    w = w.lower()
    w = w.replace("\u2019", "'")
    if w.endswith("'s"):
        w = w[:-2]
    w = w.replace("'", "")
    return stem_english(w) if english else w


def clean_tokens(text: str, limit: int = 8, rare_normalize: bool = False) -> list[str]:
    """Strip particles/stopwords, dedupe, keep at most `limit` tokens (order preserved).

    rare_normalize=True (config recall.rare_normalize): English tokens are lightly
    stemmed (see stem_english; stopwords are checked before stemming) and every month name
    also adds a "date:<n>" pseudo token that build_pool matches against numeric dates
    ("february" ~ "2/8"). The pseudo tokens do not count toward `limit`."""
    out, seen, dates = [], set(), []
    for w in WORD_RE.findall(text or ""):
        raw = normalize_word(w.strip("'\u2019"))
        if len(raw) < 2 or raw in STOPWORDS_KO or raw in STOPWORDS_EN or raw.isdigit() and len(raw) < 3:
            continue
        if rare_normalize and raw in MONTHS and f"{DATE_TOKEN}{MONTHS[raw]}" not in dates:
            dates.append(f"{DATE_TOKEN}{MONTHS[raw]}")
        w = stem_english(raw) if rare_normalize and not HANGUL_RE.search(raw) else raw
        if w not in seen:
            seen.add(w)
            out.append(w)
        if len(out) >= limit:
            break
    return out + dates


def date_token_regex(tok: str):
    """Regex for a "date:<month>" pseudo token: m/d or mm/dd in that month, not part of a
    longer number or a fraction-like chain."""
    m = int(tok[len(DATE_TOKEN):])
    return re.compile(rf"(?<![\d/.])0?{m}/(?:[0-2]?\d|3[01])(?![\d/])")
