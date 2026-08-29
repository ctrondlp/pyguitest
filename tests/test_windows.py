"""Compositor IPC backends, tested against recorded output.

The risk here is parsing, not subprocess: sway nests views inside layout
containers that must not be mistaken for windows, and Hyprland reports geometry
as separate `at` and `size` pairs, and niri reports a position relative to the
workspace view rather than the screen.
"""

import json
import unittest
from unittest import mock

from pyguitest.backends.windows import (
    HyprlandBackend,
    NiriBackend,
    SwayBackend,
    for_compositor,
    for_tool,
)
from pyguitest.capabilities import Capability
from pyguitest.errors import CapabilityUnsupported, WindowNotFound
from pyguitest.session import Compositor

SWAY_TREE = {
    "id": 1,
    "name": "root",
    "nodes": [
        {
            "id": 2,
            "name": "HDMI-1",
            "nodes": [
                {
                    "id": 5,
                    "name": None,
                    "nodes": [  # a split container, not a window
                        {
                            "id": 7,
                            "name": "vim",
                            "app_id": "foot",
                            "pid": 123,
                            "focused": True,
                            "rect": {"x": 0, "y": 0, "width": 960, "height": 1080},
                        },
                        {
                            "id": 8,
                            "name": "Firefox",
                            "app_id": "firefox",
                            "pid": 456,
                            "focused": False,
                            "rect": {"x": 960, "y": 0, "width": 960, "height": 1080},
                        },
                    ],
                },
            ],
            "floating_nodes": [],
        },
    ],
    "floating_nodes": [],
}

SWAY_OUTPUTS = [
    {
        "name": "HDMI-1",
        "scale": 1.0,
        "rect": {"x": 0, "y": 0, "width": 1920, "height": 1080},
    },
    {
        "name": "DP-2",
        "scale": 2.0,
        "rect": {"x": 1920, "y": 0, "width": 2560, "height": 1440},
    },
]

HYPR_CLIENTS = [
    {
        "address": "0x55a1",
        "title": "vim",
        "class": "foot",
        "pid": 123,
        "at": [0, 0],
        "size": [960, 1080],
    },
    {
        "address": "0x55b2",
        "title": "Firefox",
        "class": "firefox",
        "pid": 456,
        "at": [960, 0],
        "size": [960, 1080],
    },
]


class FakeSway:
    """Stands in for a sway IPC transport."""

    def __init__(self, tree=None, outputs=None, events=()):
        self.tree = tree if tree is not None else SWAY_TREE
        self.outputs = outputs if outputs is not None else SWAY_OUTPUTS
        self.events = list(events)
        self.commands = []

    def get_tree(self):
        return self.tree

    def get_outputs(self):
        return self.outputs

    def run_command(self, command):
        self.commands.append(command)
        return []

    def subscribe(self, events=("window",), deadline=None):
        yield from self.events

    def close(self):
        pass


class FakeHyprland:
    def __init__(self, clients=None, active_workspace_id=7):
        self._clients = clients if clients is not None else HYPR_CLIENTS
        self._active_workspace_id = active_workspace_id
        self.dispatched = []

    def clients(self):
        return self._clients

    def active_window(self):
        return self._clients[0]

    def monitors(self):
        return [{"name": "DP-1", "width": 2560, "height": 1440, "scale": 1.0}]

    def active_workspace(self):
        return {"id": self._active_workspace_id}

    def dispatch(self, command):
        self.dispatched.append(command)
        return ""

    def close(self):
        pass


NIRI_WINDOWS = [
    {
        "id": 7,
        "title": "vim",
        "app_id": "foot",
        "pid": 123,
        "workspace_id": 1,
        "is_focused": True,
        "is_floating": False,
        "layout": {
            "pos_in_scrolling_layout": [0, 0],
            "tile_size": [960.0, 1080.0],
            "window_size": [960, 1060],
            "tile_pos_in_workspace_view": [0.0, 0.0],
            "window_offset_in_tile": [0.0, 20.0],
        },
    },
    {
        "id": 8,
        "title": "Firefox",
        "app_id": "firefox",
        "pid": 456,
        # On the second output, so its rectangle is offset by that output's
        # logical origin rather than starting at zero.
        "workspace_id": 2,
        "is_focused": False,
        "is_floating": False,
        "layout": {
            "pos_in_scrolling_layout": [1, 0],
            "tile_size": [960.0, 1080.0],
            "window_size": [960, 1080],
            "tile_pos_in_workspace_view": [0.0, 0.0],
            "window_offset_in_tile": [0.0, 0.0],
        },
    },
    {
        # Scrolled out of view: niri reports no position for it at all.
        "id": 9,
        "title": "Offscreen",
        "app_id": "offscreen",
        "pid": 789,
        "workspace_id": 1,
        "is_focused": False,
        "is_floating": False,
        "layout": {
            "pos_in_scrolling_layout": [2, 0],
            "tile_size": [960.0, 1080.0],
            "window_size": [960, 1080],
            "tile_pos_in_workspace_view": None,
            "window_offset_in_tile": [0.0, 0.0],
        },
    },
]

NIRI_OUTPUTS = {
    "HDMI-1": {
        "name": "HDMI-1",
        "logical": {"x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1.0},
    },
    "DP-2": {
        "name": "DP-2",
        "logical": {"x": 1920, "y": 0, "width": 2560, "height": 1440, "scale": 2.0},
    },
    "eDP-1": {"name": "eDP-1", "logical": None},  # disabled output
}

NIRI_WORKSPACES = [
    {"id": 1, "idx": 1, "output": "HDMI-1"},
    {"id": 2, "idx": 2, "output": "DP-2"},
]


class FakeNiri:
    """Stands in for a niri IPC transport."""

    def __init__(self, windows=None, outputs=None, workspaces=None, events=()):
        self._windows = windows if windows is not None else NIRI_WINDOWS
        self._outputs = outputs if outputs is not None else NIRI_OUTPUTS
        self._workspaces = workspaces if workspaces is not None else NIRI_WORKSPACES
        self.events = list(events)
        self.actions = []

    def windows(self):
        return self._windows

    def outputs(self):
        return self._outputs

    def workspaces(self):
        return self._workspaces

    def action(self, name, **arguments):
        self.actions.append((name, arguments))
        return "Handled"

    def event_stream(self, deadline=None):
        yield from self.events

    def close(self):
        pass


class TestSway(unittest.TestCase):
    def setUp(self):
        self.transport = FakeSway()
        self.gui = SwayBackend(self.transport)

    def test_layout_containers_are_not_windows(self):
        # Node 5 is a split container: it has a null name and no pid.
        windows = self.gui.windows()
        self.assertEqual([w.title for w in windows], ["vim", "Firefox"])
        self.assertNotIn(5, [w.handle for w in windows])

    def test_windows_carry_app_id_and_pid(self):
        vim = self.gui.windows()[0]
        self.assertEqual(vim.app_id, "foot")
        self.assertEqual(vim.pid, 123)

    def test_a_window_with_no_title_yet_is_still_listed(self):
        # Regression: filtering on name too made a window invisible to
        # windows() until it set a title -- exactly the window a
        # wait_for_window caller is racing against.
        tree = {
            "id": 1,
            "name": "root",
            "nodes": [
                {
                    "id": 9,
                    "name": None,
                    "app_id": "starting-app",
                    "pid": 999,
                    "focused": False,
                    "rect": {"x": 0, "y": 0, "width": 100, "height": 100},
                }
            ],
            "floating_nodes": [],
        }
        gui = SwayBackend(FakeSway(tree=tree))
        windows = gui.windows()
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].title, "")
        self.assertEqual(windows[0].pid, 999)

    def test_geometry(self):
        self.assertEqual(self.gui.geometry(self.gui.windows()[1]), (960, 0, 960, 1080))

    def test_geometry_for_unknown_handle(self):
        with self.assertRaises(WindowNotFound):
            self.gui.geometry(9999)

    def test_viewable_defaults_true_when_the_field_is_absent(self):
        # None of the fixture windows set "visible" explicitly -- sway always
        # sends it in practice, but a missing field should not read as hidden.
        self.assertTrue(self.gui.is_window_viewable(self.gui.windows()[0]))

    def test_viewable_reads_the_visible_field(self):
        tree = json.loads(json.dumps(SWAY_TREE))  # deep copy
        hidden_node = tree["nodes"][0]["nodes"][0]["nodes"][0]
        hidden_node["visible"] = False
        gui = SwayBackend(FakeSway(tree=tree))
        self.assertFalse(gui.is_window_viewable(gui.windows()[0]))

    def test_viewable_for_unknown_handle(self):
        with self.assertRaises(WindowNotFound):
            self.gui.is_window_viewable(9999)

    def test_active_window_follows_the_focused_flag(self):
        self.assertEqual(self.gui.active_window().title, "vim")

    def test_hit_test_is_computed_from_geometry(self):
        # No compositor query needed once every rectangle is known.
        self.assertEqual(self.gui.window_at(100, 100).title, "vim")
        self.assertEqual(self.gui.window_at(1000, 100).title, "Firefox")
        self.assertIsNone(self.gui.window_at(5000, 5000))

    def test_hit_test_fetches_the_tree_exactly_once(self):
        # Regression: window_at called geometry() per window, and each
        # geometry() call re-fetched and re-walked the whole tree -- n
        # round trips and O(n^2) parsing for one hit-test.
        calls = []
        original = self.transport.get_tree

        def counting_get_tree():
            calls.append(1)
            return original()

        self.transport.get_tree = counting_get_tree
        self.gui.window_at(100, 100)
        self.assertEqual(len(calls), 1)

    def test_placement_commands_address_the_container(self):
        window = self.gui.windows()[0]
        self.gui.move_window(window, 40, 50)
        self.gui.resize_window(window, 800, 600)
        self.gui.activate_window(window)
        commands = self.transport.commands
        self.assertEqual(len(commands), 3)
        self.assertTrue(all(c.startswith("[con_id=7]") for c in commands))
        self.assertIn("position", commands[0])
        self.assertIn("resize", commands[1])
        self.assertIn("focus", commands[2])

    def test_screens_report_scale(self):
        screens = self.gui.screens()
        self.assertEqual([s.name for s in screens], ["HDMI-1", "DP-2"])
        self.assertEqual(screens[1].size, (2560, 1440))
        self.assertEqual(screens[1].scale, 2.0)

    def test_capabilities_cover_the_audit_gap(self):
        # The capabilities no Wayland protocol provides.
        for cap in (
            Capability.WINDOW_GEOMETRY,
            Capability.WINDOW_PLACEMENT,
            Capability.WINDOW_AT_POINT,
        ):
            self.assertIn(cap, self.gui.capabilities)


NIRI_EVENTS = [
    {"WindowOpenedOrChanged": {"window": dict(NIRI_WINDOWS[0], id=11, title="Editor")}},
    {
        "WindowOpenedOrChanged": {
            "window": dict(NIRI_WINDOWS[0], id=11, title="README.md - Editor")
        }
    },
    {"WindowFocusChanged": {"id": 11}},
    {"WindowFocusChanged": {"id": None}},
    {"WindowClosed": {"id": 11}},
]


class TestNiri(unittest.TestCase):
    def setUp(self):
        self.transport = FakeNiri()
        self.gui = NiriBackend(self.transport)

    def test_windows_carry_title_app_id_and_pid(self):
        windows = self.gui.windows()
        self.assertEqual([w.title for w in windows], ["vim", "Firefox", "Offscreen"])
        self.assertEqual(windows[0].app_id, "foot")
        self.assertEqual(windows[0].pid, 123)
        self.assertEqual(windows[0].handle, 7)

    def test_geometry_adds_the_window_offset_within_its_tile(self):
        # The tile starts at the workspace-view origin, but the window sits
        # 20px down inside it -- a titlebar or border's worth.
        self.assertEqual(self.gui.geometry(7), (0, 20, 960, 1060))

    def test_geometry_is_offset_by_the_output_origin(self):
        # Workspace 2 lives on DP-2, whose logical origin is x=1920, so a
        # window at the workspace-view origin is not at screen x=0.
        self.assertEqual(self.gui.geometry(8), (1920, 0, 960, 1080))

    def test_geometry_of_an_offscreen_window_is_unsupported_not_wrong(self):
        # niri reports null rather than a stale rectangle; inventing one
        # would put a window under the pointer that is not on screen.
        with self.assertRaises(CapabilityUnsupported):
            self.gui.geometry(9)

    def test_geometry_for_unknown_handle(self):
        with self.assertRaises(WindowNotFound):
            self.gui.geometry(9999)

    def test_viewable_tracks_whether_the_window_is_in_view(self):
        self.assertTrue(self.gui.is_window_viewable(7))
        self.assertFalse(self.gui.is_window_viewable(9))

    def test_viewable_for_unknown_handle(self):
        with self.assertRaises(WindowNotFound):
            self.gui.is_window_viewable(9999)

    def test_active_window_follows_the_focused_flag(self):
        self.assertEqual(self.gui.active_window().title, "vim")

    def test_hit_test_skips_windows_with_no_position(self):
        self.assertEqual(self.gui.window_at(100, 100).title, "vim")
        self.assertEqual(self.gui.window_at(2000, 100).title, "Firefox")
        self.assertIsNone(self.gui.window_at(5000, 5000))

    def test_activate_sends_focus_window_by_id(self):
        self.gui.activate_window(self.gui.windows()[1])
        self.assertEqual(self.transport.actions, [("FocusWindow", {"id": 8})])

    def test_resize_sets_each_axis_with_an_absolute_size_change(self):
        self.gui.resize_window(7, 800, 600)
        self.assertEqual(
            self.transport.actions,
            [
                ("SetWindowWidth", {"id": 7, "change": {"SetFixed": 800}}),
                ("SetWindowHeight", {"id": 7, "change": {"SetFixed": 600}}),
            ],
        )

    def test_placement_and_minimize_are_refused_up_front(self):
        # niri is a scrolling tiler: position falls out of the layout and
        # there is no minimize at all. supports() must say so rather than
        # letting the call fail later.
        self.assertNotIn(Capability.WINDOW_PLACEMENT, self.gui.capabilities)
        self.assertNotIn(Capability.WINDOW_MINIMIZE, self.gui.capabilities)
        with self.assertRaises(CapabilityUnsupported):
            self.gui.move_window(7, 10, 10)
        with self.assertRaises(CapabilityUnsupported):
            self.gui.minimize_window(7)
        self.assertEqual(self.transport.actions, [])

    def test_resize_is_supported_even_though_placement_is_not(self):
        self.assertIn(Capability.WINDOW_RESIZE, self.gui.capabilities)

    def test_screens_skip_disabled_outputs_and_report_scale(self):
        screens = self.gui.screens()
        self.assertEqual([s.name for s in screens], ["DP-2", "HDMI-1"])
        self.assertEqual(screens[0].size, (2560, 1440))
        self.assertEqual(screens[0].scale, 2.0)
        self.assertEqual([s.index for s in screens], [0, 1])

    def test_events_map_niri_names_onto_the_shared_verbs(self):
        gui = NiriBackend(FakeNiri(events=NIRI_EVENTS))
        events = list(gui.window_events())
        # The unfocus event carries a null id -- focus moved to a
        # layer-shell surface, which is not a window -- and is dropped.
        self.assertEqual([e.change for e in events], ["new", "title", "focus", "close"])
        self.assertEqual(events[0].window.title, "Editor")
        self.assertEqual(events[1].window.title, "README.md - Editor")

    def test_first_sighting_of_an_id_is_new_and_later_ones_are_title(self):
        # WindowOpenedOrChanged is one event for both cases and niri does
        # not say which; the id is the only thing that distinguishes them.
        gui = NiriBackend(FakeNiri(events=NIRI_EVENTS[:2]))
        self.assertEqual([e.change for e in gui.window_events()], ["new", "title"])

    def test_wait_for_window_returns_an_already_open_window(self):
        self.assertEqual(self.gui.wait_for_window("Fire.*").title, "Firefox")

    def test_wait_for_window_waits_for_one_that_appears(self):
        gui = NiriBackend(FakeNiri(windows=[], events=NIRI_EVENTS))
        self.assertEqual(gui.wait_for_window("README").title, "README.md - Editor")


class TestHyprland(unittest.TestCase):
    def setUp(self):
        self.transport = FakeHyprland()
        self.gui = HyprlandBackend(self.transport)

    def test_windows(self):
        self.assertEqual([w.title for w in self.gui.windows()], ["vim", "Firefox"])

    def test_geometry_joins_at_and_size(self):
        self.assertEqual(self.gui.geometry(self.gui.windows()[1]), (960, 0, 960, 1080))

    def test_hit_test_fetches_clients_exactly_once(self):
        calls = []
        original = self.transport.clients

        def counting_clients():
            calls.append(1)
            return original()

        self.transport.clients = counting_clients
        self.assertEqual(self.gui.window_at(100, 100).title, "vim")
        self.assertEqual(len(calls), 1)

    def test_dispatch_addresses_the_window(self):
        self.gui.activate_window(self.gui.windows()[0])
        self.assertIn("address:0x55a1", self.transport.dispatched[-1])

    def test_viewable_defaults_true_when_fields_are_absent(self):
        self.assertTrue(self.gui.is_window_viewable(self.gui.windows()[0]))

    def test_unmapped_is_not_viewable(self):
        clients = json.loads(json.dumps(HYPR_CLIENTS))
        clients[0]["mapped"] = False
        gui = HyprlandBackend(FakeHyprland(clients=clients))
        self.assertFalse(gui.is_window_viewable(gui.windows()[0]))

    def test_mapped_but_hidden_is_not_viewable(self):
        clients = json.loads(json.dumps(HYPR_CLIENTS))
        clients[0]["mapped"] = True
        clients[0]["hidden"] = True
        gui = HyprlandBackend(FakeHyprland(clients=clients))
        self.assertFalse(gui.is_window_viewable(gui.windows()[0]))

    def test_viewable_for_unknown_handle(self):
        with self.assertRaises(WindowNotFound):
            self.gui.is_window_viewable("0xdead")


class TestParsingFailures(unittest.TestCase):
    def test_for_tool_maps_known_tools_only(self):
        self.assertIsInstance(for_tool("swaymsg"), SwayBackend)
        self.assertIsInstance(for_tool("hyprctl"), HyprlandBackend)
        self.assertIsNone(for_tool("wmctrl"))

    def test_cli_fallback_still_wraps_a_transport(self):
        backend = for_tool("swaymsg", runner=lambda argv: json.dumps(SWAY_TREE))
        self.assertEqual([w.title for w in backend.windows()], ["vim", "Firefox"])


class TestCompositorSelection(unittest.TestCase):
    """for_compositor must pick sway vs Hyprland by environment signature.

    Regression: a Hyprland session with swaymsg merely installed used to
    get a SwayBackend, because for_compositor tried connect_sway() first
    unconditionally and picked whichever transport connected, dispatching
    on hasattr(transport, "get_tree") rather than on which compositor is
    actually running.
    """

    def test_hyprland_signature_selects_hyprland_even_with_swaymsg_present(self):
        env = {"HYPRLAND_INSTANCE_SIGNATURE": "abc123"}
        with mock.patch.dict("os.environ", env, clear=True):
            with mock.patch(
                "pyguitest.ipc.connect_hyprland", return_value=FakeHyprland()
            ):
                # connect_sway must not even be consulted: nothing in this
                # environment claims sway is running.
                with mock.patch(
                    "pyguitest.ipc.connect_sway", side_effect=AssertionError
                ):
                    backend = for_compositor(Compositor.WLROOTS)
        self.assertIsInstance(backend, HyprlandBackend)

    def test_sway_signature_selects_sway(self):
        env = {"SWAYSOCK": "/run/sway.sock"}
        with mock.patch.dict("os.environ", env, clear=True):
            with mock.patch("pyguitest.ipc.connect_sway", return_value=FakeSway()):
                backend = for_compositor(Compositor.WLROOTS)
        self.assertIsInstance(backend, SwayBackend)

    def test_niri_signature_selects_niri(self):
        env = {"NIRI_SOCKET": "/run/niri.sock"}
        with mock.patch.dict("os.environ", env, clear=True):
            with mock.patch("pyguitest.ipc.connect_niri", return_value=FakeNiri()):
                backend = for_compositor(Compositor.WLROOTS)
        self.assertIsInstance(backend, NiriBackend)

    def test_niri_is_not_reached_by_a_sway_session(self):
        # The same regression the sway/Hyprland split covers: a niri
        # transport must not be built for a compositor that is not niri,
        # however many of these tools happen to be installed.
        env = {"SWAYSOCK": "/run/sway.sock"}
        with mock.patch.dict("os.environ", env, clear=True):
            with mock.patch("pyguitest.ipc.connect_sway", return_value=FakeSway()):
                with mock.patch(
                    "pyguitest.ipc.connect_niri", side_effect=AssertionError
                ):
                    backend = for_compositor(Compositor.WLROOTS)
        self.assertIsInstance(backend, SwayBackend)

    def test_no_signature_present_returns_none(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(for_compositor(Compositor.WLROOTS))


if __name__ == "__main__":
    unittest.main()


SUBSCRIBE_EVENTS = [
    {
        "change": "new",
        "container": {
            "id": 11,
            "name": "Untitled - Editor",
            "app_id": "gedit",
            "pid": 900,
        },
    },
    {
        "change": "title",
        "container": {
            "id": 11,
            "name": "README.md - Editor",
            "app_id": "gedit",
            "pid": 900,
        },
    },
    {
        "change": "close",
        "container": {
            "id": 11,
            "name": "README.md - Editor",
            "app_id": "gedit",
            "pid": 900,
        },
    },
]


class TestWindowEvents(unittest.TestCase):
    def setUp(self):
        self.gui = SwayBackend(FakeSway(events=SUBSCRIBE_EVENTS))

    def test_events_are_parsed_in_order(self):
        events = list(self.gui.window_events())
        self.assertEqual([e.change for e in events], ["new", "title", "close"])
        self.assertEqual(events[1].window.title, "README.md - Editor")
        self.assertEqual(events[0].window.pid, 900)

    def test_only_sway_declares_event_support(self):
        self.assertIn(Capability.WINDOW_EVENTS, self.gui.capabilities)
        hypr = HyprlandBackend(FakeHyprland())
        self.assertNotIn(Capability.WINDOW_EVENTS, hypr.capabilities)

    def test_wait_returns_an_already_open_window_without_waiting(self):
        # The race the polling version had: the window opens before the watch
        # starts and is never seen.
        found = self.gui.wait_for_window("Firefox")
        self.assertEqual(found.handle, 8)

    def test_wait_matches_a_window_that_appears_later(self):
        found = self.gui.wait_for_window("Untitled")
        self.assertEqual(found.title, "Untitled - Editor")

    def test_wait_matches_a_title_change(self):
        found = self.gui.wait_for_window(r"README\.md")
        self.assertEqual(found.handle, 11)

    def test_wait_returns_none_when_the_stream_ends_without_a_match(self):
        self.assertIsNone(self.gui.wait_for_window("NoSuchWindow"))

    def test_wait_bounds_the_total_wait_not_just_each_event(self):
        # timeout was previously accepted and never read at all, so a
        # caller waiting on a window that never appears blocked forever.
        found = self.gui.wait_for_window("NoSuchWindow", timeout=0.05)
        self.assertIsNone(found)


class TestSubscriptionUsesASeparateSocket(unittest.TestCase):
    """A live socket transport must not share its connection with a stream.

    Once subscribe() puts a SwaySocket into event-streaming mode, any
    request() on that same connection reads an event frame as its reply.
    window_events() must open a second connection for the socket transport
    rather than reusing the one windows()/geometry() calls still need.
    """

    def setUp(self):
        self.primary = FakeSway(events=SUBSCRIBE_EVENTS)
        self.primary.path = "/run/user/1000/sway-ipc.sock"
        self.gui = SwayBackend(self.primary)

    def test_a_second_socket_is_opened_for_the_subscription(self):
        from unittest import mock

        second = FakeSway(events=SUBSCRIBE_EVENTS)
        second.close = mock.Mock()
        with mock.patch("pyguitest.ipc.SwaySocket", return_value=second) as make_socket:
            list(self.gui.window_events())
        make_socket.assert_called_once_with(path=self.primary.path)
        second.close.assert_called_once()

    def test_falls_back_to_the_shared_transport_if_a_second_socket_fails(self):
        from unittest import mock

        with mock.patch("pyguitest.ipc.SwaySocket", side_effect=OSError("busy")):
            events = list(self.gui.window_events())
        self.assertEqual([e.change for e in events], ["new", "title", "close"])

    def test_a_transport_with_no_path_needs_no_second_connection(self):
        # SwayCLI has no .path -- each subscribe() already spawns its own
        # process, so there is nothing to separate. FakeSway without a path
        # set stands in for that shape here.
        from unittest import mock

        with mock.patch("pyguitest.ipc.SwaySocket") as make_socket:
            list(SwayBackend(FakeSway(events=SUBSCRIBE_EVENTS)).window_events())
        make_socket.assert_not_called()


class TestMinimize(unittest.TestCase):
    def test_sway_uses_the_scratchpad(self):
        transport = FakeSway()
        gui = SwayBackend(transport)
        window = gui.windows()[0]
        gui.minimize_window(window)
        self.assertIn("scratchpad", transport.commands[-1])
        gui.minimize_window(window, minimized=False)
        self.assertIn("show", transport.commands[-1])

    def test_hyprland_uses_a_special_workspace(self):
        transport = FakeHyprland()
        gui = HyprlandBackend(transport)
        gui.minimize_window(gui.windows()[0])
        self.assertIn("special:minimized,", transport.dispatched[-1])

    def test_hyprland_restore_targets_the_resolved_active_workspace(self):
        # Regression: restore used movetoworkspacesilent with "e+0" -- silent
        # moves the window without showing it (backwards for a restore), and
        # "e+0" means "the next empty workspace", not "the current one".
        transport = FakeHyprland(active_workspace_id=3)
        gui = HyprlandBackend(transport)
        gui.minimize_window(gui.windows()[0], minimized=False)
        dispatched = transport.dispatched[-1]
        self.assertTrue(dispatched.startswith("movetoworkspace "))
        self.assertNotIn("movetoworkspacesilent", dispatched)
        self.assertIn("3,", dispatched)

    def test_hyprland_restore_falls_back_when_active_workspace_is_unavailable(self):
        # An older Hyprland without the activeworkspace request should
        # degrade to the previous best-effort form rather than raising.
        transport = FakeHyprland()
        transport.active_workspace = None  # simulate a transport lacking it
        gui = HyprlandBackend(transport)
        gui.minimize_window(gui.windows()[0], minimized=False)
        self.assertIn("e+0,", transport.dispatched[-1])


KDOTOOL = {
    ("kdotool", "search", "."): "{aaa-1}\n{bbb-2}\n",
    ("kdotool", "getwindowname", "{aaa-1}"): "Dolphin\n",
    ("kdotool", "getwindowname", "{bbb-2}"): "Konsole\n",
    ("kdotool", "getactivewindow"): "{bbb-2}\n",
    (
        "kdotool",
        "getwindowgeometry",
        "{aaa-1}",
    ): "Window {aaa-1}\n  Position: 100,200 (screen: 0)\n  Geometry: 800x600\n",
}


class TestKdotool(unittest.TestCase):
    def setUp(self):
        from pyguitest.backends.windows import KdotoolBackend

        self.calls = []

        def runner(argv):
            self.calls.append(argv)
            return KDOTOOL.get(tuple(argv), "")

        self.gui = KdotoolBackend(runner=runner)

    def test_windows_are_listed_by_kwin_uuid(self):
        windows = self.gui.windows()
        self.assertEqual([w.title for w in windows], ["Dolphin", "Konsole"])
        self.assertEqual(windows[0].handle, "{aaa-1}")

    def test_xdotool_style_geometry_text_is_parsed(self):
        self.assertEqual(self.gui.geometry("{aaa-1}"), (100, 200, 800, 600))

    def test_missing_geometry_is_an_error_not_a_silent_zero(self):
        with self.assertRaises(WindowNotFound):
            self.gui.geometry("{nope}")

    def test_active_window(self):
        self.assertEqual(self.gui.active_window().title, "Konsole")

    def test_capabilities_exclude_what_kdotool_cannot_do(self):
        # No pid lookup, no output enumeration, no event subscription.
        self.assertNotIn(Capability.WINDOW_PID, self.gui.capabilities)
        self.assertNotIn(Capability.SCREEN_INFO, self.gui.capabilities)
        self.assertNotIn(Capability.WINDOW_EVENTS, self.gui.capabilities)
        self.assertIn(Capability.WINDOW_GEOMETRY, self.gui.capabilities)

    def test_placement_commands(self):
        self.gui.move_window("{aaa-1}", 10, 20)
        self.assertEqual(
            self.calls[-1], ["kdotool", "windowmove", "{aaa-1}", "10", "20"]
        )
        self.gui.minimize_window("{aaa-1}")
        self.assertIn("windowminimize", self.calls[-1])

    def test_for_tool_now_maps_kdotool(self):
        from pyguitest.backends.windows import KdotoolBackend

        self.assertIsInstance(for_tool("kdotool"), KdotoolBackend)

    def test_viewable_is_refused_not_a_bare_notimplementederror(self):
        # kdotool has no mapped/visibility query, despite declaring
        # WINDOW_STATE for active_window's sake -- the per-operation refusal
        # has to live here, not in withholding the whole capability.
        with self.assertRaises(CapabilityUnsupported):
            self.gui.is_window_viewable("{aaa-1}")
