from difflib import SequenceMatcher


def normalize_text(text):

    return " ".join(text.lower().strip().split())


def is_duplicate_segment(
    current,
    previous_segments,
    overlap_start,
    overlap_end,
):

    current_start = current["start"]
    current_end = current["end"]

    if current_end <= overlap_start or current_start >= overlap_end:
        return False

    current_text = normalize_text(current["text"])

    if not current_text:
        return False

    for previous in previous_segments:

        previous_start = previous["start"]
        previous_end = previous["end"]

        if (
            current_end <= previous_start
            or current_start >= previous_end
        ):
            continue

        previous_text = normalize_text(previous["text"])

        if not previous_text:
            continue

        similarity = SequenceMatcher(
            None,
            current_text,
            previous_text,
        ).ratio()

        if similarity >= 0.75:
            return True

        if (
            current_text in previous_text
            or previous_text in current_text
        ):
            return True

    return False


def deduplicate_segments(
    current_segments,
    previous_segments,
    overlap_start,
    overlap_end,
):

    result = []

    for segment in current_segments:
        if is_duplicate_segment(
            segment,
            previous_segments,
            overlap_start,
            overlap_end,
        ):
            continue

        result.append(segment)

    return result
