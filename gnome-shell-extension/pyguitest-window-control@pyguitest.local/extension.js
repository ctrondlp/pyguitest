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
//
// CaptureWindow's Mutter 51 path (_captureModern) is a repair for a live
// regression: Meta.WindowActor.get_image(), the only capture API the file
// above was validated against, is gone as of Mutter 51 with no drop-in
// replacement. paint_to_content()+Shell.Screenshot.composite_to_stream()
// were live-validated against a GNOME Shell 51 beta on 2026-09-02 with
// scripts/validate-gnome-extension.sh's capture check -- it produced a
// real PNG, confirming the API pair works at all. That first run also
// found the composited image was the actor's full allocation, not the
// window: get_buffer_rect() vs get_frame_rect() showed +50px on both
// axes, matching Mutter's invisible CSD-shadow margin. The crop that
// corrects it is live-validated now, on two machines that disagreed --
// see _captureModern, which crops relative to the actor because
// buffer_rect turned out not to describe the painted area everywhere.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Shell from 'gi://Shell';

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
    <signal name="WindowEvent">
      <arg type="s" name="change"/>
      <arg type="u" name="id"/>
      <arg type="s" name="title"/>
    </signal>
  </interface>
</node>`;

class PyguitestService {
    constructor() {
        // id -> last geometry we asked Mutter for, plus when we asked.
        this._pending = new Map();
        // id -> {window, handlerIds}, for the per-window signals watched
        // by startWatching(). Kept so a window's own signal handlers can be
        // disconnected exactly once, whichever comes first: the window
        // closing (we do it ourselves) or the extension being disabled.
        this._watched = new Map();
        this._displayHandlerIds = [];
        // Set by the extension once the D-Bus object is exported -- signals
        // can only be emitted through it, and construction happens first.
        this._dbusImpl = null;
    }

    _emitWindowEvent(change, window) {
        if (!this._dbusImpl)
            return;
        this._dbusImpl.emit_signal('WindowEvent', new GLib.Variant('(sus)', [
            change, window.get_stable_sequence(), window.get_title() ?? '',
        ]));
    }

    _watchWindow(window) {
        const id = window.get_stable_sequence();
        if (this._watched.has(id))
            return;
        // 'unmanaging' fires once, right before Mutter actually tears the
        // window down -- late enough that get_title() above still answers,
        // early enough that it is unambiguously a close and not, say, a
        // move to a different workspace.
        const handlerIds = [
            window.connect('unmanaging', () => {
                this._emitWindowEvent('close', window);
                this._unwatchWindow(id);
            }),
            window.connect('notify::title', () => {
                this._emitWindowEvent('title', window);
            }),
        ];
        this._watched.set(id, {window, handlerIds});
    }

    _unwatchWindow(id) {
        const entry = this._watched.get(id);
        if (!entry)
            return;
        for (const handlerId of entry.handlerIds)
            entry.window.disconnect(handlerId);
        this._watched.delete(id);
    }

    // Called once, after the D-Bus object is exported (see enable()) --
    // not from the constructor, so _emitWindowEvent has somewhere to send
    // to for every window already open at the time.
    startWatching() {
        for (const window of global.display.list_all_windows()) {
            if (!window.is_override_redirect())
                this._watchWindow(window);
        }
        this._displayHandlerIds.push(
            global.display.connect('window-created', (_display, window) => {
                if (window.is_override_redirect())
                    return;
                this._watchWindow(window);
                this._emitWindowEvent('new', window);
            })
        );
    }

    stopWatching() {
        for (const handlerId of this._displayHandlerIds)
            global.display.disconnect(handlerId);
        this._displayHandlerIds = [];
        for (const id of [...this._watched.keys()])
            this._unwatchWindow(id);
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

    // Async because the Mutter >=51 path (_captureModern) is: Meta
    // dropped the synchronous get_image() this used to call, and its
    // replacement only comes as an async callback. wrapJSObject (see
    // enable()) dispatches a `<Name>Async(params, invocation)` method
    // asynchronously and leaves completion to it; id-0 probes and
    // validation failures still reply synchronously, from inside this
    // same call, just via invocation instead of a return value.
    CaptureWindowAsync([id, path], invocation) {
        const reply = (ok, error) =>
            invocation.return_value(new GLib.Variant('(bs)', [ok, error]));

        // id 0 is a capability probe, not a window: 0 is never a real
        // stable_sequence, and the alternative -- a caller discovering
        // that this shell cannot capture only when a real screenshot
        // fails -- is exactly the late, confusing failure the rest of
        // this project exists to avoid. pyguitest calls this at
        // construction to decide whether to declare WINDOW_CAPTURE.
        const missing = findMissingCaptureApis();
        if (id === 0) {
            reply(missing.length === 0, missing.join(', '));
            return;
        }
        if (missing.length > 0) {
            reply(false, `this GNOME Shell lacks ${missing.join(', ')}`);
            return;
        }

        // The shell's working directory is not the caller's, so a
        // relative path would be written somewhere neither of them
        // intended. Refusing is better than writing the right image to
        // the wrong place.
        if (!path || !path.startsWith('/')) {
            reply(false, `path must be absolute, got ${JSON.stringify(path)}`);
            return;
        }

        const window = this._findWindow(id);
        if (!window) {
            reply(false, `no window with id ${id}`);
            return;
        }

        const actor = window.get_compositor_private();
        if (!actor) {
            reply(false, `window ${id} has no actor; it may be unmapped`);
            return;
        }

        if (typeof actor.get_image === 'function')
            this._captureLegacy(actor, reply, path);
        else
            this._captureModern(window, actor, reply, path);
    }

    // Shell <=50: Meta.WindowActor.get_image() reads the window's own
    // texture straight into a Cairo surface, which is why this path
    // exists at all -- it is the window's content, not whatever is
    // stacked on top of those screen coordinates, and it needs no
    // portal and raises no consent dialog because this code is already
    // inside gnome-shell. This is the path validated against a live
    // shell -- see the file header.
    _captureLegacy(actor, reply, path) {
        let surface;
        try {
            surface = actor.get_image(null);
        } catch (e) {
            reply(false, `get_image failed: ${e}`);
            return;
        }
        if (!surface) {
            reply(false, 'window has no painted content to capture');
            return;
        }

        try {
            if (typeof surface.writeToPNG !== 'function') {
                reply(false, 'this GJS build cannot write PNG from a surface');
                return;
            }
            surface.writeToPNG(path);
        } catch (e) {
            reply(false, `could not write ${path}: ${e}`);
            return;
        } finally {
            if (typeof surface.finish === 'function')
                surface.finish();
        }
        reply(true, '');
    }

    // Mutter >=51 removed get_image with no drop-in replacement; this
    // reassembles the same result from what it does still expose:
    // paint_to_content() renders the actor to a Clutter.Content, whose
    // backing Cogl.Texture composite_to_stream() can encode to PNG,
    // mirroring the pattern gnome-shell's own screenshot service uses
    // internally for the same job. Both the API pair and the crop below
    // are live-validated (2026-09-02 and 2026-09-04, GNOME Shell 51 beta,
    // scripts/validate-gnome-extension.sh's capture check).
    _captureModern(window, actor, reply, path) {
        let content;
        try {
            content = actor.paint_to_content(null);
        } catch (e) {
            reply(false, `paint_to_content failed: ${e}`);
            return;
        }
        if (!content) {
            reply(false, 'window has no painted content to capture');
            return;
        }

        const texture = content.get_texture();
        // paint_to_content() renders the actor's whole allocation, which
        // for a client-side-decorated window is larger than the window
        // itself: get_buffer_rect() includes the invisible margin Mutter
        // reserves for GTK/libadwaita's drop shadow, get_frame_rect() is
        // just the visible frame. Observed live: a 939x537 frame_rect
        // produced an 989x587 (+50 both axes = +25/side) uncropped
        // capture. Scaling the crop by texture-pixels-per-buffer-pixel
        // (not just subtracting screen coordinates) keeps this correct
        // on a HiDPI/fractional-scale monitor too, where the texture has
        // more pixels than the buffer rect has logical units.
        const buffer = window.get_buffer_rect();
        const frame = window.get_frame_rect();
        // Relative to the ACTOR, not to buffer_rect. The texture came from
        // paint_to_content(actor), so the actor's allocation is what it
        // covers -- and buffer_rect is not reliably the same thing. Two
        // machines disagree: on GNOME Shell 51/Fedora buffer_rect included
        // the CSD shadow margin (a 939x537 frame inside a 989x587 buffer),
        // while on a CI runner with software rendering buffer_rect came
        // back *equal* to frame_rect while the texture was still padded --
        // 628x429 around a 600x400 frame, a 28x29 margin, asymmetric the
        // way a drop shadow with a vertical offset is.
        //
        // The old arithmetic divided texture size by buffer size and called
        // it the device-pixel ratio. That is only true while buffer_rect
        // describes the whole painted area; where it equals frame_rect the
        // ratio silently becomes the margin ratio, cropX/cropY come out 0,
        // and the "crop" expands to the entire texture instead of trimming
        // it. That is exactly the 628x429 image the runner produced.
        //
        // Using the actor reduces to the old behaviour wherever buffer_rect
        // was right (the actor sits at the buffer origin there), so the
        // Fedora result this was validated against is unchanged.
        const originX = actor.get_x();
        const originY = actor.get_y();
        const allocW = actor.get_width();
        const allocH = actor.get_height();
        // One scale, from the axis with something to divide by: the ratio
        // is a device-pixel factor and is uniform by definition, so taking
        // it per-axis is what let a margin masquerade as a scale.
        const scale = allocW > 0 ? texture.get_width() / allocW : 1;
        const clamp = (value, limit) => Math.max(0, Math.min(value, limit));
        const cropX = clamp(Math.round((frame.x - originX) * scale),
                            texture.get_width());
        const cropY = clamp(Math.round((frame.y - originY) * scale),
                            texture.get_height());
        const cropW = clamp(Math.round(frame.width * scale),
                            texture.get_width() - cropX);
        const cropH = clamp(Math.round(frame.height * scale),
                            texture.get_height() - cropY);
        // printerr(), not console.log(): GJS routes console.log through
        // structured logging, which lands in the journal and never on
        // stderr -- so the first attempt at this diagnostic produced
        // nothing at all in the log scripts/headless-session.sh keeps.
        // printerr writes straight to stderr, which is what that log is.
        //
        // Kept rather than removed now the crop is fixed: these five
        // numbers are what distinguished the two machines, and a third
        // one disagreeing again is exactly what this needs to report.
        printerr(`pyguitest capture: frame=${frame.width}x${frame.height}` +
            `+${frame.x}+${frame.y} buffer=${buffer.width}x${buffer.height}` +
            `+${buffer.x}+${buffer.y} actor=${allocW}x${allocH}` +
            `+${originX}+${originY} texture=${texture.get_width()}x` +
            `${texture.get_height()} scale=${scale.toFixed(3)} ` +
            `crop=${cropW}x${cropH}+${cropX}+${cropY}`);

        const stream = Gio.MemoryOutputStream.new_resizable();
        try {
            Shell.Screenshot.composite_to_stream(
                texture, cropX, cropY, cropW, cropH,
                1.0, null, 0, 0, 1.0, stream,
                (_source, result) => {
                    let pixbuf;
                    try {
                        pixbuf = Shell.Screenshot.composite_to_stream_finish(result);
                    } catch (e) {
                        reply(false, `composite_to_stream failed: ${e}`);
                        return;
                    } finally {
                        stream.close(null);
                    }
                    if (!pixbuf) {
                        reply(false, 'composite_to_stream produced no image');
                        return;
                    }
                    try {
                        pixbuf.savev(path, 'png', [], []);
                    } catch (e) {
                        reply(false, `could not write ${path}: ${e}`);
                        return;
                    }
                    reply(true, '');
                });
        } catch (e) {
            reply(false, `composite_to_stream failed: ${e}`);
        }
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

    // Two independent routes to the same result: get_image (Shell <=50)
    // or paint_to_content + Shell.Screenshot.composite_to_stream
    // (Mutter >=51, which removed get_image). Either is sufficient, so
    // only report missing when neither is available; checking function
    // *existence* rather than calling anything keeps this probe free of
    // side effects on a window that only happens to be the first one
    // found.
    const hasLegacy = typeof actor.get_image === 'function';
    const hasModern =
        typeof actor.paint_to_content === 'function' &&
        typeof Shell.Screenshot.composite_to_stream === 'function' &&
        typeof Shell.Screenshot.composite_to_stream_finish === 'function';
    if (!hasLegacy && !hasModern) {
        missing.push(
            'Meta.WindowActor.get_image (or paint_to_content + ' +
            'Shell.Screenshot.composite_to_stream on Mutter >=51)');
    }
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
        // Only after export: _emitWindowEvent sends through this._dbusImpl,
        // so a window-created signal firing before it exists would be lost
        // rather than merely late. Nothing genuinely races here since both
        // happen synchronously within enable(), but the order still matters.
        this._service._dbusImpl = this._dbusImpl;
        try {
            this._service.startWatching();
        } catch (e) {
            // 'window-created'/'unmanaging'/'notify::title' have been core
            // Meta/GObject signals for far longer than this extension has
            // existed, so this is not expected to fire in practice -- but
            // if it ever does on some future Mutter, the rest of the
            // interface (window listing, move, resize, capture) still
            // works and stays exported; only WINDOW_EVENTS quietly does
            // not, which is the same shape of degradation the rest of this
            // package uses everywhere else. Logged, not raised: pyguitest's
            // GnomeShellBackend already has no way to be told this failed
            // (Capability.WINDOW_EVENTS is declared unconditionally, on
            // the reasoning explained in gnomeshell.py's own docstring),
            // so this is the only place the failure is visible at all.
            logError(e, 'pyguitest-window-control: startWatching failed; ' +
                'window events will not be delivered');
        }
    }

    disable() {
        this._service?.stopWatching();
        this._dbusImpl?.unexport();
        this._dbusImpl = null;
        this._service = null;
    }
}
