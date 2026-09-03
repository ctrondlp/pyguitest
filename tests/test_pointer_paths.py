"""Session.glide() and Session.drag(): pointer motion with events in between.

The interesting assertions here are about what the *compositor* would see --
how many events, in what order, spaced how far apart in wall-clock time --
because that is the whole difference between these and move_mouse().
"""

import contextlib
import time
import unittest
from unittest import mock

from pyguitest import Capability, CapabilityUnsupported, PyGUITestError, Session
from pyguitest.backends.base import GUIBackend
from pyguitest.capabilities import CapabilitySet
from pyguitest.session import Compositor, Environment, SessionType

POINTER = CapabilitySet(
    {Capability.POINTER_MOVE, Capability.POINTER_BUTTON, Capability.POINTER_SCROLL}
)


class FakeClock:
    """A monotonic clock that moves only when something asks it to.

    Duck-types the two functions of `time` that Session's scheduling uses,
    so it can stand in for the module wholesale.

    Here because an *upper* bound on wall-clock duration is a coin flip on a
    busy machine: two tests below asserted one and failed on a developer's
    desktop at 0.255s against a 0.2s bound, having passed twenty consecutive
    runs on an idle one. No delta fixes that -- an arbitrarily loaded
    machine overshoots any bound eventually. The properties they check are
    structural rather than temporal (backend cost comes out of the pauses
    instead of being added to the total; event_delay is charged once per
    gesture, not per point), so a clock that only advances when this code
    advances it checks them exactly, on any machine, in no time at all.

    Lower bounds stay on the real clock deliberately -- "the events were
    spread out at all" is a claim load can only make more true.
    """

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@contextlib.contextmanager
def driving(clock):
    """Run the body with `clock` standing in for Session's use of `time`."""
    with (
        mock.patch("pyguitest.time.monotonic", clock.monotonic),
        mock.patch("pyguitest.time.sleep", clock.sleep),
    ):
        yield clock


class RecordingBackend(GUIBackend):
    """Records every input event with the moment it arrived."""

    name = "recording"

    def __init__(self, capabilities=POINTER, cost=0.0, clock=time):
        self.events = []
        self._capabilities = capabilities
        self._cost = cost
        # `time` itself by default; a FakeClock when a test needs the cost
        # to be simulated rather than actually waited out.
        self._clock = clock

    @property
    def capabilities(self):
        return self._capabilities

    def _record(self, event):
        if self._cost:
            self._clock.sleep(self._cost)
        self.events.append((event, self._clock.monotonic()))

    def move_mouse(self, x, y, screen=0):
        self.require(Capability.POINTER_MOVE)
        self._record(("move", x, y, screen))

    def press_button(self, button):
        self._record(("press", button))

    def release_button(self, button):
        self._record(("release", button))

    @property
    def moves(self):
        return [event[1:3] for event, _ in self.events if event[0] == "move"]

    @property
    def kinds(self):
        return [event[0] for event, _ in self.events]

    @property
    def times(self):
        return [when for _, when in self.events]


class QueryingBackend(RecordingBackend):
    """An X11-shaped backend: injection *and* readback."""

    name = "querying"

    def __init__(self):
        super().__init__(CapabilitySet(set(POINTER) | {Capability.POINTER_QUERY}))
        self.position = (7, 9)

    def pointer_position(self):
        self.require(Capability.POINTER_QUERY)
        return self.position


def session(backend=None, **kwargs):
    return Session(
        backend or RecordingBackend(),
        Environment(SessionType.WAYLAND, Compositor.OTHER),
        **kwargs,
    )


class TestTheRoute(unittest.TestCase):
    def test_glide_emits_many_events_where_move_mouse_emits_one(self):
        gui = session()
        gui.move_mouse(0, 0)
        self.assertEqual(len(gui.backend.moves), 1)
        gui.glide(100, 0, duration=0.05, rate=200)
        self.assertGreater(len(gui.backend.moves), 5)

    def test_the_path_starts_after_the_origin_and_ends_on_the_target(self):
        gui = session()
        gui.move_mouse(0, 0)
        gui.backend.events.clear()
        gui.glide(100, 50, duration=0.05, rate=400)
        moves = gui.backend.moves
        self.assertNotEqual(moves[0], (0, 0), "the origin is not re-emitted")
        self.assertEqual(moves[-1], (100, 50))

    def test_a_straight_glide_stays_on_the_line(self):
        gui = session()
        gui.move_mouse(0, 0)
        gui.glide(100, 100, duration=0.05, rate=400)
        for x, y in gui.backend.moves:
            self.assertEqual(x, y, "a straight route should not wander")

    def test_the_step_count_is_duration_times_rate(self):
        gui = session()
        gui.move_mouse(0, 0)
        gui.backend.events.clear()
        gui.glide(500, 0, duration=0.1, rate=100)
        self.assertEqual(len(gui.backend.moves), 10)

    def test_waypoints_bend_the_route_away_from_the_straight_line(self):
        # The genuinely useful non-straight path: cross a particular widget
        # on the way, deliberately and reproducibly.
        gui = session()
        gui.move_mouse(0, 0)
        gui.backend.events.clear()
        gui.glide(100, 0, via=[(50, 80)], duration=0.05, rate=400)
        self.assertTrue(
            any(y > 40 for _, y in gui.backend.moves),
            "the route should climb towards the waypoint",
        )
        self.assertEqual(gui.backend.moves[-1], (100, 0))

    def test_a_glide_is_never_silently_a_teleport(self):
        # A duration too short to pay for even one point at `rate` would
        # otherwise collapse to a single event -- indistinguishable from
        # move_mouse(), and silently so.
        gui = session()
        gui.move_mouse(0, 0)
        gui.backend.events.clear()
        gui.glide(100, 100, duration=0, rate=1)
        self.assertGreaterEqual(len(gui.backend.moves), 2)

    def test_waypoints_are_walked_even_when_the_duration_rounds_to_nothing(self):
        gui = session()
        gui.move_mouse(0, 0)
        gui.backend.events.clear()
        gui.glide(10, 0, via=[(5, 5), (5, -5)], duration=0.001, rate=1)
        self.assertGreaterEqual(len(gui.backend.moves), 3)

    def test_speed_is_even_across_legs_of_different_length(self):
        # Sampling per leg rather than by distance would spend half the
        # events on the short leg, which is exactly the velocity artefact a
        # gesture recogniser would trip over.
        gui = session()
        gui.move_mouse(0, 0)
        gui.backend.events.clear()
        gui.glide(1000, 0, via=[(100, 0)], duration=0.05, rate=2200)
        short = [x for x, _ in gui.backend.moves if x <= 100]
        self.assertLess(len(short), 30, "the short leg should not hog the events")

    def test_a_glide_to_the_current_position_still_terminates(self):
        gui = session()
        gui.move_mouse(40, 40)
        gui.backend.events.clear()
        gui.glide(40, 40, duration=0.02, rate=200)
        self.assertEqual(set(gui.backend.moves), {(40, 40)})


class TestEasing(unittest.TestCase):
    def test_the_default_is_constant_velocity(self):
        gui = session()
        gui.move_mouse(0, 0)
        gui.backend.events.clear()
        gui.glide(100, 0, duration=0.1, rate=1000)
        moves = gui.backend.moves
        gaps = {b - a for (a, _), (b, _) in zip(moves, moves[1:], strict=False)}
        self.assertEqual(gaps, {1})

    def test_an_ease_out_decelerates_into_the_target(self):
        gui = session()
        gui.move_mouse(0, 0)
        gui.backend.events.clear()
        gui.glide(1000, 0, duration=0.1, rate=1000, ease=lambda t: 1 - (1 - t) ** 3)
        moves = gui.backend.moves
        first = moves[1][0] - moves[0][0]
        last = moves[-1][0] - moves[-2][0]
        self.assertGreater(first, last)
        self.assertEqual(moves[-1], (1000, 0))

    def test_an_overshooting_ease_is_clamped_to_the_route(self):
        gui = session()
        gui.move_mouse(0, 0)
        gui.backend.events.clear()
        gui.glide(100, 0, duration=0.02, rate=400, ease=lambda t: t * 1.5)
        self.assertTrue(all(0 <= x <= 100 for x, _ in gui.backend.moves))
        self.assertEqual(gui.backend.moves[-1], (100, 0))


class TestTiming(unittest.TestCase):
    def test_a_glide_takes_about_as_long_as_it_was_asked_to(self):
        gui = session()
        gui.move_mouse(0, 0)
        began = time.monotonic()
        gui.glide(100, 0, duration=0.2, rate=50)
        self.assertGreaterEqual(time.monotonic() - began, 0.18)

    def test_the_events_are_spread_out_rather_than_bunched(self):
        # A path emitted as fast as the loop runs carries no velocity
        # information at all; spacing is the point.
        gui = session()
        gui.move_mouse(0, 0)
        gui.backend.events.clear()
        gui.glide(100, 0, duration=0.15, rate=60)
        times = gui.backend.times
        self.assertGreater(times[-1] - times[0], 0.1)

    def test_a_slow_backend_does_not_stretch_the_total(self):
        # 5ms an event over 9 events is 45ms of backend cost inside a 100ms
        # budget: the schedule should absorb it, not append it. Measured on
        # a clock this test drives, so "absorbed" is exact rather than a
        # bound some other machine's load can breach -- appending would put
        # this at 0.145 and absorbing puts it at 0.1, which no plausible
        # delta confuses.
        clock = FakeClock()
        gui = session(RecordingBackend(cost=0.005, clock=clock))
        with driving(clock):
            gui.move_mouse(0, 0)
            began = clock.now
            gui.glide(100, 0, duration=0.1, rate=90)
            elapsed = clock.now - began
        self.assertAlmostEqual(elapsed, 0.1, delta=0.01)

    def test_event_delay_is_charged_once_for_the_whole_gesture(self):
        # Per point it would be charged twenty times and swamp the schedule.
        clock = FakeClock()
        gui = session(RecordingBackend(clock=clock), event_delay=0.01)
        with driving(clock):
            gui.move_mouse(0, 0)
            clock.sleeps.clear()  # drop that move's own event_delay
            began = clock.now
            gui.glide(100, 0, duration=0.01, rate=2000)
            elapsed = clock.now - began
        # The 20-point schedule (0.01) plus exactly one event_delay (0.01).
        # Charged per point it would be 0.21.
        self.assertAlmostEqual(elapsed, 0.02, delta=0.005)
        self.assertEqual(clock.sleeps.count(0.01), 1)


class TestTheOrigin(unittest.TestCase):
    def test_the_session_remembers_where_it_put_the_pointer(self):
        gui = session()
        gui.move_mouse(30, 40)
        gui.backend.events.clear()
        gui.glide(30, 100, duration=0.02, rate=200)
        self.assertTrue(all(x == 30 for x, _ in gui.backend.moves))

    def test_an_explicit_start_overrides_the_remembered_one(self):
        gui = session()
        gui.move_mouse(0, 0)
        gui.backend.events.clear()
        gui.glide(0, 100, start=(500, 0), duration=0.05, rate=200)
        self.assertEqual(gui.backend.moves[0][0], 450)

    def test_an_unknown_origin_is_an_error_rather_than_a_guess(self):
        # Silently starting from (0, 0) would make a drag succeed at the
        # wrong thing, which is worse than failing.
        gui = session()
        with self.assertRaises(PyGUITestError) as caught:
            gui.glide(100, 100)
        self.assertIn("start=", str(caught.exception))

    def test_x11_readback_supplies_the_origin_when_there_is_no_history(self):
        gui = session(QueryingBackend())
        gui.glide(7, 109, duration=0.02, rate=200)
        self.assertTrue(all(x == 7 for x, _ in gui.backend.moves))

    def test_readback_still_supplies_the_origin_through_a_composite(self):
        # Regression: `connect()` composes, so this is the shape a real X11
        # session actually has -- and CompositeBackend had no
        # pointer_position to forward, so _origin's getattr() came back
        # None and every glide/drag with no prior move_mouse raised
        # "POINTER_QUERY is unavailable" on a session whose own
        # supports(POINTER_QUERY) said otherwise.
        from pyguitest.backends.composite import CompositeBackend

        member = QueryingBackend()
        empty = RecordingBackend(CapabilitySet(set()))
        gui = session(CompositeBackend([empty, member]))
        self.assertTrue(gui.supports(Capability.POINTER_QUERY))
        gui.glide(7, 109, duration=0.02, rate=200)
        self.assertTrue(all(x == 7 for x, _ in member.moves))


class TestDrag(unittest.TestCase):
    def test_motion_happens_between_the_press_and_the_release(self):
        # Press, teleport, release is a click at the destination in both GTK
        # and Qt -- the motion in between is what makes it a drag.
        gui = session()
        gui.drag((0, 0), (100, 100), duration=0.02, rate=400, settle=0)
        kinds = gui.backend.kinds
        self.assertEqual(kinds[0], "move")
        self.assertEqual(kinds[1], "press")
        self.assertEqual(kinds[-1], "release")
        self.assertGreater(kinds[2:-1].count("move"), 1)

    def test_the_press_lands_on_the_start_and_the_release_on_the_end(self):
        gui = session()
        gui.drag((11, 22), (88, 99), duration=0.02, rate=400, settle=0)
        moves = gui.backend.moves
        self.assertEqual(moves[0], (11, 22))
        self.assertEqual(moves[-1], (88, 99))

    def test_settle_pauses_around_the_button_events(self):
        gui = session()
        began = time.monotonic()
        gui.drag((0, 0), (10, 10), duration=0.01, rate=100, settle=0.03)
        self.assertGreaterEqual(time.monotonic() - began, 0.08)

    def test_the_button_is_released_even_on_a_non_default_one(self):
        gui = session()
        gui.drag((0, 0), (10, 10), button=3, duration=0.01, rate=100, settle=0)
        buttons = [e[1] for e, _ in gui.backend.events if e[0] in ("press", "release")]
        self.assertEqual(buttons, [3, 3])

    def test_waypoints_route_the_drag(self):
        gui = session()
        gui.drag((0, 0), (100, 0), via=[(50, 60)], duration=0.02, rate=400, settle=0)
        self.assertTrue(any(y > 30 for _, y in gui.backend.moves))


class TestArgumentChecking(unittest.TestCase):
    def test_a_negative_duration_is_rejected(self):
        with self.assertRaises(ValueError):
            session().glide(1, 1, start=(0, 0), duration=-1)

    def test_a_rate_of_zero_is_rejected(self):
        with self.assertRaises(ValueError):
            session().glide(1, 1, start=(0, 0), rate=0)

    def test_a_negative_settle_is_rejected(self):
        with self.assertRaises(ValueError):
            session().drag((0, 0), (1, 1), settle=-0.1)

    def test_a_backend_without_pointer_move_still_refuses(self):
        gui = session(RecordingBackend(CapabilitySet({Capability.POINTER_BUTTON})))
        with self.assertRaises(CapabilityUnsupported):
            gui.glide(10, 10, start=(0, 0), duration=0.001)


if __name__ == "__main__":
    unittest.main()
