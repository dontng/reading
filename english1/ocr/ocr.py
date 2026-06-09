#!/usr/bin/env python3
"""
考研英语一 阅读理解 OCR 脚本
从 PDF 提取 Section II Part A (Text 1-4, 题目 21-40) 并输出 Markdown。

用法:
  python3 ocr.py <pdf路径> <年份> [输出目录]
  python3 ocr.py ../papers/2019年考研英语一真题.pdf 2019
  python3 ocr.py /path/to/2019年考研英语二真题.pdf 2019 /path/to/english2/ocr/

输出文件: <输出目录>/<两位年份>.md，例如 19.md
"""

import subprocess
import sys
import re
import os


# Lines matching any of these are discarded (page headers / exam cover text)
JUNK_RE = re.compile(
    r'[JYy1][ifr]\s*\)'                            # garbled （共N页）: Ji)/Jf)/Yi)/yf)/1f )
    r'|^\s*英\s*语'                                # 英语（一）试题 (incl. spaced "英 语")
    r'|^\s*[•·\-–\.—]*\s*\d+\s*[•·\-–\.—]*\s*$'  # lone page numbers .3. / -3- / •4• / •5 •
    r'|^\s*•[\s.]*$'                               # bullet-only lines: •         . (2025)
    r'|^\s*[（(]?共\s*[\d\s]+页[）)]?\s*$'         # （共 N 页）and variants
    r'|^\s*\d{4}-\d+\s*$'                          # 2024-3 / 25-4 style page tags
    r'|^绝密|^\d{4}\s*年全国|^\s*\d{4}\s*年\s*(?:英|考)|^☆|^（以下信息|^考生编号|^考生姓名'
    r'|^\*\s*\*\s*\*'                              # *** separators
    r'|^\s*[b-df-hj-np-tv-z]\s*$'                 # stray single consonant (OCR artifact)
    r'|�'                                     # Unicode replacement char = undecodable PDF bytes
)

# Question number: handles "21." and "2 1." formats, plus "21。" (U+3002 normalized to '.' earlier)
QNUM_RE = re.compile(r'^(2\s*[1-9]|3\s*[0-9]|4\s*0)\s*[\.\．]\s*(.*)', re.DOTALL)

# Option patterns tried in order:
#   1. [A]  [AJ  [A ]  [ A]   — standard bracket form
#   2. A.   text / a.   text  — no-bracket dot form (2018 English 2, sometimes lowercase)
#   3. ED]  D]               — OCR artifact: missing/mangled opening bracket (2022 Q28)
#   4. [D   [ text           — option text itself starts with [ (2012 English 1 Q27)
_OPT_PATTERNS = [
    re.compile(r'^\s*\[\s*([ABCD])\s*[J\]]\s*(.*)', re.DOTALL),
    re.compile(r'^\s*([ABCDabcd])\.\s{2,}(.*)', re.DOTALL),
    re.compile(r'^\s*[A-Z]?([ABCD])[J\]]\s*(.*)', re.DOTALL),
    re.compile(r'^\s*\[\s*([ABCD])\s+(\[.*)', re.DOTALL),
]


def match_option(stripped):
    """Return (LETTER, text) if line is an option line, else None."""
    for pat in _OPT_PATTERNS:
        m = pat.match(stripped)
        if m:
            return m.group(1).upper(), m.group(2).strip()
    return None

# Text header (normal form): "Text 1", "Textl" (OCR l→1), "Text2"
_TEXT_HDR_RE = re.compile(r'^Text\s*([1-4l])\s*$', re.IGNORECASE)

# OCR substitution map for digit after "Text"
_TEXT_NUM_MAP = {'1': 1, 'l': 1, 'I': 1, '!': 2, '2': 2, '3': 3, '4': 4}


def clean_stem(stem):
    """Strip trailing OCR garbage from a question stem."""
    # Remove content from first block/bracket symbol (■ 「 etc.)
    stem = re.sub(r'\s+[■▪□▫☐◾「」・☆★�].*$', '', stem)
    # Remove trailing bullet page markers: "• 4 •", "•       ."
    stem = re.sub(r'\s*•[\s•·\d]*\.?\s*$', '', stem)
    # Remove trailing Chinese year/exam markers: "2022年 考 研 英 语 二 试 题..."
    stem = re.sub(r'\s*\d{4}\s*年.*$', '', stem)
    # Remove trailing 5+ spaces (with optional trailing .  or ,) — fill-in-blank artifacts
    stem = re.sub(r'\s{5,}[.,]?\s*$', '', stem)
    return stem.rstrip()


# Matches vocabulary-type question stems: The word/phrase "X" (Line N, Para. M)...
# Use \u escapes in double-quoted strings so the file stays ASCII-clean.
_c201c = chr(0x201C)
_c201d = chr(0x201D)
_c2018 = chr(0x2018)
_c2019 = chr(0x2019)
_OPENQ  = "[\u201c\u2018\"']"
_CLOSEQ = "[\u201d\u2019\"']"
_INNERQ = "[^\u201c\u201d\u2018\u2019\"']+"
_VOCAB_RE = re.compile(
    r'[Tt]he (?:word|phrase|expression)s?\s+' + _OPENQ + r'(' + _INNERQ + r')' + _CLOSEQ
    + r'\s*\(.*?Para\.?\s*(\d+)',
    re.IGNORECASE,
)
_VOCAB_RE2 = re.compile(
    r'[Tt]he (?:word|phrase|expression)s?\s+' + _OPENQ + r'(' + _INNERQ + r')' + _CLOSEQ,
    re.IGNORECASE,
)


def _is_clean_italic(t):
    """Return True if t looks like a real italic word/phrase (not garbled OCR)."""
    if len(t) < 3:
        return False
    if any(ord(c) < 32 for c in t):   # control characters
        return False
    if not all(c.isascii() for c in t):   # non-ASCII (Cyrillic, CJK, etc.)
        return False
    if not (t[0].isalpha() or t[0] in '"\'('):
        return False
    alpha = sum(1 for c in t if c.isalpha())
    return alpha >= len(t) * 0.4


def get_italic_spans(pdf_path):
    """Return deduplicated list of clean italic phrases from PDF, longest first."""
    try:
        import fitz
    except ImportError:
        return []

    doc = fitz.open(pdf_path)
    seen = set()
    spans = []

    for page in doc:
        for b in page.get_text("dict")["blocks"]:
            if b["type"] != 0:
                continue
            for line in b["lines"]:
                parts = []
                for span in line["spans"]:
                    if span["flags"] & 2:   # italic flag
                        t = span["text"].strip()
                        if t:
                            parts.append(t)
                    else:
                        if parts:
                            merged = re.sub(r'\s+', ' ', ' '.join(parts)).strip()
                            merged = merged.rstrip('.,;:"\'')
                            if _is_clean_italic(merged) and merged not in seen:
                                seen.add(merged)
                                spans.append(merged)
                            parts = []
                if parts:
                    merged = re.sub(r'\s+', ' ', ' '.join(parts)).strip()
                    merged = merged.rstrip('.,;:"\'')
                    if _is_clean_italic(merged) and merged not in seen:
                        seen.add(merged)
                        spans.append(merged)

    # Sort longest first; drop spans that are pure substrings of a longer span
    spans.sort(key=len, reverse=True)
    final = []
    for s in spans:
        if not any(s in longer for longer in final):
            final.append(s)
    return final


def apply_formatting(texts, pdf_path):
    """Apply *italic* markup and <u>vocab</u> underlines to passage paragraphs."""
    italic_spans = get_italic_spans(pdf_path)

    for text in texts:
        passage = text['passage']

        # ── 1. Italic markup ────────────────────────────────────────────────
        for span in italic_spans:
            escaped = re.escape(span)
            # Use word boundaries for single-word spans to avoid partial matches
            # (e.g. span "purpose" should not match inside "purposes")
            if ' ' not in span and not re.search(r'[^\w]', span):
                pattern = r'\b' + escaped + r'\b'
            else:
                pattern = escaped
            for i, para in enumerate(passage):
                passage[i] = re.sub(pattern, f'*{span}*', passage[i])

        # ── 2. Vocabulary / quoted-phrase underlines ────────────────────────
        # _VOCAB_RE  : "The word/phrase X" with Para reference (vocab questions)
        # _QUOTE_RE  : any quoted text + (Para N) reference (covers "By saying X" etc.)
        # _VOCAB_RE2 : "The word/phrase X" without Para reference (fallback)
        _QUOTE_RE = re.compile(
            _OPENQ + r'([^'
            + _c201c + _c201d + _c2018 + _c2019
            + r'"\']{3,90})' + _CLOSEQ
            + r'\s*[^(]{0,20}\(.*?Para\.?\s*(\d+)',
            re.IGNORECASE,
        )
        for q in text['questions']:
            stem = q['stem']
            # Skip if ellipsis — truncated phrase can't match passage text
            if '...' in stem or '\u2026' in stem:
                m_skip = _VOCAB_RE.search(stem)
                if not m_skip:
                    continue

            m = _VOCAB_RE.search(stem) or _QUOTE_RE.search(stem)
            if m:
                word, para_num = m.group(1), int(m.group(2))
                idx = para_num - 1   # 1-indexed → 0-indexed
            else:
                m2 = _VOCAB_RE2.search(stem)
                if not m2:
                    continue
                word, idx = m2.group(1), None

            word = word.strip()
            if len(word) < 2 or '...' in word or '\u2026' in word:
                continue

            repl = lambda mo: f'<u>{mo.group(0)}</u>'
            pattern = re.compile(re.escape(word), re.IGNORECASE)

            if idx is not None and 0 <= idx < len(passage):
                new_para = pattern.sub(repl, passage[idx], count=1)
                if new_para != passage[idx]:
                    passage[idx] = new_para
                    continue
            # Fallback: first occurrence in any paragraph
            for i, para in enumerate(passage):
                new_para = pattern.sub(repl, para, count=1)
                if new_para != para:
                    passage[i] = new_para
                    break

    return texts


def parse_text_header(stripped):
    """Return text number 1-4 if stripped is a Text header line, else None."""
    m = _TEXT_HDR_RE.match(stripped)
    if m:
        n = m.group(1)
        return 1 if n == 'l' else int(n)
    # Handle garbled/spaced variants e.g. "T e x t!" → "Text!" → 2
    compact = re.sub(r'\s+', '', stripped)
    if re.match(r'^[Tt]ext.{1,2}$', compact):
        c = compact[4]  # character immediately after "Text"
        return _TEXT_NUM_MAP.get(c)


def extract_text(pdf_path):
    result = subprocess.run(
        ['pdftotext', '-layout', pdf_path, '-'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        sys.exit(f"pdftotext failed: {result.stderr}")
    return result.stdout


def normalize_fullwidth(text):
    """Convert full-width Unicode to ASCII (needed for 2024/2025 PDFs)."""
    result = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            result.append(chr(code - 0xFEE0))
        elif code == 0x3000:
            result.append(' ')
        elif code == 0x3002:   # ideographic full stop 。→ . (2025 question numbers)
            result.append('.')
        else:
            result.append(ch)
    return ''.join(result)


def clean_lines(raw):
    lines = []
    for line in raw.split('\n'):
        line = line.replace('\x0c', '')   # form feed → nothing (acts as blank line below)
        if not JUNK_RE.search(line):
            lines.append(line.rstrip())
    return lines


def find_section_bounds(lines):
    """Return (start_idx, end_idx) bounding Section II content."""
    start = end = None
    for i, line in enumerate(lines):
        s = line.strip()
        if re.search(r'Section\s+II', s, re.IGNORECASE) and start is None:
            start = i
        elif start is not None and re.search(r'Section\s+III', s, re.IGNORECASE):
            end = i
            break
    return start, end


def parse_section(lines):
    """
    Return list of:
      {'num': int, 'passage': [para_str, ...], 'questions': [q_dict, ...]}
    where q_dict = {'num': str, 'stem': str, 'options': [(letter, text), ...]}
    """
    texts = []
    cur_text = None
    cur_para_buf = []     # accumulated words for current paragraph
    cur_passage = []      # finished paragraphs
    cur_q = None
    cur_questions = []
    mode = 'search'       # search | passage | questions

    def flush_para():
        nonlocal cur_para_buf
        text = ' '.join(cur_para_buf).strip()
        if text:
            cur_passage.append(text)
        cur_para_buf = []

    def flush_text():
        nonlocal cur_text, cur_para_buf, cur_passage, cur_q, cur_questions
        if cur_text is not None:
            flush_para()
            if cur_q:
                cur_questions.append(cur_q)
                cur_q = None
            texts.append({
                'num': cur_text,
                'passage': list(cur_passage),
                'questions': list(cur_questions),
            })
        cur_para_buf = []
        cur_passage = []
        cur_questions = []
        cur_q = None

    for line in lines:
        raw = line.rstrip()          # preserve leading spaces for indent detection
        stripped = raw.strip()
        leading = len(raw) - len(raw.lstrip(' '))

        # ── Stop at Part B / C (end of reading comprehension) ──────────────
        # Collapse spaces first to handle "P a r tB" (2010 spaced OCR) as well as
        # normal "Part B", "PartB", "Part .B" variants.
        compact = re.sub(r'\s+', '', stripped)
        if re.match(r'^[Pp]art\.?[B-Z]', compact):
            break

        # ── Stop at Part B Directions when there is no "Part B" label (2025) ─
        if mode == 'questions' and re.match(r'^\s*Directions\s*:', stripped, re.IGNORECASE):
            break

        # ── Text N header ────────────────────────────────────────────────────
        text_num = parse_text_header(stripped)
        if text_num:
            flush_text()
            cur_text = text_num
            mode = 'passage'
            continue

        if cur_text is None:
            continue

        # ── Option line ──────────────────────────────────────────────────────
        opt = match_option(stripped)
        if opt and cur_q is not None:
            cur_q['options'].append(opt)
            continue

        # ── Question number ──────────────────────────────────────────────────
        # Normalize "2 1." → "21." before matching
        norm = re.sub(r'^(\d)\s+(\d)', r'\1\2', stripped)
        m = QNUM_RE.match(norm)
        if m:
            qnum = m.group(1).replace(' ', '')
            qval = int(qnum)
            if 21 <= qval <= 40:
                flush_para()
                mode = 'questions'
                if cur_q:
                    cur_questions.append(cur_q)
                cur_q = {'num': qnum, 'stem': clean_stem(m.group(2).strip()), 'options': []}
                continue

        # ── Passage mode ─────────────────────────────────────────────────────
        if mode == 'passage':
            if stripped == '':
                flush_para()
            elif leading >= 3:
                # Indented line = start of a new paragraph
                flush_para()
                cur_para_buf.append(stripped)
            else:
                # Continuation line (0-2 leading spaces)
                if stripped:
                    cur_para_buf.append(stripped)
            continue

        # ── Questions mode (stem continuation) ──────────────────────────────
        if mode == 'questions':
            # Skip stray bracket lines that aren't valid options (e.g. [G] OCR artifact)
            if re.match(r'^\s*\[', stripped):
                continue
            # Only append to stem before the first option is seen; post-option lines
            # are page markers or junk (JUNK_RE may not catch all of them).
            if cur_q and stripped and len(cur_q['options']) == 0:
                cur_q['stem'] += ' ' + stripped
            continue

    flush_text()
    return texts


def format_markdown(year, texts, series='英语一'):
    out = [f"# {year} 考研{series} 阅读理解\n"]

    for i, text in enumerate(texts):
        out.append(f"## Text {text['num']}\n")

        for para in text['passage']:
            out.append(para)
            out.append('')

        for q in text['questions']:
            qnum, stem, options = q['num'], q['stem'], q['options']
            out.append(f"{qnum}\\. {stem}  ")
            for j, (letter, opt_text) in enumerate(options):
                trailer = '  ' if j < len(options) - 1 else ''
                out.append(f"&emsp;[{letter}] {opt_text}{trailer}")
            out.append('')

        if i < len(texts) - 1:
            out.append("---\n")

    return '\n'.join(out)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    pdf_path = sys.argv[1]
    year = sys.argv[2]
    out_dir = sys.argv[3] if len(sys.argv) >= 4 else os.path.dirname(os.path.abspath(__file__))

    if not os.path.exists(pdf_path):
        sys.exit(f"File not found: {pdf_path}")

    raw = extract_text(pdf_path)
    raw = normalize_fullwidth(raw)
    lines = clean_lines(raw)

    start, end = find_section_bounds(lines)
    if start is None:
        print("Warning: Section II not found, processing entire document", file=sys.stderr)
        section = lines
    else:
        section = lines[start:end]
        if end is None:
            print("Warning: Section III not found, taking rest of document", file=sys.stderr)

    texts = parse_section(section)

    if not texts:
        sys.exit("No texts parsed — check PDF structure or adjust patterns")

    apply_formatting(texts, pdf_path)

    print(f"Parsed {len(texts)} texts:")
    for t in texts:
        nq = len(t['questions'])
        np = len(t['passage'])
        warn = f"  *** WARNING: expected 5 questions" if nq != 5 else ''
        print(f"  Text {t['num']}: {np} paragraphs, {nq} questions{warn}")
        for q in t['questions']:
            nopt = len(q['options'])
            if nopt != 4:
                print(f"    Q{q['num']}: {nopt} options (expected 4)")

    series = '英语二' if '英语二' in pdf_path else '英语一'
    md = format_markdown(year, texts, series)

    short = year[-2:]
    out_path = os.path.join(out_dir, f"{short}.md")

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(md)

    print(f"\nWritten: {out_path}")


if __name__ == '__main__':
    main()
