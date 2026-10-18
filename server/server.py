import os
import json
import re
from datetime import datetime, timezone
import string
import random
import difflib
from dataclasses import dataclass
from typing import Optional, Any
import yaml

# Import modern MCPServer (SDK v2) with fallback to standalone fastmcp
try:
    from mcp.server import MCPServer
except ImportError:
    try:
        from fastmcp import FastMCP as MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP as MCPServer

# Initialize server
mcp = MCPServer(
    "MarkdownCriticReviewer",
    instructions="""
    You are an editorial review assistant using the CriticMarkup MCP server.
    CRITICAL PROTOCOL RULES:
    1. IMMUTABLE DIFFS: CriticMarkup tags ({~~...~~}, {--...--}, {++...++}) are read-only markers
       reserved strictly for the human author to accept or reject in their editor (e.g., Monoleaf).
       Preserve human governance and accountability.
    2. NEVER REVERT OR CLEAN DIFFS: Never include CriticMarkup delimiters in 'search_string' or 'replacement_string'.
       Do not attempt to undo, revert, clean up, or accept previous edits.
    3. SURGICAL REVISIONS: Keep search and replacement strings minimal (under 15 words). Do not replace whole paragraphs.
    """
)

# --- REGEX & PARSING PATTERNS ---

# Matches YAML frontmatter at the very start of the file
FRONTMATTER_RE = re.compile(r'^---\r?\n([\s\S]*?)\r?\n---\r?\n?')

# Unresolved CriticMarkup diffs inside body prose
CRITIC_MARKUP_PATTERN = re.compile(r'\{[\+\-\~\=\>]{2}[\s\S]*?[\+\-\~\=\>]{2}\}')

# Inline anchors marking comment highlights: <!--c:id1s--> and <!--c:id1e-->
INLINE_ANCHOR_PATTERN = re.compile(r'<!--c:[a-zA-Z0-9]+[se]-->')

# Trailing JSON comment thread payloads: <!--c:{id} {json}-->
COMMENT_PAYLOAD_RE = re.compile(r'<!--c:([a-zA-Z0-9]+)\s+(\{[\s\S]*?\})\s*-->')


# --- SETTINGS & DOCUMENT MODEL ---

@dataclass
class ReviewerSettings:
    """
    Typed, extensible settings model.
    Pulls configuration strictly from the 'reviewer' YAML frontmatter dictionary.
    """
    max_lines: Optional[int] = None

    @classmethod
    def from_yaml_dict(cls, data: dict[str, Any]) -> "ReviewerSettings":
        reviewer_data = data.get("reviewer")
        if not isinstance(reviewer_data, dict):
            return cls()

        max_lines_val = reviewer_data.get("max_lines")
        max_lines: Optional[int] = None
        if max_lines_val is not None:
            try:
                parsed = int(max_lines_val)
                if parsed > 0:
                    max_lines = parsed
            except (ValueError, TypeError):
                pass

        return cls(max_lines=max_lines)


class ReviewDocument:
    """
    Separates a markdown file into three protected layers:
    1. raw_frontmatter: Preserved byte-for-byte; hidden from and untouchable by the AI.
    2. body: The active prose, CriticMarkup, and inline comment anchors.
    3. threads: Comment thread JSON payloads detached from body text.
    """
    def __init__(
        self,
        filepath: str,
        raw_frontmatter: str,
        settings: ReviewerSettings,
        body: str,
        threads: dict[str, dict]
    ):
        self.filepath = filepath
        self.raw_frontmatter = raw_frontmatter
        self.settings = settings
        self.body = body
        self.threads = threads

    @classmethod
    def load(cls, filepath: str) -> "ReviewDocument":
        if not filepath.lower().endswith(('.md', '.markdown')):
            raise ValueError(f"Access denied: '{filepath}'. Only .md and .markdown files are supported.")
        if not os.path.exists(filepath):
            raise ValueError(f"File not found: '{filepath}'. Please supply a valid absolute path.")

        with open(filepath, 'r', encoding='utf-8') as f:
            raw_content = f.read().replace('\r\n', '\n')

        # 1. Extract frontmatter
        fm_match = FRONTMATTER_RE.match(raw_content)
        raw_frontmatter = ""
        settings = ReviewerSettings()
        content_after_fm = raw_content

        if fm_match:
            raw_frontmatter = fm_match.group(0)
            yaml_body = fm_match.group(1)
            content_after_fm = raw_content[fm_match.end():]
            try:
                parsed_yaml = yaml.safe_load(yaml_body) or {}
                if isinstance(parsed_yaml, dict):
                    settings = ReviewerSettings.from_yaml_dict(parsed_yaml)
            except yaml.YAMLError:
                # If frontmatter YAML is malformed, retain it byte-for-byte and use defaults
                pass

        # 2. Extract trailing comment payloads
        threads: dict[str, dict] = {}
        for match in COMMENT_PAYLOAD_RE.finditer(content_after_fm):
            tid = match.group(1)
            try:
                threads[tid] = json.loads(match.group(2))
            except json.JSONDecodeError:
                threads[tid] = {"corrupted": True, "raw": match.group(2)}

        # 3. Clean body prose
        body = COMMENT_PAYLOAD_RE.sub('', content_after_fm).rstrip('\n')

        return cls(filepath, raw_frontmatter, settings, body, threads)

    @property
    def cutoff_char_offset(self) -> Optional[int]:
        """Calculates the character index in body where max_lines ends."""
        if not self.settings.max_lines:
            return None
        lines = self.body.splitlines(keepends=True)
        if len(lines) <= self.settings.max_lines:
            return None
        return sum(len(line) for line in lines[:self.settings.max_lines])

    def check_editable_range(self, start_pos: int, end_pos: int) -> None:
        """Enforces line cutoffs on targeted edits."""
        cutoff = self.cutoff_char_offset
        if cutoff is not None and end_pos > cutoff:
            line_no = self.body[:start_pos].count('\n') + 1
            raise ValueError(
                f"Edit rejected: Target text spans past line {self.settings.max_lines} (found around line {line_no}). "
                "The user configured a cutoff for this document. You may request the user to increase this limit in the frontmatter."
            )

    def get_visible_body(self) -> str:
        """Returns the body truncated at max_lines, with a cutoff notice if truncated."""
        cutoff = self.cutoff_char_offset
        if cutoff is None:
            return self.body
        truncated_text = self.body[:cutoff].rstrip('\n')
        cutoff_notice = "\n\n[user configured cutoff lies here, you may request the user to increase this limit]"
        return truncated_text + cutoff_notice

    def save(self) -> None:
        """Reconstructs the document with frontmatter, body, and serialized comment payloads."""
        output_parts = []
        if self.raw_frontmatter:
            output_parts.append(self.raw_frontmatter.rstrip('\n') + '\n')

        output_parts.append(self.body.rstrip('\n'))

        if self.threads:
            serialized_payloads = []
            for tid, payload in self.threads.items():
                if payload.get("corrupted"):
                    serialized_payloads.append(f"<!--c:{tid} {payload.get('raw')}-->")
                else:
                    serialized_payloads.append(f"<!--c:{tid} {json.dumps(payload, separators=(',', ':'))}-->")
            output_parts.append("\n\n" + "\n\n".join(serialized_payloads))

        output_parts.append("\n")

        with open(self.filepath, 'w', encoding='utf-8') as f:
            f.write("".join(output_parts))


# --- EDITING & MATCHING HELPERS ---

def _is_slicing_anchor(content: str, start_pos: int, end_pos: int) -> bool:
    """Prevents an edit from starting or ending inside an inline anchor tag."""
    for match in INLINE_ANCHOR_PATTERN.finditer(content):
        m_start, m_end = match.span()
        if m_start < start_pos < m_end or m_start < end_pos < m_end:
            return True
    return False


def _get_valid_matches(body: str, search_string: str, allow_critic: bool = False) -> list[tuple[int, int]]:
    """
    Finds (start, end) spans of search_string in body text outside CriticMarkup diffs.
    If allow_critic=True, matches inside CriticMarkup are permitted (used for comments).
    """
    if not search_string:
        return []

    matches = []
    start = 0
    while True:
        pos = body.find(search_string, start)
        if pos == -1:
            break
        end = pos + len(search_string)

        is_protected = False
        if not allow_critic:
            is_protected = any(
                max(pos, m.start()) < min(end, m.end())
                for m in CRITIC_MARKUP_PATTERN.finditer(body)
            )

        if not is_protected and not _is_slicing_anchor(body, pos, end):
            matches.append((pos, end))
        start = pos + 1
    return matches


def _diff_to_critic(old_text: str, new_text: str) -> str:
    """Word-level diff wrapper preserving whitespace and minimal modifications."""
    tokens_old = re.findall(r'\S+|\s+', old_text)
    tokens_new = re.findall(r'\S+|\s+', new_text)

    matcher = difflib.SequenceMatcher(None, tokens_old, tokens_new)
    parts = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        chunk_old = "".join(tokens_old[i1:i2])
        chunk_new = "".join(tokens_new[j1:j2])

        if tag == 'equal':
            parts.append(chunk_old)
        elif tag == 'replace':
            parts.append(f"{{~~{chunk_old}~>{chunk_new}~~}}")
        elif tag == 'delete':
            core = chunk_old.strip()
            if not core:
                continue
            leading_ws = re.match(r'^\s*', chunk_old).group()
            trailing_ws = re.search(r'\s*$', chunk_old).group()
            parts.append(f"{leading_ws}{{--{core}--}}{trailing_ws}")
        elif tag == 'insert':
            core = chunk_new.strip()
            if not core:
                parts.append(chunk_new)
                continue
            leading_ws = re.match(r'^\s*', chunk_new).group()
            trailing_ws = re.search(r'\s*$', chunk_new).group()
            parts.append(f"{leading_ws}{{++{core}++}}{trailing_ws}")

    return "".join(parts)


def _apply_change_at_span(
    doc: ReviewDocument,
    start_pos: int,
    end_pos: int,
    search_string: str,
    replacement_string: str
) -> None:
    """Applies a CriticMarkup replacement directly at verified start_pos:end_pos."""
    # Enforce cutoff boundaries
    doc.check_editable_range(start_pos, end_pos)

    # Extract existing anchors from target span
    start_anchors = re.findall(r'<!--c:[a-zA-Z0-9]+s-->', search_string)
    end_anchors = re.findall(r'<!--c:[a-zA-Z0-9]+e-->', search_string)

    clean_search = INLINE_ANCHOR_PATTERN.sub('', search_string or '')
    clean_replacement = INLINE_ANCHOR_PATTERN.sub('', replacement_string or '')

    if not clean_search.strip():
        raise ValueError(
            "'search_string' contains no editable text (only anchor tags or whitespace)."
        )

    if clean_search == clean_replacement:
        raise ValueError(
            f"No-op edit rejected: 'replacement_string' is identical to 'search_string' ('{clean_search}')."
        )

    critic_body = _diff_to_critic(clean_search, clean_replacement)
    critic_markup = f"{''.join(start_anchors)}{critic_body}{''.join(end_anchors)}"

    doc.body = doc.body[:start_pos] + critic_markup + doc.body[end_pos:]


def _apply_single_change(doc: ReviewDocument, search_string: str, replacement_string: str) -> None:
    """Finds match and applies change (used by request_changes)."""
    valid_matches = _get_valid_matches(doc.body, search_string)
    if len(valid_matches) == 0:
        raise ValueError(
            f"Target text '{search_string[:60]}...' not found in editable text. "
            "Ensure exact capitalization/whitespace or supply more surrounding context."
        )
    if len(valid_matches) > 1:
        raise ValueError(
            f"Target text '{search_string[:60]}...' matched {len(valid_matches)} times. "
            "Include more surrounding context words to make the search string unique."
        )

    start_pos, end_pos = valid_matches[0]
    _apply_change_at_span(doc, start_pos, end_pos, search_string, replacement_string)


# --- MCP TOOLS ---

@mcp.tool()
def get_md_contents(filepath: str) -> str:
    """
    Reads a markdown file, returning prose up to the user-configured cutoff line (if set),
    followed by an abbreviated summary of discussion comment threads.
    YAML frontmatter is completely hidden from view.
    """
    doc = ReviewDocument.load(filepath)
    visible_prose = doc.get_visible_body()

    if not doc.threads:
        return visible_prose

    summary_lines = ["\n\n---", "### Comments & Threads:"]
    for thread_id, payload in doc.threads.items():
        if payload.get("corrupted"):
            summary_lines.append(f"\nThread [{thread_id}]: [Corrupted JSON Payload]")
            continue
        status = "RESOLVED" if payload.get("resolved") else "OPEN"
        summary_lines.append(f"\nThread [{thread_id}] ({status}):")
        for entry in payload.get("thread", []):
            author = entry.get("author", "Unknown")
            text = entry.get("text", "")
            summary_lines.append(f"  - {author}: {text}")

    return visible_prose + "\n" + "\n".join(summary_lines)


@mcp.tool()
def replace_multiple(filepath: str, edits: list[dict]) -> str:
    """
    Applies multiple targeted revisions across different sections in a single call using CriticMarkup.
    Edits are applied bottom-to-top to maintain offset stability.
    Targeted edits past the configured cutoff line are rejected.
    """
    doc = ReviewDocument.load(filepath)

    if not edits:
        raise ValueError("No edits supplied in the 'edits' list.")

    applied_count = 0
    failed_edits = []
    planned_edits = []

    # Phase 1: Validate matches and cutoff constraints
    for i, edit in enumerate(edits):
        search_str = edit.get("search_string") or edit.get("search") or ""
        replace_str = edit.get("replacement_string") if "replacement_string" in edit else edit.get("replace", "")
        if replace_str is None:
            replace_str = ""

        if not search_str.strip():
            failed_edits.append(f"Edit #{i+1}: 'search_string' was empty or whitespace.")
            continue

        matches = _get_valid_matches(doc.body, search_str)
        if len(matches) == 0:
            failed_edits.append(f"Edit #{i+1}: Target text '{search_str[:50]}...' not found in editable text.")
            continue
        if len(matches) > 1:
            failed_edits.append(f"Edit #{i+1}: Target text '{search_str[:50]}...' matched {len(matches)} times (ambiguous).")
            continue

        start_pos, end_pos = matches[0]

        # Check line cutoff
        try:
            doc.check_editable_range(start_pos, end_pos)
        except ValueError as e:
            failed_edits.append(f"Edit #{i+1}: {str(e)}")
            continue

        planned_edits.append((start_pos, end_pos, search_str, replace_str, i + 1))

    # Phase 2: Check for overlapping spans
    planned_edits.sort(key=lambda x: x[0])
    non_overlapping_edits = []
    last_end = -1
    for start_pos, end_pos, search_str, replace_str, idx in planned_edits:
        if start_pos < last_end:
            failed_edits.append(f"Edit #{idx}: Overlapped with an earlier edit in this batch.")
            continue
        non_overlapping_edits.append((start_pos, end_pos, search_str, replace_str))
        last_end = end_pos

    # Phase 3: Apply valid edits in reverse order (bottom-to-top) using exact offsets
    non_overlapping_edits.sort(key=lambda x: x[0], reverse=True)
    for start_pos, end_pos, search_str, replace_str in non_overlapping_edits:
        _apply_change_at_span(doc, start_pos, end_pos, search_str, replace_str)
        applied_count += 1

    if applied_count > 0:
        doc.save()

    report = [f"Successfully applied {applied_count} of {len(edits)} edits."]
    if failed_edits:
        report.append("\nSkipped edits:")
        for fail in failed_edits:
            report.append(f"- {fail}")

    if applied_count == 0 and failed_edits:
        raise ValueError("\n".join(report))

    return "\n".join(report)


@mcp.tool()
def request_changes(filepath: str, search_string: str, replacement_string: str) -> str:
    """
    Applies a single targeted change or deletion using CriticMarkup.
    Targeted edits past the configured cutoff line are prohibited.
    """
    doc = ReviewDocument.load(filepath)
    _apply_single_change(doc, search_string, replacement_string)
    doc.save()
    return "Successfully applied change while preserving comment anchors."


@mcp.tool()
def replace_all(filepath: str, search_string: str, replacement_string: str) -> str:
    """
    Globally replaces ALL instances of a word or phrase throughout the document body using CriticMarkup.
    Operates across the full document body regardless of cutoff line, while frontmatter, inline anchors,
    and JSON comment payloads remain strictly protected.
    """
    doc = ReviewDocument.load(filepath)

    if not search_string.strip():
        raise ValueError("'search_string' cannot be empty or whitespace.")

    clean_replacement = INLINE_ANCHOR_PATTERN.sub('', replacement_string or '')

    if search_string == clean_replacement:
        raise ValueError(
            f"No-op edit rejected: 'replacement_string' is identical to 'search_string' ('{search_string}')."
        )

    prefix_b = r'\b' if search_string[:1].isalnum() else ''
    suffix_b = r'\b' if search_string[-1:].isalnum() else ''
    target_pattern = prefix_b + re.escape(search_string) + suffix_b

    # Protect both CriticMarkup AND inline comment anchors
    combined_pattern = re.compile(
        CRITIC_MARKUP_PATTERN.pattern + r'|' + INLINE_ANCHOR_PATTERN.pattern + r'|(' + target_pattern + r')',
        re.MULTILINE
    )

    matches_found = 0
    def replacer(match: re.Match) -> str:
        nonlocal matches_found
        if match.group(1) is None:
            return match.group(0)  # Protected region: CriticMarkup or Anchor tag
        matches_found += 1
        return f"{{~~{match.group(1)}~>{clean_replacement}~~}}"

    new_body = combined_pattern.sub(replacer, doc.body)

    if matches_found == 0:
        raise ValueError(f"No instances of '{search_string}' found in editable text.")

    doc.body = new_body
    doc.save()

    return f"Successfully replaced {matches_found} instances of '{search_string}' globally."


@mcp.tool()
def write_comment(filepath: str, search_string: str, comment_text: str) -> str:
    """
    Attaches an inline comment anchor around target text and creates an open discussion thread.
    Comments cannot be anchored past the configured cutoff line.
    """
    doc = ReviewDocument.load(filepath)

    if not search_string.strip():
        raise ValueError("'search_string' cannot be empty or whitespace.")
    if not comment_text.strip():
        raise ValueError("'comment_text' cannot be empty or whitespace.")

    valid_matches = _get_valid_matches(doc.body, search_string, allow_critic=True)
    if len(valid_matches) == 0:
        raise ValueError(
            f"Comment target '{search_string}' not found in editable text. "
            "Ensure exact capitalization/whitespace or supply more context words."
        )
    if len(valid_matches) > 1:
        raise ValueError(
            f"Comment target '{search_string}' matched {len(valid_matches)} times. "
            "Include more surrounding context to guarantee uniqueness."
        )

    start_pos, end_pos = valid_matches[0]
    doc.check_editable_range(start_pos, end_pos)

    # Generate unique 4-character ID
    while True:
        candidate_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
        if candidate_id not in doc.threads and f"<!--c:{candidate_id}" not in doc.body:
            thread_id = candidate_id
            break

    safe_comment_text = comment_text.replace("-->", "->")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    doc.threads[thread_id] = {
        "resolved": False,
        "thread": [{"author": "AI AGENT", "ts": ts, "text": safe_comment_text}]
    }

    # Wrap target with inline anchors
    anchor_replacement = f"<!--c:{thread_id}s-->{search_string}<!--c:{thread_id}e-->"
    doc.body = doc.body[:start_pos] + anchor_replacement + doc.body[end_pos:]

    doc.save()
    return f"Comment successfully written under Thread ID [{thread_id}]."


@mcp.tool()
def reply_to_comment(filepath: str, thread_id: str, reply_text: str) -> str:
    """
    Replies to an existing discussion thread by its 4-character UID (e.g. 'o1b7').
    Unaffected by the line cutoff.
    """
    doc = ReviewDocument.load(filepath)

    if not thread_id.strip():
        raise ValueError("'thread_id' cannot be empty or whitespace.")
    if not reply_text.strip():
        raise ValueError("'reply_text' cannot be empty or whitespace.")

    # Normalize various formats an LLM might send: "[o1b7]", "<!--c:o1b7s-->", "c:o1b7", "o1b7s"
    clean_id = thread_id.strip().strip('[]')
    clean_id = re.sub(r'^<!--\s*', '', clean_id)
    clean_id = re.sub(r'\s*-->$', '', clean_id)
    clean_id = re.sub(r'^c:', '', clean_id)
    if len(clean_id) == 5 and clean_id[-1].lower() in ('s', 'e'):
        clean_id = clean_id[:-1]
    thread_id = clean_id

    if thread_id not in doc.threads:
        raise ValueError(f"Comment thread with UID '{thread_id}' not found.")

    payload = doc.threads[thread_id]
    if payload.get("corrupted"):
        raise ValueError(f"Comment thread '{thread_id}' contains corrupted metadata.")

    safe_reply_text = reply_text.replace("-->", "->")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    payload.setdefault("thread", []).append({
        "author": "AI AGENT",
        "ts": ts,
        "text": safe_reply_text
    })

    doc.save()
    return f"Successfully added reply to thread [{thread_id}]."


def main():
    mcp.run()

if __name__ == "__main__":
    main()
