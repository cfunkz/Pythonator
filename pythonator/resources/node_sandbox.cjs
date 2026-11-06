'use strict';
// Pythonator: makes Node.js work in the Windows sandbox (an AppContainer). Loaded first with --require.
//
// An AppContainer may use its own folders, but it may not even look at the folders above them (C:\,
// C:\Users, your profile folder...). Node's own realpath and npm's (@npmcli/arborist) check every folder on
// the way down with lstat, so they stop at "EPERM: operation not permitted, lstat 'C:\'". Here, and only for
// the folders *above* the ones this sandbox was given (PYTHONATOR_NODE_ROOTS), a refused stat is answered with
// "a plain folder": true, since they lead to a folder that exists, and it reveals nothing (no name, size or
// date of anything). A refused realpath falls back to Windows' own (fs.realpath.native), and when that is
// refused too (it names the drive, which a sandbox may not ask: "EPERM: operation not permitted, realpath"),
// to the path itself, made absolute, once a stat shows it exists: what --preserve-symlinks already does for
// modules (a link isn't followed). Nothing else changes, and nothing here grants any access.

const fs = require('fs');
const path = require('path');

if (process.env.PYTHONATOR_SANDBOX === '1' && !fs.__pythonatorSandbox) {
  Object.defineProperty(fs, '__pythonatorSandbox', { value: true });
  const fold = (p) => (process.platform === 'win32' ? p.toLowerCase() : p);
  const roots = (process.env.PYTHONATOR_NODE_ROOTS || '')
    .split(path.delimiter)
    .filter(Boolean)
    .map((r) => fold(path.resolve(r)));

  const text = (p) => {
    if (typeof p === 'string') return p;
    if (p instanceof URL) return require('url').fileURLToPath(p);
    if (Buffer.isBuffer(p)) return p.toString();
    return null;
  };

  // A folder strictly above one of the sandbox's folders.
  const above = (p) => {
    let s;
    try {
      const t = text(p);
      if (t === null) return false;
      s = fold(path.resolve(t));
    } catch {
      return false;
    }
    const prefix = s.endsWith(path.sep) ? s : s + path.sep;
    return roots.some((r) => r !== s && r.startsWith(prefix));
  };

  const denied = (e) => Boolean(e) && (e.code === 'EPERM' || e.code === 'EACCES');

  const folder = () => {
    const st = Object.create(fs.Stats.prototype);
    Object.assign(st, {
      dev: 0, mode: 0o40755, nlink: 1, uid: 0, gid: 0, rdev: 0, blksize: 4096, ino: 0, size: 0, blocks: 0,
      atimeMs: 0, mtimeMs: 0, ctimeMs: 0, birthtimeMs: 0,
    });
    return st;
  };

  for (const name of ['lstatSync', 'statSync']) {
    const original = fs[name];
    fs[name] = function (p, options) {
      try {
        return original.call(this, p, options);
      } catch (e) {
        if (denied(e) && above(p)) return folder();
        throw e;
      }
    };
  }

  for (const name of ['lstat', 'stat']) {
    const original = fs[name];
    fs[name] = function (p, options, callback) {
      if (typeof options === 'function') {
        callback = options;
        options = undefined;
      }
      return original.call(this, p, options, (e, st) =>
        denied(e) && above(p) ? callback(null, folder()) : callback(e, st));
    };
    const promised = fs.promises[name];
    fs.promises[name] = function (p, options) {
      return promised.call(this, p, options).catch((e) => {
        if (denied(e) && above(p)) return folder();
        throw e;
      });
    };
  }

  // The path made absolute, if it exists (a stat says ENOENT and the like, as realpath would).
  const plain = (p, options) => {
    fs.statSync(p);
    const resolved = path.resolve(text(p));
    const encoding = typeof options === 'string' ? options : options && options.encoding;
    return encoding === 'buffer' ? Buffer.from(resolved) : resolved;
  };

  const realSync = fs.realpathSync;
  const nativeSync = realSync.native;
  const nativeOrPlainSync = function (p, options) {
    try {
      return nativeSync(p, options);
    } catch (e) {
      if (denied(e)) return plain(p, options);
      throw e;
    }
  };
  fs.realpathSync = function realpathSync(p, options) {
    try {
      return realSync.call(this, p, options);
    } catch (e) {
      if (denied(e)) return nativeOrPlainSync(p, options);
      throw e;
    }
  };
  fs.realpathSync.native = nativeOrPlainSync;

  const real = fs.realpath;
  const nativeAsync = real.native;
  const nativeOrPlain = function (p, options, callback) {
    if (typeof options === 'function') {
      callback = options;
      options = undefined;
    }
    return nativeAsync(p, options, (e, resolved) => {
      if (!denied(e)) return callback(e, resolved);
      let out;
      try {
        out = plain(p, options);
      } catch (e2) {
        return callback(e2);
      }
      return callback(null, out);
    });
  };
  fs.realpath = function realpath(p, options, callback) {
    if (typeof options === 'function') {
      callback = options;
      options = undefined;
    }
    return real.call(this, p, options, (e, resolved) =>
      denied(e) ? nativeOrPlain(p, options, callback) : callback(e, resolved));
  };
  fs.realpath.native = nativeOrPlain;

  const promised = fs.promises.realpath;  // Windows' own, like fs.realpath.native
  fs.promises.realpath = function realpath(p, options) {
    return promised.call(this, p, options).catch((e) => {
      if (denied(e)) return plain(p, options);
      throw e;
    });
  };
}
