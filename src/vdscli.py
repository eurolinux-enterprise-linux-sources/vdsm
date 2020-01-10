# vdscli: contact vdsm running on localhost over xmlrpc easily
#
# Copyright 2009-2010 Red Hat, Inc.
#
# Licensed to you under the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

import xmlrpclib
import subprocess
import sys
import os

d_useSSL = False
d_tsPath = '@TRUSTSTORE@'
d_addr = '0'
d_port = '54321'

def __guessDefaults():
    global d_useSSL, d_tsPath, d_addr, d_port
    VDSM_DIR = '/usr/share/vdsm'
    VDSM_CONF = '/etc/vdsm/vdsm.conf'
    try:
        try:
            from config import config
            config.read(VDSM_CONF)
            d_useSSL = config.getboolean('vars', 'ssl')
            d_tsPath = config.get('vars', 'trust_store_path')
            d_port = config.get('addresses', 'management_port')
            if d_useSSL:
                d_addr = __getLocalVdsName(d_tsPath)
            else:
                if config.get('addresses', 'management_ip'):
                    d_addr = config.get('addresses', 'management_ip')
                else:
                    import netinfo
                    proposed_addr = netinfo.ifconfig()['rhevm']['addr']
                    if proposed_addr:
                        d_addr = proposed_addr
        except:
            pass
        if os.name == 'nt':
            def getRHEVMInstallPath():
                import _winreg
                key_path = 'SOFTWARE\\RedHat\\RHEVM Service'
                root = _winreg.OpenKey(_winreg.HKEY_LOCAL_MACHINE, key_path)
                val, v_type = _winreg.QueryValueEx(root,"Location")
                retval = os.path.dirname(os.path.dirname(val))
                return str(retval)
            d_tsPath = os.path.join(getRHEVMInstallPath(), "Service", "ca")
    except:
        pass

def __getLocalVdsName(tsPath):
    p = subprocess.Popen(['openssl', 'x509', '-noout', '-subject', '-in',
            '%s/certs/vdsmcert.pem' % tsPath],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
    out, err = p.communicate()
    if p.returncode != 0:
        return '0'
    return out.split('=')[-1].strip()

__guessDefaults()

def cannonizeAddrPort(addrport=None):
    if addrport is None or addrport == '0':
        return d_addr + ':' + d_port
    elif ':' in addrport:
        return addrport
    else:
        return addrport + ':' + d_port

def connect(addrport=None, useSSL=None, tsPath=None):
    addrport = cannonizeAddrPort(addrport)
    if useSSL is None: useSSL = d_useSSL
    if tsPath is None: tsPath = d_tsPath
    if useSSL:
        from M2Crypto.m2xmlrpclib import SSL_Transport
        from M2Crypto import SSL

        if os.name == 'nt':
            KEYFILE = tsPath + '\\keys\\rhevm.pem'
            CERTFILE = tsPath + '\\certs\\rhevm.cer'
            CACERT = tsPath + '\\certs\\ca.pem'
        else:
            KEYFILE = tsPath + '/keys/vdsmkey.pem'
            CERTFILE = tsPath + '/certs/vdsmcert.pem'
            CACERT = tsPath + '/certs/cacert.pem'

        ctx = SSL.Context ('sslv3')

        ctx.set_verify(SSL.verify_peer | SSL.verify_fail_if_no_peer_cert, 16)
        ctx.load_verify_locations(CACERT)
        ctx.load_cert(CERTFILE, KEYFILE, lambda v: "mypass")

        server = xmlrpclib.Server('https://%s' % addrport,
                                SSL_Transport(ctx))
    else:
        server = xmlrpclib.Server('http://%s' % addrport)
    return server

if __name__ == '__main__':
    print 'connecting to %s:%s ssl %s ts %s' % (d_addr, d_port, d_useSSL, d_tsPath)
    server = connect()
    print server.getVdsCapabilities()
