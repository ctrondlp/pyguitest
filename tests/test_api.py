"""The user-facing convenience layer on Session."""

import os
import tempfile
import unittest
from unittest import mock

import pyguitest
from pyguitest import (
    Capability,
    ElementNotFound,
    ImageMatch,
    ImageNotFound,
    Role,
    WindowNotFound,
)
from pyguitest.backends.base import GUIBackend, Window
from pyguitest.capabilities import CapabilitySet


class FakeElement:
    def __init__(self, role, name):
        self.role = role
        self.name = name
        self.clicked = False
        self.text = None
        self.chosen = None
        self.checked = False
        self.enabled = True

    def click(self):
        self.clicked = True

    def set_text(self, text):
        self.text = text

    def choose(self, option):
        self.chosen = option


class FakeBackend(GUIBackend):
    name = "fake"

    def __init__(self):
        self.elements = [
            FakeElement(Role.PUSH_BUTTON, "OK"),
            FakeElement(Role.PUSH_BUTTON, "Cancel"),
            FakeElement(Role.ENTRY, "Name"),
            FakeElement(Role.COMBO_BOX, "Country"),
            FakeElement(Role.CHECK_BOX, "Remember me"),
        ]
        self._windows = [
            Window("a", self, title="Document - Editor"),
            Window("b", self, title="Firefox"),
        ]

    @property
    def capabilities(self):
        return CapabilitySet({Capability.ELEMENT_TREE, Capability.WINDOW_LIST})

    def find_elements(self, role=None, name=None, within=None):
        return [
            e
            for e in self.elements
            if (role is None or e.role == role) and (name is None or e.name == name)
        ]

    def windows(self):
        return self._windows


def session():
    return pyguitest.Session(FakeBackend(), pyguitest.detect())


class TestWindowFinders(unittest.TestCase):
    def test_find_windows_matches_a_regex(self):
        self.assertEqual(len(session().find_windows("Editor")), 1)
        self.assertEqual(len(session().find_windows(".")), 2)

    def test_find_window_returns_the_first_match(self):
        self.assertEqual(session().find_window("Fire").title, "Firefox")

    def test_missing_window_raises_rather_than_returning_none(self):
        with self.assertRaises(WindowNotFound):
            session().find_window("NoSuchApp")


class TestWaitForWindow(unittest.TestCase):
    """The generic fallback: polling find_windows when there is no event feed.

    FakeBackend declares WINDOW_LIST but not WINDOW_EVENTS, so every case
    here exercises the poll loop, not backend delegation -- that path is
    covered separately below.
    """

    def test_returns_immediately_when_already_open(self):
        gui = session()
        window = gui.wait_for_window("Editor", timeout=1, interval=0.01)
        self.assertEqual(window.title, "Document - Editor")

    def test_returns_none_on_timeout_rather_than_raising(self):
        gui = session()
        self.assertIsNone(
            gui.wait_for_window("NoSuchWindow", timeout=0.05, interval=0.01)
        )

    def test_keeps_polling_until_the_window_actually_appears(self):
        gui = session()
        all_windows = gui.backend._windows
        gui.backend._windows = []
        calls = []

        def flaky_windows():
            calls.append(None)
            if len(calls) >= 3:
                gui.backend._windows = all_windows
            return gui.backend._windows

        gui.backend.windows = flaky_windows
        window = gui.wait_for_window("Editor", timeout=2, interval=0.01)
        self.assertEqual(window.title, "Document - Editor")
        self.assertGreaterEqual(len(calls), 3)

    def test_delegates_to_the_backend_when_window_events_is_supported(self):
        class EventBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.asked = None

            @property
            def capabilities(self):
                return CapabilitySet(
                    set(FakeBackend.capabilities.fget(self))
                    | {Capability.WINDOW_EVENTS}
                )

            def wait_for_window(self, title, timeout):
                self.asked = (title, timeout)
                return self.windows()[0]

        gui = pyguitest.Session(EventBackend(), pyguitest.detect())
        window = gui.wait_for_window("Editor", timeout=5)
        self.assertEqual(gui.backend.asked, ("Editor", 5))
        self.assertEqual(window.title, "Document - Editor")


class TestWaitWindowClose(unittest.TestCase):
    def test_returns_true_immediately_if_already_closed(self):
        gui = session()
        ghost = Window("gone", gui.backend, title="Ghost")
        self.assertTrue(gui.wait_window_close(ghost, timeout=1, interval=0.01))

    def test_polls_until_the_window_disappears(self):
        gui = session()
        target = gui.find_window("Editor")
        calls = []

        def flaky_windows():
            calls.append(None)
            if len(calls) >= 3:
                return [w for w in gui.backend._all if w.handle != target.handle]
            return gui.backend._all

        gui.backend._all = gui.backend._windows
        gui.backend.windows = flaky_windows
        self.assertTrue(gui.wait_window_close(target, timeout=2, interval=0.01))
        self.assertGreaterEqual(len(calls), 3)

    def test_returns_false_on_timeout_while_still_open(self):
        gui = session()
        target = gui.find_window("Editor")
        self.assertFalse(gui.wait_window_close(target, timeout=0.05, interval=0.01))

    def test_matches_by_handle_not_by_title(self):
        # Two windows can share a title; a title can also change before the
        # window actually closes. Only the handle identifies it reliably.
        gui = session()
        target = gui.find_window("Editor")
        gui.backend._windows = [w for w in gui.backend._windows if w is not target]
        self.assertTrue(gui.wait_window_close(target, timeout=1, interval=0.01))

    def test_delegates_to_window_events_when_supported(self):
        from pyguitest.backends.windows import WindowEvent

        class EventBackend(FakeBackend):
            @property
            def capabilities(self):
                return CapabilitySet(
                    set(FakeBackend.capabilities.fget(self))
                    | {Capability.WINDOW_EVENTS}
                )

            def window_events(self, timeout=None):
                closing = self._windows[0]
                yield WindowEvent(change="close", window=closing)

        gui = pyguitest.Session(EventBackend(), pyguitest.detect())
        target = gui.find_window("Editor")
        self.assertTrue(gui.wait_window_close(target, timeout=1))


class TestWaitUntil(unittest.TestCase):
    def test_returns_true_immediately_when_predicate_is_already_true(self):
        gui = session()
        self.assertTrue(gui.wait_until(lambda: True, timeout=1, interval=0.01))

    def test_returns_false_on_timeout_rather_than_raising(self):
        gui = session()
        self.assertFalse(gui.wait_until(lambda: False, timeout=0.05, interval=0.01))

    def test_keeps_polling_until_element_state_actually_changes(self):
        gui = session()
        button = gui.button("OK")
        button.enabled = False
        calls = []

        def became_enabled():
            calls.append(None)
            if len(calls) >= 3:
                button.enabled = True
            return button.enabled

        self.assertTrue(gui.wait_until(became_enabled, timeout=2, interval=0.01))
        self.assertGreaterEqual(len(calls), 3)


class TestWaitForElement(unittest.TestCase):
    def test_returns_immediately_when_already_present(self):
        gui = session()
        element = gui.wait_for_element(
            role=Role.PUSH_BUTTON, name="OK", timeout=1, interval=0.01
        )
        self.assertEqual(element.name, "OK")

    def test_returns_none_on_timeout_rather_than_raising(self):
        gui = session()
        self.assertIsNone(
            gui.wait_for_element(name="NoSuchElement", timeout=0.05, interval=0.01)
        )

    def test_keeps_polling_until_the_element_actually_appears(self):
        gui = session()
        all_elements = gui.backend.elements
        gui.backend.elements = [e for e in all_elements if e.name != "OK"]
        calls = []

        def flaky_find_elements(role=None, name=None, within=None):
            calls.append(None)
            if len(calls) >= 3:
                gui.backend.elements = all_elements
            return [
                e
                for e in gui.backend.elements
                if (role is None or e.role == role) and (name is None or e.name == name)
            ]

        gui.backend.find_elements = flaky_find_elements
        element = gui.wait_for_element(
            role=Role.PUSH_BUTTON, name="OK", timeout=2, interval=0.01
        )
        self.assertEqual(element.name, "OK")
        self.assertGreaterEqual(len(calls), 3)


class TestWaitUntilGone(unittest.TestCase):
    def test_returns_true_immediately_if_already_absent(self):
        gui = session()
        self.assertTrue(
            gui.wait_until_gone(name="NoSuchElement", timeout=1, interval=0.01)
        )

    def test_returns_false_on_timeout_while_still_present(self):
        gui = session()
        self.assertFalse(
            gui.wait_until_gone(
                role=Role.PUSH_BUTTON, name="OK", timeout=0.05, interval=0.01
            )
        )

    def test_polls_until_the_element_actually_disappears(self):
        gui = session()
        calls = []

        def flaky_find_elements(role=None, name=None, within=None):
            calls.append(None)
            elements = gui.backend.elements
            if len(calls) >= 3:
                elements = [e for e in elements if e.name != "OK"]
            return [
                e
                for e in elements
                if (role is None or e.role == role) and (name is None or e.name == name)
            ]

        gui.backend.find_elements = flaky_find_elements
        self.assertTrue(
            gui.wait_until_gone(
                role=Role.PUSH_BUTTON, name="OK", timeout=2, interval=0.01
            )
        )
        self.assertGreaterEqual(len(calls), 3)


class FakeImageBackend(GUIBackend):
    name = "fake-image"

    def __init__(self, match=None, geometry_result=(10, 20, 100, 50)):
        self.captured_paths = []
        self.capture_calls = []
        self.geometry_calls = []
        self.locate_calls = []
        self._match = match
        self._geometry_result = geometry_result

    @property
    def capabilities(self):
        return CapabilitySet(
            {
                Capability.SCREEN_CAPTURE,
                Capability.WINDOW_GEOMETRY,
                Capability.IMAGE_LOCATE,
            }
        )

    def capture(self, window=None, path=None, region=None):
        if path is None:
            descriptor, path = tempfile.mkstemp(suffix=".png")
            os.close(descriptor)
        else:
            open(path, "wb").close()
        self.captured_paths.append(path)
        self.capture_calls.append({"window": window, "region": region})
        return path

    def geometry(self, window):
        self.geometry_calls.append(window)
        return self._geometry_result

    def locate(self, haystack, template, region=None, **kwargs):
        self.locate_calls.append((haystack, template, region))
        return self._match


class TestLocateImage(unittest.TestCase):
    def test_raises_when_nothing_matches(self):
        backend = FakeImageBackend(match=None)
        gui = pyguitest.Session(backend, pyguitest.detect())
        with self.assertRaises(ImageNotFound):
            gui.locate_image("/tmp/button.png")

    def test_returns_the_match_when_found(self):
        match = ImageMatch(x=1, y=2, width=3, height=4, score=0.0)
        backend = FakeImageBackend(match=match)
        gui = pyguitest.Session(backend, pyguitest.detect())
        self.assertIs(gui.locate_image("/tmp/button.png"), match)

    def test_looks_up_window_geometry_when_within_is_given(self):
        backend = FakeImageBackend(
            match=ImageMatch(0, 0, 1, 1, 0.0), geometry_result=(5, 6, 7, 8)
        )
        gui = pyguitest.Session(backend, pyguitest.detect())
        window = Window("h", backend, title="Some Window")
        gui.locate_image("/tmp/button.png", within=window)
        self.assertEqual(backend.geometry_calls, [window])
        self.assertEqual(backend.locate_calls[0][2], (5, 6, 7, 8))

    def test_no_geometry_lookup_when_within_is_omitted(self):
        backend = FakeImageBackend(match=ImageMatch(0, 0, 1, 1, 0.0))
        gui = pyguitest.Session(backend, pyguitest.detect())
        gui.locate_image("/tmp/button.png")
        self.assertEqual(backend.geometry_calls, [])
        self.assertIsNone(backend.locate_calls[0][2])

    def test_captured_screenshot_is_cleaned_up(self):
        backend = FakeImageBackend(match=ImageMatch(0, 0, 1, 1, 0.0))
        gui = pyguitest.Session(backend, pyguitest.detect())
        gui.locate_image("/tmp/button.png")
        self.assertFalse(os.path.exists(backend.captured_paths[0]))


class TestWidgetFinders(unittest.TestCase):
    def setUp(self):
        self.gui = session()

    def test_button_by_label(self):
        self.gui.button("OK").click()
        self.assertTrue(self.gui.backend.elements[0].clicked)

    def test_text_field_accepts_any_text_role(self):
        field = self.gui.text_field("Name")
        field.set_text("Ada")
        self.assertEqual(field.text, "Ada")

    def test_dropdown_choose(self):
        self.gui.dropdown("Country").choose("Norway")
        self.assertEqual(self.gui.backend.elements[3].chosen, "Norway")

    def test_checkbox(self):
        self.assertFalse(self.gui.checkbox("Remember me").checked)

    def test_missing_element_names_what_was_wanted(self):
        with self.assertRaises(ElementNotFound) as ctx:
            self.gui.button("Nope")
        self.assertIn("push button", str(ctx.exception))
        self.assertIn("Nope", str(ctx.exception))

    def test_missing_text_field_has_its_own_message(self):
        with self.assertRaises(ElementNotFound):
            self.gui.text_field("Absent")

    def test_elements_returns_a_list_rather_than_raising(self):
        self.assertEqual(len(self.gui.elements(role=Role.PUSH_BUTTON)), 2)
        self.assertEqual(self.gui.elements(role=Role.SLIDER), [])


class TestRoleVocabulary(unittest.TestCase):
    def test_roles_are_the_atspi_strings(self):
        self.assertEqual(Role.PUSH_BUTTON, "push button")
        self.assertEqual(Role.COMBO_BOX, "combo box")

    def test_grouped_role_tuples(self):
        self.assertIn(Role.DIALOG, Role.WINDOW_ROLES)
        self.assertIn(Role.PASSWORD_TEXT, Role.TEXT_ROLES)
        self.assertIn(Role.COMBO_BOX, Role.CHOICE_ROLES)

    def test_role_is_exported_from_the_package(self):
        self.assertIs(pyguitest.Role, Role)


class TestBackendOptions(unittest.TestCase):
    """connect(backend_options=...) reaches the named backend's constructor."""

    def setUp(self):
        from pyguitest import backends

        self.backends = backends
        self.seen = {}

        def factory(environment, **options):
            self.seen.update(options)
            return FakeBackend()

        backends.register(factory, "optiontest", priority=1, opt_in=True)
        self.addCleanup(
            lambda: backends._REGISTRY.remove(
                next(r for r in backends._REGISTRY if r[1] == "optiontest")
            )
        )

    def test_options_reach_the_named_factory(self):
        self.backends.select(None, "optiontest", {"persist_mode": 2})
        self.assertEqual(self.seen, {"persist_mode": 2})

    def test_no_options_still_calls_the_factory_plainly(self):
        self.backends.select(None, "optiontest")
        self.assertEqual(self.seen, {})


if __name__ == "__main__":
    unittest.main()


class TestTierOneAlwaysAvailable(unittest.TestCase):
    """Regression from a live run: a real backend hid the tier-1 capabilities.

    Session implements process launch and timing itself, so they must be
    reported regardless of which backend was selected -- previously they showed
    as unsupported while run_app() worked perfectly.
    """

    def test_reported_even_when_the_backend_omits_them(self):
        gui = session()
        self.assertNotIn(Capability.PROCESS_LAUNCH, gui.backend.capabilities)
        self.assertTrue(gui.supports(Capability.PROCESS_LAUNCH))
        self.assertTrue(gui.supports(Capability.TIMING))

    def test_and_they_actually_work(self):
        self.assertEqual(session().run_app(["true"]).returncode, 0)

    def test_require_accepts_them(self):
        session().require(Capability.PROCESS_LAUNCH, Capability.TIMING)

    def test_capabilities_is_the_union_not_a_replacement(self):
        gui = session()
        self.assertIn(Capability.ELEMENT_TREE, gui.capabilities)
        self.assertIn(Capability.PROCESS_LAUNCH, gui.capabilities)


class TestPublicExports(unittest.TestCase):
    def test_element_not_found_is_in_all(self):
        # Regression: raised by five Session methods and imported directly
        # in examples/03_widgets.py, but omitted from __all__ while its five
        # sibling exceptions were listed.
        self.assertIn("ElementNotFound", pyguitest.__all__)


class TestScreenshot(unittest.TestCase):
    """Session.screenshot: the caller-invoked capture."""

    def setUp(self):
        self.backend = FakeImageBackend()
        self.gui = pyguitest.Session(self.backend, pyguitest.detect())

    def _tempname(self):
        descriptor, path = tempfile.mkstemp(suffix=".png")
        os.close(descriptor)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def test_no_arguments_captures_the_whole_desktop(self):
        path = self.gui.screenshot()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        self.assertEqual(self.backend.capture_calls, [{"window": None, "region": None}])

    def test_a_path_is_passed_through_and_returned(self):
        path = self._tempname()
        self.assertEqual(self.gui.screenshot(path=path), path)

    def test_a_window_is_passed_to_the_backend(self):
        window = Window("a", self.backend, title="Editor")
        self.gui.screenshot(path=self._tempname(), window=window)
        self.assertEqual(self.backend.capture_calls[0]["window"], window)

    def test_a_region_is_passed_to_the_backend(self):
        self.gui.screenshot(path=self._tempname(), region=(1, 2, 3, 4))
        self.assertEqual(self.backend.capture_calls[0]["region"], (1, 2, 3, 4))


class TestCaptureOnFailure(unittest.TestCase):
    """The automatic capture: a screenshot of the moment a test failed.

    Taking it afterwards is too late -- by then the app under test has been
    torn down -- so it has to happen while the exception is still
    propagating, which is the whole reason this is a context manager and not
    a call the caller makes in an except: block.
    """

    def setUp(self):
        self.backend = FakeImageBackend()
        self.gui = pyguitest.Session(self.backend, pyguitest.detect())
        self.directory = tempfile.mkdtemp()

    def tearDown(self):
        for name in os.listdir(self.directory):
            os.unlink(os.path.join(self.directory, name))
        os.rmdir(self.directory)

    def test_nothing_is_captured_when_the_block_succeeds(self):
        with self.gui.capture_on_failure(self.directory):
            pass
        self.assertEqual(self.backend.capture_calls, [])
        self.assertEqual(os.listdir(self.directory), [])

    def test_a_failure_captures_and_re_raises_the_original(self):
        with self.assertRaises(ValueError) as caught:
            with self.gui.capture_on_failure(self.directory):
                raise ValueError("the button was not there")
        self.assertEqual(str(caught.exception), "the button was not there")
        self.assertEqual(len(self.backend.capture_calls), 1)

    def test_the_path_is_attached_to_the_exception(self):
        # So a test runner that prints the exception can lead the reader to
        # the image, rather than the image being written somewhere nobody
        # is told about.
        with self.assertRaises(ValueError) as caught:
            with self.gui.capture_on_failure(self.directory):
                raise ValueError("nope")
        self.assertTrue(os.path.exists(caught.exception.screenshot))
        self.assertTrue(caught.exception.screenshot.endswith(".png"))

    def test_the_file_is_named_after_the_failure_by_default(self):
        with self.assertRaises(KeyError):
            with self.gui.capture_on_failure(self.directory):
                raise KeyError("missing")
        self.assertTrue(os.listdir(self.directory)[0].startswith("KeyError-"))

    def test_an_explicit_name_is_used_instead(self):
        with self.assertRaises(ValueError):
            with self.gui.capture_on_failure(self.directory, name="save-dialog"):
                raise ValueError("nope")
        self.assertTrue(os.listdir(self.directory)[0].startswith("save-dialog-"))

    def test_repeated_failures_do_not_overwrite_each_other(self):
        # A parameterized suite failing the same assertion in several cases
        # would otherwise leave only the last image.
        for _ in range(3):
            with self.assertRaises(ValueError):
                with self.gui.capture_on_failure(self.directory, name="same"):
                    raise ValueError("nope")
        self.assertEqual(len(self.backend.captured_paths), 3)

    def test_the_directory_is_created_if_it_does_not_exist(self):
        nested = os.path.join(self.directory, "artifacts", "run-1")
        with self.assertRaises(ValueError):
            with self.gui.capture_on_failure(nested):
                raise ValueError("nope")
        self.assertTrue(os.path.isdir(nested))
        for name in os.listdir(nested):
            os.unlink(os.path.join(nested, name))
        os.rmdir(nested)
        os.rmdir(os.path.dirname(nested))

    def test_the_environment_variable_is_the_default_directory(self):
        target = os.path.join(self.directory, "from-env")
        with mock.patch.dict(os.environ, {"PYGUITEST_SCREENSHOT_DIR": target}):
            with self.assertRaises(ValueError):
                with self.gui.capture_on_failure():
                    raise ValueError("nope")
        self.assertEqual(len(os.listdir(target)), 1)
        for name in os.listdir(target):
            os.unlink(os.path.join(target, name))
        os.rmdir(target)

    def test_a_window_narrows_the_capture(self):
        window = Window("a", self.backend, title="Editor")
        with self.assertRaises(ValueError):
            with self.gui.capture_on_failure(self.directory, window=window):
                raise ValueError("nope")
        self.assertEqual(self.backend.capture_calls[0]["window"], window)

    def test_a_failing_capture_never_replaces_the_failure_it_documents(self):
        # The important one. A session with no capture backend, an
        # unwritable directory, a display already gone -- none of it is
        # more important than the exception being reported, and all of it
        # happens in the field.
        gui = pyguitest.Session(FakeBackend(), pyguitest.detect())
        with self.assertRaises(ValueError) as caught:
            with gui.capture_on_failure(self.directory):
                raise ValueError("the real problem")
        self.assertEqual(str(caught.exception), "the real problem")
        self.assertIsNone(caught.exception.screenshot)
        self.assertIsInstance(caught.exception.screenshot_error, Exception)
