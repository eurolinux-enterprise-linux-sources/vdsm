# Copyright 2009-2010 Red Hat, Inc. and/or its affiliates.
#
# Licensed to you under the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.  See the files README and
# LICENSE_GPL_v2 which accompany this distribution.
#

import os, glob, subprocess
import shlex
import logging, traceback
from fnmatch import fnmatch

import constants
import utils
from config import config

NET_CONF_PREF = utils.NET_CONF_DIR + 'ifcfg-'
PROC_NET_VLAN = '/proc/net/vlan/'

def nics():
    res = []
    for b in glob.glob('/sys/class/net/*/device'):
        nic = b.split('/')[-2]
        if not any(map(lambda p: fnmatch(nic, p),
                       config.get('vars', 'hidden_nics').split(',')) ):
            res.append(nic)
    return res

def bondings():
    return [ b.split('/')[-2] for b in glob.glob('/sys/class/net/*/bonding')]

def vlans():
    return [ b.split('/')[-1] for b in glob.glob('/sys/class/net/*.*')]

def bridges():
    return [ b.split('/')[-2] for b in glob.glob('/sys/class/net/*/bridge')]

def slaves(bonding):
    return [ b.split('/')[-1].split('_', 1)[-1] for b in
                glob.glob('/sys/class/net/' + bonding + '/slave_*')]

def ports(bridge):
    return os.listdir('/sys/class/net/' + bridge + '/brif')

def bridge_stp_state(bridge):
    stp = file('/sys/class/net/%s/bridge/stp_state' % bridge).readline()
    if stp == '1\n':
        return 'on'
    else:
        return 'off'

def speed(nic):
    cmd = [constants.EXT_ETHTOOL, nic]
    rc, out, err = utils.execCmd(cmd, sudo=(os.geteuid() != 0))
    try:
        for line in out:
            if line.startswith('\tSpeed: '):
                if line[8:15] == 'Unknown':
                    return 0
                speed = int(line[8:line.index('M')])
                return speed
    except:
        logging.error(traceback.format_exc())
    return 0

def ifconfig():
    """ Partial parser to ifconfig output """

    p = subprocess.Popen([constants.EXT_IFCONFIG, '-a'],
            close_fds=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
    out, err = p.communicate()
    ifaces = {}
    for ifaceblock in out.split('\n\n'):
        if not ifaceblock: continue
        addr = netmask = hwaddr = ''
        for line in ifaceblock.splitlines():
            if line[0] != ' ':
                ls = line.split()
                name = ls[0]
                if ls[2] == 'encap:Ethernet' and ls[3] == 'HWaddr':
                    hwaddr = ls[4]
            if line.startswith('          inet addr:'):
                sp = line.split()
                for col in sp:
                    if ':' not in col: continue
                    k, v = col.split(':')
                    if k == 'addr':
                        addr = v
                    if k == 'Mask':
                        netmask = v
        ifaces[name] = {'addr': addr, 'netmask': netmask, 'hwaddr': hwaddr}
    return ifaces

def graph():
    for bridge in bridges():
        print bridge
        for iface in os.listdir('/sys/class/net/' + bridge + '/brif'):
            print '\t' + iface
            if iface in vlans():
                iface = iface.split('.')[0]
            if iface in bondings():
                for slave in slaves(iface):
                    print '\t\t' + slave

def getVlanBondingNic(bridge):
    """Return the (vlan, bonding, nics) tupple that belogs to bridge."""

    if bridge not in bridges():
        raise ValueError, 'unknown bridge %s' % (bridge,)
    vlan = bonding = ''
    nics = []
    for iface in os.listdir('/sys/class/net/' + bridge + '/brif'):
        if iface in vlans():
            iface, vlan = iface.split('.')
        if iface in bondings():
            bonding = iface
            nics = slaves(iface)
        else:
            nics = [iface]
    return vlan, bonding, nics

def getIfaceCfg(iface):
    d = {}
    try:
        for line in open(NET_CONF_PREF + iface).readlines():
            line = line.strip()
            if line.startswith('#'): continue
            try:
                k, v = line.split('=', 1)
                d[k] = ''.join(shlex.split(v))
            except:
                pass
    except:
        pass
    return d

def permAddr():
    paddr = {}
    for b in bondings():
        slave = ''
        for line in file('/proc/net/bonding/' + b):
            if line.startswith('Slave Interface: '):
                slave = line[len('Slave Interface: '):-1]
            if line.startswith('Permanent HW addr: '):
                addr = line[len('Permanent HW addr: '):-1]
                paddr[slave] = addr.upper()
    return paddr

def get():
    d = {}
    ifaces = ifconfig()
    # FIXME handle bridge/nic missing from ifconfig
    d['networks'] = dict([ (bridge, {'ports': ports(bridge),
                                     'stp': bridge_stp_state(bridge),
                                     'addr': ifaces[bridge]['addr'],
                                     'netmask': ifaces[bridge]['netmask'],
                                     'cfg': getIfaceCfg(bridge)})
                           for bridge in bridges() ])
    d['nics'] = dict([ (nic, {'speed': speed(nic),
                              'addr': ifaces[nic]['addr'],
                              'netmask': ifaces[nic]['netmask'],
                              'hwaddr': ifaces[nic]['hwaddr']})
                        for nic in nics() ])
    paddr = permAddr()
    for nic, nd in d['nics'].iteritems():
        if paddr.get(nic):
            nd['permhwaddr'] = paddr[nic]
    d['bondings'] = dict([ (bond, {'slaves': slaves(bond),
                              'addr': ifaces[bond]['addr'],
                              'netmask': ifaces[bond]['netmask'],
                              'hwaddr': ifaces[bond]['hwaddr'],
                              'cfg': getIfaceCfg(bond)})
                        for bond in bondings() ])
    d['vlans'] = dict([ (vlan, {'iface': vlan.split('.')[0],
                                'addr': ifaces[vlan]['addr'],
                                'netmask': ifaces[vlan]['netmask']})
                        for vlan in vlans() ])
    return d

def getVlanDevice(vlan):
    """ Return the device of the given VLAN. """
    dev = None

    if os.path.exists(PROC_NET_VLAN + vlan):
        for line in file(PROC_NET_VLAN + vlan).readlines():
            if "Device:" in line:
                dummy, dev = line.split()
                break

    return dev

def getVlanID(vlan):
    """ Return the ID of the given VLAN. """
    id = None

    if os.path.exists(PROC_NET_VLAN):
        for line in file(PROC_NET_VLAN + vlan).readlines():
            if "VID" in line:
                id = line.split()[2]
                break

    return id

