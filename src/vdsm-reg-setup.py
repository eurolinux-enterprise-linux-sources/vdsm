#!/usr/bin/python
# Script to setup basic vdsm environment and register the VDSM with VDC.
# Input: none.
# Output: none.
#
# Steps to perform: Initiate Certificate Initalization
#   a. Find menagement bridge and rename it to rhevm.
#   b. Create .ssh directory and fetch authorized_keys
#   c. Call VDC registration.
#   d. Set time according to rhev-m time.

import sys
import getopt
import os
import socket
import httplib
import time
import logging
import logging.config
import traceback
import urllib
import ssl
from config import config
import deployUtil

DEFAULT_CONFIG_FILE="/etc/vdsm-reg/vdsm-reg.conf"
VDSM_CONF="/etc/vdsm/vdsm.conf"
SCRIPT_NAME_SAVE="vdsm-store-net-config"
MGT_BRIDGE_NAME="rhevm"

class Setup:
    """
    Makes sure that vdsmd has its Certificate in place
    """
    def __init__(self, oConfig):
        logging.debug("__init__ begin.")
        self.registered = False
        self.fInitOK = True
        config = oConfig
        self.vdcURL = "None"
        self.vdcName = config.get('vars', 'vdc_host_name')
        if self.vdcName != "None":
            try: self.vdcURL = socket.gethostbyname(self.vdcName)
            except: self.vdcURL = "None"
        else:
            self.vdcURL = config.get('vars', 'vdc_host_ip')

        self.vdsmDir = config.get('vars', 'vdsm_dir')
        if self.vdcURL != "None":
            self.ovirtURL = deployUtil.getMGTIP(self.vdsmDir, self.vdcName)
            self.ovirtName = socket.getfqdn()
            self.ovirtUID = deployUtil.getHostID()
        else:
            self.ovirtURL = "None"
            self.ovirtName = "None"
            self.ovirtUID = "None"

        self.vdcPORT = config.get('vars', 'vdc_host_port')
        self.vdcURI = config.get('vars', 'vdc_reg_uri')
        self.vdcRegPort = config.get('vars', 'vdc_reg_port')
        self.VDCTime = None
        logging.debug("Setup::__init__ vars:" +
            "\n\tself.vdcURL " + self.vdcURL +
            "\n\tself.vdcPORT " + self.vdcPORT +
            "\n\tself.vdcURI " + self.vdcURI +
            "\n\tself.vdcRegPort " + self.vdcRegPort +
            "\n\tself.ovirtURL " + self.ovirtURL +
            "\n\tself.ovirtName " + self.ovirtName +
            "\n\tself.ovirtUID " + self.ovirtUID +
            "\n\tself.vdcName " + self.vdcName
        )

    def validateData(self):
        logging.debug("validate start")
        fReturn = True

        if self.ovirtURL == None or self.ovirtURL == "" or self.ovirtURL == "None" or \
           self.ovirtName  == None or self.ovirtName == "" or self.ovirtName == "None":
            fReturn = False

        logging.debug("validate end. return: " + str(fReturn))
        return fReturn

    def renameBridge(self):
        """
            Rename oVirt default bridge to rhevm bridge.
        """
        logging.debug("renameBridge begin.")
        fReturn = True

        #Rename existing bridge
        fReturn = deployUtil.makeBridge(self.vdcName, self.vdsmDir)
        if not fReturn:
            logging.error("renameBridge Failed to rename existing bridge!")

        #Persist changes
        if fReturn:
            try:
                out, err, ret = deployUtil._logExec([os.path.join(self.vdsmDir, SCRIPT_NAME_SAVE)])
                if ret:
                    fReturn = False
                    logging.error("renameBridge Failed to persist rhevm bridge changes. out=" + out + "\nerr=" + str(err) + "\nret=" + str(ret))
            except:
                fReturn = False
                logging.error("renameBridge Failed to persist bridge changes. out=" + out + "\nerr=" + str(err) + "\nret=" + str(ret))

        #Fix file permissions to relevant mask
        if fReturn:
            try:
                os.chmod("/config/etc/sysconfig/network-scripts/ifcfg-" + MGT_BRIDGE_NAME, 0644)
            except:
                fReturn = False
                logging.error("renameBridge: failed to chmod bridge file")

        logging.debug("renameBridge return.")
        return fReturn

    def registerVDS(self):
        logging.debug("registerVDS begin.")

        strFullURI = (
            self.vdcURI + "?vds_ip=" + urllib.quote(self.ovirtURL) +
            "&vds_name=" + urllib.quote(self.ovirtName) +
            "&vds_unique_id=" + urllib.quote(self.ovirtUID) +
            "&port=" + urllib.quote(self.vdcRegPort) +
            "&__VIEWSTATE="
        )
        logging.debug("registerVDS URI= " + strFullURI + "\n")
        if self.ovirtUID == "None" and "localhost.localdomain" in self.ovirtName:
            logging.warn("WARNING! registering RHEV-H with no UUID and no unique host-name!")

        fReturn = True
        res = None
        nTimeout = int(config.get('vars', 'test_socket_timeout'))
        old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(nTimeout)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        try:
            sock.connect((self.vdcURL, int(self.vdcPORT)))
            conn = httplib.HTTPSConnection(self.vdcURL + ":" + self.vdcPORT)
            conn.sock = ssl.wrap_socket(sock)
            conn.request("GET", strFullURI)
            res = conn.getresponse()
        except:
           logging.debug("registerVDS failed in HTTPS. Retrying using HTTP.")
           try:
                conn = None
                conn = httplib.HTTPConnection(self.vdcURL + ":" + self.vdcPORT)
                conn.request("GET", strFullURI)
                res = conn.getresponse()
                logging.debug("registerVDS succeeded using HTTP.")
           except:
                fReturn = False
                logging.error("registerVDS failed using HTTP!")
                logging.error(traceback.format_exc())

        else:
            logging.debug("registerVDS status: " + str(res.status) +
                " reason: " + res.reason
            )

        if res == None or res.status != 200:
            if conn != None: conn.close()
            fReturn = False

        if fReturn:
            try:
                try:
                    self.VDCTime = res.read()
                    logging.debug("registerVDS time read: " + str(self.VDCTime))
                except:
                    fReturn = False
                    logging.error(traceback.format_exc())
            finally:
                if conn != None: conn.close()

        socket.setdefaulttimeout(old_timeout)
        logging.debug("registerVDS end.")
        return fReturn

    def execute(self):
        fOK = True
        fOKNow = True
        logging.debug("execute start.")
        self.registered = False

        if deployUtil.preventDuplicate():
            logging.debug("execute: found existing management bridge. Skipping rename.")
        else:
            fOK = self.renameBridge()
            logging.debug("execute: after renameBridge: " + str(fOK))

        if fOK:
            strKey = deployUtil.getAuthKeysFile(self.vdcURL, self.vdcPORT)
            if strKey is not None:
                fOKNow = deployUtil.handleSSHKey(strKey)
            else:
                fOKNow = False
            fOK = fOK and fOKNow
            logging.debug("execute: after getAuthKeysFile: " + str(fOK))

        if fOK:
            fOKNow = self.registerVDS()
            fOK = fOK and fOKNow
            logging.debug("execute: after registerVDS: " + str(fOK))

        if fOK:
            self.registered = True

        logging.debug("Registration status:" +
            str(self.registered)
        )

        # Utility settings. This will not fail the registration process.
        if self.registered == True:
            fOK = (fOK and deployUtil.setHostTime(self.VDCTime))
            if fOK:
                logging.debug("Node time in sync with RHEVM server")
            else:
                logging.warning("Node time failed to sync with RHEVM server. This may cause problems !")

def run(confFile, daemonize):
    #print "entered run(conf='%s', daemonize='%s')"%(confFile,str(daemonize))
    import random

    registered = False
    log = None
    pidfile = False
    try:
        try:
            config.read([confFile])
            loggerConf = config.get('vars','logger_conf')
            vdsmDir = config.get('vars', 'vdsm_dir')
            pidfile = config.get('vars','pidfile')
            sleepTime = float(config.get('vars','reg_req_interval'))
            sys.path.append(vdsmDir)
            import utils # taken from vdsm rpm

            if daemonize:
                utils.createDaemon()
            #set up logger
            logging.config.fileConfig(loggerConf)
            log = logging.getLogger('')
            if daemonize:
                log = logging.getLogger('vdsRegistrator')
            if not daemonize:
                log.handlers.append(logging.StreamHandler())

            log.info("After daemonize - My pid is " + str(os.getpid()))
            file(pidfile, 'w').write(str(os.getpid()) + "\n")
            os.chmod(pidfile, 0664)

            itr = 0
            while daemonize and not registered:
                oSetup = Setup(config)
                if oSetup.validateData():
                    oSetup.execute()
                registered = oSetup.registered;
                oSetup = None
                itr += 1
                nRandom = random.randint(1, 5)
                if not registered:
                    # wait random time, so multiple machines access randomly.
                    time.sleep(sleepTime + nRandom)

                log.debug("Total retry count: %d, waited: %d seconds." % (itr, sleepTime+nRandom))
        except:
            if log is not None:
                log.error(traceback.format_exc())
    finally:
        if pidfile and os.path.exists(pidfile):
            os.unlink(pidfile)
        if log:
            log.info("Exiting ....")

def usage():
    print "Usage: %s [-c <config_file>]"
    print "    -c         - configuration file"
    print "    -l         - run local (= no daemon)"
    print "    -h,--help  - prints this usage"

if __name__ == "__main__":
    config_file = DEFAULT_CONFIG_FILE
    daemonize = True
    try:
        opts,args = getopt.getopt(sys.argv[1:], "hc:l",["help"])
        for o,v in opts:
            if o == "-h" or o == "--help":
                usage()
                sys.exit(0)
            elif o == "-c":
                config_file = v
                if not os.path.exists(config_file):
                    print "ERROR: file %s does not exist"%(config_file)
                    usage()
                    sys.exit(1)
            elif o == "-l":
                daemonize = False
    except getopt.GetoptError,e:
        print "ERROR: '%s'"%(e.msg)
        usage()
        sys.exit(1)
    run(config_file, daemonize)
