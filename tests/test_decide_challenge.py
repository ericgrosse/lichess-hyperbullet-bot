from lichess_client import decide_challenge


def challenge(**overrides):
    data = {
        "variant": {"key": "standard"},
        "speed": "bullet",
        "perf": {"key": "bullet"},
        "timeControl": {"type": "clock", "limit": 30, "increment": 0},
        "challenger": {"title": "BOT"},
    }
    data.update(overrides)
    return data


def test_accepts_bot_standard_clock_under_30s_zero_increment():
    assert decide_challenge(challenge()).accept


def test_accepts_human_challengers_when_allowed():
    assert decide_challenge(challenge(challenger={"title": None}), allow_human_challenges=True).accept


def test_rejects_non_bot_challengers_when_disabled():
    assert not decide_challenge(challenge(challenger={"title": None}), allow_human_challenges=False).accept


def test_rejects_non_standard_variants():
    assert not decide_challenge(challenge(variant={"key": "crazyhouse"})).accept


def test_rejects_increment_above_zero():
    assert not decide_challenge(challenge(timeControl={"type": "clock", "limit": 30, "increment": 1})).accept


def test_rejects_clock_above_30_seconds():
    assert not decide_challenge(challenge(timeControl={"type": "clock", "limit": 31, "increment": 0})).accept
