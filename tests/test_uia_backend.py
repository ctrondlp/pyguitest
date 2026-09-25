"""The UI Automation backend, driven by a fake COM client.

No Windows machine and no `comtypes`: everything the backend calls goes through
`uia._connection`, so what these tests check is the conversation -- which
condition got built, which pattern was asked for, which property was read, what
happens when a provider refuses -- and never whether Windows obeys. That last
part is a list in docs/validation.md rather than something a fake can reach.

The fakes are shaped like the COM objects rather than like this backend's
methods: `FakeElement` answers `FindAll`/`GetCurrentPattern` the way
`IUIAutomationElement` does, `FakeAutomation` builds conditions the way
`IUIAutomation` does, and `_satisfies` evaluates those conditions the way a
provider would. What that buys is a test of the *two-layer* search: the
condition narrows the tree and the predicate decides the answer, and a test can
assert both halves separately.
"""

import re
import unittest
from unittest import mock

import pyguitest
from pyguitest.backends import uia
from pyguitest.capabilities import Capability
from pyguitest.errors import (
    BackendUnavailable,
    CapabilityUnsupported,
    ElementNotActionable,
    ElementNotFound,
    PyGUITestError,
)


class ComFailure(Exception):
    """What a provider's refused HRESULT looks like from Python.

    Deliberately not `comtypes.COMError`: constructing one needs the COM error
    machinery, and what the backend does with it does not depend on which
    exception class it is -- every refusal from a provider is treated the same.
    """


class _Interface:
    """One generated interface class, standing in for the real one.

    Its only job is to be a distinct object per name, so that a pattern fetched
    by one id and asked for another interface's class is refused here the way a
    real `QueryInterface` would refuse it.
    """

    def __init__(self, name):
        """Record the interface name this stands for."""
        self.name = name

    def __repr__(self):
        """The interface's own name, so a failure names the right one."""
        return self.name


class FakePoint:
    """A stand-in for the typelib's `POINT`, to check `_point` prefers it."""

    def __init__(self, x, y):
        """Carry the two coordinates, as the real structure's fields do."""
        self.x = x
        self.y = y


class FakePattern:
    """One control pattern: records its calls, answers its properties."""

    def __init__(self, **state):
        """Start with no calls recorded and whatever state a test passed."""
        self.calls = []
        self.state = state

    def Invoke(self):
        """The button's own "I have been pressed"."""
        self.calls.append("Invoke")

    def Toggle(self):
        """Flip a check box or switch."""
        self.calls.append("Toggle")

    def Select(self):
        """Select a list item, tab or menu entry."""
        self.calls.append("Select")

    def Expand(self):
        """Open a dropdown or tree item."""
        self.calls.append("Expand")

    def Collapse(self):
        """Close one."""
        self.calls.append("Collapse")

    def ScrollIntoView(self):
        """Scroll an off-screen element into view."""
        self.calls.append("ScrollIntoView")

    def DoDefaultAction(self):
        """The MSAA default action, which is usually a click."""
        self.calls.append("DoDefaultAction")

    def SetValue(self, text):
        """Write a value into an editable field."""
        self.calls.append(("SetValue", text))

    def GetText(self, length):
        """Answer a text range's text, as `maxLength` allows."""
        self.calls.append(("GetText", length))
        return self.state.get("text")

    def fail(self, method):
        """Make `method` raise, the way a provider that refuses would."""

        def explode(*_args):
            raise ComFailure(f"0x80004005: {method} failed")

        setattr(self, method, explode)

    @property
    def CurrentToggleState(self):
        """`ToggleState_Off` unless a test said otherwise."""
        return self.state.get("toggle_state", uia.ToggleState_Off)

    @property
    def CurrentIsSelected(self):
        """False unless a test said otherwise."""
        return self.state.get("selected", False)

    @property
    def CurrentExpandCollapseState(self):
        """`ExpandCollapseState_LeafNode` unless a test said otherwise."""
        return self.state.get("expand_collapse_state", uia.ExpandCollapseState_LeafNode)

    @property
    def CurrentValue(self):
        """Whatever a test put there, or None."""
        return self.state.get("value")

    @property
    def DocumentRange(self):
        """The text range a Text pattern hands back."""
        return self.state.get("document")


class PatternHandle:
    """The `IUnknown` a pattern comes back as, before it is asked for a type."""

    def __init__(self, pattern, interface):
        """Carry the pattern object and the one interface it may be asked for."""
        self.pattern = pattern
        self.interface = interface

    def QueryInterface(self, interface):
        """The pattern, if the interface matches the pattern that was fetched."""
        if interface is not self.interface:
            raise ComFailure("0x80004002: E_NOINTERFACE")
        return self.pattern


class FakeArray:
    """`IUIAutomationElementArray`: a `Length` and an indexed `GetElement`."""

    def __init__(self, elements, gone=()):
        """Hold the elements, and the indices that will refuse to be read."""
        self.elements = elements
        self.gone = set(gone)

    @property
    def Length(self):
        """How many elements the search answered with."""
        return len(self.elements)

    def GetElement(self, index):
        """One element, or a failure where it closed before it could be read."""
        if index in self.gone:
            raise ComFailure("0x80040201: UIA_E_ELEMENTNOTAVAILABLE")
        return self.elements[index]


_PROPERTY_READERS = {
    uia.UIA_ControlTypePropertyId: ("CurrentControlType", None),
    uia.UIA_NamePropertyId: ("CurrentName", ""),
    uia.UIA_IsEnabledPropertyId: ("CurrentIsEnabled", True),
    uia.UIA_IsOffscreenPropertyId: ("CurrentIsOffscreen", False),
    uia.UIA_HelpTextPropertyId: ("CurrentHelpText", ""),
}
"""Property id -> (the attribute a provider reads, what a refusal means).

Keyed by this module's own constants rather than by the numbers, so a
transcribed id and the attribute it stands for cannot drift apart here -- the
numbers themselves are pinned by `TestTheTranscribedIds`. The defaults are the
same ones the backend's own predicate uses, which is what keeps the two layers
of a search agreeing about a widget that answers only some questions.
"""


class FakeCondition:
    """One search condition, as a shape a test can assert against.

    Equal to another condition with the same shape, and to the shape itself, so
    a test can write the expected condition out in one expression:
    `("and", ("view",), ("property", 30003, 50000))`.
    """

    def __init__(self, shape):
        """Record the tuple this condition is, e.g. ("property", 30003, 50000)."""
        self.shape = shape

    def __eq__(self, other):
        """Whether `other` is this shape, or a condition that is."""
        if isinstance(other, FakeCondition):
            return self.shape == other.shape
        return self.shape == other

    def __repr__(self):
        """The shape, so a failing assertion prints something readable."""
        return f"Condition({self.shape!r})"


def _read(node, attribute, default):
    """One property, with the answer a provider that refuses it would give."""
    try:
        return getattr(node, attribute)
    except (AttributeError, ComFailure):
        return default


def _satisfies(node, condition):
    """Whether `node` satisfies `condition`, the way a provider would.

    Reads go through `_read`, so a property this fake does not implement is
    answered with the same default the backend's own predicate uses. The
    control-view condition selects everything here: these fakes carry no
    layout-only elements to filter out, so what it means is checked by asserting
    that a search is *combined* with it, not by the answers that come back.
    """
    shape = condition.shape
    if shape[0] in ("true", "view"):
        return True
    if shape[0] == "property":
        attribute, default = _PROPERTY_READERS[shape[1]]
        return _read(node, attribute, default) == shape[2]
    if shape[0] == "and":
        return _satisfies(node, shape[1]) and _satisfies(node, shape[2])
    if shape[0] == "or":
        return _satisfies(node, shape[1]) or _satisfies(node, shape[2])
    raise AssertionError(f"unknown condition {shape!r}")


class FakeElement:
    """One element of the fake tree, with the properties a test gives it.

    Properties are plain attributes, so `getattr` works on them exactly as it
    does on a comtypes pointer, and a test can leave one out entirely -- which
    is how the two-spelling hedge in `_current` is exercised: an element with
    neither `CurrentName` nor a `get_CurrentName()` is one a provider does not
    implement. `getters` holds the other spelling, `failure` makes every read
    raise, and `patterns` is what `GetCurrentPattern` answers from.
    """

    def __init__(self, automation, **properties):
        """Record this element's properties, and everything it has not got."""
        self.automation = automation
        self.children = []
        self.patterns = {}
        self.getters = {}
        self.finds = []
        self.focus_calls = 0
        self.pattern_calls = []
        self.failure = None
        self.find_failure = None
        self.focus_failure = None
        for name, value in properties.items():
            setattr(self, name, value)

    def __getattr__(self, name):
        """Answer the `get_Current*()` spelling, or refuse as a provider does.

        Only reached for names that are not attributes, which is exactly the
        case the backend's second spelling exists for.
        """
        failure = self.__dict__.get("failure")
        if failure is not None:
            raise failure
        getters = self.__dict__.get("getters", {})
        if name.startswith("get_") and name[4:] in getters:
            return getters[name[4:]]
        raise AttributeError(name)

    def add_pattern(self, key, pattern):
        """Publish control pattern `key` here, as a provider would.

        The interface class comes from the name in this module's own table, so a
        pattern fetched by one id and asked for another interface fails inside
        `QueryInterface` -- the one mistake a fake can catch here that a
        permissive provider would quietly survive.
        """
        pattern_id, interface_name = uia._PATTERN_INTERFACES[key]
        interface = getattr(self.automation.client, interface_name)
        self.patterns[pattern_id] = (pattern, interface)
        return pattern

    def GetCurrentPattern(self, pattern_id):
        """The pattern for `pattern_id`, or the failure a provider returns."""
        self.pattern_calls.append(pattern_id)
        if self.failure is not None:
            raise self.failure
        if pattern_id not in self.patterns:
            raise ComFailure(f"0x80004005: pattern {pattern_id} is not supported")
        pattern, interface = self.patterns[pattern_id]
        return PatternHandle(pattern, interface)

    def SetFocus(self):
        """Record a focus attempt, or fail the way a provider would."""
        self.focus_calls += 1
        if self.focus_failure is not None:
            raise self.focus_failure

    def FindAll(self, scope, condition):
        """Every element in `scope` whose properties satisfy `condition`."""
        self.finds.append((scope, condition))
        if self.find_failure is not None:
            raise self.find_failure
        if scope == uia.TreeScope_Children:
            candidates = list(self.children)
        else:
            candidates = list(self.descendants())
        return FakeArray(
            [child for child in candidates if _satisfies(child, condition)]
        )

    def descendants(self):
        """Every element below this one, depth first, this one excluded."""
        for child in self.children:
            yield child
            yield from child.descendants()


class NullPointer:
    """A NULL COM interface pointer: falsy, and emphatically not `None`.

    What COM actually answers where there is no object -- the root's parent,
    a pattern an element does not publish, a point over nothing. ctypes wraps
    address zero in an ordinary Python object and defines truthiness as
    "not NULL", so `if result is None` never fires for one.

    This exists because its absence hid a real bug: every fake here returned
    Python's `None`, so `_parent` at the root looked correct under test and
    returned a null-pointer-wrapping `Element('unknown', '')` against real UI
    Automation on Windows 11. A fake that cannot express the platform's own
    answer cannot test the code that handles it.
    """

    def __bool__(self):
        """False, the way a NULL ctypes pointer is."""
        return False

    def __repr__(self):
        """Recognisable in a failure message."""
        return "<NullPointer>"


class FakeWalker:
    """`IUIAutomationTreeWalker`, as far as `_parent` needs one."""

    def __init__(self, failure=None):
        """Start with no parents known and no failure configured."""
        self.parents = {}
        self.failure = failure

    def GetParentElement(self, node):
        """The parent a test recorded, or a NULL pointer at the root.

        A NULL pointer rather than None, because that is what COM answers --
        see `NullPointer`.
        """
        if self.failure is not None:
            raise self.failure
        return self.parents.get(node) or NullPointer()


class FakeAutomation:
    """`IUIAutomation`, as far as this backend calls it."""

    def __init__(self, client, root=None):
        """Hold the client module and the root every search starts from."""
        self.client = client
        self.walker = FakeWalker()
        self.root = root if root is not None else FakeElement(self)
        self.view = FakeCondition(("view",))
        self.created = []
        self.point_calls = []
        self.point_answer = None
        self.point_failure = None

    @property
    def ControlViewCondition(self):
        """The view condition every search is folded into."""
        return self.view

    @property
    def ControlViewWalker(self):
        """The walker `_parent` asks for a containing element."""
        return self.walker

    def GetRootElement(self):
        """The desktop element, which is this fake tree's root."""
        return self.root

    def CreatePropertyCondition(self, property_id, value):
        """One property condition, recorded so a test can assert the id."""
        return self._record(FakeCondition(("property", property_id, value)))

    def CreateAndCondition(self, first, second):
        """Two conditions requiring both, folded pairwise by the backend."""
        return self._record(FakeCondition(("and", first, second)))

    def CreateOrCondition(self, first, second):
        """Two conditions requiring either."""
        return self._record(FakeCondition(("or", first, second)))

    def CreateTrueCondition(self):
        """The match-everything condition."""
        return self._record(FakeCondition(("true",)))

    def ElementFromPoint(self, point):
        """Record the point and answer with whatever a test put there."""
        self.point_calls.append(point)
        if self.point_failure is not None:
            raise self.point_failure
        return self.point_answer

    def CompareElements(self, first, second):
        """Identity, which is what the real call answers about two pointers."""
        return first is second

    def _record(self, condition):
        """Note a condition as built, and hand it back."""
        self.created.append(condition)
        return condition


class FakeClient:
    """The generated `UIAutomationClient` module, as this backend uses it.

    Any `IUIAutomation*` name the module asks for comes back as its own
    sentinel, so the pattern interfaces need no maintenance here. Anything else
    -- `POINT`, say -- is genuinely absent unless a test puts one in, which is
    how `_point`'s fallback branch is reached.
    """

    def __init__(self, point=None):
        """Optionally carry a typelib `POINT` class for the point test."""
        self.interfaces = {}
        if point is not None:
            self.POINT = point

    def __getattr__(self, name):
        """One sentinel per `IUIAutomation*` interface name; others are absent."""
        if not name.startswith("IUIAutomation"):
            raise AttributeError(name)
        interfaces = self.__dict__.setdefault("interfaces", {})
        return interfaces.setdefault(name, _Interface(name))


class FakeComtypes:
    """`comtypes.client`, recording the module load and the object creation."""

    def __init__(self, module=None, load_failure=None, create_failure=None):
        """Configure what `GetModule` and `CreateObject` should answer."""
        self.module = FakeClient() if module is None else module
        self.load_failure = load_failure
        self.create_failure = create_failure
        self.modules = []
        self.created = []

    def GetModule(self, name):
        """The generated bindings, or the failure a broken typelib gives."""
        self.modules.append(name)
        if self.load_failure is not None:
            raise self.load_failure
        return self.module

    def CreateObject(self, clsid, interface=None):
        """The automation object, or the failure a refused CLSID gives."""
        self.created.append((clsid, interface))
        if self.create_failure is not None:
            raise self.create_failure
        return "the automation object"


class UiaTestCase(unittest.TestCase):
    """A backend wired to a fake COM client, and the helpers to build a tree."""

    real_connection = staticmethod(uia._connection)
    """The module's own `_connection`, captured before `setUp` replaces it.

    `TestAvailability` is a test *of* that function, so it has to call the real
    one while the seam is patched for everything else.
    """

    def setUp(self):
        """Wire the seam to the fakes, then build a backend through it."""
        self.client = FakeClient()
        self.automation = FakeAutomation(self.client)
        patcher = mock.patch.object(uia, "_connection", self._connection)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The type-library cache is module-level state: a test that exercises
        # client_module() would otherwise leave a fake in it for the next one.
        cache = mock.patch.object(uia, "_CLIENT_MODULE", uia._CLIENT_MODULE)
        cache.start()
        self.addCleanup(cache.stop)
        self.backend = uia.UiaBackend()

    def _connection(self):
        """The fake pair `uia._connection` answers with for this test."""
        return self.client, self.automation

    def node(self, control_type=50000, name="", **fields):
        """One element of the fake tree, with the properties a test cares about.

        The defaults make it an enabled, on-screen button with no name, so a
        test spells out only the parts it is about.
        """
        defaults = {
            "CurrentControlType": control_type,
            "CurrentName": name,
            "CurrentIsEnabled": True,
            "CurrentIsOffscreen": False,
            "CurrentHelpText": "",
        }
        defaults.update(fields)
        return FakeElement(self.automation, **defaults)

    def add(self, parent, **properties):
        """One more element under `parent`, which the walker knows about too."""
        child = self.node(**properties)
        parent.children.append(child)
        self.automation.walker.parents[child] = parent
        return child

    def element(self, node, session=None):
        """The wrapper a caller would be handed for `node`."""
        return uia.Element(node, self.backend, session)


class TestAvailability(UiaTestCase):
    """What decides whether a Windows session can have elements at all."""

    def test_available_is_false_where_comtypes_is_missing(self):
        with mock.patch.object(uia, "comtypes_client", lambda: None):
            self.assertFalse(uia.available())

    def test_available_follows_the_type_library_load(self):
        comtypes = FakeComtypes()
        with mock.patch.object(uia, "comtypes_client", lambda: comtypes):
            self.assertTrue(uia.available())
        self.assertEqual(comtypes.modules, ["UIAutomationCore.dll"])

    def test_a_successful_load_is_cached(self):
        comtypes = FakeComtypes()
        with mock.patch.object(uia, "comtypes_client", lambda: comtypes):
            uia.client_module()
            uia.client_module()
        self.assertEqual(len(comtypes.modules), 1)

    def test_a_failed_load_is_not_cached(self):
        # A box that had no UIAutomationCore.dll and has since installed one
        # must be able to answer yes without restarting the process.
        comtypes = FakeComtypes(load_failure=OSError("no typelib"))
        with mock.patch.object(uia, "comtypes_client", lambda: comtypes):
            self.assertIsNone(uia.client_module())
            comtypes.load_failure = None
            self.assertIsNotNone(uia.client_module())

    def test_the_connection_names_the_extra_when_there_is_no_uia(self):
        with mock.patch.object(uia, "comtypes_client", lambda: None):
            with self.assertRaises(BackendUnavailable) as caught:
                self.real_connection()
        self.assertIn("pyguitest[windows]", str(caught.exception))

    def test_the_connection_asks_for_the_documented_clsid_and_interface(self):
        comtypes = FakeComtypes()
        with mock.patch.object(uia, "comtypes_client", lambda: comtypes):
            client, automation = self.real_connection()
        [(clsid, interface)] = comtypes.created
        self.assertEqual(clsid, "{ff48dba4-60ef-4201-aa87-54103eef594e}")
        self.assertIs(interface, client.IUIAutomation)
        self.assertEqual(automation, "the automation object")

    def test_a_refused_create_object_is_a_typed_error(self):
        comtypes = FakeComtypes(create_failure=ComFailure("0x80040154"))
        with mock.patch.object(uia, "comtypes_client", lambda: comtypes):
            with self.assertRaises(BackendUnavailable) as caught:
                self.real_connection()
        self.assertIn("0x80040154", str(caught.exception))

    def test_the_backend_refuses_to_build_without_uia(self):
        def refuse():
            raise BackendUnavailable("no comtypes, no elements")

        with mock.patch.object(uia, "_connection", refuse):
            with self.assertRaises(BackendUnavailable):
                uia.UiaBackend()

    def test_the_thread_is_put_in_an_apartment_before_the_object_is_created(self):
        # COM is per thread and comtypes only initializes the one that imported
        # it, so a Session built on any other thread would reach CreateObject
        # with no apartment and have "CoInitialize has not been called"
        # reported as this machine having no UI Automation at all.
        order = []
        comtypes = FakeComtypes()
        create = comtypes.CreateObject

        def record_create(clsid, interface=None):
            order.append("create")
            return create(clsid, interface=interface)

        comtypes.CreateObject = record_create
        with mock.patch.object(uia, "comtypes_client", lambda: comtypes):
            with mock.patch.object(
                uia, "_initialize_com", lambda: order.append("coinit")
            ):
                self.real_connection()
        self.assertEqual(order, ["coinit", "create"])

    def test_initializing_com_is_silent_where_there_is_no_comtypes(self):
        # Every failure belongs to CreateObject one line later, which reports
        # it with a reason; this call only ever prepares the thread.
        self.assertIsNone(uia._initialize_com())


class TestTheTranscribedIds(unittest.TestCase):
    """The numbers this module transcribes.

    No fake can check these -- they are what a provider on the other side
    compares against -- so they are pinned here against the documentation, and
    docs/validation.md carries the first run that confirms them live. A change
    here is a claim about the SDK, and should be made with uiautomationclient.h
    open.
    """

    def test_the_property_ids_are_the_documented_ones(self):
        self.assertEqual(uia.UIA_BoundingRectanglePropertyId, 30001)
        self.assertEqual(uia.UIA_ProcessIdPropertyId, 30002)
        self.assertEqual(uia.UIA_ControlTypePropertyId, 30003)
        self.assertEqual(uia.UIA_NamePropertyId, 30005)
        self.assertEqual(uia.UIA_IsEnabledPropertyId, 30010)
        self.assertEqual(uia.UIA_HelpTextPropertyId, 30013)
        self.assertEqual(uia.UIA_IsOffscreenPropertyId, 30022)

    def test_the_pattern_ids_are_the_documented_ones(self):
        self.assertEqual(uia.UIA_InvokePatternId, 10000)
        self.assertEqual(uia.UIA_ValuePatternId, 10002)
        self.assertEqual(uia.UIA_RangeValuePatternId, 10003)
        self.assertEqual(uia.UIA_ExpandCollapsePatternId, 10005)
        self.assertEqual(uia.UIA_SelectionItemPatternId, 10010)
        self.assertEqual(uia.UIA_TextPatternId, 10014)
        self.assertEqual(uia.UIA_TogglePatternId, 10015)
        self.assertEqual(uia.UIA_ScrollItemPatternId, 10017)
        self.assertEqual(uia.UIA_LegacyIAccessiblePatternId, 10018)

    def test_the_tree_scopes_and_toggle_states_are_the_documented_ones(self):
        self.assertEqual(uia.TreeScope_Children, 2)
        self.assertEqual(uia.TreeScope_Descendants, 4)
        self.assertEqual(
            (uia.ToggleState_Off, uia.ToggleState_On, uia.ToggleState_Indeterminate),
            (0, 1, 2),
        )

    def test_every_pattern_names_an_interface_the_client_module_has(self):
        client = FakeClient()
        for key, (_pattern_id, interface_name) in uia._PATTERN_INTERFACES.items():
            with self.subTest(pattern=key):
                self.assertIsNotNone(getattr(client, interface_name))

    def test_every_action_names_a_pattern_and_a_method(self):
        for action, (key, method) in uia._ACTIONS.items():
            with self.subTest(action=action):
                self.assertIn(key, uia._PATTERN_INTERFACES)
                self.assertIsInstance(method, str)
                self.assertTrue(method)


class TestTheCapabilitySet(UiaTestCase):
    """What this backend claims, which is one half of Windows support."""

    def test_the_element_capabilities_are_claimed(self):
        for capability in (
            Capability.ELEMENT_TREE,
            Capability.ELEMENT_ACTION,
            Capability.ELEMENT_GEOMETRY,
        ):
            with self.subTest(capability=capability):
                self.assertIn(capability, self.backend.capabilities)

    def test_no_window_capability_is_claimed(self):
        # The ADR's split, and structural rather than cautious: the objects this
        # backend hands back are elements, so a placement or state call granted
        # here would have no window handle to act on.
        claimed = [
            capability.name
            for capability in self.backend.capabilities
            if capability.name.startswith("WINDOW_")
        ]
        self.assertEqual(claimed, [])

    def test_no_input_capability_is_claimed(self):
        for capability in (
            Capability.POINTER_MOVE,
            Capability.POINTER_BUTTON,
            Capability.POINTER_SCROLL,
            Capability.KEY_EVENT,
            Capability.TEXT_ENTRY,
            Capability.INPUT_SYNC,
        ):
            with self.subTest(capability=capability):
                self.assertNotIn(capability, self.backend.capabilities)

    def test_the_dpi_context_is_set_when_the_backend_is_built(self):
        # UIA answers in physical pixels, so a thread that has not said which
        # it is would be reading a virtualised desktop instead.
        calls = []
        with mock.patch.object(
            uia._winapi, "set_thread_dpi_awareness", lambda: calls.append(1)
        ):
            uia.UiaBackend()
        self.assertEqual(calls, [1])

    def test_a_search_after_close_refuses_and_says_which_backend(self):
        self.backend.close()
        with self.assertRaises(CapabilityUnsupported) as caught:
            self.backend.find_elements(role="push button")
        self.assertIn("closed", str(caught.exception))
        self.assertEqual(caught.exception.backend, "uia")

    def test_an_element_the_caller_holds_still_answers_after_close(self):
        # The pointers are the caller's own; only the automation object is gone.
        node = self.node(name="Save")
        element = self.element(node)
        self.backend.close()
        self.assertEqual(element.name, "Save")


class TestFindingElements(UiaTestCase):
    """The search, which is asked twice: once of UIA, once of the predicate."""

    def property_conditions(self):
        """The (id, value) pairs among the conditions built so far."""
        return [
            condition.shape[1:]
            for condition in self.automation.created
            if condition.shape[0] == "property"
        ]

    def condition_kinds(self):
        """The kind of every condition built so far, e.g. ["property", "or"]."""
        return [condition.shape[0] for condition in self.automation.created]

    def searched_condition(self):
        """The one condition the last search handed to `FindAll`."""
        _scope, condition = self.automation.root.finds[-1]
        return condition

    def test_a_role_is_pushed_down_as_a_control_type_condition(self):
        self.automation.root.children.append(self.node(control_type=50000, name="OK"))
        found = self.backend.find_elements(role="push button")
        self.assertEqual([element.name for element in found], ["OK"])
        self.assertIn(
            (uia.UIA_ControlTypePropertyId, 50000), self.property_conditions()
        )

    def test_a_role_that_spans_two_control_types_becomes_an_or(self):
        # `push button` covers Button and SplitButton, `panel` covers Pane and
        # Group; anything else would find only half of what a caller asked for.
        self.automation.root.children.append(
            self.node(control_type=50033, name="Group")
        )
        found = self.backend.find_elements(role="panel")
        self.assertEqual([element.name for element in found], ["Group"])
        self.assertIn(
            (uia.UIA_ControlTypePropertyId, 50026), self.property_conditions()
        )
        self.assertIn(
            (uia.UIA_ControlTypePropertyId, 50033), self.property_conditions()
        )
        self.assertIn("or", self.condition_kinds())

    def test_a_name_is_pushed_down_as_a_property_condition(self):
        self.automation.root.children.append(self.node(name="Save"))
        self.backend.find_elements(name="Save")
        self.assertIn((uia.UIA_NamePropertyId, "Save"), self.property_conditions())

    def test_an_enabled_filter_is_pushed_down(self):
        self.backend.find_elements(enabled=False)
        self.assertIn((uia.UIA_IsEnabledPropertyId, False), self.property_conditions())

    def test_a_visible_filter_asks_the_inverse_property(self):
        # The property is IsOffscreen: asking for visible elements is asking UIA
        # for the ones that are not offscreen.
        for visible, offscreen in ((True, False), (False, True)):
            with self.subTest(visible=visible):
                self.automation.created.clear()
                self.backend.find_elements(visible=visible)
                self.assertIn(
                    (uia.UIA_IsOffscreenPropertyId, offscreen),
                    self.property_conditions(),
                )

    def test_a_description_is_pushed_down_as_help_text(self):
        self.backend.find_elements(description="Saves the file")
        self.assertIn(
            (uia.UIA_HelpTextPropertyId, "Saves the file"), self.property_conditions()
        )

    def test_a_whole_search_is_combined_with_the_control_view(self):
        self.backend.find_elements(role="check box")
        self.assertEqual(
            self.searched_condition().shape,
            ("and", ("view",), ("property", uia.UIA_ControlTypePropertyId, 50002)),
        )

    def test_several_filters_are_folded_pairwise(self):
        # No array of conditions is built: `CreateAndCondition` takes two, and
        # the three-or-more form wants pointers a Python caller cannot pass.
        self.backend.find_elements(role="push button", name="OK", enabled=True)
        outer = self.searched_condition()
        self.assertEqual(outer.shape[0], "and")
        self.assertEqual(
            outer.shape[2].shape, ("property", uia.UIA_IsEnabledPropertyId, True)
        )
        inner = outer.shape[1]
        self.assertEqual(inner.shape[0], "and")
        self.assertEqual(
            inner.shape[2].shape, ("property", uia.UIA_NamePropertyId, "OK")
        )
        both = inner.shape[1]
        self.assertEqual(both.shape[0], "and")
        self.assertIs(both.shape[1], self.automation.view)
        # `push button` is two control types, so the role filter is an or --
        # the deepest fold here, and still two conditions at a time.
        self.assertEqual(
            both.shape[2],
            (
                "or",
                ("property", uia.UIA_ControlTypePropertyId, 50000),
                ("property", uia.UIA_ControlTypePropertyId, 50031),
            ),
        )

    def test_the_scope_is_descendants_and_starts_at_the_root(self):
        self.backend.find_elements(role="push button")
        scope, _condition = self.automation.root.finds[-1]
        self.assertEqual(scope, uia.TreeScope_Descendants)

    def test_a_within_element_is_where_the_search_starts(self):
        parent = self.node()
        self.add(parent, name="Inside")
        found = self.backend.find_elements(within=self.element(parent), name="Inside")
        self.assertEqual([element.name for element in found], ["Inside"])
        self.assertEqual(self.automation.root.finds, [])
        self.assertEqual(len(parent.finds), 1)
        self.assertEqual(parent.finds[0][0], uia.TreeScope_Descendants)

    def test_a_regex_name_is_not_pushed_down_and_still_matches(self):
        # A regex cannot be a property condition, so the search is unnarrowed
        # and the predicate is what picks the answer out.
        self.automation.root.children.append(self.node(name="Save As"))
        self.automation.root.children.append(self.node(name="Delete"))
        found = self.backend.find_elements(name=re.compile("^Save"))
        self.assertEqual([element.name for element in found], ["Save As"])
        self.assertEqual(self.property_conditions(), [])

    def test_a_predicate_is_applied_after_the_search(self):
        self.automation.root.children.append(self.node(name="Delete"))
        self.automation.root.children.append(self.node(name="Keep"))
        found = self.backend.find_elements(
            predicate=lambda element: element.name == "Delete"
        )
        self.assertEqual([element.name for element in found], ["Delete"])

    def test_a_role_uia_cannot_report_finds_nothing_and_says_so(self):
        # UIA has one Text control type, reported as `text`, so a Windows label
        # is found by that name and `label` matches nothing at all.
        self.automation.root.children.append(self.node(control_type=50020, name="Hi"))
        self.assertEqual(self.backend.find_elements(role="label"), [])
        self.assertEqual(self.property_conditions(), [])

    def test_find_element_is_the_first_match(self):
        for name in ("One", "Two"):
            self.automation.root.children.append(self.node(name=name))
        self.assertEqual(self.backend.find_element(name="Two").name, "Two")

    def test_find_element_is_none_when_nothing_matches(self):
        self.assertIsNone(self.backend.find_element(name="Nowhere"))

    def test_an_empty_result_is_an_empty_list_rather_than_an_error(self):
        # "Nothing matched" is an answer; only a refused search raises, which is
        # the distinction the next test is about.
        self.assertEqual(self.backend.find_elements(role="push button"), [])

    def test_a_search_that_is_refused_is_a_typed_error(self):
        self.automation.root.find_failure = ComFailure("0x80004005: provider gone")
        with self.assertRaises(CapabilityUnsupported) as caught:
            self.backend.find_elements(role="push button")
        self.assertIn("could not search", str(caught.exception))

    def test_an_element_that_vanishes_mid_walk_is_skipped(self):
        first, second = self.node(name="One"), self.node(name="Two")
        self.automation.root.FindAll = lambda _scope, _condition: FakeArray(
            [first, second], gone={0}
        )
        found = self.backend.find_elements()
        self.assertEqual([element.name for element in found], ["Two"])

    def test_a_widget_that_answers_half_the_questions_is_not_filtered_out(self):
        # A provider that does not implement IsEnabled has not said "disabled",
        # and both layers of the search take it as enabled -- the narrowing here
        # and the predicate after it.
        node = FakeElement(self.automation, CurrentControlType=50000)
        self.automation.root.children.append(node)
        found = self.backend.find_elements(enabled=True)
        self.assertEqual(len(found), 1)

    def test_a_read_that_a_provider_refuses_falls_back_to_a_safe_answer(self):
        # Every read raises: a stale pointer, or a provider that has stopped.
        node = FakeElement(self.automation, failure=ComFailure("0x80040201"))
        element = self.element(node)
        self.assertEqual(element.name, "")
        self.assertTrue(element.enabled)
        self.assertTrue(element.visible)
        self.assertFalse(element.alive)
        self.assertEqual(element.role, "unknown")


class TestElementState(UiaTestCase):
    """The reads, including the two spellings `_current` exists for."""

    def test_the_name_comes_from_the_element(self):
        self.assertEqual(self.element(self.node(name="Save")).name, "Save")

    def test_a_name_that_is_not_there_reads_as_empty(self):
        node = FakeElement(self.automation, CurrentControlType=50000)
        self.assertEqual(self.element(node).name, "")

    def test_the_getter_spelling_answers_when_there_is_no_attribute(self):
        # comtypes may bind a [propget] as an attribute or as `get_X()`; this
        # is the second, and which one it is cannot be settled from a document.
        node = FakeElement(self.automation, CurrentControlType=50000)
        node.getters["CurrentName"] = lambda: "Save"
        self.assertEqual(self.element(node).name, "Save")

    def test_the_role_is_translated_from_the_control_type(self):
        self.assertEqual(
            self.element(self.node(control_type=50000)).role, "push button"
        )
        self.assertEqual(self.element(self.node(control_type=50010)).role, "menu bar")

    def test_a_control_type_outside_the_table_is_unknown(self):
        self.assertEqual(self.element(self.node(control_type=50999)).role, "unknown")
        self.assertEqual(self.element(self.node(control_type=None)).role, "unknown")

    def test_enabled_and_visible_are_booleans(self):
        self.assertFalse(self.element(self.node(CurrentIsEnabled=False)).enabled)
        self.assertFalse(self.element(self.node(CurrentIsOffscreen=True)).visible)

    def test_the_description_is_the_help_text(self):
        node = self.node(CurrentHelpText="Saves the current file")
        self.assertEqual(self.element(node).description, "Saves the current file")

    def test_text_prefers_the_text_pattern(self):
        node = self.node()
        pattern = node.add_pattern("text", FakePattern())
        pattern.state["document"] = FakePattern(text="the whole paragraph")
        self.assertEqual(self.element(node).text, "the whole paragraph")

    def test_text_falls_back_to_the_value_pattern(self):
        node = self.node()
        node.add_pattern("value", FakePattern(value="typed text"))
        self.assertEqual(self.element(node).text, "typed text")

    def test_an_element_with_neither_pattern_has_no_text(self):
        self.assertIsNone(self.element(self.node()).text)

    def test_value_comes_from_the_range_value_pattern(self):
        node = self.node()
        node.add_pattern("range value", FakePattern(value=42))
        self.assertEqual(self.element(node).value, 42.0)

    def test_a_value_that_is_not_a_number_is_none(self):
        node = self.node()
        node.add_pattern("range value", FakePattern(value="not a number"))
        self.assertIsNone(self.element(node).value)

    def test_an_element_without_a_range_value_pattern_has_no_value(self):
        self.assertIsNone(self.element(self.node()).value)

    def test_checked_follows_the_toggle_state(self):
        for state, expected in (
            (uia.ToggleState_On, True),
            (uia.ToggleState_Off, False),
            # A mixed-state check box has not said "set" or "not set", and True
            # would be a claim about the widget rather than about its answer.
            (uia.ToggleState_Indeterminate, None),
        ):
            with self.subTest(state=state):
                node = self.node()
                node.add_pattern("toggle", FakePattern(toggle_state=state))
                element = self.element(node)
                self.assertEqual(element.checked, expected)
                self.assertTrue(element.checkable)

    def test_an_element_without_a_toggle_pattern_is_not_checkable(self):
        element = self.element(self.node())
        self.assertIsNone(element.checked)
        self.assertFalse(element.checkable)

    def test_selected_follows_the_selection_item_pattern(self):
        node = self.node()
        node.add_pattern("selection item", FakePattern(selected=True))
        element = self.element(node)
        self.assertTrue(element.selected)
        self.assertTrue(element.selectable)

    def test_an_element_that_cannot_be_selected_has_no_selection(self):
        element = self.element(self.node())
        self.assertIsNone(element.selected)
        self.assertFalse(element.selectable)

    def test_expanded_follows_the_expand_collapse_state(self):
        for state, expected in (
            (uia.ExpandCollapseState_Expanded, True),
            (uia.ExpandCollapseState_Collapsed, False),
            # A tree view showing some but not all of a node's children --
            # read as expanded, the way a real user would describe it.
            (uia.ExpandCollapseState_PartiallyExpanded, True),
        ):
            with self.subTest(state=state):
                node = self.node()
                node.add_pattern(
                    "expand collapse", FakePattern(expand_collapse_state=state)
                )
                element = self.element(node)
                self.assertEqual(element.expanded, expected)
                self.assertTrue(element.expandable)

    def test_a_leaf_node_is_not_expandable(self):
        # LeafNode is what a provider answers for "nothing to expand", not a
        # closed disclosure control -- the same None `checked` gives an
        # element with no Toggle pattern at all.
        node = self.node()
        node.add_pattern(
            "expand collapse",
            FakePattern(expand_collapse_state=uia.ExpandCollapseState_LeafNode),
        )
        element = self.element(node)
        self.assertIsNone(element.expanded)
        self.assertFalse(element.expandable)

    def test_an_element_without_an_expand_collapse_pattern_is_not_expandable(self):
        element = self.element(self.node())
        self.assertIsNone(element.expanded)
        self.assertFalse(element.expandable)

    def test_focus_is_read_from_the_element(self):
        self.assertTrue(self.element(self.node(CurrentHasKeyboardFocus=True)).focused)
        self.assertFalse(self.element(self.node()).focused)

    def test_actions_are_the_patterns_that_are_actually_there(self):
        node = self.node()
        node.add_pattern("invoke", FakePattern())
        node.add_pattern("toggle", FakePattern())
        self.assertEqual(self.element(node).actions, ["invoke", "toggle"])
        self.assertEqual(self.element(self.node()).actions, [])

    def test_a_pattern_lookup_is_not_repeated_on_the_same_element(self):
        # `expandable` and `expanded` both ask "does this element publish
        # expand collapse" -- that used to be two separate GetCurrentPattern
        # calls for the identical question, and pyguitest-recorder's
        # ElementRef reads several such pairs off one Element per click
        # (actions checks all seven patterns; expanded/selectable each
        # check one of those seven again). Measured live as part of a real
        # click's total round-trip time, which a native double-click's own
        # synchronous toggle of a tree row was racing and winning. See
        # pyguitest-recorder's status.md.
        node = self.node()
        node.add_pattern(
            "expand collapse",
            FakePattern(expand_collapse_state=uia.ExpandCollapseState_Expanded),
        )
        element = self.element(node)
        self.assertTrue(element.expandable)
        self.assertTrue(element.expanded)
        expand_collapse_id = uia._PATTERN_INTERFACES["expand collapse"][0]
        self.assertEqual(node.pattern_calls.count(expand_collapse_id), 1)

    def test_a_second_element_for_the_same_node_asks_again(self):
        # The cache lives on the Element, not the node -- two separate
        # Elements (as a fresh hit-test on the same point would hand back)
        # must not see each other's answers.
        node = self.node()
        node.add_pattern("toggle", FakePattern(toggle_state=uia.ToggleState_On))
        first = self.element(node)
        self.assertTrue(first.checked)
        second = self.element(node)
        self.assertTrue(second.checked)
        toggle_id = uia._PATTERN_INTERFACES["toggle"][0]
        self.assertEqual(node.pattern_calls.count(toggle_id), 2)

    def test_a_lookup_that_raised_is_asked_again_not_remembered(self):
        # VITAL -- keep this test. Without it the per-Element pattern cache
        # can silently remember a transient COM failure as "no such pattern",
        # and expanded/expand()/checked on that Element then lie for its
        # whole life -- in exactly the double-click race the cache was added
        # for.
        node = self.node()
        node.add_pattern(
            "expand collapse",
            FakePattern(expand_collapse_state=uia.ExpandCollapseState_Collapsed),
        )
        element = self.element(node)
        node.failure = ComFailure("0x80131505: the provider timed out")
        self.assertFalse(element.expandable)
        node.failure = None
        self.assertTrue(element.expandable)
        self.assertFalse(element.expanded)

    def test_a_null_pattern_is_remembered_as_absent(self):
        # VITAL -- keep this test. A NULL pointer is the real Windows answer
        # for a pattern an element does not publish, and it *is* settled: if
        # it stopped being cached, every `actions` read would go back to
        # re-ask all seven patterns, undoing the round-trip saving above.
        node = self.node()
        asked = []

        def null_pattern(pattern_id):
            asked.append(pattern_id)
            return NullPointer()

        node.GetCurrentPattern = null_pattern
        element = self.element(node)
        self.assertFalse(element.expandable)
        self.assertIsNone(element.expanded)
        self.assertEqual(len(asked), 1)

    def test_the_pid_is_read_and_zero_reads_as_unknown(self):
        self.assertEqual(self.element(self.node(CurrentProcessId=4242)).pid, 4242)
        self.assertIsNone(self.element(self.node(CurrentProcessId=0)).pid)

    def test_alive_follows_whether_the_element_answers_at_all(self):
        self.assertTrue(self.element(self.node()).alive)
        stale = FakeElement(self.automation, failure=ComFailure("0x80040201: gone"))
        self.assertFalse(self.element(stale).alive)

    def test_a_child_carries_the_parent_and_the_session(self):
        parent = self.node(name="Window")
        self.add(parent, name="Save")
        session = object()
        element = self.element(parent, session)
        self.assertIsNone(element.parent)
        self.assertEqual([child.name for child in element.children], ["Save"])
        wrapper = element.children[0]
        self.assertIs(wrapper._session, session)
        self.assertIs(wrapper.parent.node, parent)

    def test_the_repr_reads_like_a_locator(self):
        self.assertEqual(
            repr(self.element(self.node(name="Save"))),
            "Element('push button', 'Save')",
        )


class TestElementActions(UiaTestCase):
    """Acting on an element, through its own patterns rather than the pointer."""

    def test_click_prefers_the_invoke_pattern(self):
        node = self.node(name="Save")
        invoke = node.add_pattern("invoke", FakePattern())
        node.add_pattern("toggle", FakePattern())
        self.element(node).click()
        self.assertEqual(invoke.calls, ["Invoke"])

    def test_click_falls_back_to_the_toggle_pattern(self):
        # A check box publishes Toggle and no Invoke, and `atspi.Element.click`
        # reaches it on Linux, so the same role has to keep working here.
        node = self.node(control_type=50002)
        toggle = node.add_pattern("toggle", FakePattern())
        self.element(node).click()
        self.assertEqual(toggle.calls, ["Toggle"])

    def test_click_falls_back_to_the_legacy_default_action(self):
        node = self.node()
        legacy = node.add_pattern("legacy", FakePattern())
        self.element(node).click()
        self.assertEqual(legacy.calls, ["DoDefaultAction"])

    def test_click_with_no_pattern_at_all_is_not_actionable(self):
        node = self.node(name="Kickoff")
        with self.assertRaises(ElementNotActionable) as caught:
            self.element(node).click()
        self.assertEqual(caught.exception.role, "push button")
        self.assertEqual(caught.exception.name, "Kickoff")
        self.assertIn("gui.extents(element)", str(caught.exception))

    def test_click_reports_a_pattern_that_refused_rather_than_trying_another(self):
        # A disabled button's Invoke really did fail, and quietly trying Toggle
        # instead would hide that.
        node = self.node()
        invoke = node.add_pattern("invoke", FakePattern())
        invoke.fail("Invoke")
        node.add_pattern("toggle", FakePattern())
        with self.assertRaises(CapabilityUnsupported) as caught:
            self.element(node).click()
        self.assertIn("invoke pattern failed", str(caught.exception))

    def test_set_text_writes_through_the_value_pattern(self):
        node = self.node()
        value = node.add_pattern("value", FakePattern())
        self.element(node).set_text("hello")
        self.assertEqual(value.calls, [("SetValue", "hello")])

    def test_set_text_without_a_value_pattern_refuses_and_says_why(self):
        with self.assertRaises(CapabilityUnsupported) as caught:
            self.element(self.node()).set_text("hello")
        self.assertIn("read-only", str(caught.exception))

    def test_focus_calls_set_focus(self):
        node = self.node()
        self.element(node).focus()
        self.assertEqual(node.focus_calls, 1)

    def test_focus_that_is_refused_is_a_typed_error(self):
        node = self.node()
        node.focus_failure = ComFailure("0x80004005: not focusable")
        with self.assertRaises(CapabilityUnsupported) as caught:
            self.element(node).focus()
        self.assertIn("could not focus", str(caught.exception))

    def test_select_uses_the_selection_item_pattern(self):
        node = self.node()
        select = node.add_pattern("selection item", FakePattern())
        self.element(node).select()
        self.assertEqual(select.calls, ["Select"])

    def test_select_without_a_selection_pattern_refuses(self):
        with self.assertRaises(CapabilityUnsupported) as caught:
            self.element(self.node()).select()
        self.assertIn("SelectionItem", str(caught.exception))

    def test_expand_calls_expand_on_a_collapsed_node(self):
        node = self.node()
        pattern = node.add_pattern(
            "expand collapse",
            FakePattern(expand_collapse_state=uia.ExpandCollapseState_Collapsed),
        )
        self.element(node).expand()
        self.assertEqual(pattern.calls, ["Expand"])

    def test_expand_is_a_no_op_once_already_expanded(self):
        # Some providers raise calling Expand() on an already-expanded node
        # rather than treating it as a no-op; checked first for that reason,
        # not only to match the at-spi backend's own idempotence.
        node = self.node()
        pattern = node.add_pattern(
            "expand collapse",
            FakePattern(expand_collapse_state=uia.ExpandCollapseState_Expanded),
        )
        self.element(node).expand()
        self.assertEqual(pattern.calls, [])

    def test_expand_without_an_expand_collapse_pattern_refuses(self):
        with self.assertRaises(CapabilityUnsupported) as caught:
            self.element(self.node()).expand()
        self.assertIn("ExpandCollapse", str(caught.exception))

    def test_collapse_calls_collapse_on_an_expanded_node(self):
        node = self.node()
        pattern = node.add_pattern(
            "expand collapse",
            FakePattern(expand_collapse_state=uia.ExpandCollapseState_Expanded),
        )
        self.element(node).collapse()
        self.assertEqual(pattern.calls, ["Collapse"])

    def test_collapse_is_a_no_op_once_already_collapsed(self):
        node = self.node()
        pattern = node.add_pattern(
            "expand collapse",
            FakePattern(expand_collapse_state=uia.ExpandCollapseState_Collapsed),
        )
        self.element(node).collapse()
        self.assertEqual(pattern.calls, [])

    def test_collapse_without_an_expand_collapse_pattern_refuses(self):
        with self.assertRaises(CapabilityUnsupported) as caught:
            self.element(self.node()).collapse()
        self.assertIn("ExpandCollapse", str(caught.exception))

    def test_do_action_performs_a_ui_automation_action(self):
        node = self.node()
        scroll = node.add_pattern("scroll item", FakePattern())
        self.element(node).do_action("scroll into view")
        self.assertEqual(scroll.calls, ["ScrollIntoView"])

    def test_do_action_accepts_at_spis_name_for_invoke(self):
        # A script written against the Linux backend says `click`, and UIA's
        # Invoke pattern is exactly what that means.
        node = self.node()
        invoke = node.add_pattern("invoke", FakePattern())
        self.element(node).do_action("click")
        self.assertEqual(invoke.calls, ["Invoke"])

    def test_do_action_with_an_unknown_name_lists_what_is_offered(self):
        node = self.node()
        node.add_pattern("invoke", FakePattern())
        with self.assertRaises(ValueError) as caught:
            self.element(node).do_action("teleport")
        self.assertIn("invoke", str(caught.exception))

    def test_do_action_that_this_element_does_not_publish_is_not_actionable(self):
        with self.assertRaises(ElementNotActionable) as caught:
            self.element(self.node(name="Cancel")).do_action("select")
        self.assertIn("select", str(caught.exception))

    def test_options_prefers_menu_items(self):
        node = self.node(control_type=50003)
        self.add(node, control_type=50011, name="Cut")
        self.assertEqual(
            [option.name for option in self.element(node).options()], ["Cut"]
        )

    def test_options_falls_back_to_list_items(self):
        node = self.node(control_type=50003)
        self.add(node, control_type=50007, name="First")
        self.assertEqual(
            [option.name for option in self.element(node).options()], ["First"]
        )

    def test_choose_expands_then_selects_the_named_item(self):
        combo = self.node(control_type=50003)
        expand = combo.add_pattern("expand collapse", FakePattern())
        item = self.add(combo, control_type=50007, name="Second")
        select = item.add_pattern("selection item", FakePattern())
        self.element(combo).choose("Second")
        self.assertEqual(expand.calls, ["Expand"])
        self.assertEqual(select.calls, ["Select"])

    def test_choose_clicks_an_item_that_publishes_no_selection_pattern(self):
        combo = self.node(control_type=50003)
        item = self.add(combo, control_type=50011, name="Open")
        invoke = item.add_pattern("invoke", FakePattern())
        self.element(combo).choose("Open")
        self.assertEqual(invoke.calls, ["Invoke"])

    def test_choose_finds_a_popup_item_outside_the_combo_box(self):
        # A UWP or WPF combo box puts its popup beside the window, not inside
        # the box, so the search falls back to the tree root.
        combo = self.node(control_type=50003)
        popup = self.add(self.automation.root, control_type=50009, name="Popup")
        item = self.add(popup, control_type=50011, name="Third")
        select = item.add_pattern("selection item", FakePattern())
        self.element(combo).choose("Third")
        self.assertEqual(select.calls, ["Select"])

    def test_choose_says_so_when_the_option_is_nowhere(self):
        with self.assertRaises(ElementNotFound) as caught:
            self.element(self.node(control_type=50003)).choose("Nowhere")
        self.assertIn("Nowhere", str(caught.exception))


class FakeRect:
    """The `RECT` UIA answers `CurrentBoundingRectangle` with."""

    def __init__(self, left, top, right, bottom):
        """Carry the four fields, as the real structure does."""
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom


class TestGeometry(UiaTestCase):
    """Rectangles and hit-testing, in the one coordinate space there is."""

    def rect(self, node, left, top, right, bottom):
        """Give `node` a screen rectangle and hand it back."""
        node.CurrentBoundingRectangle = FakeRect(left, top, right, bottom)
        return node

    def test_extents_converts_the_rectangle_to_an_origin_and_a_size(self):
        node = self.rect(self.node(), 10, 20, 110, 70)
        self.assertEqual(self.backend.extents(self.element(node)), (10, 20, 100, 50))

    def test_an_empty_rectangle_is_none(self):
        # Everything not laid out answers an empty rectangle; a zero-sized box
        # at the origin would be a plausible-looking lie.
        node = self.rect(self.node(), 0, 0, 0, 0)
        self.assertIsNone(self.backend.extents(self.element(node)))

    def test_a_rectangle_a_provider_refuses_is_none(self):
        node = FakeElement(self.automation, CurrentControlType=50000)
        self.assertIsNone(self.backend.extents(self.element(node)))

    def test_element_at_descends_to_the_deepest_element_covering_the_point(self):
        window = self.rect(
            self.add(self.automation.root, name="Window"), 0, 0, 800, 600
        )
        self.rect(self.add(window, name="Save"), 100, 100, 200, 140)
        self.automation.point_answer = window
        self.assertEqual(self.backend.element_at(150, 120).name, "Save")

    def test_element_at_stops_where_no_child_covers_the_point(self):
        window = self.rect(
            self.add(self.automation.root, name="Window"), 0, 0, 800, 600
        )
        self.rect(self.add(window, name="Save"), 100, 100, 200, 140)
        self.automation.point_answer = window
        self.assertEqual(self.backend.element_at(400, 300).name, "Window")

    def test_element_at_rejects_an_element_that_does_not_cover_the_point(self):
        # A provider reporting a rectangle it does not cover cannot be checked
        # any other way, and an answer that is demonstrably elsewhere is worse
        # than None.
        node = self.rect(self.add(self.automation.root, name="Elsewhere"), 0, 0, 10, 10)
        self.automation.point_answer = node
        self.assertIsNone(self.backend.element_at(500, 500))

    def test_element_at_is_none_where_nothing_claims_the_point(self):
        self.automation.point_answer = None
        self.assertIsNone(self.backend.element_at(1, 1))

    def test_the_documented_race_under_the_point_is_none(self):
        # UIA_E_ELEMENTNOTAVAILABLE: the element under the point was removed
        # before it could be read, which is ordinary on a live desktop.
        self.automation.point_failure = ComFailure("0x80040201")
        self.assertIsNone(self.backend.element_at(1, 1))

    def test_the_point_is_built_from_the_typelib_structure_when_there_is_one(self):
        self.backend._client.POINT = FakePoint
        self.backend.element_at(7, 9)
        point = self.automation.point_calls[-1]
        self.assertIsInstance(point, FakePoint)
        self.assertEqual((point.x, point.y), (7, 9))

    def test_the_point_falls_back_to_this_packages_own_structure(self):
        # A typelib carrying no POINT of its own: `_winapi`'s declaration is
        # the same two LONGs, where a structure of another class is a TypeError.
        self.backend.element_at(7, 9)
        point = self.automation.point_calls[-1]
        self.assertEqual((point.x, point.y), (7, 9))


class FakeSession:
    """A Session, as far as an element needs one: somewhere to delegate to."""

    def __init__(self):
        """Start with nothing double-clicked."""
        self.double_clicked = []

    def double_click_element(self, element):
        """Record the element a caller asked to double-click."""
        self.double_clicked.append(element)


class TestSessionBinding(UiaTestCase):
    """double_click is the pointer's job, and the pointer is the session's."""

    def test_double_click_delegates_to_the_session(self):
        session = FakeSession()
        element = self.element(self.node(name="Save"), session)
        element.double_click()
        self.assertEqual(session.double_clicked, [element])

    def test_double_click_without_a_session_says_where_to_get_one(self):
        element = self.element(self.node(name="Save"))
        with self.assertRaises(PyGUITestError) as caught:
            element.double_click()
        self.assertIn("gui.double_click_element(element)", str(caught.exception))

    def test_a_session_the_binder_is_offered_is_recorded(self):
        session = FakeSession()
        element = self.element(self.node())
        element._bind_session(session)
        self.assertIs(element._session, session)

    def test_an_element_found_while_walking_carries_the_session_on(self):
        session = FakeSession()
        parent = self.node(name="Window")
        self.add(parent, name="Save")
        element = self.element(parent, session)
        self.assertIs(element.children[0]._session, session)
        self.assertIs(element.child(name="Save")._session, session)
        self.assertIs(element.parent, None)
        self.assertIs(element.find(name="Save")[0]._session, session)

    def test_an_element_from_the_backend_directly_has_no_session(self):
        # find_elements hands back what COM gave it; only the wrappers a session
        # or an element make carry one, which is what double_click complains
        # about rather than silently doing nothing.
        self.assertEqual(self.backend.find_elements(), [])
        self.automation.root.children.append(self.node(name="Save"))
        self.assertIsNone(self.backend.find_elements()[0]._session)


class TestTheTree(UiaTestCase):
    """Walking up and down, which is what makes a predicate useful."""

    def test_children_are_searched_in_the_control_view_at_child_scope(self):
        parent = self.node()
        self.add(parent, name="One")
        self.add(parent, name="Two")
        names = [child.name for child in self.element(parent).children]
        self.assertEqual(names, ["One", "Two"])
        scope, condition = parent.finds[-1]
        self.assertEqual(scope, uia.TreeScope_Children)
        self.assertIs(condition, self.automation.view)

    def test_the_root_element_is_the_desktop_element(self):
        self.assertIs(self.backend.root_element().node, self.automation.root)

    def test_the_root_has_no_parent(self):
        # Regression, found live: COM answers the root's parent with a NULL
        # pointer, not None, so `is None` never fired and this came back as
        # Element('unknown', '') -- an element wrapping address zero, whose
        # every property answers its empty default. See uia._null_to_none.
        self.assertIsNone(self.element(self.automation.root).parent)

    def test_a_null_pointer_is_not_mistaken_for_an_element(self):
        # The distinction itself, asserted once where it is legible: a NULL
        # pointer is falsy and is not None, which is the whole trap.
        from pyguitest.backends import uia

        null = NullPointer()
        self.assertIsNotNone(null)
        self.assertFalse(null)
        self.assertIsNone(uia._null_to_none(null))
        self.assertIsNone(uia._null_to_none(None))
        kept = object()
        self.assertIs(uia._null_to_none(kept), kept)

    def test_a_parent_that_will_not_answer_is_none(self):
        parent = self.node()
        child = self.add(parent, name="Save")
        self.automation.walker.failure = ComFailure("0x80004005: gone")
        self.assertIsNone(self.element(child).parent)

    def test_is_ancestor_of_walks_up_comparing_elements(self):
        window = self.node(name="Window")
        button = self.add(window, name="Save")
        element = self.element(window)
        self.assertTrue(element.is_ancestor_of(self.element(button)))
        self.assertFalse(self.element(button).is_ancestor_of(element))

    def test_an_element_off_the_tree_is_nobody_s_descendant(self):
        window = self.node(name="Window")
        stranger = self.node(name="Elsewhere")
        self.assertFalse(self.element(window).is_ancestor_of(self.element(stranger)))


# -- through a session -------------------------------------------------------


class TestThroughASession(UiaTestCase):
    """The backend behind a real Session, which is how a caller reaches it.

    Everything above drives the backend directly. These wrap it in a Session,
    which is where an interface mismatch between Session, the backend protocol
    and `Element` would show up -- `_bind_session`, the widget finders, and the
    wrapper each finder hands back.

    Built with `Session(backend, environment)` rather than
    `connect(backend="uia")` on purpose: the registry is global state, and
    `test_backends.py` swaps it out to exercise composition, so a test that
    needed the registered factory would pass on its own and fail in a full run.
    """

    def session(self):
        """A Session wrapped around this test's fake-backed `uia` backend."""
        return pyguitest.Session(self.backend, pyguitest.detect())

    def test_a_widget_finder_hands_back_a_bound_element(self):
        self.automation.root.children.append(self.node(control_type=50000, name="Save"))
        gui = self.session()
        element = gui.button("Save")
        self.assertEqual(element.name, "Save")
        self.assertIs(element._session, gui)

    def test_a_finder_that_matches_nothing_raises_element_not_found(self):
        gui = self.session()
        with self.assertRaises(ElementNotFound):
            gui.button("Nowhere")

    def test_the_session_reports_what_the_backend_claims(self):
        gui = self.session()
        self.assertTrue(gui.supports(Capability.ELEMENT_TREE))
        self.assertFalse(gui.supports(Capability.POINTER_MOVE))

    def test_extents_and_the_root_element_reach_the_backend(self):
        self.automation.root.children.append(self.node(control_type=50000, name="Save"))
        gui = self.session()
        self.assertEqual(gui.root_element().node, self.automation.root)
        described = gui.element(role="push button", name="Save")
        described.node.CurrentBoundingRectangle = FakeRect(5, 6, 25, 26)
        self.assertEqual(gui.extents(described), (5, 6, 20, 20))

    def test_double_click_without_a_rectangle_says_so(self):
        # The element is the locator: no rectangle means no point to click, and
        # that is reported before any pointer capability is asked about.
        self.automation.root.children.append(self.node(control_type=50000, name="Save"))
        gui = self.session()
        with self.assertRaises(PyGUITestError) as caught:
            gui.button("Save").double_click()
        self.assertIn("no extents", str(caught.exception))

    def test_double_click_through_the_session_needs_the_pointer_backend(self):
        # On a real Windows session the pointer comes from `win32`, not from
        # here, so the refusal is the composition being honest about which half
        # does what -- and it is raised before anything is clicked.
        node = self.node(control_type=50000, name="Save")
        node.CurrentBoundingRectangle = FakeRect(10, 10, 110, 30)
        self.automation.root.children.append(node)
        gui = self.session()
        with self.assertRaises(CapabilityUnsupported) as caught:
            gui.button("Save").double_click()
        self.assertEqual(caught.exception.capability, Capability.POINTER_MOVE)


if __name__ == "__main__":
    unittest.main()
