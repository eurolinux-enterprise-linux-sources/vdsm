#
# Copyright 2009 Red Hat, Inc. and/or its affiliates.
#
# Licensed to you under the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.  See the files README and
# LICENSE_GPL_v2 which accompany this distribution.
#


"""
NFS helper module
"""
import os
import types
import pwd
import grp
import stat
from StringIO import StringIO
from contextlib import closing
import ctypes
from contextlib import contextmanager

import shutil
import constants
import misc
import logging
import storage_exception as se
from config import config
import errno
libc = ctypes.CDLL("libc.so.6", use_errno=True)

# We must insure there are no white spaces in the NFS options
NFS_OPTIONS = "".join(config.get('irs', 'nfs_mount_options').split())

FSTYPE_NFS = "nfs"
FSTYPE_EXT = "ext"
FSTYPE_EXT3 = "ext3"
FSTYPE_EXT4 = "ext4"

log = logging.getLogger('fileUtils')

PAGESIZE = libc.getpagesize()
CharPointer = ctypes.POINTER(ctypes.c_char)

def getMounts():
    """
    returns a list of tuples represnting the mounts in this host.
    For each mount you have a tuple `` (resource, mount, type, options, freq, passno) ``.
    """
    rawMounts = open("/proc/mounts").readlines()
    parsedMounts = []
    for mount in rawMounts:
        parsedMounts.append(tuple(mount.split()))

    return parsedMounts

def mount(resource, mountPoint, mountType):
    """
    Mount the requested resource
    """
    if isStaleHandle(mountPoint):
        rc = umount(resource, mountPoint, mountType)
        if rc != 0:
            return rc

    if isMounted(resource, mountPoint, mountType):
        return 0

    if mountType == FSTYPE_NFS:
        cmd = [constants.EXT_MOUNT, "-o", NFS_OPTIONS, "-t", FSTYPE_NFS, resource, mountPoint]
    elif mountType in [FSTYPE_EXT3, FSTYPE_EXT4]:
        options = []
        if os.path.isdir(resource):
            # We should perform bindmount here,
            # because underlying resource is FS and not a block device
            options.append("-B")

        cmd = [constants.EXT_MOUNT] + options + [resource, mountPoint]
    else:
        raise se.MountTypeError()

    rc = misc.execCmd(cmd)[0]
    return rc

def umount(resource=None, mountPoint=None, mountType=None, force=True):
    """
    Unmount the requested resource from the associated mount point
    """
    if mountPoint is None and resource is None:
        raise ValueError("`mountPoint` or `resource` must be specified")

    if not isMounted(resource, mountPoint):
        if isMounted(mountPoint=mountPoint):
            return -1
        elif isMounted(resource=resource):
            return -1

        return 0

    options = []
    if mountType is not None:
        options.extend(["-t", mountType])

    if force:
        options.append("-f")

    cmd = [constants.EXT_UMOUNT] + options + [mountPoint if mountPoint is not None else resource]
    rc = misc.execCmd(cmd)[0]
    return rc

def isStaleHandle(path):
    exists = os.path.exists(path)
    stat = False
    try:
        os.statvfs(path)
        stat = True
        os.listdir(path)
    except OSError as ex:
        if ex.errno in (errno.EIO, errno.ESTALE):
            return  True
        # We could get contradictory results because of
        # soft mounts
        if (exists or stat) and ex.errno == errno.ENOENT:
            return True

    return False

def isMounted(resource=None, mountPoint=None, mountType=None):
    """
    Verify that "resource" (if given) is mounted on "mountPoint"
    """
    if mountPoint is None and resource is None:
        raise ValueError("`mountPoint` or `resource` must be specified")

    mounts = getMounts()

    for m in mounts:
        (res, mp, fs) = m[0:3]
        if ( (mountPoint is None or mp == mountPoint)
         and (resource is None   or res == resource)
         and (mountType is None  or fs.startswith(mountType)) ):
            return True

    return False

def transformPath(remotePath):
    """
    Transform remote path to new one for local mount
    """
    return remotePath.replace('_','__').replace('/','_')

def validateAccess(path):
    """
    Validate the RWX access to a given path
    """
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        raise se.StorageServerAccessPermissionError()

def validateQemuReadable(path):
    """
    Validate that qemu process can read file
    """
    gids = (grp.getgrnam(constants.DISKIMAGE_GROUP).gr_gid,
            grp.getgrnam(constants.METADATA_GROUP).gr_gid)
    st = os.stat(path)
    if not (st.st_gid in gids and st.st_mode & stat.S_IRGRP or
            st.st_mode & stat.S_IROTH):
        raise se.StorageServerAccessPermissionError()

def validatePermissions(path):
    """
    Validate 'vdsm:kvm' permissions
    """
    uid = pwd.getpwnam(constants.METADATA_USER).pw_uid
    gid = grp.getgrnam(constants.METADATA_GROUP).gr_gid
    st = os.stat(path)
    # Check to proper uid and gid
    if st.st_uid != uid or st.st_gid != gid:
        raise se.StorageServerAccessPermissionError()

def pathExists(filename, writeable=False):
    # This function is workarround for a NFS issue where
    # sometimes os.exists/os.access fails due to NFS stale handle.
    # In such cases we should try again and stat the file
    check = os.R_OK
    if writeable:
        check |= os.W_OK

    if os.access(filename, check):
        return True

    try:
        s = os.stat(filename)
        if check & s[0] == check:
            return True
    except OSError:
        pass
    return False

def cleanupfiles(filelist):
    """
    Removes the files in the list
    """
    for item in filelist:
        if os.path.lexists(item):
            os.remove(item)

def cleanupdir(dirPath, ignoreErrors=True):
    """
    Recursively remove all the files and directories in the given directory
    """
    cleanupdir_errors = []
    def logit(func, path, exc_info):
        cleanupdir_errors.append('%s: %s' % (func.__name__, exc_info[1]))

    shutil.rmtree(dirPath, onerror=logit)

    if not ignoreErrors and cleanupdir_errors:
        raise se.MiscDirCleanupFailure("%s %s" % (dirPath, cleanupdir_errors))

def createdir(dirPath, mode=None):
    """
    Recursively create directory if doesn't exist
    """
    if not os.path.exists(dirPath):
        if mode:
            os.makedirs(dirPath, mode)
        else:
            os.makedirs(dirPath)

def chown(path, user=-1, group=-1):
    """
    Change the owner and\or group of a file.
    The user and group parameters can either be a name or an id.
    """
    if isinstance(user, types.StringTypes):
        uid = pwd.getpwnam(user).pw_uid
    else:
        uid = int(user)

    if isinstance(group, types.StringTypes):
        gid = grp.getgrnam(group).gr_gid
    else:
        gid = int(group)

    stat = os.stat(path)
    currentUid = stat.st_uid
    currentGid = stat.st_gid

    if (uid == currentUid or user == -1) and (gid == currentGid or group == -1):
        return True

    os.chown(path, uid, gid)
    return True

def open_ex(path, mode):
    # TODO: detect if on nfs to do this out of process
    if "d" in mode:
        return DirectFile(path, mode)
    else:
        return open(path, mode)

class DirectFile(object):
    def __init__(self, path, mode):
        if not "d" in mode:
            raise ValueError("This class only handles direct IO")

        if len(mode) != 2:
            raise ValueError("Invalid mode parameter")

        flags = os.O_DIRECT
        if "r" in mode:
            flags |= os.O_RDONLY
            self._writable = False
        elif "w" in mode:
            flags |= os.O_RDWR | os.O_CREAT
            self._writable = True
        elif "a" in mode:
            flags |= os.O_APPEND
        else:
            raise ValueError("Invalid mode parameter")

        self._mode = mode
        self._fd = os.open(path, flags)
        self._closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def fileno(self):
        return self._fd

    @property
    def closed(self):
        return self._closed

    @property
    def mode(self):
        return self._mode

    def seekable(self):
        return True

    def readable(self):
        return True

    def writable(self):
        return self._writable

    def readlines(self):
        return self.readall().splitlines()

    def writelines(self, lines):
        data = ''.join([l if l.endswith('\n') else l + '\n' for l in lines])
        self.write(data)

    def tell(self):
        return self.seek(0, os.SEEK_CUR)

    @contextmanager
    def _createAlignedBuffer(self, size):
        pbuff = ctypes.c_char_p(0)
        ppbuff = ctypes.pointer(pbuff)
        # Because we usually have fixed sizes for our reads, caching
        # buffers might give a slight performance boost.
        if libc.posix_memalign(ppbuff, PAGESIZE, size):
            raise OSError("Could not allocate aligned buffer")
        try:
            ctypes.memset(pbuff, 0, size)
            yield pbuff
        finally:
            libc.free(pbuff)

    def read(self, n=-1):
        if (n < 0):
            return self.readall()

        if (n % 512):
            raise ValueError("You can only read in 512 multiplies")

        with self._createAlignedBuffer(n) as pbuff:
            numRead = libc.read(self._fd, pbuff, n)
            if numRead < 0:
                err = ctypes.get_errno()
                if err != 0:
                    msg = os.strerror(err)
                    raise OSError(msg, err)
            ptr = CharPointer.from_buffer(pbuff)
            return ptr[:numRead]

    def readall(self):
        buffsize = 1024
        res = StringIO()
        with closing(res):
            while True:
                buff = self.read(buffsize)
                res.write(buff)
                if len(buff) < buffsize:
                    return res.getvalue()

    def write(self, data):
        length = len(data)
        padding = 512 - (length % 512)
        if padding == 512:
            padding = 0
        length = length + padding
        pdata = ctypes.c_char_p(data)
        with self._createAlignedBuffer(length) as pbuff:
            ctypes.memmove(pbuff, pdata, len(data))
            numWritten = libc.write(self._fd, pbuff, length)
            if numWritten < 0:
                err = ctypes.get_errno()
                if err != 0:
                    msg = os.strerror(err)
                    raise OSError(msg, err)

    def seek(self, offset, whence=os.SEEK_SET):
        return os.lseek(self._fd, offset, whence)

    def close(self):
        if self.closed:
            return

        os.close(self._fd)
        self._closed = True

    def __del__(self):
        if not hasattr(self, "_fd"):
            return

        if not self.closed:
            self.close()

