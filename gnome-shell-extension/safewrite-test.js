// Tests for safewrite.js, the extension's PNG write path.
//
// Run through gjs, by tests/test_gnomeshell.py or by hand:
//
//     gjs -m gnome-shell-extension/safewrite-test.js
//
// Deliberately not inside the extension directory: installation copies
// `$UUID/*.js` into ~/.local/share/gnome-shell/extensions, and a test file
// has no business being loaded by gnome-shell.
//
// This is the first automated test of any of the extension's JavaScript.
// Everything else in it needs `gi://Shell`, whose typelib exists only where
// gnome-shell is installed, so the Python suite models the D-Bus contract
// with a fake proxy and the real file is only ever exercised live by
// scripts/validate-gnome-extension.sh. safewrite.js needs GLib and Gio
// alone, which is why the security-relevant part could be split out and
// tested anywhere gjs runs.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {
    commitTemp, discardTemp, reserveTempBeside,
} from './pyguitest-window-control@pyguitest.local/safewrite.js';

let failures = 0;

function check(name, condition, detail) {
    print(`${condition ? 'ok  ' : 'FAIL'} ${name}${detail ? ` -- ${detail}` : ''}`);
    if (!condition)
        failures++;
}

function read(path) {
    const [ok, bytes] = GLib.file_get_contents(path);
    return ok ? new TextDecoder().decode(bytes) : null;
}

const dir = GLib.dir_make_tmp('pyguitest-safewrite-XXXXXX');

// 1. The ordinary case, which is also the one pyguitest takes: the backend
//    hands over a `mkstemp` name, so the destination already exists.
{
    const dest = GLib.build_filenamev([dir, 'shot.png']);
    GLib.file_set_contents(dest, '');
    const tmp = reserveTempBeside(dest);
    check('the temp file is in the destination directory',
        GLib.path_get_dirname(tmp) === dir, tmp);
    check('the temp file is hidden and carries the destination name',
        GLib.path_get_basename(tmp).startsWith('.shot.png.'));
    GLib.file_set_contents(tmp, 'PNGDATA');
    commitTemp(tmp, dest);
    check('the destination holds what was written', read(dest) === 'PNGDATA');
    check('no temp file is left behind',
        !GLib.file_test(tmp, GLib.FileTest.EXISTS));
}

// 2. The reason any of this exists. A destination naming a symlink used to
//    have its *target* overwritten by gnome-shell, on behalf of whatever
//    could reach the session bus -- so a caller could pick a path it had no
//    business writing and have the Shell write it. The rename replaces the
//    link; the target must be untouched.
{
    const victim = GLib.build_filenamev([dir, 'victim']);
    GLib.file_set_contents(victim, 'PRECIOUS');
    const link = GLib.build_filenamev([dir, 'link.png']);
    Gio.File.new_for_path(link).make_symbolic_link(victim, null);
    check('the symlink is in place',
        GLib.file_test(link, GLib.FileTest.IS_SYMLINK));

    const tmp = reserveTempBeside(link);
    GLib.file_set_contents(tmp, 'ATTACKER');
    commitTemp(tmp, link);

    check('a symlinked destination does not overwrite its target',
        read(victim) === 'PRECIOUS', `target now ${JSON.stringify(read(victim))}`);
    check('the symlink itself was replaced by the image',
        !GLib.file_test(link, GLib.FileTest.IS_SYMLINK) &&
        read(link) === 'ATTACKER');
}

// 3. The window between create and rename is not readable by other local
//    accounts either, which is what Gio.FileCreateFlags.PRIVATE buys.
{
    const dest = GLib.build_filenamev([dir, 'mode.png']);
    const tmp = reserveTempBeside(dest);
    const mode = Gio.File.new_for_path(tmp)
        .query_info('unix::mode', Gio.FileQueryInfoFlags.NONE, null)
        .get_attribute_uint32('unix::mode') & 0o777;
    check('the temp file is mode 0600', mode === 0o600, `0${mode.toString(8)}`);
    discardTemp(tmp);
}

// 3b. A destination whose own name is legal but long. The temp name is
//     always longer -- `.` + stem + `.` + uuid + `.tmp` -- so without
//     `boundedStem` a ~234-character destination that the direct write
//     handled fine failed to capture at all. Bounded by bytes, so a
//     multibyte name counts the same way the filesystem counts it.
{
    const encoder = new TextEncoder();
    for (const base of [
        `${'x'.repeat(230)}.png`,
        `${'é'.repeat(100)}.png`,
        `${'\u{1F4F7}'.repeat(50)}.png`,
    ]) {
        const dest = GLib.build_filenamev([dir, base]);
        let legal = true;
        try {
            GLib.file_set_contents(dest, '');
        } catch {
            legal = false;
        }
        if (!legal)
            continue;  // the destination itself is over the limit: not our case
        const tmp = reserveTempBeside(dest);
        const bytes = encoder.encode(GLib.path_get_basename(tmp)).length;
        GLib.file_set_contents(tmp, 'PNGDATA');
        commitTemp(tmp, dest);
        check(`a ${encoder.encode(base).length}-byte destination still captures`,
            read(dest) === 'PNGDATA' && bytes <= 255, `temp name ${bytes} bytes`);
    }
}

// 4. Two calls never collide, which is what lets one capture overlap
//    another without either writing into the other's file.
{
    const dest = GLib.build_filenamev([dir, 'twice.png']);
    const first = reserveTempBeside(dest);
    const second = reserveTempBeside(dest);
    check('two reservations get different names', first !== second);
    discardTemp(first);
    discardTemp(second);
}

// 5. Cleanup after a failed write, and the best-effort promise: a second
//    call must not raise, because the caller is already reporting the real
//    failure and must not lose it to one from the cleanup.
{
    const dest = GLib.build_filenamev([dir, 'failed.png']);
    const tmp = reserveTempBeside(dest);
    discardTemp(tmp);
    check('discardTemp removes the file',
        !GLib.file_test(tmp, GLib.FileTest.EXISTS));
    discardTemp(tmp);
    check('discardTemp on a missing file does not raise', true);
    check('a failed write leaves no destination behind',
        !GLib.file_test(dest, GLib.FileTest.EXISTS));
}

// 6. A commit whose source is gone reports rather than silently succeeding.
{
    const dest = GLib.build_filenamev([dir, 'gone.png']);
    let raised = false;
    try {
        commitTemp(GLib.build_filenamev([dir, '.nonexistent.tmp']), dest);
    } catch {
        raised = true;
    }
    check('committing a missing temp file raises', raised);
}

// 7. The sequences the two capture paths actually use, against the real
//    imaging libraries. Everything above writes text, which proves the
//    rename semantics but not that cairo and GdkPixbuf are happy writing to
//    a file something else created -- the one thing that would break capture
//    outright while every check above still passed.
//
//    Imported dynamically so the file keeps running where these typelibs are
//    absent: the checks above need only GLib and Gio, and that is what makes
//    them runnable anywhere gjs is.
let Cairo = null;
let GdkPixbuf = null;
try {
    Cairo = (await import('gi://cairo')).default;
    GdkPixbuf = (await import('gi://GdkPixbuf')).default;
} catch (e) {
    print(`skip  the capture-path checks -- no cairo/GdkPixbuf typelib (${e})`);
}

if (Cairo && GdkPixbuf) {
    const isPng = path => {
        const [ok, bytes] = GLib.file_get_contents(path);
        return ok && bytes.length > 8 && bytes[0] === 0x89 &&
            bytes[1] === 0x50 && bytes[2] === 0x4e && bytes[3] === 0x47;
    };

    // _captureLegacy: an ImageSurface written with writeToPNG.
    {
        const dest = GLib.build_filenamev([dir, 'legacy.png']);
        // Empty, as the mkstemp name pyguitest hands over arrives.
        GLib.file_set_contents(dest, '');
        const surface = new Cairo.ImageSurface(Cairo.Format.ARGB32, 64, 48);
        const cr = new Cairo.Context(surface);
        cr.setSourceRGB(0.2, 0.6, 0.9);
        cr.paint();
        surface.flush();
        const tmp = reserveTempBeside(dest);
        surface.writeToPNG(tmp);
        commitTemp(tmp, dest);
        check('cairo writeToPNG through the temp file produces a real PNG',
            isPng(dest));
        const decoded = GdkPixbuf.Pixbuf.new_from_file(dest);
        check('the legacy-path PNG decodes at the surface size',
            decoded.get_width() === 64 && decoded.get_height() === 48,
            `${decoded.get_width()}x${decoded.get_height()}`);
    }

    // _captureModern: a Pixbuf written with savev.
    {
        const dest = GLib.build_filenamev([dir, 'modern.png']);
        GLib.file_set_contents(dest, '');
        const pixbuf = GdkPixbuf.Pixbuf.new(
            GdkPixbuf.Colorspace.RGB, true, 8, 32, 16);
        pixbuf.fill(0xff8800ff);
        const tmp = reserveTempBeside(dest);
        pixbuf.savev(tmp, 'png', [], []);
        commitTemp(tmp, dest);
        check('pixbuf savev through the temp file produces a real PNG',
            isPng(dest));
        const decoded = GdkPixbuf.Pixbuf.new_from_file(dest);
        check('the modern-path PNG decodes at the pixbuf size',
            decoded.get_width() === 32 && decoded.get_height() === 16,
            `${decoded.get_width()}x${decoded.get_height()}`);
        // pyguitest reads the file back, so 0600 has to mean *its* owner.
        const [readable] = GLib.file_get_contents(dest);
        check('the caller can read the finished capture', readable);
    }
}

GLib.spawn_command_line_sync(`rm -rf ${dir}`);
print(failures ? `\n${failures} check(s) failed` : '\nall checks passed');
if (failures)
    imports.system.exit(1);
