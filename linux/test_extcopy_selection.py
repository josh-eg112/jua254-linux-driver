from extcopy_selection import select_exact


def test_headless_exact_match():
    outputs = [{"name": "eDP-1"}, {"name": "HEADLESS-1"}]
    assert select_exact("HEADLESS-1", outputs) == "HEADLESS-1"


def test_missing_does_not_fallback():
    try:
        select_exact("HEADLESS-2", [{"name": "eDP-1"}, {"name": "HEADLESS-1"}])
    except ValueError as exc:
        assert "HEADLESS-2" in str(exc)
    else:
        raise AssertionError("selection unexpectedly fell back")


if __name__ == "__main__":
    test_headless_exact_match()
    test_missing_does_not_fallback()
    print("extcopy output-selection regression: PASS")
