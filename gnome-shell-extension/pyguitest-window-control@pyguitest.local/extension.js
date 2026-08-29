// pyguitest window control -- exposes Meta.Window operations over D-Bus so
// pyguitest's GnomeShellBackend can drive them from outside the shell
// process.
//
// Validated against a live GNOME Shell 50.4 on 2026-08-25 with
// scripts/validate-gnome-extension.sh. That first live run found two bugs
// this file had carried since it was written from the headers alone:
// WindowAtPoint assumed list_all_windows() was in stacking order, and
// partial move/resize filled the untouched axes from a frame rect that an
// in-flight async move had not yet updated. Both are fixed below, and both
// were invisible to the fake-proxy unit tests, which reimplement the same
// logic in Python and so agree with whatever it does.
//
// Runs inside the gnome-shell process, which already owns the "org.gnome.Shell"
// bus name -- so this exports a sub-path on the existing session bus
// connection rather than owning a name of its own.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const OBJECT_PATH = '/org/gnome/Shell/Extensions/Pyguitest';

// How long a requested-but-not-yet-applied geometry stays authoritative
// for filling in the axes of a later partial move/resize. Long enough to
// cover Mutter applying an async move_resize_frame(), short enough that a
// window the user drags by hand is not yanked back. See MoveResizeWindow.
const PENDING_USEC = 1000 * 1000; // 1s, in microseconds

// Window tuple: (id, pid, title, x, y, width, height, minimized, focused).
// id is Meta.Window's own stable_sequence -- a monotonically increasing
// integer Mutter assigns per window and keeps stable for its lifetime,
// unlike an X11 id, which is only meaningful for windows that have one.
const IFACE_XML = `
<node>
  <interface name="org.gnome.Shell.Extensions.Pyguitest">
    <method name="ListWindows">
      <arg type="a(uisiiiibb)" direction="out" name="windows"/>
    </method>
    <method name="MoveResizeWindow">
      <arg type="u" direction="in" name="id"/>
      <arg type="i" direction="in" name="x"/>
      <arg type="i" direction="in" name="y"/>
      <arg type="i" direction="in" name="width"/>
      <arg type="i" direction="in" name="height"/>
      <arg type="b" direction="in" name="moveX"/>
      <arg type="b" direction="in" name="moveY"/>
      <arg type="b" direction="in" name="resizeW"/>
      <arg type="b" direction="in" name="resizeH"/>
      <arg type="b" direction="out" name="ok"/>
    </method>
    <method name="ActivateWindow">
      <arg type="u" direction="in" name="id"/>
      <arg type="b" direction="out" name="ok"/>
    </method>
    <method name="MinimizeWindow">
      <arg type="u" direction="in" name="id"/>
      <arg type="b" direction="in" name="minimize"/>
      <arg type="b" direction="out" name="ok"/>
    </method>
    <method name="WindowAtPoint">
      <arg type="i" direction="in" name="x"/>
      <arg type="i" direction="in" name="y"/>
      <arg type="u" direction="out" name="id"/>
    </method>
    <method name="CaptureWindow">
      <arg type="u" direction="in" name="id"/>
      <arg type="s" direction="in" name="path"/>
      <arg type="b" direction="out" name="ok"/>
      <arg type="s" direction="out" name="error"/>
    </method>
  </interface>
</node>`;

class PyguitestService {
    constructor() {
        // id -> last geometry we asked Mutter for, plus when we asked.
        this._pending = new Map();
    }

    _findWindow(id) {
        for (const window of global.display.list_all_windows()) {
            if (window.get_stable_sequence() === id)
                return window;
        }
        return null;
    }

    ListWindows() {
        const result = [];
        for (const window of global.display.list_all_windows()) {
            // override-redirect windows (tooltips, menus, ...) have no
            // frame and are not toplevels a caller means by "a window".
            if (window.is_override_redirect())
                continue;
            const rect = window.get_frame_rect();
            result.push([
                window.get_stable_sequence(),
                window.get_pid(),
                window.get_title() ?? '',
                rect.x,
                rect.y,
                rect.width,
                rect.height,
                window.minimized,
                window.has_focus(),
            ]);
        }
        return result;
    }

    MoveResizeWindow(id, x, y, width, height, moveX, moveY, resizeW, resizeH) {
        const window = this._findWindow(id);
        if (!window)
            return false;
        // Only the axes the caller actually asked to change move at all --
        // matches move_window/resize_window's contract on every other
        // backend: a pure resize must not also silently reposition, and
        // vice versa.
        //
        // The unchanged axes have to be filled in from somewhere, and the
        // live frame rect is not safe on its own: move_resize_frame() is
        // asynchronous, so a move immediately followed by a resize would
        // read a frame rect that still holds the *pre-move* position and
        // re-assert it, silently undoing the move. Observed on GNOME 50:
        // move to (320,288) then resize left the window at (337,301).
        //
        // So prefer the geometry we last *asked* for while the window has
        // not reached it yet. The staleness cutoff keeps a window the user
        // has since dragged by hand from being yanked back: past it, or
        // once the window has settled where we asked, the live rect wins.
        //
        // This closes the two-call case above but is not a full fix for
        // rapid-fire sequences: move_resize_frame() ultimately proposes
        // geometry via an xdg_toplevel configure event, which the
        // window's own client must ack and commit a buffer for -- a
        // round trip nothing on the compositor side can shorten. Four
        // calls with no gap measurably lost 3-4 of 4 on this system;
        // giving each ~200ms to settle recovered 4/4. See
        // GnomeShellBackend.move_window's docstring in pyguitest.
        const live = window.get_frame_rect();
        const pending = this._pending.get(id);
        const settled =
            pending &&
            live.x === pending.x && live.y === pending.y &&
            live.width === pending.width && live.height === pending.height;
        const fresh =
            pending && GLib.get_monotonic_time() - pending.at < PENDING_USEC;
        const base = (fresh && !settled) ? pending : live;

        const newX = moveX ? x : base.x;
        const newY = moveY ? y : base.y;
        const newW = resizeW ? width : base.width;
        const newH = resizeH ? height : base.height;
        window.move_resize_frame(true, newX, newY, newW, newH);
        const now = GLib.get_monotonic_time();
        // Prune while we are here: entries are only meaningful inside
        // PENDING_USEC, and closed windows would otherwise accumulate.
        for (const [key, value] of this._pending) {
            if (now - value.at >= PENDING_USEC)
                this._pending.delete(key);
        }
        this._pending.set(id, {
            x: newX, y: newY, width: newW, height: newH, at: now,
        });
        return true;
    }

    ActivateWindow(id) {
        const window = this._findWindow(id);
        if (!window)
            return false;
        window.activate(global.get_current_time());
        return true;
    }

    MinimizeWindow(id, minimize) {
        const window = this._findWindow(id);
        if (!window)
            return false;
        if (minimize)
            window.minimize();
        else
            window.unminimize();
        return true;
    }

    CaptureWindow(id, path) {
        // id 0 is a capability probe, not a window: 0 is never a real
        // stable_sequence, and the alternative -- a caller discovering
        // that this shell cannot capture only when a real screenshot
        // fails -- is exactly the late, confusing failure the rest of
        // this project exists to avoid. pyguitest calls this at
        // construction to decide whether to declare WINDOW_CAPTURE.
        const missing = findMissingCaptureApis();
        if (id === 0)
            return [missing.length === 0, missing.join(', ')];
        if (missing.length > 0)
            return [false, `this GNOME Shell lacks ${missing.join(', ')}`];

        // The shell's working directory is not the caller's, so a
        // relative path would be written somewhere neither of them
        // intended. Refusing is better than writing the right image to
        // the wrong place.
        if (!path || !path.startsWith('/'))
            return [false, `path must be absolute, got ${JSON.stringify(path)}`];

        const window = this._findWindow(id);
        if (!window)
            return [false, `no window with id ${id}`];

        const actor = window.get_compositor_private();
        if (!actor)
            return [false, `window ${id} has no actor; it may be unmapped`];

        // get_image() reads the window's own texture, which is why this
        // exists at all: it is the window's content, not whatever is
        // stacked on top of those screen coordinates, and it needs no
        // portal and raises no consent dialog because this code is
        // already inside gnome-shell.
        let surface;
        try {
            surface = actor.get_image(null);
        } catch (e) {
            return [false, `get_image failed: ${e}`];
        }
        if (!surface)
            return [false, `window ${id} has no painted content to capture`];

        try {
            if (typeof surface.writeToPNG !== 'function')
                return [false, 'this GJS build cannot write PNG from a surface'];
            surface.writeToPNG(path);
        } catch (e) {
            return [false, `could not write ${path}: ${e}`];
        } finally {
            if (typeof surface.finish === 'function')
                surface.finish();
        }
        return [true, ''];
    }

    WindowAtPoint(x, y) {
        // Hit-testing has to answer with the *topmost* window at the point,
        // so the candidate list must be in stacking order.
        //
        // list_all_windows() does not give that. Mutter exposes
        // sort_windows_by_stacking() as a separate call precisely because
        // the plain list is in internal order, not stacking order -- an
        // earlier version of this method assumed otherwise and, on a live
        // GNOME 50 session, answered with a background VS Code window for
        // a point that lay inside the focused terminal.
        //
        // sort_windows_by_stacking() returns bottom-to-top, so the topmost
        // match is found by scanning from the end.
        const all = global.display.list_all_windows();
        const windows = typeof global.display.sort_windows_by_stacking === 'function'
            ? global.display.sort_windows_by_stacking(all)
            : all; // older Mutter: unordered, so the answer may be arbitrary
        for (let i = windows.length - 1; i >= 0; i--) {
            const window = windows[i];
            if (window.is_override_redirect() || window.minimized)
                continue;
            const rect = window.get_frame_rect();
            if (
                x >= rect.x && x < rect.x + rect.width &&
                y >= rect.y && y < rect.y + rect.height
            )
                return window.get_stable_sequence();
        }
        return 0; // 0 is never a real stable_sequence; the caller's "not found"
    }
}

// Every Mutter/Shell API this extension calls. metadata.json deliberately
// future-dates shell-version well past the current release, so GNOME's own
// version gate no longer tells us whether we actually work -- this list is
// what does. Checked once at enable() rather than trusting the version
// number, because a version match has never been a real compatibility
// guarantee in either direction.
const REQUIRED_WINDOW_METHODS = [
    'activate',
    'get_compositor_private',
    'get_frame_rect',
    'get_pid',
    'get_stable_sequence',
    'get_title',
    'has_focus',
    'is_override_redirect',
    'minimize',
    'move_resize_frame',
    'unminimize',
];

// Capture needs APIs beyond window control, and a shell can perfectly well
// have one and not the other. They are probed separately so that a shell
// missing only these still gets everything else, rather than the interface
// being withheld entirely.
function findMissingCaptureApis() {
    const missing = [];
    const [sample] = global.display.list_all_windows();
    if (!sample)
        return missing; // nothing to probe against; re-checked per call

    if (typeof sample.get_compositor_private !== 'function') {
        missing.push('Meta.Window.get_compositor_private');
        return missing;
    }
    const actor = sample.get_compositor_private();
    if (!actor)
        return missing; // unmapped sample; says nothing about the API

    if (typeof actor.get_image !== 'function')
        missing.push('Meta.WindowActor.get_image');
    return missing;
}

/**
 * Return a list of missing APIs; empty means this shell is compatible.
 *
 * Probes a real window where one exists, since GJS only materialises
 * GObject-introspected methods on instances, not on the wrapper class.
 * With no windows open there is nothing to probe, so the per-window
 * methods are taken on trust and re-checked on the next enable().
 */
function findMissingApis() {
    const missing = [];

    if (typeof global?.get_current_time !== 'function')
        missing.push('global.get_current_time');
    if (typeof global?.display?.list_all_windows !== 'function')
        missing.push('global.display.list_all_windows');

    if (missing.length > 0)
        return missing; // can't reach a window to probe the rest

    const [sample] = global.display.list_all_windows();
    if (sample) {
        for (const method of REQUIRED_WINDOW_METHODS) {
            if (typeof sample[method] !== 'function')
                missing.push(`Meta.Window.${method}`);
        }
    }
    return missing;
}

export default class PyguitestWindowControlExtension extends Extension {
    enable() {
        const missing = findMissingApis();
        if (missing.length > 0) {
            // Deliberately do NOT export the D-Bus object. Exporting a
            // service whose methods would throw on every call is worse
            // than being absent: pyguitest's GnomeShellBackend already
            // treats a missing object path as BackendUnavailable and
            // degrades to "capability unsupported", which is exactly the
            // right outcome on an incompatible shell.
            logError(new Error(
                `pyguitest-window-control: incompatible GNOME Shell, ` +
                `missing ${missing.join(', ')} -- not exporting ` +
                `${OBJECT_PATH}. The extension needs updating for this ` +
                `shell version.`));
            return;
        }

        this._service = new PyguitestService();
        this._dbusImpl = Gio.DBusExportedObject.wrapJSObject(
            IFACE_XML, this._service);
        this._dbusImpl.export(Gio.DBus.session, OBJECT_PATH);
    }

    disable() {
        this._dbusImpl?.unexport();
        this._dbusImpl = null;
        this._service = null;
    }
}
