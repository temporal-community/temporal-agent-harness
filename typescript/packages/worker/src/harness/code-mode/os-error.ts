// What a failed filesystem call is in Python terms. A filesystem throws `FsError`, or lets a Node
// fs error through; either is the `OSError` (or the subclass Python picks for its errno) that a
// Python filesystem raises, and a rejected argument is Python's `ValueError`.

import { constants } from "node:os";
import { FsError, type OsErrorType } from "@temporalio/agent-harness";

export interface OsFailure {
  type: OsErrorType;
  errno: number;
  strerror: string;
}

// The OSError subclass Python raises for an errno; any other errno is a plain OSError.
const SUBCLASS: Readonly<Record<string, OsErrorType>> = {
  ENOENT: "FileNotFoundError",
  EEXIST: "FileExistsError",
  EISDIR: "IsADirectoryError",
  ENOTDIR: "NotADirectoryError",
  EACCES: "PermissionError",
  EPERM: "PermissionError",
};

// Python's strerror, where libuv's wording differs.
const STRERROR: Readonly<Record<string, string>> = {
  EPERM: "Operation not permitted",
  ENOENT: "No such file or directory",
  EIO: "Input/output error",
  EBADF: "Bad file descriptor",
  EAGAIN: "Resource temporarily unavailable",
  ENOMEM: "Cannot allocate memory",
  EACCES: "Permission denied",
  EBUSY: "Device or resource busy",
  EEXIST: "File exists",
  EXDEV: "Invalid cross-device link",
  ENOTDIR: "Not a directory",
  EISDIR: "Is a directory",
  EINVAL: "Invalid argument",
  EMFILE: "Too many open files",
  EFBIG: "File too large",
  ENOSPC: "No space left on device",
  EROFS: "Read-only file system",
  ENAMETOOLONG: "File name too long",
  ENOTEMPTY: "Directory not empty",
  ELOOP: "Too many levels of symbolic links",
  ETIMEDOUT: "Connection timed out",
  ECONNREFUSED: "Connection refused",
  ECONNRESET: "Connection reset by peer",
};

// Node's codes for an argument it refused before making the call.
const BAD_ARGUMENT = /^ERR_(INVALID_ARG_|OUT_OF_RANGE$|ENCODING_INVALID_ENCODED_DATA$)/;

const errorCode = (e: Error): unknown => (e as { code?: unknown }).code;

/** `e` as the OSError Python would raise for it, or null when it is not an OS error. */
export function osFailureOf(e: unknown): OsFailure | null {
  if (e instanceof FsError) return { type: e.fsType, errno: e.errno, strerror: e.strerror };
  if (!(e instanceof Error)) return null;
  const code = errorCode(e);
  const { errno, syscall } = e as { errno?: unknown; syscall?: unknown };
  if (typeof code !== "string" || typeof errno !== "number" || typeof syscall !== "string")
    return null;
  // "ENOENT: no such file or directory, open '/x'" holds libuv's wording for codes not listed.
  const described = e.message.match(/^[A-Z0-9_]+: ([^,]+)/)?.[1] ?? e.message;
  return {
    type: SUBCLASS[code] ?? "OSError",
    errno: constants.errno[code as keyof typeof constants.errno] ?? Math.abs(errno),
    strerror: STRERROR[code] ?? described.charAt(0).toUpperCase() + described.slice(1),
  };
}

/** Whether `e` is Node refusing an argument, which Python raises as `ValueError`. */
export function isBadArgument(e: unknown): e is Error {
  if (!(e instanceof Error)) return false;
  const code = errorCode(e);
  return typeof code === "string" && BAD_ARGUMENT.test(code);
}
