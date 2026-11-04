"""行为指纹 summarize / compare 逻辑。"""

from behavior_fingerprint import STEALTH_FLAGS, compare, summarize

ARGS = ["--disable-features=LocalNetworkAccessChecks", *STEALTH_FLAGS[1:]]


def _human_events():
    events, t = [], 0.0
    for _ in range(2):
        for _ in range(8):
            t += 30
            events.append({"type": "mousemove", "t": t, "trusted": True})
        events += [{"type": "pointerdown", "t": t + 10, "trusted": True},
                   {"type": "pointerup", "t": t + 210, "trusted": True}]
        t += 300
    for _ in range(3):
        t += 100
        events += [{"type": "keydown", "t": t, "trusted": True},
                   {"type": "input", "t": t + 1, "trusted": True, "inputType": "insertText"},
                   {"type": "keyup", "t": t + 40, "trusted": True}]
    return events


def _fp(events, **kw):
    params = dict(args=ARGS, programmatic={}, press_at_calls=2, phases=["click.before"],
                  workflow="w", ok=True)
    params.update(kw)
    return summarize(events, **params)


def test_identical_fingerprints_compare_clean():
    base = _fp(_human_events())
    assert base["safe"] is True
    assert compare(base, _fp(_human_events())) == []


def test_jitter_in_stats_is_ignored():
    jittered = [dict(e, t=e["t"] * 1.1) for e in _human_events()]
    assert compare(_fp(_human_events()), _fp(jittered)) == []


def test_fill_style_zero_key_input_is_flagged():
    events = _human_events() + [{"type": "input", "t": 99999, "trusted": True, "inputType": "insertText"}]
    current = _fp(events)
    assert current["invariants"]["keyboard.zero_key_inputs"] == 1
    diffs = compare(_fp(_human_events()), current)
    assert any("zero_key_inputs" in d for d in diffs)
    assert any("safe=false" in d for d in diffs)


def test_programmatic_click_and_fast_press_and_missing_flag_are_flagged():
    events = _human_events()
    events[9]["t"] = events[8]["t"] + 1  # 1ms 按压：locator.click 特征
    current = _fp(events, programmatic={"Locator.click": 1}, press_at_calls=1,
                  args=[a for a in ARGS if "LocalNetworkAccessChecks" not in a])
    diffs = "\n".join(compare(_fp(_human_events()), current))
    for needle in ("programmatic_calls", "pointer.press_human", "press_at.calls",
                   "stealth_flags", "launch_args"):
        assert needle in diffs


def test_hover_window_with_press_is_flagged():
    events = _human_events()
    first_move, first_down = events[0]["t"], events[8]["t"]
    times = [("hover.before", first_move - 1), ("hover.after", first_down + 1)]
    fp = _fp(events, phase_times=times)
    assert fp["invariants"]["hover.pointerdowns"] == 1 and fp["safe"] is False
    clean = _fp(events, phase_times=[("hover.before", first_move - 1), ("hover.after", first_down - 1)])
    assert clean["invariants"]["hover.pointerdowns"] == 0 and clean["invariants"]["hover.moves_ok"]
    assert any("hover.pointerdowns" in d for d in compare(clean, fp))
