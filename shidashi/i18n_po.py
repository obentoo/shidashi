"""Read and write gettext ``.pot``/``.po`` files and compile ``.mo`` ones, stdlib only.

Why not GNU gettext's tools: ``xgettext`` cannot see Typer's help, which is read from
the built Click tree (:mod:`shidashi.i18n_catalog`), and the tests must compile a
catalog on a host without ``msgfmt``. The subset handled is what Shidashi's files
use: plural entries and ``#, fuzzy``, no ``msgctxt``. The files are written the way
``msgmerge --no-wrap`` writes them, without line numbers or dates, so they change
only when the messages do.
"""

import dataclasses
import re
import struct
from collections.abc import Iterable
from pathlib import Path

# A named or positional str.format field: "{name}", "{n}", "{}", "{size:>8}".
FIELD_RE = re.compile(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*|)(?:![rsa])?(?::[^{}]*)?\}(?!\})")


@dataclasses.dataclass
class Entry:
    """One message: its msgid, the files that use it and, in a ``.po``, its translation."""

    msgid: str
    plural: str = ""  # msgid_plural; set for i18n.tn
    refs: list[str] = dataclasses.field(default_factory=list)
    msgstr: str = ""  # a singular message's translation
    forms: list[str] = dataclasses.field(default_factory=list)  # a plural one's
    fuzzy: bool = False
    brace_format: bool = False  # a str.format string: "#, python-brace-format"

    def translations(self) -> list[str]:
        """Every translated form, empty ones included."""
        return list(self.forms) if self.plural else [self.msgstr]

    def complete(self) -> bool:
        """Whether every form is translated and the entry is not fuzzy."""
        forms = self.translations()
        return not self.fuzzy and bool(forms) and all(forms)


def _quote(text: str) -> str:
    out = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    return f'"{out}"'


_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}


def _unquote(quoted: str, number: int) -> str:
    """Decode one PO string literal: ``"..."`` with ``\\n``, ``\\t``, ``\\"`` and ``\\\\``."""
    if len(quoted) < 2 or quoted[0] != '"' or quoted[-1] != '"':
        raise ValueError(f"line {number}: malformed string {quoted!r}")
    out: list[str] = []
    chars = iter(quoted[1:-1])
    for char in chars:
        if char == '"':
            raise ValueError(f"line {number}: unescaped quote in {quoted!r}")
        if char != "\\":
            out.append(char)
            continue
        escaped = next(chars, "")
        if escaped not in _ESCAPES:
            raise ValueError(f"line {number}: unsupported escape \\{escaped} in {quoted!r}")
        out.append(_ESCAPES[escaped])
    return "".join(out)


def _field(keyword: str, text: str) -> list[str]:
    """``keyword "..."`` on one line, or one line per ``\\n``-ended piece when multi-line."""
    if "\n" not in text.removesuffix("\n"):
        return [f"{keyword} {_quote(text)}"]
    return [f'{keyword} ""', *(_quote(piece) for piece in text.splitlines(keepends=True))]


def render(header: str, comment: str, entries: Iterable[Entry], nplurals: int) -> str:
    """Return a ``.po``/``.pot`` file: ``comment`` lines, the header entry, then ``entries``."""
    out = [f"# {line}".rstrip() for line in comment.splitlines()]
    out += ['msgid ""', 'msgstr ""']
    out += [_quote(line) for line in header.splitlines(keepends=True)]
    for entry in entries:
        out.append("")
        out.append(f"#: {' '.join(entry.refs)}")
        flags = ["fuzzy"] * entry.fuzzy + ["python-brace-format"] * entry.brace_format
        if flags:
            out.append(f"#, {', '.join(flags)}")
        out += _field("msgid", entry.msgid)
        if entry.plural:
            out += _field("msgid_plural", entry.plural)
            for index, form in enumerate(entry.forms or [""] * nplurals):
                out += _field(f"msgstr[{index}]", form)
        else:
            out += _field("msgstr", entry.msgstr)
    return "\n".join(out) + "\n"


_KEYWORD_RE = re.compile(r'^(msgid|msgid_plural|msgstr|msgstr\[(\d+)\])\s+(".*")$')


class _Reader:
    """The state of :func:`parse` between lines."""

    def __init__(self) -> None:
        self.header = ""
        self.entries: list[Entry] = []
        self.current: Entry | None = None
        self.target = ""
        self.refs: list[str] = []
        self.flags: set[str] = set()

    def flush(self) -> None:
        if self.current is not None:
            if self.current.msgid:
                self.entries.append(self.current)
            else:
                self.header = self.current.msgstr
        self.current = None

    def append(self, value: str) -> None:
        entry = self.current
        assert entry is not None
        if self.target == "msgid":
            entry.msgid += value
        elif self.target == "msgid_plural":
            entry.plural += value
        elif self.target == "msgstr":
            entry.msgstr += value
        else:
            entry.forms[int(self.target)] += value

    def comment(self, line: str) -> None:
        self.flush()
        if line.startswith("#:"):
            self.refs += line[2:].split()
        elif line.startswith("#,"):
            self.flags |= {flag.strip() for flag in line[2:].split(",")}

    def keyword(self, line: str, number: int) -> None:
        match = _KEYWORD_RE.match(line)
        if match is None:
            raise ValueError(f"line {number}: unsupported {line!r}")
        keyword, index, quoted = match.groups()
        if keyword == "msgid":
            self.flush()
            self.current = Entry(
                msgid="",
                refs=self.refs,
                fuzzy="fuzzy" in self.flags,
                brace_format="python-brace-format" in self.flags,
            )
            self.refs, self.flags, self.target = [], set(), "msgid"
        elif self.current is None:
            raise ValueError(f"line {number}: {keyword} without msgid")
        elif keyword == "msgid_plural":
            self.target = "msgid_plural"
        elif index is not None:
            if int(index) != len(self.current.forms) or not self.current.plural:
                raise ValueError(f"line {number}: msgstr[{index}] out of order")
            self.current.forms.append("")
            self.target = index
        else:
            self.target = "msgstr"
        self.append(_unquote(quoted, number))


def parse(text: str) -> tuple[str, list[Entry]]:
    """Return the header (the empty msgid's msgstr) and the entries of a ``.po``/``.pot``.

    Obsolete (``#~``) entries are dropped; an unsupported line raises :class:`ValueError`.
    """
    reader = _Reader()
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#~"):
            continue
        if line.startswith("#"):
            reader.comment(line)
        elif line.startswith('"'):
            if reader.current is None:
                raise ValueError(f"line {number}: a string outside an entry")
            reader.append(_unquote(line, number))
        else:
            reader.keyword(line, number)
    reader.flush()
    return reader.header, reader.entries


def header_value(header: str, name: str) -> str:
    """Return the value of a header line (``Plural-Forms``, ``Language``), or ``""``."""
    for line in header.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == name:
            return value.strip()
    return ""


def nplurals_of(header: str) -> int:
    """Return the ``nplurals`` a header declares (2, gettext's default, when it declares none)."""
    match = re.search(r"nplurals\s*=\s*(\d+)", header_value(header, "Plural-Forms"))
    return int(match.group(1)) if match else 2


def merge(template: list[Entry], old: list[Entry], nplurals: int) -> list[Entry]:
    """Return ``template``'s entries with ``old``'s translations (``msgmerge``, no fuzzy match).

    A message that left the code is dropped; a new one comes in untranslated; one
    whose msgid_plural changed starts over.
    """
    known = {entry.msgid: entry for entry in old}
    out: list[Entry] = []
    for entry in template:
        merged = dataclasses.replace(entry, refs=list(entry.refs), msgstr="", forms=[])
        before = known.get(entry.msgid)
        if before is not None and before.plural == entry.plural:
            merged.msgstr, merged.forms = before.msgstr, list(before.forms)
            merged.fuzzy = before.fuzzy
        if merged.plural and len(merged.forms) != nplurals:
            merged.forms = (merged.forms + [""] * nplurals)[:nplurals]
        out.append(merged)
    return out


def compile_mo(header: str, entries: Iterable[Entry]) -> bytes:
    """Return a GNU ``.mo`` file (little-endian, no hash table) of the complete entries.

    Fuzzy and untranslated entries are left out, as ``msgfmt`` does: those messages
    stay English.
    """
    pairs: dict[bytes, bytes] = {b"": header.encode()}
    for entry in entries:
        if not entry.complete():
            continue
        if entry.plural:
            key, value = f"{entry.msgid}\0{entry.plural}", "\0".join(entry.forms)
        else:
            key, value = entry.msgid, entry.msgstr
        pairs[key.encode()] = value.encode()
    keys = sorted(pairs)
    count = len(keys)
    originals_at = 28
    translations_at = originals_at + 8 * count
    strings_at = translations_at + 8 * count
    blob = bytearray()
    descriptors: list[int] = []
    for item in [*keys, *(pairs[key] for key in keys)]:
        descriptors += [len(item), strings_at + len(blob)]
        blob += item + b"\0"
    # magic, revision, count, originals table, translations table, hash size, hash offset
    head = struct.pack("<7I", 0x950412DE, 0, count, originals_at, translations_at, 0, 0)
    return head + struct.pack(f"<{len(descriptors)}I", *descriptors) + bytes(blob)


def write_if_changed(path: Path, content: str) -> bool:
    """Write ``content`` through a temporary file and a rename; return whether it changed."""
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return False
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    return True
