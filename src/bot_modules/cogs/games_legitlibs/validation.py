import re


_MARKER_RE = re.compile(r"\{(\w+)\}")
_URL_RE = re.compile(r"https?://\S+|discord\.gg/\S+", re.IGNORECASE)
_MENTION_RE = re.compile(r"<@[!&]?\d+>|@everyone|@here")

MAX_BLANKS = 25
MIN_BLANKS = 1


def validate_template(body: str, blanks: list[dict], tier: int, axes: dict) -> list[str]:
    """
    Validate a template definition. Returns a list of error strings (empty = valid).
    Runs identically on the bot at load time and on the web portal at save/publish time.

    axes: output of data.get_axes() —
        {"pos": [{"value", "min_tier"}, ...],
         "domains": {pos: [{"value", "min_tier"}, ...]},
         "forms":   {pos: [{"value", "min_tier"}, ...]}}
    blanks: [{"id": str, "pos": str, "domain": str|None, "form": str|None, "position": int}, ...]
    """
    errors = []

    if not body or not body.strip():
        errors.append("Template body cannot be empty.")
        return errors

    if not (1 <= tier <= 4):
        errors.append(f"Tier must be 1–4, got {tier}.")

    if len(blanks) < MIN_BLANKS:
        errors.append(f"Template must have at least {MIN_BLANKS} blank.")
    if len(blanks) > MAX_BLANKS:
        errors.append(f"Template has {len(blanks)} blanks; maximum is {MAX_BLANKS}.")

    marker_ids = set(_MARKER_RE.findall(body))
    blank_ids = {b["id"] for b in blanks}
    missing_markers = blank_ids - marker_ids
    extra_markers = marker_ids - blank_ids
    if missing_markers:
        errors.append(f"Blanks defined but not in body: {', '.join(sorted(missing_markers))}")
    if extra_markers:
        errors.append(f"Markers in body with no blank definition: {', '.join(sorted(extra_markers))}")

    pos_values = {p["value"] for p in axes.get("pos", [])}
    domain_by_pos = {
        pos: {d["value"]: d["min_tier"] for d in doms}
        for pos, doms in axes.get("domains", {}).items()
    }
    form_by_pos = {
        pos: {f["value"] for f in forms}
        for pos, forms in axes.get("forms", {}).items()
    }

    for blank in blanks:
        bid = blank.get("id", "?")
        pos = blank.get("pos")
        domain = blank.get("domain")
        form = blank.get("form")

        if not pos:
            errors.append(f"Blank '{bid}' is missing required POS.")
            continue
        if pos not in pos_values:
            errors.append(f"Blank '{bid}' has unknown POS '{pos}'.")
            continue

        if domain:
            pos_domains = domain_by_pos.get(pos, {})
            if domain not in pos_domains:
                errors.append(f"Blank '{bid}': domain '{domain}' is not valid for POS '{pos}'.")
            else:
                min_tier = pos_domains[domain]
                if tier < min_tier:
                    errors.append(
                        f"Blank '{bid}': domain '{domain}' requires tier {min_tier} "
                        f"(template is tier {tier})."
                    )

        if form:
            pos_forms = form_by_pos.get(pos, set())
            if form not in pos_forms:
                errors.append(f"Blank '{bid}': form '{form}' is not valid for POS '{pos}'.")

    return errors


def validate_fill(value: str, length_cap: int) -> str | None:
    """Validate a single player fill. Returns an error string or None if valid."""
    if not value or not value.strip():
        return "Fill cannot be empty."
    if len(value) > length_cap:
        return f"Too long (max {length_cap} characters)."
    if _URL_RE.search(value):
        return "URLs and invite links are not allowed."
    return None


def strip_mass_mentions(value: str) -> str:
    """Remove @everyone, @here, and user/role mentions from a fill."""
    return _MENTION_RE.sub("", value).strip()
