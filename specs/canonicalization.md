# Spec: Canonicalization

- **Prefix:** `CAN`
- **Status:** `draft`
- **Interface:** See below
- **Test locations:** unit: `tests/v1/unit/test_canonicalization.py`; integration: not applicable; e2e: not applicable

## Purpose

Define a deterministic, pure transformation from raw text to named canonical forms used by entity extraction, triage, and provenance. It preserves the original text and makes each derived form reproducible. It does not extract entities, classify content, or assign trust.

Which form a downstream consumer matches against is defined by that consumer’s spec, not here.

## Interface

```python
class ScriptCount(BaseModel):
    script: str = Field(min_length=1)
    count: int = Field(gt=0)

class TextForm(BaseModel):
    text: str
    char_to_raw: list[TextSpan]  # one entry per code point in text

    def to_raw_span(self, start: int, end: int) -> TextSpan: ...

class TextForms(BaseModel):
    raw: TextForm
    nfkc: TextForm
    format_stripped: TextForm
    confusable_skeleton: TextForm
    script_profile: list[ScriptCount]
    mixed_script: bool
    canonicalizer_version: str = Field(min_length=1)
    unicode_version: str = Field(min_length=1)
    script_unicode_version: str = Field(min_length=1)
    confusables_version: str = Field(min_length=1)

def canonicalize(text: str) -> TextForms: ...
```

`TextSpan` is the approved TRU span type. All offsets are Python code-point indices, not bytes or grapheme clusters. Ranges are half-open: `start` is inclusive, `end` is exclusive.

Public errors are `TypeError`, `ValueError`, and `ValidationError` as specified below.

## Requirements

### `CAN-001` Pure deterministic transformation

- **Behavior:** `canonicalize` is a pure function of the input text and the pinned Unicode, confusables, and policy data loaded once at import. It performs no per-call I/O, network, clock, randomness, locale, or mutable process-state reads. The same input and data versions produce identical output.
- **Approval:** `agent-proposed`
- **Verification level:** `unit`
- **Invariants:** Two calls in the same deployment produce equal `TextForms`. Output depends only on the input and the recorded data versions. No side effects occur. A deployment with different pinned data versions may produce different output and records the versions that produced it.
- **Errors and edge cases:** A non-`str` input raises `TypeError`. A `str` containing an unpaired surrogate (`General_Category=Cs`) raises `ValueError`; such a value cannot be persisted as UTF-8 and is rejected before transformation.

| Given / Input | When | Expected result |
|---|---|---|
| `"abc"` | Call twice | Equal `TextForms` |
| `None` | Call | `TypeError` |
| `"\ud800"` | Call | `ValueError` |
| `"😀"` | Call | Accepted; one code point |

### `CAN-002` Raw text preservation

- **Behavior:** For a valid input string, `raw.text` equals the input exactly, including whitespace, case, and astral code points. No trimming, case folding, normalization, or whitespace collapse occurs.
- **Approval:** `agent-proposed`
- **Verification level:** `unit`
- **Invariants:** `raw.text == input`; `len(raw.char_to_raw) == len(raw.text)`; `raw.char_to_raw[i] == TextSpan(start=i, end=i+1)`; `raw.to_raw_span(i, i+1) == TextSpan(i, i+1)`.
- **Errors and edge cases:** Unpaired surrogates are rejected by `CAN-001`. Empty input is valid.

| Given / Input | When | Expected result |
|---|---|---|
| `"  Hello  "` | Canonicalize | `raw.text` unchanged |
| `"Ａ"` | Canonicalize | `raw.text` remains fullwidth `Ａ` |
| `"e\u0301"` | Canonicalize | `raw.text` remains two code points |
| `"😀"` | Canonicalize | `raw.text` has length 1; identity span `(0, 1)` |
| `""` | Canonicalize | `raw.text == ""` and `raw.char_to_raw == []` |
| `"a\nb"` | Canonicalize | `raw.text` unchanged |

### `CAN-003` NFKC form

- **Behavior:** `nfkc.text` is the NFKC normalization of `raw.text` as defined by UCD 15.1.0. When the runtime reports Unicode 15.1.0, `unicodedata.normalize("NFKC", raw.text)` is the reference implementation. It performs compatibility decomposition and canonical composition. It does not case-fold, transliterate, or strip.
- **Approval:** `agent-proposed`
- **Verification level:** `unit`
- **Invariants:** `NFKC(nfkc.text) == nfkc.text`; `len(nfkc.char_to_raw) == len(nfkc.text)`; every span lies within `[0, len(raw.text)]`; every span is non-empty; each output code point maps to the smallest raw range that produced it. Spans need not be monotonic because canonical ordering may reorder combining marks.
- **Errors and edge cases:** None for a valid `str`.

| Given / Input | When | Expected result |
|---|---|---|
| `"Ｙｏｕｒ"` | Canonicalize | `nfkc.text == "Your"` |
| `"ﬁle"` | Canonicalize | `nfkc.text == "file"`; `f` and `i` both map to `(0, 1)`, `l` to `(1, 2)`, `e` to `(2, 3)` |
| `"e\u0301"` | Canonicalize | `nfkc.text == "é"`; `é` maps to `(0, 2)` |
| `"Straße"` | Canonicalize | `nfkc.text == "Straße"`; no case folding |
| `"①"` | Canonicalize | `nfkc.text == "1"` |
| `"㌀"` | Canonicalize | `nfkc.text == "アパート"`; every output character maps to `(0, 1)` |
| `"a\u0315\u0300"` | Canonicalize | `nfkc.text == "à\u0315"`; `à` maps to `(0, 3)`, `\u0315` maps to `(1, 2)` |
| `"가"` | Canonicalize | `nfkc.text == "가"`; `가` maps to `(0, 2)` |

### `CAN-004` Format and invisible character stripping

- **Behavior:** `format_stripped.text` is the fixed point of `x -> remove(NFKC(x))` starting from `nfkc.text`. `remove` deletes a character when either: its `General_Category` is `Cc`, `Cf`, `Co`, or `Cn` and it is not Unicode `White_Space`; or it has the Unicode `Default_Ignorable_Code_Point` property. A character with `White_Space=true` is always preserved, even if another property would otherwise remove it. Whitespace is not collapsed. The fixed point is the result of iterating until a full pass leaves the text unchanged.
- **Approval:** `agent-proposed`
- **Verification level:** `unit`
- **Invariants:** `NFKC(format_stripped.text) == format_stripped.text`. No character in `format_stripped.text` matches the removal predicate. `len(format_stripped.char_to_raw) == len(format_stripped.text)`. Every span is non-empty and maps into the original raw text. No whitespace is collapsed. No case changes occur. The fixed point exists and is unique for the pinned Unicode data.
- **Errors and edge cases:** `White_Space` means the Unicode property, not Python’s `str.isspace()`. `U+001C`–`U+001F` are not `White_Space` and are removed. `U+000B`, `U+000C`, `U+0085`, `U+0009`, `U+000A`, and `U+000D` are `White_Space` and are preserved. `U+00A0` is mapped to `U+0020` by NFKC before stripping. `Cs` cannot occur because `CAN-001` rejects it.

| Given / Input | When | Expected result |
|---|---|---|
| `"pay\u200bnow"` | Canonicalize | `format_stripped.text == "paynow"` |
| `"pay\nnow"` | Canonicalize | Newline preserved |
| `"a\tb"` | Canonicalize | Tab preserved |
| `"a\x00b"` | Canonicalize | `"ab"` |
| `"a\x1cb"` | Canonicalize | `"ab"`; `U+001C` is not `White_Space` |
| `"a\u0085b"` | Canonicalize | `U+0085` preserved |
| `"a\u000bb"` | Canonicalize | `U+000B` preserved |
| `"a\u200db"` | Canonicalize | `"ab"`; ZWJ removed |
| `"a\ufeffb"` | Canonicalize | `"ab"`; BOM removed |
| `"a\u034fb"` | Canonicalize | `"ab"`; combining grapheme joiner is default-ignorable |
| `"a\ufe0fb"` | Canonicalize | `"ab"`; variation selector is default-ignorable |
| `"a\u3164b"` | Canonicalize | `"ab"`; Hangul filler is default-ignorable |
| `"a\uE000b"` | Canonicalize | `"ab"`; private-use character removed |
| `"a\ufdd0b"` | Canonicalize | `"ab"`; noncharacter removed |
| `"e\u200b\u0301"` | Canonicalize | `format_stripped.text == "é"`; stripping allows the second NFKC pass to compose |
| `"\u00a0"` | Canonicalize | `format_stripped.text == " "`; NFKC mapped NBSP to space |
| `"   "` | Canonicalize | Three spaces preserved |

### `CAN-005` Confusable skeleton

- **Behavior:** `confusable_skeleton.text` is the result of the Unicode UTS #39 skeleton algorithm applied to `format_stripped.text`, using the pinned Unicode 15.1.0 `confusables.txt` prototypes: (1) NFD; (2) remove code points with `Default_Ignorable_Code_Point`; (3) replace each code point with its prototype from `confusables.txt`; (4) NFD. The table maps a single source code point to a non-empty prototype string; multi-code-point source sequences are not part of this algorithm. There is no case folding or transliteration. The result may be NFD and is not required to be NFC or ASCII.
- **Approval:** `agent-proposed`
- **Verification level:** `unit`
- **Invariants:** The skeleton is derived only from `format_stripped`. `len(confusable_skeleton.char_to_raw) == len(confusable_skeleton.text)`. Every span lies within `[0, len(raw.text)]` and is non-empty. `confusables_version` identifies the pinned UTS #39 table. The skeleton follows UTS #39, not a custom mapping table.
- **Errors and edge cases:** A prototype target that expands to multiple code points maps every target code point to the source code point’s span. A prototype target may be a combining sequence; the final NFD step preserves it in decomposed form. Default-ignorable characters have already been removed by `CAN-004`; the UTS #39 removal step is therefore a no-op for this input but remains part of the algorithm.

| Given / Input | When | Expected result |
|---|---|---|
| `"аpple"` (Cyrillic `а`) | Canonicalize | Skeleton `"apple"`; first `a` maps to `(0, 1)` |
| `"раy"` (Cyrillic `р`, `а`) | Canonicalize | Skeleton `"pay"` |
| `"pаy"` (Latin `p`, Cyrillic `а`, Latin `y`) | Canonicalize | Skeleton `"pay"` |
| `"О"` (Cyrillic capital O) | Canonicalize | Skeleton `"O"` |
| `"ο"` (Greek small omicron) | Canonicalize | Skeleton `"o"` |
| `"é"` | Canonicalize | Skeleton `"e\u0301"`; both output code points map to `(0, 1)` |
| `"É"` | Canonicalize | Skeleton `"E\u0301"`; no case folding |
| `"你好"` | Canonicalize | Skeleton `"你好"`; unchanged and not ASCII |
| `"a\nb"` | Canonicalize | Skeleton `"a\nb"` |

### `CAN-006` Script profile and mixed-script flag

- **Behavior:** `script_profile` counts **counted letters** in `format_stripped.text`. A counted letter has `General_Category` in `{Lu, Ll, Lt, Lm, Lo}` and a Unicode `Script` other than `Common` or `Inherited`. `Nl` characters are not counted. Entries use Unicode long script names, sorted by count descending, then script name ascending. `mixed_script` is true if and only if the profile has at least two entries.
- **Approval:** `agent-proposed`
- **Verification level:** `unit`
- **Invariants:** The sum of counts equals the number of counted letters in `format_stripped.text`. Entries are unique. Each count is positive. The profile is empty if and only if there are no counted letters. `mixed_script == (len(script_profile) >= 2)`. The profile is computed from `format_stripped`, not from the skeleton.
- **Errors and edge cases:** `mixed_script` is a descriptive count-based signal, not UTS #39 mixed-script detection. It does not use resolved script sets or `Script_Extensions`, and downstream security logic must not treat it as a complete confusable or mixed-script verdict. Common-script letters such as `U+3006` and `U+30FC` are preserved but not counted. `U+3007` is `Nl` and is not counted. Text with only punctuation, digits, or combining marks has an empty profile.

| Given / Input | When | Expected result |
|---|---|---|
| `"hello world"` | Canonicalize | `[Latin: 10]`; `mixed_script == False` |
| `"Привет hello"` | Canonicalize | `[Cyrillic: 6, Latin: 5]`; `mixed_script == True` |
| `"hello Привет"` | Canonicalize | `[Cyrillic: 6, Latin: 5]`; sorted by count |
| `"123 !!!"` | Canonicalize | `[]`; `mixed_script == False` |
| `""` | Canonicalize | `[]`; `mixed_script == False` |
| `"аpple"` | Canonicalize | `[Latin: 4, Cyrillic: 1]`; `mixed_script == True` |
| `"你好"` | Canonicalize | `[Han: 2]`; `mixed_script == False` |
| `"\u3006"` | Canonicalize | `[]`; `Lo` but `Script=Common` |
| `"\u30fc"` | Canonicalize | `[]`; `Lm` but `Script=Common` |
| `"\u3007"` | Canonicalize | `[]`; `Nl` is not a counted letter |
| `"hello\u3164"` | Canonicalize | `[Latin: 5]`; Hangul filler removed before profiling |

### `CAN-007` Raw-span mapping and alignment

- **Behavior:** Each `TextForm` exposes `char_to_raw`, one span per code point, and `to_raw_span(start, end)` returns the smallest raw span covering the form code points in `[start, end)`. `char_to_raw` is produced by tracking source segments through every stage:
  1. Start with each raw code point as a segment carrying its raw span.
  2. NFKC assigns each output code point the union of the source segments that the normalization algorithm decomposed or composed into it. A compatibility expansion maps each expanded code point to the original source segment. A canonical composition maps the composed code point to the union of the base and combining-mark segments. Canonical reordering keeps each code point’s own source segment; order may be non-monotonic. Hangul composition maps the syllable to the union of its Jamo segments.
  3. Each removal pass drops segments for removed code points.
  4. The UTS #39 skeleton applies NFD, default-ignorable removal, prototype mapping, and NFD again. Each output code point inherits the union of the segments of the source code points that produced it. A prototype expansion maps every target code point to the source code point’s span.
  5. `char_to_raw[i]` is the smallest contiguous raw span covering the source union: `min(start)`, `max(end)`.
- **Approval:** `agent-proposed`
- **Verification level:** `unit`
- **Invariants:** `len(char_to_raw) == len(text)`. Every span is non-empty and lies within `[0, len(raw.text)]`. The raw form is the identity map. For a non-empty range, the result start is the minimum mapped start and the result end is the maximum mapped end. An empty range returns the end of the previous form code point; at `start == end == 0` it returns `(0, 0)`. Mapping composes through normalization, stripping, and skeleton stages. Spans may be non-monotonic.
- **Errors and edge cases:** `start < 0`, `end > len(text)`, or `start > end` raises `ValueError`. Non-integer offsets raise `TypeError`. Astral code points count as one code point.

| Given / Input | When | Expected result |
|---|---|---|
| `"a\u200bb"`, `format_stripped` | `to_raw_span(1, 2)` | `(2, 3)` |
| `"a\u200bb"`, `format_stripped` | `to_raw_span(1, 1)` | `(1, 1)` |
| `"ﬁle"`, `nfkc` | `to_raw_span(0, 2)` | `(0, 1)` |
| `"ﬁle"`, `nfkc` | `to_raw_span(1, 1)` | `(1, 1)` |
| `"e\u0301"`, `nfkc` | `to_raw_span(0, 1)` | `(0, 2)` |
| `"a\u0315\u0300"`, `nfkc` | `to_raw_span(0, 1)` | `(0, 3)` |
| `"a\u0315\u0300"`, `nfkc` | `to_raw_span(1, 2)` | `(1, 2)` |
| `"㌀"`, `nfkc` | `to_raw_span(0, 4)` | `(0, 1)` |
| `"가"`, `nfkc` | `to_raw_span(0, 1)` | `(0, 2)` |
| `"аpple"`, `confusable_skeleton` | `to_raw_span(0, 1)` | `(0, 1)` |
| `"é"`, `confusable_skeleton` | `to_raw_span(0, 2)` | `(0, 1)` |
| `"abc"`, `raw` | `to_raw_span(1, 3)` | `(1, 3)` |
| `"abc"`, `raw` | `to_raw_span(2, 2)` | `(2, 2)` |
| `"😀"`, `raw` | `to_raw_span(0, 1)` | `(0, 1)` |
| `"abc"`, `raw` | `to_raw_span(-1, 1)` | `ValueError` |

### `CAN-008` Fixed stage order

- **Behavior:** Forms are derived in the fixed order raw → NFKC → format-stripped fixed point → UTS #39 confusable skeleton. The script profile is computed from format-stripped. No form is derived from a later form. Format-stripped is itself NFKC-stable and removal-stable.
- **Approval:** `agent-proposed`
- **Verification level:** `unit`
- **Invariants:** `nfkc.text == NFKC(raw.text)`. `format_stripped.text` is the fixed point of `x -> remove(NFKC(x))` starting from `nfkc.text`. `confusable_skeleton.text` is the UTS #39 skeleton of `format_stripped.text`. `script_profile` counts counted letters in `format_stripped.text`. `NFKC(format_stripped.text) == format_stripped.text`. Re-canonicalizing `nfkc.text` leaves `nfkc.text` unchanged; re-canonicalizing `format_stripped.text` leaves `format_stripped.text` unchanged.
- **Errors and edge cases:** None for a valid `str`. The fixed-point step can compose text that was previously blocked by a removed character; this is intentional.

| Given / Input | When | Expected result |
|---|---|---|
| `"Ａ\u200b"` | Canonicalize | NFKC `"A\u200b"`; stripped `"A"`; skeleton `"A"`; profile `[Latin: 1]` |
| `"ﬁ\u200b"` | Canonicalize | NFKC `"fi\u200b"`; stripped `"fi"`; skeleton `"fi"`; profile `[Latin: 2]` |
| `"а\u200b"` | Canonicalize | NFKC `"а\u200b"`; stripped `"а"`; skeleton `"a"`; profile `[Cyrillic: 1]` |
| `"pay\nnow"` | Canonicalize | Stable through stripping and skeleton; profile `[Latin: 6]` |
| `"e\u200b\u0301"` | Canonicalize | NFKC `"e\u200b\u0301"`; stripped `"é"`; skeleton `"e\u0301"` |

### `CAN-009` Version metadata

- **Behavior:** Output carries non-empty `canonicalizer_version`, `unicode_version`, `script_unicode_version`, and `confusables_version`. The pinned data releases are:
  - `unicode_version = "15.1.0"` for NFKC, general categories, `White_Space`, and `Default_Ignorable_Code_Point` from UCD 15.1.0 (`UnicodeData.txt`, `PropList.txt`, `DerivedCoreProperties.txt`).
  - `script_unicode_version = "15.1.0"` for the Unicode `Script` property from UCD 15.1.0 (`Scripts.txt`).
  - `confusables_version = "15.1.0"` for the UTS #39 `confusables.txt` table.
  - `canonicalizer_version = "can/1"` for this stage order, alignment rule, removal predicate, and data pinning.
- **Approval:** `agent-proposed`
- **Verification level:** `unit`
- **Invariants:** Versions are fixed for a deployment and stable within a process. The same input and pinned data produce identical output. Every version field is non-empty. The implementation uses the declared releases and does not mix data from different Unicode or library releases. A future data release requires a new `canonicalizer_version` and a new spec revision. A change to stage order, alignment, or removal predicate also requires a new `canonicalizer_version`.
- **Errors and edge cases:** Constructing `TextForms` with any empty version raises `ValidationError`. A runtime whose default normalization data is not Unicode 15.1.0 must use pinned 15.1.0 data rather than the runtime default. `unicode_version` and `script_unicode_version` are both `"15.1.0"` for this revision.

| Given / Input | When | Expected result |
|---|---|---|
| `canonicalize("abc")` | Inspect | `canonicalizer_version="can/1"`, `unicode_version="15.1.0"`, `script_unicode_version="15.1.0"`, `confusables_version="15.1.0"` |
| `unicode_version` | Compare with UCD data | `"15.1.0"` |
| `script_unicode_version` | Compare with Script data | `"15.1.0"` |
| `confusables_version` | Compare with UTS #39 data | `"15.1.0"` |
| Empty `unicode_version` | Construct `TextForms` | `ValidationError` |
| Empty `canonicalizer_version` | Construct `TextForms` | `ValidationError` |

### `CAN-010` Empty and whitespace boundaries

- **Behavior:** Empty input yields empty forms, empty maps, an empty profile, and `mixed_script=False`. Whitespace-only input preserves its whitespace in every form, with an empty profile and `mixed_script=False`. No trimming occurs.
- **Approval:** `agent-proposed`
- **Verification level:** `unit`
- **Invariants:** `canonicalize("")` has empty `text` and `char_to_raw` for all four forms. Whitespace-only input has no counted letters. No form trims or collapses whitespace.
- **Errors and edge cases:** An input consisting only of removed characters is valid and yields empty derived forms; it is not the same as whitespace-only input.

| Given / Input | When | Expected result |
|---|---|---|
| `""` | Canonicalize | All forms empty; profile `[]`; `mixed_script == False` |
| `"   "` | Canonicalize | All forms preserve three spaces; profile `[]`; `mixed_script == False` |
| `"\n\t"` | Canonicalize | All forms preserve the newline and tab; profile `[]`; `mixed_script == False` |
| `"\u200b"` | Canonicalize | Raw and NFKC contain ZWSP; stripped and skeleton empty; profile `[]`; `mixed_script == False` |

### `CAN-011` Output models reject inconsistent structure

- **Behavior:** `TextForm` and `TextForms` validate their structural invariants at construction. `TextForm` rejects a `char_to_raw` length that differs from `len(text)` and any span that violates `0 <= span.start < span.end`. `TextForms` rejects a raw form that is not the identity map, any form span outside `[0, len(raw.text)]`, duplicate or non-positive script counts, an unsorted script profile, a `mixed_script` value that disagrees with the profile length, and empty version fields.
- **Approval:** `agent-proposed`
- **Verification level:** `unit`
- **Invariants:** Any `TextForms` instance that passes validation satisfies the structural invariants in `CAN-002`, `CAN-006`, `CAN-007`, and `CAN-009`. `canonicalize` always returns a valid instance. Validation checks structure, not derivation; it does not recompute NFKC, stripping, or skeletonization.
- **Errors and edge cases:** Each structural violation raises `ValidationError`. A structurally valid instance can still be semantically wrong if constructed manually; consumers must obtain forms from `canonicalize`.

| Given / Input | When | Expected result |
|---|---|---|
| `TextForm(text="ab", char_to_raw=[(0, 1)])` | Construct | `ValidationError`; length mismatch |
| `TextForm(text="a", char_to_raw=[(1, 1)])` | Construct | `ValidationError`; empty span |
| `TextForm(text="a", char_to_raw=[(-1, 1)])` | Construct | `ValidationError`; negative source offset |
| `TextForms` with `raw.text="a"` and `nfkc` span `(0, 2)` | Construct | `ValidationError`; out of raw bounds |
| `TextForms` with raw map `[(0, 1)]` for `"ab"` | Construct | `ValidationError`; raw map not identity |
| `ScriptCount(script="Latin", count=0)` | Construct | `ValidationError` |
| Duplicate script entries | Construct `TextForms` | `ValidationError` |
| Profile `[Latin: 1, Cyrillic: 2]` | Construct `TextForms` | `ValidationError`; not sorted |
| `mixed_script=False` with two profile entries | Construct `TextForms` | `ValidationError` |
| Empty `confusables_version` | Construct `TextForms` | `ValidationError` |
| `canonicalize("abc")` | Construct | Valid |
