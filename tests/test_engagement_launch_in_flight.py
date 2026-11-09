"""INV-BGJOB-016: a job's launch is in flight exactly while its enrolment
stands — from the committed create until the launcher's finally drops the
handle — which is what keeps the job's delivery worker from writing."""

from engagement_registry import EngagementRegistry


def test_launch_in_flight_follows_the_enrolment(tmp_path):
    registry = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    assert registry.launch_in_flight("a" * 32) is False
    first = registry.register_launch("a" * 32, object())
    second = registry.register_launch("b" * 32, object())
    assert registry.launch_in_flight("a" * 32) is True
    registry.unregister_launch(first)
    assert registry.launch_in_flight("a" * 32) is False
    assert registry.launch_in_flight("b" * 32) is True
    registry.unregister_launch(second)
    assert registry.launch_in_flight("b" * 32) is False
