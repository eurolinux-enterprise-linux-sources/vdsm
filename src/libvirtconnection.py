# Copyright 2009, 2010 Red Hat, Inc. and/or its affiliates.
#
# Licensed to you under the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.  See the files README and
# LICENSE_GPL_v2 which accompany this distribution.
#

import libvirt
import libvirtev
import constants
import traceback

def __eventCallback(conn, dom, *args):
    try:
        cif, eventid = args[-1]
        vmid = dom.UUIDString()
        v = cif.vmContainer.get(vmid)

        if not v:
            cif.log.debug('unkown vm %s eventid %s args %s', vmid, eventid, args)
            return

        if eventid == libvirt.VIR_DOMAIN_EVENT_ID_LIFECYCLE:
            event, detail = args[:-1]
            v._onLibvirtLifecycleEvent(event, detail, None)
        elif eventid == libvirt.VIR_DOMAIN_EVENT_ID_REBOOT:
            v.onReboot(False)
        elif eventid == libvirt.VIR_DOMAIN_EVENT_ID_RTC_CHANGE:
            utcoffset, = args[:-1]
            v._rtcUpdate(utcoffset)
        elif eventid == libvirt.VIR_DOMAIN_EVENT_ID_IO_ERROR_REASON:
            srcPath, devAlias, action, reason = args[:-1]
            v._onAbnormalStop(devAlias, reason)
        elif eventid == libvirt.VIR_DOMAIN_EVENT_ID_GRAPHICS:
            phase, localAddr, remoteAddr, authScheme, subject = args[:-1]
            v.log.debug('graphics event phase %s localAddr %s remoteAddr %s'
                        'authScheme %s subject %s',
                        phase, localAddr, remoteAddr, authScheme, subject)
            if phase == libvirt.VIR_DOMAIN_EVENT_GRAPHICS_INITIALIZE:
                v.onConnect(remoteAddr['node'])
            elif phase == libvirt.VIR_DOMAIN_EVENT_GRAPHICS_DISCONNECT:
                v.onDisconnect()
        else:
            v.log.warning('unkown eventid %s args %s', eventid, args)
    except:
        cif.log.error(traceback.format_exc())

__connection = None
def get(cif=None):
    """Return current connection to libvirt or open a new one.

    Wrap methods of connection object so that they catch disconnection, and
    take vdsm down.
    """
    def wrapMethod(f):
        def wrapper(*args, **kwargs):
            try:
                ret = f(*args, **kwargs)
                if isinstance(ret, libvirt.virDomain):
                    for name in dir(ret):
                        method = getattr(ret, name)
                        if callable(method) and name[0] != '_':
                            setattr(ret, name, wrapMethod(method))
                return ret
            except libvirt.libvirtError, e:
                if (e.get_error_domain() == libvirt.VIR_FROM_REMOTE and
                    e.get_error_code() == libvirt.VIR_ERR_SYSTEM_ERROR):
                    cif.log.error('connection to libvirt broken. '
                                  'taking vdsm down.')
                    cif.prepareForShutdown()
                elif e.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
                    raise
        wrapper.__name__ = f.__name__
        wrapper.__doc__ = f.__doc__
        return wrapper

    def req(credentials, user_data):
        for cred in credentials:
            if cred[0] == libvirt.VIR_CRED_AUTHNAME:
                cred[4] = 'vdsm@rhevh'
            elif cred[0] == libvirt.VIR_CRED_PASSPHRASE:
                cred[4] = file(constants.P_VDSM_KEYS +
                               'libvirt_password').read()
        return 0

    auth = [[libvirt.VIR_CRED_AUTHNAME, libvirt.VIR_CRED_PASSPHRASE], req, None]

    global __connection
    if not __connection:
        libvirtev.virEventLoopPureStart()
        __connection = libvirt.openAuth('qemu:///system', auth, 0)
        if cif != None:
            for ev in (libvirt.VIR_DOMAIN_EVENT_ID_LIFECYCLE,
                       libvirt.VIR_DOMAIN_EVENT_ID_REBOOT,
                       libvirt.VIR_DOMAIN_EVENT_ID_RTC_CHANGE,
                       libvirt.VIR_DOMAIN_EVENT_ID_IO_ERROR_REASON,
                       libvirt.VIR_DOMAIN_EVENT_ID_GRAPHICS):
                __connection.domainEventRegisterAny(None, ev, __eventCallback, (cif, ev))
            for name in dir(libvirt.virConnect):
                method = getattr(__connection, name)
                if callable(method) and name[0] != '_':
                    setattr(__connection, name, wrapMethod(method))

    return __connection
