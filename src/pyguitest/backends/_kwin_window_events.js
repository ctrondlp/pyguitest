// pyguitest window events -- calls back into a D-Bus service KWinEventsBackend
// (backends/kwinevents.py) hosts itself on window create/close/title-change,
// so a caller can subscribe instead of polling kdotool -- kdotool has no
// event-subscription mechanism of its own to poll less crudely, even (no
// `behave`/watch subcommand at all, unlike xdotool).
//
// Loaded ad hoc via org.kde.kwin.Scripting.loadScript()+Script.run() by
// KWinEventsBackend at construction, from wherever pyguitest's package data
// puts it -- not an installed/enabled KWin Script, so there is no manual
// System Settings step, unlike the GNOME Shell extension this mirrors in
// spirit but not in mechanism.
//
// Why calling out rather than exposing a signal, the way the GNOME Shell
// extension in gnome-shell-extension/ does: KWin's scripting API has no
// documented way for a script to register a new D-Bus interface or emit
// its own signal. What it does have, confirmed live via
// org.kde.kwin.Scripting's introspected methods, is:
//   - loadScript(path) / Script.run() -- load and run an arbitrary script
//     file at runtime, no install-and-enable-in-System-Settings step
//     needed (unlike a real installed KWin Script, and unlike the GNOME
//     extension, which does need manual installation).
//   - callDBus(service, path, interface, method, ...args) -- call an
//     EXISTING D-Bus service. Calling out is the well-documented, standard
//     part of the API; exposing a new one is not.
// So the architecture flips relative to the GNOME extension: instead of
// this script being the D-Bus server, a small Python service hosts the
// interface, and this script becomes the *client*, pushing one call per
// event.
//
// Confirmed live, exactly this shape: workspace.windowAdded, .windowRemoved,
// and a per-window captionChanged all fired correctly against a real
// gedit open/close, each callDBus call reaching a minimal Python-hosted
// GDBus service. window.internalId matched kdotool's own window-handle
// format exactly ({xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx}), which is what
// would let a Window this yields interoperate with KdotoolBackend's other
// operations (geometry, activate, ...) through pyguitest's composite --
// CompositeBackend._handle() reads Window.handle directly with no
// ownership check, so the shared UUID format is what makes that safe.
//
// workspace.windowList() -- called as a function -- reliably broke script
// execution in testing here: every statement after it, and even a
// callDBus call placed *before* it in the same script, failed to arrive.
// A bare callDBus and one wrapped in an ordinary function both worked
// fine in isolation, so it was specific to that one call, not scripts in
// general. Enumerating workspace's own property names (`for (var k in
// workspace)`) turned up `windowList` as a real property alongside
// `stackingOrder` -- and stackingOrder, read as a plain property rather
// than called as a function, worked without issue: iterating it and
// reading .internalId/.caption on each entry produced exactly the
// windows a real `kdotool search .` also listed, UUIDs included. Used
// below instead of windowList() for that reason -- not because it is
// semantically the "more correct" choice (stacking order is a superset
// concern this script does not otherwise care about), but because it is
// the one of the two that is confirmed not to crash.
//
// Bus name/path/interface/method here MUST match kwinevents.py's own
// _BUS_NAME/_OBJECT_PATH/_INTERFACE constants exactly, the same way
// gnome-shell-extension/extension.js's OBJECT_PATH must match
// gnomeshell.py's.

var BUS_NAME = "org.pyguitest.KWinEvents";
var OBJECT_PATH = "/WindowEvents";
var INTERFACE = "org.pyguitest.KWinEvents";

function notify(change, id, title) {
    callDBus(BUS_NAME, OBJECT_PATH, INTERFACE, "Notify", change, "" + id, title || "");
}

function watch(window) {
    window.captionChanged.connect(function () {
        notify("title", window.internalId, window.caption);
    });
}

// Windows already open when this script starts get title-change coverage
// too, not just ones created afterward -- see the stackingOrder note above.
var existing = workspace.stackingOrder;
for (var i = 0; i < existing.length; i++) {
    watch(existing[i]);
}

workspace.windowAdded.connect(function (window) {
    watch(window);
    notify("new", window.internalId, window.caption);
});

workspace.windowRemoved.connect(function (window) {
    notify("close", window.internalId, window.caption);
});
