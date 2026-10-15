from scripts.verify import environment


def test_offline_verification_cannot_accidentally_enable_paid_services():
    original = {
        "NEWS_RESEARCH_LIVE": "1",
        "NEWS_REVIEW_LIVE": "1",
        "NEWS_SERVICES_LIVE": "1",
        "NEWS_SUBMISSION_REPLAY": "1",
        "NEWS_UNKNOWN_LIVE": "1",
        "PATH": "fixture",
    }
    result = environment(original)
    assert original["NEWS_RESEARCH_LIVE"] == "1"
    assert (
        result["NEWS_RESEARCH_LIVE"]
        == result["NEWS_REVIEW_LIVE"]
        == result["NEWS_SERVICES_LIVE"]
        == "0"
    )
    assert "NEWS_UNKNOWN_LIVE" not in result and "NEWS_SUBMISSION_REPLAY" not in result
    assert result["NEWS_DOCKER_LIVE"] == result["NEWS_COMPOSE_LIVE"] == "0"
    assert result["NEWS_DEMO_RECORD"] == result["NEWS_PHASE_THREE_RECORD"] == "1"
    assert result["PATH"] == "fixture"


def test_docker_verification_enables_only_local_container_acceptance():
    result = environment({}, docker=True)
    assert result["NEWS_DOCKER_LIVE"] == result["NEWS_COMPOSE_LIVE"] == "1"
    assert result["NEWS_SERVICES_LIVE"] == result["NEWS_RESEARCH_LIVE"] == "0"
