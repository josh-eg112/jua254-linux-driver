"""Pure output-selection logic shared by diagnostics and regression tests."""


def select_exact(requested, outputs):
    names = [o.get("name", "") if isinstance(o, dict) else str(o) for o in outputs]
    matches = [name for name in names if name == requested]
    if len(matches) != 1:
        raise ValueError(f"requested output {requested!r} not uniquely enumerated: {names!r}")
    return matches[0]
