"""Recover artist shortcodes converted to emoji, without rewriting other tags."""
import re

PAINTER = re.compile(r"[\U0001f9d1\U0001f468\U0001f469]\ufe0f?[\U0001f3fb-\U0001f3ff]?\ufe0f?\u200d\ufe0f?\U0001f3a8\ufe0f?")
WEIGHT_END = re.compile(r"(?<![\w.])(?P<weight>[+-]?(?:\d+(?:\.\d*)?|\.\d+))(?P<space>[ \t]*)(?P<colon>:{1,2})(?P<gap>[ \t]*)$")
ARTIST_END = re.compile(r"\bartist[ \t]*:[ \t]*$", re.I)
ARTIST_START = re.compile(r"[ \t]*artist[ \t]*:[ \t]*", re.I)
BOUNDARY = re.compile(r"(?:^|[,\[{(])[ \t\r\n]*:?[ \t]*$")
# Emoji pictographs, keycaps, explicit emoji presentation, and BMP default emoji.
# Plain mathematical symbols, CJK, ASCII weights and brackets stay untouched.
EMOJI = re.compile(
    r"[0-9#*]\ufe0f?\u20e3|"
    r"(?:[\U0001f000-\U0001faff\u231a\u231b\u23e9-\u23ec\u23f0\u23f3"
    r"\u25fd\u25fe\u2614\u2615\u2648-\u2653\u267f\u2693\u26a1\u26aa\u26ab"
    r"\u26bd\u26be\u26c4\u26c5\u26ce\u26d4\u26ea\u26f2\u26f3\u26f5\u26fa"
    r"\u26fd\u2705\u270a\u270b\u2728\u274c\u274e\u2753-\u2755\u2757"
    r"\u2795-\u2797\u27b0\u27bf\u2b1b\u2b1c\u2b50\u2b55]|[^\r\n]\ufe0f)"
    r"[\ufe0f\U0001f3fb-\U0001f3ff]*(?:\u200d[^\r\n][\ufe0f\U0001f3fb-\U0001f3ff]*)*"
)


def repair_recipe(text):
    if not isinstance(text, str) or len(text) > 100000:
        raise ValueError("画师串需为不超过 100000 字符的文本")
    changes = []
    for match in reversed(list(PAINTER.finditer(text))):
        left, right = text[:match.start()], text[match.end():]
        prior_artist = ARTIST_END.search(left)
        next_artist = ARTIST_START.match(right)
        name = right[next_artist.end():] if next_artist else right
        if not re.match(r"[ \t]*\w", name):
            continue
        anchor = prior_artist.start() if prior_artist else len(left)
        weight = WEIGHT_END.search(left[:anchor])
        boundary = BOUNDARY.search(left)
        if not (weight or prior_artist or boundary):
            continue
        start = match.start()
        head = ""
        if weight:
            start = weight.start()
            head = left[start:weight.start('colon')] + '::' + left[weight.end('colon'):]
        elif prior_artist:
            start = prior_artist.start()
            head = left[start:]
        elif boundary and ':' in boundary[0]:
            start = left.rfind(':')
            head = left[start+1:]
        replacement = head + ('' if prior_artist or next_artist else 'artist:')
        end = match.end()
        if prior_artist and next_artist:
            end += next_artist.end()
            right = text[end:]
        before = text[start:end]
        changes.append({'before': before, 'after': replacement})
        text = text[:start] + replacement + right
    changes.reverse()
    warnings = []
    for match in EMOJI.finditer(text):
        line = text.count('\n', 0, match.start()) + 1
        column = match.start() - text.rfind('\n', 0, match.start())
        warnings.append({'line': line, 'column': column, 'emoji': match[0],
                         'message': f'第 {line} 行第 {column} 列有待处理的 emoji，请修改该片段。'})
    return {'text': text, 'count': len(changes), 'changes': changes, 'warnings': warnings}


def require_clean(text, label='画师串'):
    result = repair_recipe(text)
    if result['warnings']:
        raise ValueError(label + '：' + result['warnings'][0]['message'] + ' 已暂停提交 NAI。')
    return result
