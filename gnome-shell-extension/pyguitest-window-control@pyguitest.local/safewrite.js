// Writing a PNG to a caller-supplied path without that path becoming a
// write primitive.
//
// Split out of extension.js so it can be tested at all. extension.js
// imports `gi://Shell`, whose typelib exists only where gnome-shell is
// installed, so nothing in it can be exercised off a GNOME machine -- and
// these three functions are the security-relevant part, needing only GLib
// and Gio. tests/test_gnomeshell.py runs safewrite-test.js against them
// through gjs, and skips where gjs is absent.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

// As much of a destination's name as can safely be carried into the temp
// file's, for legibility in a directory listing.
//
// Bounded by bytes rather than characters, because NAME_MAX is a byte limit
// and a filename need not be ASCII. The temp name is unavoidably longer than
// the destination it sits beside -- `.` + this + `.` + a 36-character uuid +
// `.tmp` -- so without a bound a destination whose own name was legal but
// long (over about 213 bytes, against ext4's 255) could be captured by the
// direct write this replaced and not by the temp-and-rename, which would
// have been a regression rather than a hardening. 120 bytes leaves room to
// spare, and is measured the same way the filesystem measures it.
function boundedStem(base) {
    const encoder = new TextEncoder();
    let stem = base;
    while (encoder.encode(stem).length > 120)
        stem = stem.slice(0, -1);
    return stem;
}

// Reserve a uniquely-named regular file in the same directory as `path` and
// return its name. The caller writes the PNG there and then calls
// `commitTemp`.
//
// This is what keeps a caller-supplied destination from being a write
// primitive. `CaptureWindowAsync` checks only that the path is absolute --
// a correctness check, since the Shell's working directory is not the
// caller's -- and both PNG writers (Cairo's `writeToPNG`, GdkPixbuf's
// `savev`) take a filename and open it following symlinks. So a path
// naming a symlink to anything the user's account can write had its
// *target* overwritten, by gnome-shell, on behalf of whatever reached the
// session bus. Writing beside the destination and renaming over it closes
// that: `rename` replaces a symlink rather than following it, the temp
// name is random so nothing can be planted at it, and `create` is
// O_CREAT|O_EXCL so a collision is refused instead of followed.
//
// It also makes the write atomic, which the direct write was not: a
// failure partway through used to leave a truncated PNG where a caller
// would read it as the screenshot.
function reserveTempBeside(path) {
    const dir = GLib.path_get_dirname(path);
    const stem = boundedStem(GLib.path_get_basename(path));
    for (let attempt = 0; attempt < 8; attempt++) {
        const name = GLib.build_filenamev(
            [dir, `.${stem}.${GLib.uuid_string_random()}.tmp`]);
        let stream = null;
        try {
            // PRIVATE is mode 0600, so the window before the rename is not
            // readable by other local accounts either.
            stream = Gio.File.new_for_path(name)
                .create(Gio.FileCreateFlags.PRIVATE, null);
            stream.close(null);
            return name;
        } catch (e) {
            // The file exists from here on if `create` is what succeeded, so
            // a failure out of `close` has to take it with it: the callers
            // reserve this name *before* their own try blocks, so nothing
            // downstream knows there is anything to discard, and a 0600
            // `.<stem>.<uuid>.tmp` would be left beside the destination for
            // good.
            if (stream !== null)
                discardTemp(name);
            const taken = typeof e.matches === 'function' &&
                e.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.EXISTS);
            if (!taken)
                throw e;
        }
    }
    throw new Error('could not reserve a temporary file beside the destination');
}

// Move a finished temp file over its destination, and never leave the temp
// behind. Same directory, so this is a rename: atomic, and it replaces a
// symlink at the destination instead of writing through it.
function commitTemp(tmpPath, path) {
    try {
        Gio.File.new_for_path(tmpPath).move(
            Gio.File.new_for_path(path), Gio.FileCopyFlags.OVERWRITE,
            null, null);
    } catch (e) {
        discardTemp(tmpPath);
        throw e;
    }
}

// Delete a temp file whose write or rename failed. Best effort by design:
// the caller is already reporting the real failure and must not lose it to
// a second one from the cleanup.
function discardTemp(tmpPath) {
    try {
        Gio.File.new_for_path(tmpPath).delete(null);
    } catch (e) {
        log(`pyguitest: could not remove ${tmpPath}: ${e}`);
    }
}

export {reserveTempBeside, commitTemp, discardTemp};
