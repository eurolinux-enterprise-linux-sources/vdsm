#
# Copyright 2009 Red Hat, Inc. and/or its affiliates.
#
# Licensed to you under the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.  See the files README and
# LICENSE_GPL_v2 which accompany this distribution.
#

#FIXME: Some of the methods here contain SUDO in their names and some don't.
#This doesn't mean if the method will use sudo by default or not.
#Further more 'SUDO' never needs to be in a name of a method and should always be 'False' by default.
#Using sudo isn't something a method should do by default. As a rule of thumb. If you didn't
#have to you wouldn't use SUDO. This makes it the less desirable option thus making it the optional one.

#FIXME: A lot of methods here use DD. A smart thing would be to wrap DD in a method that does all
#the arg concatenation and stream handling once. Also most method when they fail don't tell why even
#though DD is more then happy to let you know. Exceptions thrown should contain DD's stderr output.

"""
Various storage misc procedures
"""
from contextlib import contextmanager
import contextlib
import logging
import subprocess
import traceback
import types
import time
import signal
import os
import io
import struct
import sys
from StringIO import StringIO
from array import array
import threading
import Queue
import string
import random
import errno
from collections import defaultdict
from itertools import chain
from functools import wraps, partial
import select
import gc
from weakref import proxy
import re
import weakref

sys.path.append("../")
import constants
from config import config
import storage_exception as se

IOUSER = "vdsm"
DIRECTFLAG = "direct"
DATASYNCFLAG = "fdatasync"
STR_UUID_SIZE = 36
UUID_HYPHENS = [8, 13, 18, 23]
OVIRT_NODE = False
MEGA = 1 << 20
SUDO_NON_INTERACTIVE_FLAG = "-n"

log = logging.getLogger('Storage.Misc')

def enableLogSkip(logger, *args, **kwargs):
    skipFunc = partial(findCaller, *args, **kwargs)
    logger.findCaller = types.MethodType(lambda self: skipFunc(),
            logger, logger.__class__)

    return logger

# Buffsize is 1K because I tested it on some use cases and 1k was fastets
# If you find this number to be a bottleneck in any way you are welcome to change it
BUFFSIZE = 1024

def stripNewLines(lines):
    return [l[:-1] if l.endswith('\n') else l for l in lines]

class _LogSkip(object):
    _ignoreMap = defaultdict(list)
    ALL_KEY = "##ALL##"
    @classmethod
    def registerSkip(cls, codeId, loggerName=None):
        if loggerName is None:
            loggerName = cls.ALL_KEY

        cls._ignoreMap[loggerName].append(codeId)

    @classmethod
    def checkForSkip(cls, codeId, loggerName):
        return codeId in chain(cls._ignoreMap[cls.ALL_KEY], cls._ignoreMap[loggerName])

    @classmethod
    def wrap(cls, func, loggerName):
        cls.registerSkip(id(func.func_code), loggerName)
        return func


def logskip(var):
    if isinstance(var, types.StringTypes):
        return lambda func: _LogSkip.wrap(func, var)
    return _LogSkip.wrap(var, None)

def findCaller(skipUp=0, ignoreSourceFiles=[], ignoreMethodNames=[], logSkipName=None):
    """
    Find the stack frame of the caller so that we can note the source
    file name, line number and function name.
    """
    # Ignore file extension can be either py or pyc
    ignoreSourceFiles = [os.path.splitext(sf)[0] for sf in ignoreSourceFiles + [logging._srcfile]]
    try:
        raise Exception
    except:
        # get the caller of my caller
        frame = sys.exc_info()[2].tb_frame.f_back.f_back

    result = "(unknown file)", 0, "(unknown function)"
    # pop frames untill you find an unfiltered one
    while hasattr(frame, "f_code"):
        code = frame.f_code
        filename = os.path.normcase(code.co_filename)
        logSkip = logSkipName is not None and _LogSkip.checkForSkip(id(code), logSkipName)

        if logSkip or (skipUp > 0) or (os.path.splitext(filename)[0] in ignoreSourceFiles) or (code.co_name in ignoreMethodNames):
            skipUp -= 1
            frame = frame.f_back
            continue

        result = (filename, frame.f_lineno, code.co_name)
        break

    return result

def panic(msg):
    log.error("Panic: %s" % (str(msg)))
    log.error(traceback.format_exc())
    os.killpg(0, 9)
    sys.exit(-3)

execCmdLogger = enableLogSkip(logging.getLogger('Storage.Misc.excCmd'), ignoreSourceFiles=[__file__],
               logSkipName="Storage.Misc.excCmd")

@logskip("Storage.Misc.excCmd")
def execCmd(command, sudo=True, cwd=None, infile=None, outfile=None,
            shell=False, data=None, raw=False, logErr=True, printable=None,
            env=None, sync=True):
    """
    Executes an external command, optionally via sudo.
    """
    if sudo:
        if isinstance(command, types.StringTypes):
            command = " ".join([constants.EXT_SUDO, SUDO_NON_INTERACTIVE_FLAG, command])
        else:
            command = [constants.EXT_SUDO, SUDO_NON_INTERACTIVE_FLAG] + command

    if not printable:
        printable = command
    execCmdLogger.debug("%s (cwd %s)", repr(subprocess.list2cmdline(printable)), cwd)

    # FIXME: if infile == None and data:
    if infile == None:
        infile = subprocess.PIPE

    if outfile == None:
        outfile = subprocess.PIPE

    with disabledGcBlock:
        p = subprocess.Popen(command, shell=shell, close_fds=True, cwd=cwd,
                       stdin=infile, stdout=outfile, stderr=subprocess.PIPE,
                       env=env)
    p = AsyncProc(p)
    if not sync:
        if data is not None:
            p.stdout.write(data)
        return p

    (out, err) = p.communicate(data)

    if out == None:
        # Prevent splitlines() from barfing later on
        out = ""

    execCmdLogger.debug("%s: <err> = %s; <rc> = %d", {True: "SUCCESS", False: "FAILED"}[p.returncode == 0],
        repr(err), p.returncode)

    if not raw:
        out = out.splitlines(False)
        err = err.splitlines(False)

    return (p.returncode, out, err)


def pidExists(pid):
    try:
        os.stat(os.path.join('/proc', str(pid)))
    except OSError, e:
        # The actual exception for 'File does not exists' is ENOENT
        if e.errno == errno.ENOENT:
            return False
        else:
            log.error("Error on stat pid %s (%s)", pid, str(e))

    return True


def watchCmd(command, stop, idle, sudo=True, cwd=None, infile=None, outfile=None,
            shell=False, data=None):
    """
    Executes an external command, optionally via sudo with stop abilities.
    """
    proc = execCmd(command, sudo=sudo, cwd=cwd, infile=infile, outfile=outfile, shell=shell, data=data, sync=False)
    if not proc.wait(cond=stop):
        proc.kill()
        raise se.ActionStopped()

    out = stripNewLines(proc.stdout)
    err = stripNewLines(proc.stderr)

    execCmdLogger.debug("%s: <err> = %s; <rc> = %d", {True: "SUCCESS", False: "FAILED"}[proc.returncode == 0],
        repr(err), proc.returncode)

    return (proc.returncode, out, err)

def readfile(name):
    """
    Read the content of the file using /bin/dd command
    """
    cmd = [constants.EXT_DD, "iflag=%s" % DIRECTFLAG, "if=%s" % name]
    (rc, out, err) = execCmd(cmd, sudo=False)
    if rc:
        raise se.MiscFileReadException(name)
    return out

def readfileSUDO(name):
    """
    Read the content of the file using 'cat' command via sudo.
    """
    cmd = [constants.EXT_CAT, name]
    (rc, out, err) = execCmd(cmd, sudo=True)
    if rc:
        raise se.MiscFileReadException(name)
    return out

def writefileSUDO(name, lines):
    """
    Write the 'lines' to the file using /bin/echo command via sudo
    """
    cmd = [constants.EXT_DD, "of=%s" % name]
    data = "".join(lines)
    (rc, out, err) = execCmd(cmd, data=data, sudo=True)
    if rc:
        log.warning("write failed with rc: %s, stderr: %s", rc, repr(err))
        raise se.MiscFileWriteException(name)

    if not validateDDBytes(err, len(data)):
        log.warning("write failed stderr: %s, data: %s", repr(err), len(data))
        raise se.MiscFileWriteException(name)

    return (rc, out)

def readblockSUDO(name, offset, size, sudo=False):
    '''
    Read (direct IO) the content of device 'name' at offset, size bytes
    '''

    # direct io must be aligned on block size boundaries
    if (size % 512) or (offset % 512):
        raise se.MiscBlockReadException(name, offset, size)

    left = size
    ret = ""
    baseoffset = offset

    while left > 0:
        (iounit, count, iooffset) = _alignData(left, offset)

        cmd = [constants.EXT_DD, "iflag=%s" % DIRECTFLAG, "skip=%d" % iooffset,
                "bs=%d" % iounit, "if=%s" % name, 'count=%s' % count]

        (rc, out, err) = execCmd(cmd, raw=True, sudo=sudo)
        if rc:
            raise se.MiscBlockReadException(name, offset, size)
        if not validateDDBytes(err.splitlines(), iounit*count):
            raise se.MiscBlockReadIncomplete(name, offset, size)

        ret += out
        left = left % iounit
        offset = baseoffset + size - left
    return ret.splitlines()


def validateDDBytes(ddstderr, size):
    log.debug("err: %s, size: %s" % (ddstderr, size))
    try:
        size = int(size)
    except (ValueError, ):
        raise se.InvalidParameterException("size", str(size))

    if len(ddstderr) != 3:
        raise se.InvalidParameterException("len(ddstderr)", ddstderr)

    try:
        xferred = int(ddstderr[2].split()[0])
    except (ValueError, ):
        raise se.InvalidParameterException("ddstderr", ddstderr[2])

    if xferred != size:
        return False
    return True


def writeblockSUDO(name, offset, size, lines, sudo=False):
    '''
    Write (direct IO) the content of device 'name' at offset, size bytes
    '''
    # direct io must be aligned on block size boundaries
    if offset % 512:
        raise se.MiscBlockWriteException("%s: offset not aligned -" % (name) , offset, size)

    # align size to 512 to allow direct IO
    size = ((size+511) / 512) * 512 #FIXME: trusting '/' to round your result is considered bad practice. Using `(512 - size) % 512 + size` is safer.
    data = "".join(lines)
    if len(data) < size:
        data += "\0" * (size - len(data))
    elif len(data) > size:
        log.warning("received more data than size, truncating: size: %s, data len: %s, data: %s" % (size, str(len(data)), data))
        data = data[:size]

    (iounit, dummy, iooffset) = _alignData(size, offset)

    # Note that subprocess's "communicate" writes to the DD stdin in 512b chunks.
    # To avoid races with the pipe, ibs is set to 512 and "count" MUST NOT be used.
    cmd = [constants.EXT_DD, "oflag=%s" % DIRECTFLAG, "ibs=512",
            "obs=%d" % iounit, "seek=%d" % iooffset, "of=%s" % name]
    (rc, out, err) = execCmd(cmd, data=data, sudo=sudo)
    if rc:
        raise se.MiscBlockWriteException(name, offset, size)
    if not validateDDBytes(err, size):
        raise se.MiscBlockWriteIncomplete(name, offset, size)

    return out


def _alignData(length, offset):
    iounit = MEGA
    count = length
    iooffset = offset

    # Keep small IOps in single shot if possible
    if (length < MEGA) and (offset % length == 0) and (length % 512 == 0):
        # IO can be direct + single shot
        count = 1
        iounit = length
        iooffset = offset / iounit
        return (iounit, count, iooffset)

    # Compute largest chunk possible up to 1M for IO
    while iounit > 1:
        if (length >= iounit) and (offset % iounit == 0):
            count = length / iounit
            iooffset = offset / iounit
            break
        iounit = iounit >> 1

    return (iounit, count, iooffset)

def randomStr(strLen):
    return "".join(random.sample(string.letters, strLen))

def ddWatchCopy(src, dst, stop, idle, size, offset=0, sudo=False):
    """
    Copy src to dst using dd command with stop abilities
    """
    left = size
    baseoffset = offset

    try:
        int(size)
    except:
        raise se.InvalidParameterException("size", size)

    while left > 0:
        (iounit, count, iooffset) = _alignData(left, offset)
        oflag = None
        conv = "notrunc"
        if (iounit % 512) == 0:
            oflag = DIRECTFLAG
        else:
            conv += ",%s" % DATASYNCFLAG

        cmd = [constants.EXT_DD, "if=%s" % src, "of=%s" % dst, "bs=%d" % iounit,
               "seek=%s" % iooffset, "skip=%s" % iooffset, "conv=%s" % conv, 'count=%s' % count]

        if oflag:
            cmd.append("oflag=%s" % oflag)

        cmd = subprocess.list2cmdline(cmd)

        runAs = IOUSER
        if sudo:
            runAs = "root"

        cmd = [constants.EXT_IONICE, '-c2', '-n7', constants.EXT_SU, runAs, '-s', constants.EXT_SH, "-c", cmd]

        if not stop:
            (rc, out, err) = execCmd(cmd, sudo=True)
        else:
            (rc, out, err) = watchCmd(cmd, stop=stop, idle=idle, sudo=True)

        if rc:
            raise se.MiscBlockWriteException(dst, offset, size)

        if not validateDDBytes(err, iounit*count):
            raise se.MiscBlockWriteIncomplete(dst, offset, size)

        left = left % iounit
        offset = baseoffset + size - left

    return (rc, out, err)


def ddCopy(src, dst, size=None):
    """
    Copy src to dst using dd command
    """
    return ddWatchCopy(src, dst, None, None, size=size)


def parseBool(var):
    if isinstance(var, bool):
        return var
    # Transform: str -> bool
    if var.lower() == 'true':
        return True
    else:
        return False


def checksum(string, numBytes):
    bits = 8 * numBytes
    tmpArray = array('B')
    tmpArray.fromstring(string)
    csum = sum(tmpArray)
    return csum - (csum >> bits << bits)


def packUuid(s):
    s = ''.join([c for c in s if c != '-'])
    uuid = int(s, 16)
    high = uuid / 2**64
    low = uuid % 2**64
    # pack as 128bit little-endian <QQ
    return struct.pack('<QQ', low, high)


def unpackUuid(uuid):
    low, high = struct.unpack('<QQ', uuid)
    uuid = hex(low + 2**64 * high)[2:-1].rjust(STR_UUID_SIZE - 4,"0").lower() # remove leading 0x and trailing L
    s = ""
    prev = 0
    i = 0
    for hypInd in UUID_HYPHENS:
        s += uuid[prev:hypInd-i] + '-'
        prev = hypInd-i
        i += 1
    s += uuid[prev:]
    return s #'-'.join([ s[0:8], s[8:12], s[12:16], s[16:20], s[20:] ])


UUID_REGEX = re.compile("^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$")
def validateUUID(uuid, name="uuid"):
    """
    Ensure that uuid structure is 32 bytes long and is of the form: 8-4-4-4-12 (where each number depicts the amount of hex digits)

    Even though UUIDs can contain capital letters (because HEX strings are case insensitive) we usually compare uuids with the `==`
    operator, having uuids with upper case letters will cause unexpected bug so we filter them out
    """
    m = UUID_REGEX.match(uuid)
    if m is None:
        raise se.InvalidParameterException(name, uuid)
    return True

def validateInt(number, name): #FIXME: Consider using confutils validator?
    try:
        return int(number)
    except:
        raise se.InvalidParameterException(name, number)

def validateN(number, name):
    n = validateInt(number, name)
    if n < 0:
        raise se.InvalidParameterException(name, number)
    return n

def rotateFiles(dir, prefixName, gen, cp=False, persist=False):
    log.debug("dir: %s, prefixName: %s, versions: %s" % (dir, prefixName, gen))
    gen = int(gen)
    files = os.listdir(dir)
    files = [file for file in files if file.startswith(prefixName)] #FIXME: Why not use glob.glob?
    fd = {}
    for file in files:
        name = file.rsplit('.', 1)
        try:
            ind = int(name[1])
        except ValueError:
            name[0] = file
            ind = 0
        except IndexError:
            ind = 0
        except:
            continue
        if ind < gen:
            fd[ind] = {'old': file, 'new': name[0] + '.' + str(ind+1)}

    keys = fd.keys()
    keys.sort(reverse=True)
    log.debug("versions found: %s" % (keys))

    for key in keys:
        oldName = os.path.join(dir, fd[key]['old'])
        newName = os.path.join(dir, fd[key]['new'])
        if OVIRT_NODE and persist and not cp:
            try:
                execCmd([constants.EXT_UNPERSIST, oldName], logErr=False)
                execCmd([constants.EXT_UNPERSIST, newName], logErr=False)
            except:
                pass
        try:
            if cp:
                execCmd([constants.EXT_CP, oldName, newName])
                if OVIRT_NODE and persist and not os.path.exists(newName):
                    execCmd([constants.EXT_PERSIST, newName], logErr=False)

            else:
                os.rename(oldName, newName)
        except:
            pass
        if OVIRT_NODE and persist and not cp:
            try:
                execCmd([constants.EXT_PERSIST, newName], logErr=False)
            except:
                pass


def persistFile(name):
    if OVIRT_NODE:
        execCmd([constants.EXT_PERSIST, name])

def parseHumanReadableSize(size):
    #FIXME : Maybe use a regex -> ^(?P<num>\d+)(?P<sizeChar>[KkMmGgTt])$
    #FIXME : Why not support B and be done with it?
    if size.isdigit():
        # No suffix - pass it as is
        return int(size)

    size = size.upper()

    if size.endswith("T"):
        if size[:-1].isdigit():
            return int(size[:-1]) << 40

    if size.endswith("G"):
        if size[:-1].isdigit():
            return int(size[:-1]) << 30

    if size.endswith("M"):
        if size[:-1].isdigit():
            return int(size[:-1]) << 20

    if size.endswith("K"):
        if size[:-1].isdigit():
            return int(size[:-1]) << 10

    # Failing all the above we'd better just return 0
    return 0


def dmRemoveMapping(vgname):
    """
    Removes the mapping of the specified volume group.
    Utilizes the fact that the mapping created by the LVM looks like that
    e45c12b0--f520--498a--82bb--c6cb294b990f-master
    i.e vg name concatenated with volume name (dash is escaped with dash)
    """
    cmd = [constants.EXT_DMSETUP, "ls"]

    rc, out = execCmd(cmd)[0:2]

    if rc != 0:
        # report error
        return

    # convert the name to the style used in device mapper
    vgname = "--".join(vgname.split("-"))#FIXME : what's wrong with str.replace?

    for mapping in [i.split()[0] for i in out if i.startswith(vgname)]:
        cmd = [constants.EXT_DMSETUP, "remove", mapping]
        rc = execCmd(cmd)[0]
        if rc != 0:
            # report error
            pass

class RWLock(object):
    """
    A simple ReadWriteLock implementation.

    The lock must be released by the thread that acquired it.
    Once a thread has acquired a lock, the same thread may acquire
    it again without blocking; the thread must release it once for each time
    it has acquired it. Note that lock promotion (acquiring an exclusive lock
    under a shared lock is forbidden and will raise an exception.

    The lock puts all requests in a queue. The request is granted when
    The previous one is released.

    Each request is represented by a :class:`threading.Event` object. When the Event
    is set the request is granted. This enables multiple callers to wait for a
    request thus implementing a shared lock.
    """
    class _contextLock(object):
        def __init__(self, owner, exclusive):
            self._owner = owner
            self._exclusive = exclusive

        def __enter__(self):
            self._owner.acquire(self._exclusive)

        def __exit__(self, exc_type, exc_value, traceback):
            self._owner.release()

    def __init__(self):
        self._syncRoot = threading.Lock()
        self._queue = Queue.Queue()
        self._currentSharedLock = None
        self._currentState = None
        self._holdingThreads = {}

        self.shared = self._contextLock(self, False)
        self.exclusive = self._contextLock(self, True)

    def acquireRead(self):
        return self.acquire(False)

    def acquireWrite(self):
        return self.acquire(True)

    def acquire(self, exclusive):
        currentEvent = None
        currentThread = threading.currentThread()

        # Handle reacquiring lock in the same thread
        if currentThread in self._holdingThreads:
            if self._currentState == False and exclusive:
                raise RuntimeError("Lock promotion is forbidden.")

            self._holdingThreads[currentThread] += 1
            return

        with self._syncRoot:
            # Handle regular acquisition
            if exclusive:
                currentEvent = threading.Event()
                self._currentSharedLock = None
            else:
                if self._currentSharedLock is None:
                    self._currentSharedLock = threading.Event()

                currentEvent = self._currentSharedLock

            try:
                self._queue.put_nowait((currentEvent, exclusive))
            except Queue.Full:
                raise RuntimeError("There are too many objects waiting for this lock")

            if self._queue.unfinished_tasks == 1:
                # Bootstrap the process if needed. A lock is released the when the
                # next request is granted. When there is no one to grant the request
                # you have to grant it yourself.
                event, self._currentState = self._queue.get_nowait()
                event.set()

        currentEvent.wait()

        self._holdingThreads[currentThread] = 0

    def release(self):
        currentThread = threading.currentThread()

        if not currentThread in self._holdingThreads:
            raise RuntimeError("Releasing an lock without acquiring it first")

        # If in nested lock don't really release
        if self._holdingThreads[currentThread] > 0:
            self._holdingThreads[currentThread] -= 1
            return

        del self._holdingThreads[currentThread]

        with self._syncRoot:
            self._queue.task_done()

            if self._queue.empty():
                self._currentState = None
                return

            nextRequest, self._currentState = self._queue.get_nowait()

        nextRequest.set()

class DeferableContext():
    def __init__(self, *args):
        self._finally = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        lastException = None

        for func, args, kwargs in self._finally:
            try:
                func(*args, **kwargs)
            except Exception, ex:
                lastException = ex

        if lastException is not None:
            raise lastException

    def defer(self, func, *args, **kwargs):
        self._finally.append((func, args, kwargs))

    def prependDefer(self, func, *args, **kwargs):
        self._finally.insert(0, (func, args, kwargs))

def retry(func, expectedException=Exception, tries=None, timeout=None, sleep=1):
    """
    Retry a function. Wraps the retry logic so you don't have to
    implement it each time you need it.

    :param func: The callable to run.
    :param expectedException: The exception you expect to recieve when the function fails.
    :param tries: The number of time to try. None\0,-1 means infinite.
    :param timeout: The time you want to spend waiting. This **WILL NOT** stop the method.
                    It will just not run it if it ended after the timeout.
    :param sleep: Time to sleep between calls in seconds.
    """
    if tries in [0, None]:
        tries = -1

    if timeout in [0, None]:
        timeout = -1

    startTime = time.time()

    while True:
        tries -= 1
        try:
            return func()
        except expectedException:
            if tries == 0:
                raise

            if (timeout > 0) and ((time.time() - startTime) > timeout):
                raise

            time.sleep(sleep)

class AsyncProc(object):
    """
    AsyncProc is a funky class. It warps a standard subprocess.Popen
    Object and gives it super powers. Like the power to read from a stream
    without the fear of deadlock. It does this by always sampling all
    stream while waiting for data. By doing this the other process can freely
    write data to all stream without the fear of it getting stuck writing
    to a full pipe.
    """
    class _streamWrapper(io.RawIOBase):
        def __init__(self, parent, streamToWrap, fd):
            io.IOBase.__init__(self)
            self._stream = streamToWrap
            self._parent = proxy(parent)
            self._fd = fd
            self._closed = False
            self._emptyCounter = 0

        def close(self):
            if not self._closed:
                self._closed = True
                while not self._streamClosed:
                    self._parent._processStreams()

        @property
        def closed(self):
            return self._closed

        @property
        def _streamClosed(self):
            return (self.fileno() in self._parent._closedfds)

        def fileno(self):
            return self._fd

        def seekable(self):
            return False

        def readable(self):
            return True

        def writable(self):
            return True

        def read(self, length):
            if (self._stream.len - self._stream.pos) < length and not self._streamClosed:
                self._parent._processStreams()

            with self._parent._streamLock:
                res = self._stream.read(length)
                if self._stream.pos == self._stream.len:
                    if self._streamClosed and res == "":
                        self._emptyCounter += 1
                        if self._emptyCounter > 2:
                            self._closed = True

                    self._stream.truncate(0)

            return res

        def readinto(self, b):
            data = self.read(len(b))
            bytesRead = len(data)
            b[:bytesRead] = data

            return bytesRead

        def write(self, data):
            if hasattr(data, "tobytes"):
                data = data.tobytes()
            with self._parent._streamLock:
                oldPos = self._stream.pos
                self._stream.pos = self._stream.len
                self._stream.write(data)
                self._stream.pos = oldPos

            while self._stream.len > 0 and not self._streamClosed:
                self._parent._processStreams()

            if self._streamClosed:
                self._closed = True

            if self._stream.len != 0:
                raise IOError(errno.EPIPE, "Could not write all data to stream")

            return len(data)

    def __init__(self, popenToWrap):
        self._streamLock = threading.Lock()
        self._proc = popenToWrap

        self._stdout = StringIO()
        self._stderr = StringIO()
        self._stdin = StringIO()

        fdout = self._proc.stdout.fileno()
        fderr = self._proc.stderr.fileno()
        self._fdin = self._proc.stdin.fileno()

        self._closedfds = []

        self._poller = select.epoll()
        self._poller.register(fdout, select.EPOLLIN | select.EPOLLPRI)
        self._poller.register(fderr, select.EPOLLIN | select.EPOLLPRI)
        self._poller.register(self._fdin, 0)
        self._fdMap = { fdout: self._stdout,
                        fderr: self._stderr,
                        self._fdin: self._stdin }

        self.stdout = io.BufferedReader(self._streamWrapper(self, self._stdout, fdout), BUFFSIZE)
        self.stderr = io.BufferedReader(self._streamWrapper(self, self._stderr, fderr), BUFFSIZE)
        self.stdin = io.BufferedWriter(self._streamWrapper(self, self._stdin, self._fdin), BUFFSIZE)
        self._returncode = None

    def _processStreams(self):
        if len(self._closedfds) == 3:
            return

        if not self._streamLock.acquire(False):
            self._streamLock.acquire()
            self._streamLock.release()
            return
        try:
            if self._stdin.len > 0 and self._stdin.pos == 0:
                # Polling stdin is redundant if there is nothing to write
                # trun on only if data is waiting to be pushed
                self._poller.modify(self._fdin, select.EPOLLOUT)

            for fd, event in self._poller.poll(1):
                stream = self._fdMap[fd]
                if event & select.EPOLLOUT and self._stdin.len > 0:
                    buff = self._stdin.read(BUFFSIZE)
                    written = os.write(fd, buff)
                    stream.pos -= len(buff) - written
                    if stream.pos == stream.len:
                        stream.truncate(0)
                        self._poller.modify(fd, 0)

                elif event & (select.EPOLLIN | select.EPOLLPRI):
                    data = os.read(fd, BUFFSIZE)
                    oldpos = stream.pos
                    stream.pos = stream.len
                    stream.write(data)
                    stream.pos = oldpos

                elif event & (select.EPOLLHUP | select.EPOLLERR):
                    self._poller.unregister(fd)
                    self._closedfds.append(fd)
                    # I don't close the fd because the original Popen
                    # will do it.

            if self.stdin.closed and self._fdin not in self._closedfds:
                self._poller.unregister(self._fdin)
                self._closedfds.append(self._fdin)
                self._proc.stdin.close()

        finally:
            self._streamLock.release()


    @property
    def pid(self):
        return self._proc.pid

    @property
    def returncode(self):
        if self._returncode is None:
            self._returncode = self._proc.poll()
        return self._returncode

    def kill(self):
        try:
            self._proc.kill()
        except OSError as ex:
            if ex.errno != errno.EPERM:
                raise
            execCmd([constants.EXT_KILL, "-" + str(signal.SIGTERM), str(self.pid)], sudo=True)

    def wait(self, timeout=None, cond=None):
        startTime = time.time()
        while self.returncode is None:
            if timeout is not None and (time.time() - startTime) > timeout:
                return False
            if cond is not None and cond():
                return False
            self._processStreams()
        return True

    def communicate(self, data=None):
        if data is not None:
            self.stdin.write(data)
            self.stdin.flush()
        self.stdin.close()

        self.wait()
        return "".join(self.stdout), "".join(self.stderr)

    def __del__(self):
        self._poller.close()


class DynamicBarrier(object):
    def __init__(self):
        self._lock = threading.Lock()
        self._cond = threading.Condition()

    def enter(self):
        """
        Enter the dynamic barrier. Returns True if you should be
        the one performing the operation. False if someone already
        did that for you.

        You only have to exit() if you actually entered.

        Example:

        >> if dynamicBarrier.enter():
        >>    print "Do stuff"
        >>    dynamicBarrier.exit()
        """
        self._cond.acquire()
        try:
            if self._lock.acquire(False):
                return True

            self._cond.wait()

            if self._lock.acquire(False):
                return True

            self._cond.wait()
            return False

        finally:
            self._cond.release()

    def exit(self):
        self._cond.acquire()
        try:
            self._lock.release()
            self._cond.notifyAll()
        finally:
            self._cond.release()

class SamplingMethod(object):
    """
    This class is meant to be used as a decorator. Concurrent calls to the
    decorated function will be evaluated only once, and will share the same
    result, regardless of their specific arguments. It is the responsibility of
    the user of this decorator to make sure that this behavior is the expected
    one.

    Don't use this decorator on recursive functions!

    In addition, if an exception is thrown, only the function running it will
    get the exception, the rest will get previous run results.

    Supporting parameters or exception passing to all functions would
    make the code much more complex for no reason.
    """
    _log = logging.getLogger("SamplingMethod")
    def __init__(self, func):
        self.__func = func
        self.__lastResult = None
        self.__barrier = DynamicBarrier()

        if hasattr(self.__func, "func_name"):
            self.__funcName = self.__func.func_name
        else:
            self.__funcName = str(self.__func)

        self.__funcParent = None


    def __call__(self, *args, **kwargs):
        if self.__funcParent == None:
            if hasattr(self.__func, "func_code") and self.__func.func_code.co_varnames == 'self':
                self.__funcParent = args[0].__class__.__name__
            else:
                self.__funcParent = self.__func.__module__

        self._log.debug("Trying to enter sampling method (%s.%s)", self.__funcParent, self.__funcName)
        if self.__barrier.enter():
            self._log.debug("Got in to sampling method")
            try:
                self.__lastResult = self.__func(*args, **kwargs)
            finally:
                self.__barrier.exit()
        else:
            self._log.debug("Some one got in for me")

        self._log.debug("Returning last result")
        return self.__lastResult

def samplingmethod(func):
    sm = SamplingMethod(func)
    @wraps(func)
    def helper(*args, **kwargs):
        return sm(*args, **kwargs)
    return helper

class _DisabledGcBlock(object):
    _refCount = 0
    _refLock = threading.Lock()
    _lastCollect = 0
    forceCollectInterval = config.getint("irs", "gc_blocker_force_collect_interval")

    def __enter__(self):
        self.enter()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.exit()

    def enter(self):
        self._refLock.acquire()
        try:
            if gc.isenabled():
                self._lastCollect = time.time()
                gc.disable()

            if (time.time() - self._lastCollect) > self.forceCollectInterval:
                self._lastCollect = time.time()
                gc.collect()

            self._refCount += 1
        finally:
            self._refLock.release()

    def exit(self):
        self._refLock.acquire()
        try:
            self._refCount -= 1
            if self._refCount == 0:
                gc.enable()
        finally:
            self._refLock.release()

disabledGcBlock = _DisabledGcBlock()

def tmap(func, iterable):
    resultsDict = {}
    def wrapper(f, arg, index):
        resultsDict[index] = f(arg)

    threads = []
    for i, arg in enumerate(iterable):
        t = threading.Thread(target=wrapper, args=(func, arg, i))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    results = [None] * len(resultsDict)
    for i, result in resultsDict.iteritems():
        results[i] = result

    return tuple(results)

def getfds():
    return [int(fd) for fd in os.listdir("/proc/self/fd")]

class Event(object):
    def __init__(self, name, sync=False):
        self._log = logging.getLogger("Event.%s" % name)
        self.name = name
        self._syncRoot = threading.Lock()
        self._registrar = {}
        self._sync = sync

    def register(self, func, oneshot=False):
        with self._syncRoot:
            self._registrar[id(func)] = (weakref.ref(func), oneshot)

    def unregister(self, func):
        with self._syncRoot:
            del self._registrar[id(func)]

    def _emit(self, *args, **kwargs):
        self._log.debug("Emitting event")
        with self._syncRoot:
            for funcId, (funcRef, oneshot) in self._registrar.items():
                func = funcRef()
                if func is None or oneshot:
                    del self._registrar[funcId]
                    if func is None:
                        continue
                try:
                    self._log.debug("Calling registered method `%s`",
                            func.func_name if hasattr(func, "func_name") else str(func))
                    if self._sync:
                        func(*args, **kwargs)
                    else:
                        threading.Thread(target=func, args=args, kwargs=kwargs).start()
                except:
                    self._log.warn("Could not run registered method because of an exception", exc_info=True)

        self._log.debug("Event emitted")

    def emit(self, *args, **kwargs):
        if len(self._registrar) > 0:
            threading.Thread(target=self._emit, args=args, kwargs=kwargs).start()

class OperationMutex(object):
    log = enableLogSkip(logging.getLogger("OperationMutex"),
            ignoreSourceFiles=[__file__, contextlib.__file__])
    def __init__(self):
        self._lock = threading.Lock()
        self._cond = threading.Condition()
        self._active = None
        self._counter = 0
        self._queueSize = 0

    @contextmanager
    def acquireContext(self, operation):
        self.acquire(operation)
        try:
            yield self
        finally:
            self.release()

    def acquire(self, operation):
        generation = 0
        with self._cond:
            while not self._lock.acquire(False):
                if self._active == operation:
                    if self._queueSize == 0 or generation > 0:
                        self._counter += 1
                        self.log.debug("Got the operational mutex")
                        return

                self._queueSize += 1
                self.log.debug("Operation '%s' is holding the operation mutex, waiting...", self._active)
                self._cond.wait()
                generation += 1
                self._queueSize -= 1

            self.log.debug("Operation '%s' got the operation mutex", operation)
            self._active = operation
            self._counter = 1

    def release(self):
        with self._cond:
            self._counter -= 1
            if self._counter == 0:
                self.log.debug("Operation '%s' released the operation mutex", self._active)
                self._lock.release()
                self._cond.notifyAll()


# Upon import determine if we are running on ovirt
try:
    OVIRT_NODE = os.path.exists('/etc/rhev-hypervisor-release')
except:
    pass

