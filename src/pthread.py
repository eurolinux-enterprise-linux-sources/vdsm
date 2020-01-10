# Copyright 2011 Red Hat, Inc. and/or its affiliates.
#
# Licensed to you under the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.  See the files README and
# LICENSE_GPL_v2 which accompany this distribution.
#

"""
pthread provides a Python bindings for POSIX thread synchronization
primitives. It implements mutex and conditional variable right now,
but can be easily extended to include also spin locks, rwlocks and
barriers if needed. It also does not implement non default mutex/condvar
attributes, which also can be added if required.
"""

import ctypes as C

# This is the POSIX thread library. If we ever will need to use something else,
# we just need to redfine it here.

LIBPTHREAD = "libpthread.so.0"

# These come from pthread.h (via bits/pthreadtypes.h)
# We prefer to be on a safe side and use sizes for 64 bit implementation

SIZEOF_MUTEX_T = 40
SIZEOF_COND_T = 48

MUTEX_T = C.c_char * SIZEOF_MUTEX_T
COND_T = C.c_char * SIZEOF_COND_T

# This work well for Linux, but will fail on other OSes, where pthread library
# may have other name. So this module is not cross-platform.

_libpthread = C.CDLL(LIBPTHREAD, use_errno=True)


class timespec(C.Structure):
    _fields_ = [("tv_sec", C.c_long),
                ("tv_nsec", C.c_long)]


class PthreadMutex(object):
    def __init__(self, attr=None):
        self._mutex = MUTEX_T()
        _libpthread.pthread_mutex_init(self._mutex, attr)

    def mutex(self):
        return self._mutex

    def lock(self):
        return _libpthread.pthread_mutex_lock(self._mutex)

    def unlock(self):
        return _libpthread.pthread_mutex_unlock(self._mutex)

    def trylock(self):
        return _libpthread.pthread_mutex_trylock(self._mutex)


class PthreadCond(object):
    def __init__(self, attr=None, mutex=None):
        self._cond = COND_T()
        self._lock = mutex
        _libpthread.pthread_cond_init(self._cond, attr)

    def signal(self):
        return _libpthread.pthread_cond_signal(self._cond)

    def broadcast(self):
        return _libpthread.pthread_cond_broadcast(self._cond)

    def wait(self, mutex=None):
        m = mutex if mutex else self._lock
        return _libpthread.pthread_cond_wait(self._cond, m.mutex())

    def timedwait(self, abstime, mutex=None):
        m = mutex if mutex else self._lock
        return _libpthread.pthread_cond_timedwait(self._cond, m.mutex(),
            C.pointer(abstime))
