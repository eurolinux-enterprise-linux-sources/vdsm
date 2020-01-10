%define vdsm_reg vdsm-reg
%define vdsm_name vdsm
%include %{_rpmconfigdir}/macros.python

Summary: Virtual Desktop Server Manager
Name: vdsm
Source: vdsm-4.9-61.git0131bf.tar.gz
# Url: no upstream project exists
# tarball built from internal git repo with
#       make tarball rpmversion=<version> rpmrelease=<release>
Version: 4.9
Release: 63%{?dist}
License: GPLv2+

Group: Applications/System
BuildRoot: %{_tmppath}/%{name}-%{version}-%{release}
ExclusiveArch: x86_64
BuildRequires: python redhat-lsb redhat-rpm-config
Requires: python which
Requires: sudo >= 1.7.3
Requires: qemu-kvm
Requires: qemu-img m2crypto ethtool logrotate
Requires: iscsi-initiator-utils >= 6.2.0.872-15
Requires: nfs-utils dmidecode
Requires: lvm2 >= 2.02.72-8.el6_0.4
Requires: device-mapper-multipath >= 0.4.9-31.el6
Requires: psmisc >= 22.6-15.el6_0.1
Requires: fence-agents
Requires: bridge-utils
Requires: sos
Requires: tunctl
Requires: libvirt >= 0.8.7-5
Requires: libvirt-python
Requires: dosfstools
Requires: policycoreutils-python
Requires(post): cyrus-sasl-lib
Patch2: 0002-Always-use-uncached-tags-when-reading-metadata.patch
Patch3: 0003-Fixed-master-mount-validation-for-file-domains.patch

%description
The VDSM service is required by a RHEV Manager to manage RHEV Hypervisors
and Red Hat Enterprise Linux hosts. VDSM manages and monitors the host's
storage, memory and networks as well as virtual machine creation, other host
administration tasks, statistics gathering, and log collection.

%prep
%setup -c -q
%patch2 -p1
%patch3 -p1

%build
make -C vdsm CFLAGS="$RPM_OPT_FLAGS"
baserelease=`echo "%{release}" | sed 's/%{?dist}$//'`
sed -i 's/^software_version =.*/software_version = "%{version}"/;s/software_revision =.*/software_revision = "'"$baserelease"'"/' re/dsaversion.py

make -C vds_bootstrap CFLAGS="$RPM_OPT_FLAGS"

%install
rm -rf "%{buildroot}"
mkdir -p "%{buildroot}"
make -C vdsm PREFIX="%{buildroot}" \
    VDSMDIR=%{_datadir}/%{vdsm_name} \
    VDSMLOGDIR=%{_localstatedir}/log/%{vdsm_name} \
    TRUSTSTORE=%{_sysconfdir}/pki/%{vdsm_name} \
    BINDIR=%{_bindir} \
    LIBEXECDIR=%{_libexecdir}/%{vdsm_name} \
    CONFDIR=%{_sysconfdir}/%{vdsm_name} \
    VDSMRUNDIR=%{_localstatedir}/run/%{vdsm_name} \
    VDSMLIBDIR=%{_localstatedir}/lib/%{vdsm_name} \
    SOSPLUGINDIR=%{py_sitedir}/sos/plugins \
    install
mkdir -p "%{buildroot}"%{_localstatedir}/log/core

# hook vhostmd
make -C vdsm/hooks/vhostmd PREFIX="%{buildroot}" \
    LIBEXECDIR=%{_libexecdir}/%{vdsm_name} \
    install

# hook faqemu
make -C vdsm/hooks/faqemu PREFIX="%{buildroot}" \
    LIBEXECDIR=%{_libexecdir}/%{vdsm_name} \
    install

make -C vdsm_cli PREFIX="%{buildroot}" \
    CONFDIR=%{_sysconfdir}/%{vdsm_name} \
    BINDIR=%{_bindir} \
    COMPDIR=%{_sysconfdir}/bash_completion.d \
    VDSMDIR=%{_datadir}/%{vdsm_name} \
    TRUSTSTORE=%{_sysconfdir}/pki/%{vdsm_name} \
    TARGET=%{_datadir}/%{vdsm_name} install
cp vds_bootstrap/vds_{qualifier,bootstrap}.py "%{buildroot}"/%{_datadir}/%{vdsm_name}

#vdsm-reg
make -C vdsm_reg \
    PREFIX="%{buildroot}" \
    ETC=%{_sysconfdir} \
    CONFDIR=%{_sysconfdir}/%{vdsm_reg} \
    LOGDIR=%{_localstatedir}/log/%{vdsm_reg} \
    VDSMREGDIR=%{_datadir}/%{vdsm_reg} \
    TRUSTSTORE=%{_sysconfdir}/pki/%{vdsm_name} \
    VDSMRUNDIR=%{_localstatedir}/run/%{vdsm_name} \
    install

%clean
%{__rm} -rf %{buildroot}

%pre
getent passwd vdsm > /dev/null || /usr/sbin/useradd -u 36 -g kvm -o -r vdsm -c "RHEV node manager" -d / -s /sbin/nologin
/usr/sbin/usermod -a -G qemu vdsm

%post
tmp_sudoers=$(mktemp)
cp -a /etc/sudoers $tmp_sudoers
/bin/sed -i -e "/# vdsm/,/# end vdsm/d" $tmp_sudoers

if ! grep -q "^#includedir /etc/sudoers.d" "$tmp_sudoers";
then
    cat >> $tmp_sudoers <<EOF
# vdsm customizations
#include /etc/sudoers.d/50_vdsm
# end vdsm customizations
EOF
fi

if outerr=$(/usr/sbin/visudo -c -f $tmp_sudoers 2>&1) ; then
    /bin/cp -a $tmp_sudoers /etc/sudoers
else
    echo "Failed to add vdsm section to /etc/sudoers" 1>&2
    echo "$outerr" 1>&2
fi
rm -f $tmp_sudoers

# vdsm is intentionally on by default.
/sbin/chkconfig --add vdsmd

# create vdsm "secret" password for libvirt, if none exists
pfile=/etc/pki/%{vdsm_name}/keys/libvirt_password
if [[ ! -f "$pfile" ]];
then
    umask 077
    echo -n shibboleth > "$pfile"
    /bin/chown vdsm:kvm "$pfile"
    new_pwd=1
fi
if ! /usr/sbin/sasldblistusers2 -f /etc/libvirt/passwd.db 2>- | \
    /bin/grep -q '^vdsm@rhevh\b' || [[ -n "$new_pwd" ]] ;
then
    /usr/sbin/saslpasswd2 -p -a libvirt vdsm@rhevh < "$pfile"
fi

%preun
if [ "$1" -eq 0 ]
then
        /sbin/service vdsmd stop > /dev/null 2>&1 || :
        /sbin/chkconfig --del vdsmd

        tmp_sudoers=$(mktemp)
        cp -a /etc/sudoers $tmp_sudoers
        /bin/sed -i -e "/# vdsm/,/# end vdsm/d" $tmp_sudoers
        if outerr=$(/usr/sbin/visudo -c -f $tmp_sudoers 2>&1) ; then
            /bin/cp -a $tmp_sudoers /etc/sudoers
        else
            echo "Failed to add vdsm section to /etc/sudoers" 1>&2
            echo "$outerr" 1>&2
        fi
        rm -f $tmp_sudoers

        lconf=/etc/libvirt/libvirtd.conf
        qconf=/etc/libvirt/qemu.conf
        ldconf=/etc/sysconfig/libvirtd
        sed -i '/# by vdsm$/d' $lconf $qconf $ldconf

        /usr/sbin/semanage boolean -m -S targeted -F /dev/stdin  << _EOF
virt_use_nfs=0
_EOF

        if selinuxenabled; then
            setsebool virt_use_nfs off
        fi
fi

%postun
if [ "$1" -ge 1 ]; then
        /sbin/service vdsmd condrestart > /dev/null 2>&1
fi
exit 0

%package hook-vhostmd
Summary: VDSM hook set for interaction with vhostmd
Group: Applications/System
Requires: vhostmd

%description hook-vhostmd
start vhostmd and use it per VM according to requests from RHEV-M

%package debug-plugin
Summary:       VDSM Debug Plugin
Requires:      vdsm

%description debug-plugin
Used by the trained monkeys at Red Hat to insert chaos and mayhem in to VDSM

%package cli
Summary: VDSM command line interface
Group: Applications/System
Requires: m2crypto

%description cli
Call VDSM commands from the command line. Used for testing and debugging.

%package reg
Summary: VDSM registration package
Group: Applications/System
Requires: %{name} = %{version}-%{release}
Requires: traceroute

%description reg
VDSM registration package. Used to register a RHEV hypervisor to a RHEV
Manager.

%post reg
/sbin/chkconfig --add vdsm-reg

%preun reg
if [ "$1" -eq 0 ]
then
        /sbin/service vdsm-reg stop > /dev/null 2>&1
        /sbin/chkconfig --del vdsm-reg
fi

%package hook-faqemu
Summary: Fake qemu process for VDSM quality assurance
Group: Applications/System

%description hook-faqemu
The faqemu process is used for testing VDSM with multiple, fake, virtual
machines without running real guests.

%files
%defattr(-,root,root,-)
%dir %{_libexecdir}/%{vdsm_name}
%dir %{_datadir}/%{vdsm_name}
%dir %{_datadir}/%{vdsm_name}/storage
%{_datadir}/%{vdsm_name}/define.py*
%{_datadir}/%{vdsm_name}/clientIF.py*
%{_datadir}/%{vdsm_name}/utils.py*
%{_datadir}/%{vdsm_name}/constants.py*
%{_datadir}/%{vdsm_name}/vm.py*
%{_datadir}/%{vdsm_name}/supervdsm.py*
%{_datadir}/%{vdsm_name}/supervdsmServer.py*
%{_datadir}/%{vdsm_name}/libvirtvm.py*
%{_datadir}/%{vdsm_name}/libvirtconnection.py*
%{_datadir}/%{vdsm_name}/hooks.py*
%{_datadir}/%{vdsm_name}/hooking.py*
%{_datadir}/%{vdsm_name}/libvirtev.py*
%attr (755,root,root) %{_datadir}/%{vdsm_name}/vdsm
%attr (755,root,root) %{_datadir}/%{vdsm_name}/vdsm-restore-net-config
%attr (755,root,root) %{_datadir}/%{vdsm_name}/vdsm-store-net-config
%attr (755,root,root) %{_datadir}/%{vdsm_name}/write-net-config
%attr (755,root,root) %{_datadir}/%{vdsm_name}/mk_sysprep_floppy
%attr (755,root,root) %{_datadir}/%{vdsm_name}/get-vm-pid
%attr (755,root,root) %{_datadir}/%{vdsm_name}/prepare-vmchannel
%doc vdsm/vdsm.conf.sample
%config(noreplace) %{_sysconfdir}/%{vdsm_name}/logger.conf
%config(noreplace) %{_sysconfdir}/logrotate.d/vdsm
%config(noreplace) %{_sysconfdir}/rwtab.d/vdsm
%attr (440,root,root) %{_sysconfdir}/sudoers.d/50_vdsm
%{_sysconfdir}/cron.hourly/vdsm-logrotate
%{_datadir}/%{vdsm_name}/guestIF.py*
%{_datadir}/%{vdsm_name}/logUtils.py*
%{_datadir}/%{vdsm_name}/dsaversion.py*
%{_datadir}/%{vdsm_name}/pthread.py*
%{_datadir}/%{vdsm_name}/betterThreading.py*
%attr (755,root,root) %{_datadir}/%{vdsm_name}/logCollector.sh
%attr (755,root,root) %{_libexecdir}/%{vdsm_name}/persist-vdsm-hooks
%attr (755,root,root) %{_libexecdir}/%{vdsm_name}/unpersist-vdsm-hook
%{_datadir}/%{vdsm_name}/storage/__init__.py*
%{_datadir}/%{vdsm_name}/storage/dispatcher.py*
%{_datadir}/%{vdsm_name}/storage/storage_exception.py*
%{_datadir}/%{vdsm_name}/storage/sp.py*
%{_datadir}/%{vdsm_name}/storage/sd.py*
%{_datadir}/%{vdsm_name}/storage/spm.py*
%{_datadir}/%{vdsm_name}/storage/hsm.py*
%{_datadir}/%{vdsm_name}/storage/hba.py*
%{_datadir}/%{vdsm_name}/storage/safelease.py*
%{_datadir}/%{vdsm_name}/storage/image.py*
%{_datadir}/%{vdsm_name}/storage/fileSD.py*
%{_datadir}/%{vdsm_name}/storage/nfsSD.py*
%{_datadir}/%{vdsm_name}/storage/localFsSD.py*
%{_datadir}/%{vdsm_name}/storage/blockSD.py*
%{_datadir}/%{vdsm_name}/storage/volume.py*
%{_datadir}/%{vdsm_name}/storage/fileVolume.py*
%{_datadir}/%{vdsm_name}/storage/blockVolume.py*
%{_datadir}/%{vdsm_name}/storage/taskManager.py*
%{_datadir}/%{vdsm_name}/storage/threadPool.py*
%{_datadir}/%{vdsm_name}/storage/task.py*
%{_datadir}/%{vdsm_name}/storage/threadLocal.py*
%{_datadir}/%{vdsm_name}/storage/resourceManager.py*
%{_datadir}/%{vdsm_name}/storage/storage_connection.py*
%{_datadir}/%{vdsm_name}/storage/storage_mailbox.py*
%{_datadir}/%{vdsm_name}/storage/storageConstants.py*
%{_datadir}/%{vdsm_name}/storage/fileUtils.py*
%{_datadir}/%{vdsm_name}/storage/misc.py*
%{_datadir}/%{vdsm_name}/storage/lvm.py*
%{_datadir}/%{vdsm_name}/storage/resourceFactories.py*
%{_datadir}/%{vdsm_name}/storage/outOfProcess.py*
%{_datadir}/%{vdsm_name}/storage/processPool.py*
%{_datadir}/%{vdsm_name}/storage/iscsi.py*
%{_datadir}/%{vdsm_name}/storage/multipath.py*
%{_datadir}/%{vdsm_name}/storage/sdc.py*
%{_datadir}/%{vdsm_name}/storage/sdf.py*
%{_datadir}/%{vdsm_name}/storage/persistentDict.py*
%attr (755,root,root) %{_libexecdir}/%{vdsm_name}/safelease
%attr (755,root,root) %{_libexecdir}/%{vdsm_name}/spmprotect.sh
%attr (755,root,root) %{_libexecdir}/%{vdsm_name}/spmstop.sh
%dir %{_libexecdir}/%{vdsm_name}/hooks/before_vm_start
%dir %{_libexecdir}/%{vdsm_name}/hooks/after_vm_start
%dir %{_libexecdir}/%{vdsm_name}/hooks/before_vm_cont
%dir %{_libexecdir}/%{vdsm_name}/hooks/after_vm_cont
%dir %{_libexecdir}/%{vdsm_name}/hooks/before_vm_pause
%dir %{_libexecdir}/%{vdsm_name}/hooks/after_vm_pause
%dir %{_libexecdir}/%{vdsm_name}/hooks/before_vm_hibernate
%dir %{_libexecdir}/%{vdsm_name}/hooks/after_vm_hibernate
%dir %{_libexecdir}/%{vdsm_name}/hooks/before_vm_dehibernate
%dir %{_libexecdir}/%{vdsm_name}/hooks/after_vm_dehibernate
%dir %{_libexecdir}/%{vdsm_name}/hooks/before_vm_migrate_source
%dir %{_libexecdir}/%{vdsm_name}/hooks/after_vm_migrate_source
%dir %{_libexecdir}/%{vdsm_name}/hooks/before_vm_migrate_destination
%dir %{_libexecdir}/%{vdsm_name}/hooks/after_vm_migrate_destination
%dir %{_libexecdir}/%{vdsm_name}/hooks/after_vm_destroy
%dir %{_libexecdir}/%{vdsm_name}/hooks/before_vdsm_start
%dir %{_libexecdir}/%{vdsm_name}/hooks/after_vdsm_stop
%attr (755,root,root) %{_datadir}/%{vdsm_name}/addNetwork
%attr (755,root,root) %{_datadir}/%{vdsm_name}/delNetwork
%attr (755,root,root) %{_datadir}/%{vdsm_name}/editNetwork
%attr (755,root,root) %{_datadir}/%{vdsm_name}/respawn
%{_datadir}/%{vdsm_name}/SecureXMLRPCServer.py*
%attr (755,root,root) %{_datadir}/%{vdsm_name}/get-conf-item
%attr (755,root,root) %{_datadir}/%{vdsm_name}/set-conf-item
%{_datadir}/%{vdsm_name}/kaxmlrpclib.py*
%{_datadir}/%{vdsm_name}/config.py*
%{_datadir}/%{vdsm_name}/QemuMonitor.py*
%{_datadir}/%{vdsm_name}/ksm.py*
%{_datadir}/%{vdsm_name}/netinfo.py*
%{_datadir}/%{vdsm_name}/neterrors.py*
%attr (755,root,root) %{_datadir}/%{vdsm_name}/img_verifier
%{_sysconfdir}/udev/rules.d/12-vdsm-lvm.rules
# this is not commonplace, but we want /var/log/core to be a world-writable
# dropbox for core dumps.
%dir %attr (1777,root,root) %{_localstatedir}/log/core
%dir %attr (755,vdsm,kvm) %{_localstatedir}/lib/%{vdsm_name}
%dir %attr (755,vdsm,kvm) %{_localstatedir}/lib/%{vdsm_name}/netconfback
%dir %attr (755,vdsm,kvm) %{_localstatedir}/run/%{vdsm_name}
%dir %attr (755,vdsm,kvm) %{_localstatedir}/run/%{vdsm_name}/pools
%dir %attr (755,vdsm,kvm) %{_localstatedir}/log/%{vdsm_name}
%dir %attr (755,vdsm,kvm) %{_localstatedir}/log/%{vdsm_name}/backup
%dir %attr (755,vdsm,kvm) %{_sysconfdir}/pki/%{vdsm_name}
%dir %attr (755,vdsm,kvm) %{_sysconfdir}/pki/%{vdsm_name}/keys
%dir %attr (755,vdsm,kvm) %{_sysconfdir}/pki/%{vdsm_name}/certs
/etc/init.d/vdsmd
%doc LICENSE_GPL_v2 README
%{py_sitedir}/sos/plugins/vdsm.py*
%dir %attr (775,vdsm,qemu) %{_localstatedir}/lib/libvirt/qemu/channels/
%{_mandir}/man8/vdsmd.8*

%files hook-vhostmd
%defattr(-,root,root,-)
%doc LICENSE_GPL_v2
%attr (755,root,root) %{_libexecdir}/%{vdsm_name}/hooks/before_vm_start/50_vhostmd
%attr (755,root,root) %{_libexecdir}/%{vdsm_name}/hooks/before_vm_migrate_destination/50_vhostmd
%attr (755,root,root) %{_libexecdir}/%{vdsm_name}/hooks/before_vm_dehibernate/50_vhostmd
%attr (755,root,root) %{_libexecdir}/%{vdsm_name}/hooks/after_vm_destroy/50_vhostmd
%attr (440,root,root) %{_sysconfdir}/sudoers.d/50_vdsm_hook_vhostmd

%files debug-plugin
%defattr(-,root,root,-)
%{_datadir}/%{vdsm_name}/vdsmDebugPlugin.py*

%files cli
%defattr(-,root,root,-)
%doc LICENSE_GPL_v2
%{_datadir}/%{vdsm_name}/vdsClient.py*
%{_sysconfdir}/bash_completion.d/vdsClient
%{_datadir}/%{vdsm_name}/vdscli.py*
%{_datadir}/%{vdsm_name}/vds_qualifier.py*
%{_datadir}/%{vdsm_name}/vds_bootstrap.py*
%{_datadir}/%{vdsm_name}/dumpStorageTable.py*
%attr (755,root,root) %{_bindir}/vdsClient
%{_mandir}/man1/vdsClient.1*

%files reg
%defattr(-,root,root,-)
%doc LICENSE_GPL_v2
%dir  %{_sysconfdir}/%{vdsm_reg}
%dir  %{_datadir}/%{vdsm_reg}
%dir %attr (755,vdsm,kvm) %{_var}/log/%{vdsm_reg}
%config(noreplace) %{_sysconfdir}/%{vdsm_reg}/vdsm-reg.conf
%config(noreplace) %{_sysconfdir}/%{vdsm_reg}/logger.conf
%{_sysconfdir}/init.d/vdsm-reg
%{_datadir}/%{vdsm_reg}/vdsm-reg-setup
%{_datadir}/%{vdsm_reg}/define.py*
%{_datadir}/%{vdsm_reg}/vdsm-complete
%{_datadir}/%{vdsm_reg}/vdsm-gen-cert
%{_datadir}/%{vdsm_reg}/vdsm-upgrade
%{_datadir}/%{vdsm_reg}/config.py*
%{_datadir}/%{vdsm_reg}/deployUtil.py*
%attr (755,root,root) %{_datadir}/%{vdsm_reg}/config-rhev-manager
%attr (755,root,root) %{_datadir}/%{vdsm_reg}/save-config
%{_sysconfdir}/ovirt-config-setup.d
%{_sysconfdir}/ovirt-config-boot.d/vdsm-config
%config(noreplace) %{_sysconfdir}/logrotate.d/vdsm-reg
%{_sysconfdir}/cron.hourly/vdsm-reg-logrotate
%{_mandir}/man8/vdsm-reg.8*

%files hook-faqemu
%defattr(-,root,root,-)
%doc LICENSE_GPL_v2
%{_bindir}/qemu
%{_bindir}/qemu-system-x86_64
%{_libexecdir}/%{vdsm_name}/hooks/before_vm_start/10_faqemu

%changelog
* Thu Apr 28 2011 Eduardo Warszawski <ewarszaw@redhat.com> - 4.9-63.el6
- Fixed master mount validation for file domains
- Always use uncached tags when reading metadata
Resolves: BZ#688680

* Tue Apr 26 2011 Dan Kenigsberg <danken@redhat.com> - 4.9-62.el6
- No more ruth
- added a log line when desktop lock is called.
- BZ#661321 Reduce libvirt calls used for statistics
- BZ#696888 delNetwork: fix second instace of typo
- Fix removeVM and updateVM flows.
- BZ#676322 raise VolumeGroupSizeError in MiB not bytes.
- Ruth enhancements:
- Added test for upgrade persisting VDSM restart
- BZ#692874 - If gethostbyname fails use original value
- BZ#688616 - Turn cgroups off to workaround scalability issues
- Removed disconnect from blockSD as well, nobody uses it
- Actually resolves: BZ#661321 BZ#676322 BZ#688616 BZ#692874 BZ#696888
Resolves: BZ#688680

* Sun Apr 17 2011 Igor Lvovsky <ilvovsky@redhat.com> - 4.9-61.el6
- BZ#695355 - Force storage refresh even if nothing changed every once in a while
- BZ#689661 - Verify that redhat-lsb is installed
- Added perliminary grouping to HSM docs
- No need for getattr, all methods should have specific special impl to prevent messing with wrong keys
- Returned sp.getImagesDomains()
- fix typos in Pool.getInfo() logging
- Don't return a dict in poolInfo['domains'] to preserve BC
- domain no longer has disconnect method
- safelease exception didn't format string correctly
- print_exc() helps no one
- ClusterLock.initLock should not use spmprotect.sh
- All uses pass sdUUID, but the exception doesn't expect it
- Don't print stack trace when failing to validate parameters
- attach moved to sd.py spUUID should be taken from parameter, not self
- presistantDict.clear should also be a transaction
- There is no domainType just domainClass
- BZ#696888 delNetwork: fix typo
- BZ#498971 - Append SSH key to authorized_keys
- BZ#671169 extend lv when half of last extent was filled
- Move SPECIAL_LVS const so it's declared after MASTERLV
- BZ#691340 - Changed tag based version 23 to 2 because it was deemed 'better'
- BZ#693772 - Throw proper error when trying to create a volume on an ISO domain
- Added migrate master tests
- BZ#670432 - migrate master should update reconnect information
- Volume metadata for tag-based domains should not rely on pv mapping
- Changed supervdsm interface
- fixed bug in ruth (htmlreport)
- Changed the way testRunner handles errors in validation
- Avoid annoying logging during sdf.recycle
- Move domain test validation in the ruth to the proper test case
- BZ#695056 - Remove logging in oop.
- BZ#695057 - Avoid prepare volumes during prepareForShutdown process
- Actually resolved: BZ#498971 BZ#670432 BZ#671169 BZ#689661 BZ#691340 BZ#693772 BZ#695056 BZ#695057 BZ#695355 BZ#696888
Resolves: BZ#688680

* Tue Apr 12 2011 Igor Lvovsky <ilvovsky@redhat.com> - 4.9-60.el6
- Prevent 'None' from being written to lease info in MD
- BZ#675683, BZ#684584, BZ#664432 - Metadata refactoring
Resolves: BZ#688680

* Tue Apr 12 2011 Igor Lvovsky <ilvovsky@redhat.com> - 4.9-59.el6
- BZ#689726 Remove netconsole from bootstrap scripts
- BZ#684576 - Added Unreadable class for lvm objects that can't be reloaded.
- Gradually increment migration downtime
- BZ#683760 - Remove unneeded validates.
- BZ#683760 - Remove unneeded validates.
- Related to BZ#683760 - Validate destination domain in spm.copyImage.
- Related to BZ#683760 - Assert that srcDom and dstDom are not the same in moveImage.
- BZ#677974 retry starting vdsm after 15 minutes
- init.d/vdsmd: Avoid starting vdsm when respawn is already running
- BZ#653818 - /config/files not updated after network change
- BZ#690206 - Switch Vm state to Down on QemuDeath event
- Call destroy hook event from libvirtvm.destroy()
- BZ#635410 - Improve error reporting to RHEVM in case of error getting package info
- Bug #673806 - Set maximum migration bandwidth from config file
- BZ#688680 Don't try to refresh cache if task got stuck
- BZ#693424 bootstrap: install qemu-kvm-tools
- Actually resolved bug list is: BZ#635410 BZ#653818 BZ#673806 BZ#677974 BZ#683760 BZ#684576 BZ#688680 BZ#689726 BZ#690206 BZ#693424
Resolves: BZ#688680 BZ#690206

* Thu Apr 07 2011 Igor Lvovsky <ilvovsky@redhat.com> - 4.9-58.el6
- Fix Ruth to support python 2.7
- Added readinto() to streamwrapper for completness
- Related to BZ#666227 - execCmd() now uses async proc for all communicate() calls
- BZ#683905 - VDSM fails to start in newly installed RHEV-H
- BZ#669964 Catch hostname resolution exception
- BZ#628311 - Revert per-vm thp support
- Separated ruthAgent from ruth. ruthAgent is now just a thin proxy.
- Fixed a bug in volumeTests where it used an agent without specifying agent = True
- Added logskip to OperationMutex
- BZ#681457 - Wrong number of arguments for vdsClient commands
- Join/split lists utility
- Fixed bug where on some configurations the default vdsclient target would be ilegally empty.
- Network tests now skip on bad configuration
- BZ#675994 - Remove volume itself before its metadata
- BZ#693209 - GetVGInfo failed if there where no VGs
- Fix cleanup dir semantics
- Fixed LvMetaDataCorrupt test
- Fix teardown in CreateLargePool test
- Related to BZ#683760 - Remove SDF.produce from hsm.getVolumesList.
- BZ#683760 - Remove unnecessary validate.
- Related to BZ#683760 - Removed spm.setDomainDescription.
- Error message for vm assert
- Merge rollback had wrong parentFormat parameter
- Fix exception message in vdsClient
- Set default VG extent size to 128 MiB.
- BZ#550002 - Added support for timing tests
- Improve ruth logging
- Ignore wrong formatted rpms in testValidation
- BZ#690206 - Release VM resources before setting VM to Down
- BZ#618986 - Do not log vm is not running.
- BZ#618986 -  Better trace logs.
- fix broken test
- Related to BZ#688625 - Catch errors in getVmsInfo.
- BZ#688625 - Catch errors in getVmsList.
- Related to BZ#688625 - Cleanup of dead code.
- Fixed broken tests
- fixed duplicate and remove domain in ruthAgent
- Remove unused function lvm.lvOpened.
- BZ#689253 - Merge rollback should acquire/release resources as merge itself
- Added retry to halt functionality
- Minor ruth tweeks
- Related to BZ#685061 - Test for VM 'Powering Down' report
- Related to BZ#685104 - improve backtrace
- Related to BZ#685061 - VM should report 'Powering Down' after destroy fails.
- Raise ImageDoesNotExistInSD instead ImageIsEmpty if the image does not exist.
- Actually resolved bugs list is: BZ#550002 BZ#618986 BZ#628311 BZ#666227 BZ#669964 BZ#675994 BZ#681457 BZ#683760 BZ#683905 BZ#685061 BZ#685104 BZ#688625 BZ#689253 BZ#690206 BZ#693209
Resolves: BZ#666227 BZ#669964 BZ#683905 BZ#688625 BZ#689253 BZ#690206

* Mon Mar 28 2011 Igor Lvovsky <ilvovsky@redhat.com> - 4.9-57.el6
- storage.misc.cleandir: avoid os.path.lexists
- BZ#690126 - Trying to attach a SD to the same SP twice will succeed.
- BZ#690126 - Test attach a storage domain twice.
- Less lvm operations during lvmTests tearDown.
- Fix hibernation test both on block and file devices.
- Changed resourceManager.Owner's logger to be more descriptive
- Fix broken lvmTests after getVG throws exception instead None.
- Improve fail log for lvm.getVGbyUUID function.
- New sp.refreshDomain. Should be moved to sd.
- Don't raise exception if it's a broken link.
- Added html reporting capabilities
- Added features to BasicVdsTest and adapted all tests
- Add coverage collection support to ruthAgent
- Made ruthAgent use the generic daemon class
- Made multiple modules in a single ruth config a lot more streamlined and transparent
- Removed coverage support (If you want to make an omelette...)
- Fixed shortDescription() to conform to our comment taking practices
- Added the ability to attach extra information to a tests
- Made RUTH logging more flexible
- Made "SKIPPED" output more readable and useful
- Use readlink in isInWhiteList only when necessary
- Make libvirt log and debug options configurable
- logskip and deadlock
- floppy is taken from a monitored repo, no need to check it here
- BZ#683746 return specific error if requested fence agent does not exist
- Report stats for bondings
- safelease: avoid annoying compilation warning
Resolves: BZ#683746 BZ#690126

* Tue Mar 22 2011 Igor Lvovsky <ilvovsky@redhat.com> - 4.9-55.el6
- Fix typo in utils.validLocalHostname
- BZ#653928 - Use downtime value from migrate call
- Revert "BZ#678260 - Return masterDomain instance from the cache instead of a copy."
- BZ#669964 - Add warning for misconfigured hostname
- BZ#683724 - Call the supervdsmServer.pyc file instead of the .py file (works for RHEV-H too)
- Fix getAllPvs(VGs) for return _always_ all PVs(VGs).
- BZ#684522 - lvm.getVG raises StorageDomainDoesNotExist
- BZ#684522 - Fix catched exception.
- BZ#678260 - Return masterDomain instance from the cache instead of a copy.
- BZ#678260 - Raise if tried to read LV based metadata for an upgraded domain.
- BZ#678260 - metadata with a single field (checksum) is considered upgraded.
- BZ#598906 - monitorRepsonse during hibernation
- Added vdsmDebugPlugin it can be used to inject code into a running vdsm
- Reordered imports in clientIF to conform with vdsm conventions
- spmStart in VDSClient didn't accept the version parameter
- Remove unused imports from hsm.
- Run visudo silently
- Add migrateVmTests module to regressionNG
- Align MAX_HOST_ID value to RHEV-M default
Resolves: BZ#598906 BZ#653928 BZ#669964 BZ#678260 BZ#683724 BZ#684522

* Tue Mar 15 2011 Dan Kenigsberg <danken@redhat.com> - 4.9-54.el6
- BZ#679064 - blockSD (VG) created with default RH partial tag.
- BZ#681579 when migrating multiple vms using nfs storage migration fails.
- Validate yaml parameters
- Fix requestInvalidResource test.
Resolves: BZ#679064 BZ#681579

* Wed Mar 09 2011 Dan Kenigsberg <danken@redhat.com> - 4.9-53.el6
- fix cvs spec to match git spec, so putting 'vdsm@rhevh' instead of vdsm@f.q.d.n in
  sasldb work.
Resolves: BZ#647155

* Tue Mar 08 2011 Igor Lvovsky <ilvovsky@redhat.com> - 4.9-52.el6
- BZ#682507 - When shutting-down VMs and restarting libvirtd, prepareForShutdown makes vdsm unresponsive and the process does not restart.
- BZ#670737 - Add pvs operation lock.
- BZ#670737 - Remove lvm.listTags.
- BZ#670737 - Add vgs operation lock.
- BZ#670737 - Split lvm.getVG function.
- BZ#674128 - Order cache invalidate operations to fix races.
- BZ#674128 - Add lvs operation lock.
- BZ#674128 - Operation lock.
- BZ#678852 - use session ID instead of target to differentiate between scsi sessions
- BZ#679048 - Fix minor bug.
- BZ#681792 sense libvirt disconnection
- hide json from libvirtd log, add client-side libvirt logs
- Add ssl support for the connection between ruth and vdsm
- SPM fencing due to missing storage access while VMs are running.
- BZ#679048 - Split the getAllPVs (rare) from the usual getPV form.
- BZ#679048 - Renamed listPVS to listPVNames revealing true functionality.             getPV should not return stubs.
- BZ#679048 - One less lvm operation.
- ruthAgent: block specific iscsi port in blockConnectionToHost
- Add RUTH validation mechanism for validating test's prerequisites before running it
- put 'vdsm@rhevh' instead of vdsm@f.q.d.n in sasldb
- Do not fail Vm.create if cgroups is disabled
- BZ#669748 - VDSM installation should remove the default libvirt network
- BZ#681280 check vm state before moving to up on migration failure
- BZ#678886 - Remove lvm.validateVG function.
- BZ#678886 - Better log on BlockStorageDomain.create
- BZ#678886 - Block SD (VG) validation by vgck.
- BZ#680959 - VMs are stuck in wait for launch status when stopping them and then trying to run them
- RUTH: Add network tests
- added optional argument to assertVdscFail to ease writing more specific tests
- Bugfix for ruthAgent: Couldn't recover from a corrupt pid file
- BZ#674357 - Verify that a file is not writable with respect to the qemu user before adding the 'readonly' tag
- Related to BZ#647155 - properly check if bonding dev already exists
- BZ#680952 - Remove log that can stuck the OOP.
- Restore crashVDSM functionality in ruthAgent
- Rewrote setLogLevel verb
Resolves: BZ#647155 BZ#669748 BZ#670737 BZ#674128 BZ#674357 BZ#678852 BZ#678886 BZ#679048 BZ#680952 BZ#680959 BZ#681280 BZ#681792 BZ#682507

* Sun Feb 27 2011 Igor Lvovsky <ilvovsky@redhat.com> - 4.9-51.el6
- BZ#676395 make Vm.saveState reentrant
- BZ#676395 deepcopy status so it is not changed while pickling
- BZ#679106 - Assert unattached domains when creating the SP.
- BZ#626500 - Remove mount cmd in vdsm init script
- BZ#679845 - refresh() doesn't refresh unless other stuff were refreshed
- renamed vdsClient.py to vdsProxy.py
Resolves: BZ#626500 BZ#676395 BZ#679106 BZ#679845

* Wed Feb 23 2011 Igor Lvovsky <ilvovsky@redhat.com> - 4.9-50.el6
- Define taskDir before using it during HSM init
- BZ#591506 - prepareForShutdown should ask all WorkerThread to goAway()
- Add prioritise_write_locks to lvm conf.
- BZ#679106 - avoid sdc when validating unattached domain
- Some test folder cleanup
- BZ#598906 - Add test for non-responsive VM's
- Improve logging messages in Vm.getStats
- BZ#598906 - set monitorResponse to -1 when VM's are non-responsive
- allow unicode in domain xml
- ruthAgent: install/deinstall hook
- drop repeated produce
- Related to BZ#669976 - Fixed SDC and added some more missing refresh()
- Related to BZ#669976 - Move sp.refresh() to its rightful place
- BZ#669976 - Handle isIso() failing and refactor sdf.refresh()
- man page: fix a few spelling errors.
- BZ#677107 - add more info to iSCSI exception
- BZ#677107,BZ#595443 - iSCSI error handling
- BZ#678040 Change LVM version to match and fixed a bug in vdsm-cli
- BZ#677985 require rhel6.1 packages
- BZ#678001 bootstrap: import our own constants
- BZ#661319 - Remove unused variable.
- BZ#661319 - Avoid LV (de)activation if the path (not)exists.
- Added regexp docs to ruths help
- Remove extra logging when vhost custom parameter is not defined
- Add migration support in fillVmDiskTest
- Refactor fillVmDiskTest to improve code reuse
- BZ#674373 - Avoid connectStoragePool check if no VMs need to be restored
- Add missing capabilities to faqemu in order to be used by RHEV-M
- Revert "BZ# 672 346 Volume Metadata Preallocation"
Resolves: BZ#591506 BZ#595443 BZ#598906 BZ#661319 BZ#669976 BZ#674373 BZ#677107 BZ#677985 BZ#678001 BZ#678040 BZ#679106

* Tue Feb 15 2011 Igor Lvovsky <ilvovsky@redhat.com> - 4.9-49.el6
- BZ#674767 new verb: getDevicesVisibility
- BZ#674128 - Create volume as legal
- BZ#674128 - Reduces the number of lvm operations.             Removes the cause of the blockVolume.create() bug without resolving the race.
- BZ#674373 - prepareVolume should not start before connectStoragePool is done
- BZ#672346 Volume Metadata Preallocation
- BZ#666367 support sndbuf custom property
- BZ#677237 - iSCSI discovery regression after integrating iSCSI multi-initiator support
- BZ#676556 fix minor vdsClient typo
- Add DiskAioFullTest to check both file and block based storage
- dumpStorageTable: minimal fixes to Vladik's code
- dump storage repository names to sos report
- Move multiVmTests into vmTests.
- Rename singleVmTests to vmTests.
- BZ#663599 - raise exception on invalid dd result
- BZ#663599 - Ignore locale in vdsmd and forks.
- fix careless forward-porting
- BZ#675518 bootstrap: report meaningful error message on install error
- BZ#662388 - RUTH: VHostNetTest
- BZ#662388 - Control vhost-net on/off status.
- BZ#676395 flush tmp file before moving it
- BZ#563585 - Clean /rhev/data-center on hsm startup
- BZ#643861 pass required smbios mode to libvirt
Resolves: BZ#563585 BZ#643861 BZ#662388 BZ#663599 BZ#666367 BZ#672346 BZ#674128 BZ#674373 BZ#674767 BZ#675518 BZ#676395 BZ#676556 BZ#677237

* Wed Feb 09 2011 Igor Lvovsky <ilvovsky@redhat.com> - 4.9-48.el6
- BZ#674607 - [RFE] Support for multiple sw iSCSI initiators desired
- BZ#667113 Persist vdsm hooks on RHEVH
- BZ#611206 - Use async IO properly, depending on the storage type.
- Add regex capabilities to ruth's module filter
- Related to BZ#661321 replace threading.Condition and friends w/ better classes
- RUTH: Add HSM support in fillVmDiskTest
- RUTH: Initial networking tests
- prepareForShutdown: stop listening asap
- BZ#674755 do not log fenceNode, it contains passwords
- Remove NFS sleep from RUTH teardown, not needed due to LDAP change
- Related to BZ#661321 - pthread-based Lock/Condition/Event implementation
- Rename hibernateVmTest to singleVmTests.
- Add getVmId() method to vm.Vm.
- BZ#668672 - /root/.ssh/authorized_keys will be persisted on RHEVH
- BZ#626468 - Fix RHEVH Registration under SSL
Resolves: BZ#611206 BZ#626468 BZ#661321 BZ#667113 BZ#668672 BZ#674607 BZ#674755

* Tue Feb 01 2011 Igor Lvovsky <ilvovsky@redhat.com> - 4.9-47.el6
- BZ#673144 - Fix mbx checksum race.
- BZ#674270 Fix typo in function param.
- BZ#672493 - Fail VM destroy if teardown of one of its drives failed
- RUTH: fillVmDiskTest: set vnc password
- RUTH: verify that we do not miss sudden death of multiple VMs
- BZ#673111 Prevent overfilling of self-pipe in python event loop
- BZ#666227 - Fix seek miscalc in mailbox.
- BZ#666227 - Revert "Revert "BZ#666227 Change dd in mailbox to perform a single 1M IO op and not 2000 IOPS of 512B""
- Add getVmXMLDesc to the ruthAgent.
- Add libvirtconnection module to handle vdsm-libvirt connection
- RUTH: fillVmDiskTest: Use cpuType=pentiumpro instead of core2duo
- BZ#672770 - unmountMaster() now runc unmount outOfProcess to prevent it form getting stuck as defunct
- Related to BZ#583437 - Forgot to add the version parameter for locaFS
- RUTH: Check the disk growth in fillVmDiskTest.
- RUTH: Add kickstart file to build the live cd for fillVmDiskTest.
- RUTH: Add fillVmDiskTest
- set spice_tls=1 if ssl==true, spice_tls=0 otherwise
- BZ#583437 - Block domains are no longer limited. But, like all good things, it comes with a price
- Related to BZ#583437 - BlockSD can have an LVM tag based metadata
- BZ#583438 - Domain upgrades are now supported
- Removed unused imports in blockSD
- Changed error string for SpmParamsMismatch(). It can now be understood by mere mortal
- Fixed a race condition in changeVGTags()
Resolves: BZ#583437 BZ#583438 BZ#666227 BZ#672493 BZ#672770 BZ#673111 BZ#673144 BZ#674270

* Tue Jan 25 2011 Igor Lvovsky <ilvovsky@redhat.com> - 4.9-45.el6
- Revert "BZ#666227 Change dd in mailbox to perform a single 1M IO op and not 2000 IOPS of 512B"
Resolves: BZ#666227

* Tue Jan 25 2011 Igor Lvovsky <ilvovsky@redhat.com> - 4.9-44.el6
- BZ#670599 - fix race between VM create/destroy
- Require psmisc with a fixed killall -g
- require sudo with /etc/sudoers.d/ directory
- BZ#672055 - Unhandled Exception in setStorageDomainDescription
- Faqemu hook must remove the cpu tag before starting the vm.
- Adding waitForShutdown method to the storage RUTH vm module.
- Add cdrom and boot options to the vm.Vm RUTH class.
- New repeat directive for yaml config of ruth's environment builder
- BZ#666357 - `vendor` and `product` fields may contain spaces. Who knew?
- RUTH: spmFence
- Add new faqemu implementation (as vdsm hook).
- Remove old faqemu implementation.
- BZ#668255 -Test for double pool connection
- BZ#668255 - Raise during multiple pool connection and pool refresh
- BZ#630769 spmprotect: signal spmStop on fence attempt
- storage/dispatcher.py: check types, the python way.
Resolves: BZ#630769 BZ#666357 BZ#668255 BZ#670599 BZ#672055

* Tue Jan 18 2011 Igor Lvovsky <ilvovsky@redhat.com> - 4.9-43.el6
- remove volatileFloppy image when ejected from guest
- set spice_tls=0 if ssl!=true
- BZ#591506 shutdown storage_dispatcher on init failure
- respawn: keep respawning short-lived slaves until thrash time is exceeded
- BZ#666392 - Check if lvextend failed due to lack of free space.
- BZ#666392 - Fix lvm m = MiB issue. Units.
- BZ#575720 - Remove test for GetVGType.
- BZ#666392 - Remove listLVTags function.
- BZ#666392 - Remove lvm.getLVSize function.
- BZ#662434 - Reduce the number of lvm calls during LV creation.
- BZ#651803 - Fix units issue.
- BZ#651803 - Force refresh of lvm info.
- The start of the vdsm log analyzer
- Related to BZ#583438 - SD Metadata has issues if a key contains another key (eg. VERSION and MASTER_VERSION)
- BZ#654222 - Improve resource locking during different SPM/HSM flows (cont.)
- BZ#668220 - Remove pool lock from repoStats call
- BZ#654222 - resourceFactories code refactoring
- BZ#637486 - Move lvmActivation.py to resourceFactories.py
- BZ#637486 - Add tests for simultaneously SPM tasks
- BZ#654222 - Improve resource locking during different SPM/HSM flows
- BZ#637486 - Add new image resources and use it for async. SPM operations
- BZ#654222 - Improve resource locking during domain manipulations.
- BZ#655839 strip newline from kernel name
Resolves: BZ#575720 BZ#583438 BZ#591506 BZ#637486 BZ#651803 BZ#654222 BZ#655839 BZ#662434 BZ#666392 BZ#668220

* Wed Jan 12 2011 Dan Kenigsberg <danken@redhat.com> - 4.9-42.el6
- Revert "BZ#660162 ENOSPC: make new size depend on qcow usage"
- Related to BZ#661319 no need to adjust ksm on recovery
- BZ#665066 safelease: return checking of return value of posix_memalign
Resolves: BZ#665066
Related: BZ#660162 BZ#661319

* Mon Jan 10 2011 Dan Kenigsberg <danken@redhat.com> - 4.9-41.el6
- BZ#665066 spmprotect - remove nop nonsense
- BZ#665066 spmprotect: catch ls failure
- BZ#665066 port spmprotect changes to RHEL6 - RENEWDIR
- BZ#665066 port spmprotect changes to RHEL6
- BZ#658861 increase migration downtime for long migrations
- BZ#666700 hack drive cache to avoid windows guest driver bug
- BZ#661319 ENOSPC: make new size depend on qcow usage
- Makefile: reduce use of sed and of @CONSTANT@ in *py
- BZ#666227 Change dd in mailbox to perform a single 1M IO op and not 2000 IOPS of 512B
- Related to BZ#667722 - processPool: Close the listening socket in port 54321
- Related to BZ#667722 exit pool process on parent death
- Handle more general exception during image deletion
- BZ#602640 iso images should be qemu-readable, that's all
- BZ#662434 - Reduce the number of lvm calls during LV creation.
- BZ#648051 - Limit MAX_PVS to 10 if maximum_allowed_pvs is greater.
Resolves: BZ#602640 BZ#648051 BZ#658861 BZ#661319 BZ#662434 BZ#665066 BZ#666227 BZ#666700

* Wed Jan 05 2011 Dan Kenigsberg <danken@redhat.com> - 4.9-40.el6
- RUTH: test repeated creation of same vm
- BZ#608647 - Add udev rule to chown/chmod special domain's LVs
- BZ#662697 - Parse partial lvm vgs output.
- BZ#583429 - Fix: AttributeError: tuple object has no attribute extend.
Resolves: BZ#583429 BZ#608647 BZ#662697

* Tue Jan 04 2011 Dan Kenigsberg <danken@redhat.com> - 4.9-39.el6
- Revert "BZ#666227 - Change dd in mailbox to perform a single 1M IO op and not 2000 IOPS of 512B"
- Makefile: remove funny chars from rpmrelease
Related: BZ#666227

* Mon Jan 03 2011 Dan Kenigsberg <danken@redhat.com> - 4.9-38.el6
- BZ#666166 - Make sure tmp task dir does not already exist before creating a new one
- RUTH: vdsm22 expects explicit 'rhevm' bridge and timeOffset
- RUTH: pick two nits
- BZ#665162 vdsm_reg: no need for fqdn of rhevm
- BZ#666227 - Change dd in mailbox to perform a single 1M IO op and not 2000 IOPS of 512B
- BZ#614372 Remove FAILED error and lockfile when stopping the service while it's already down
- RUTH: multiVmTests: run Vm on hsm, too
- RUTH: Change cpu model from 'qemu64' to 'pentium' in tests
- BZ#608647 - Remove LV chown during LV activation. Do it in udev rule.
- BZ#608647 - Add udev rule to chown new LVs
- Related to BZ#626468 - host registration fails when working with SSL
- better docstring for samplingmethod
- BZ#662412 - Test set rw permission to an already writeable LV.
- BZ#662412 - Ignores lvm error when setting rw permission to an already writeable LV.
- BZ#662434 - Added log for a req LV that does not exists.
- BZ#653874 - Validate lvm locking type in runtime.
- Makefile: have a more convenient default version-release
Resolves: BZ#608647 BZ#614372 BZ#653874 BZ#662412 BZ#662434 BZ#665162 BZ#666166 BZ#666227
Related: BZ#626468

* Tue Dec 28 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-37.el6
- BZ#657854 prepare paths on vm recovery, too
- BZ#570526 require a "secret" password for libvirt rw access
- Add man page for vdsm-reg
- BZ#590368 - NFS operation now occure out of process to keep the GIL free
- Related to BZ#583429 - Implement atomic bulk tag replacement and manipulation for LVM volumes and groups
- Related to BZ#583429 - Ayal's petty fixes
- Remove a frightening error from log
- BZ#665825 - Cleanup now ignores the mnt dir and resolves symlinks
- BZ#665713 - Fix parsing multipath when device size is not in full units
- SamplingMethod now adds the name of the wrapped method to the log
- UUID validation now passes all tests
- Don't get domain lock if not shared domain
Resolves: BZ#570526 BZ#590368 BZ#657854 BZ#665713 BZ#665825

* Thu Dec 23 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-35.el6
- BZ#602640 access qemu-owned vmchannel socket
Resolves: BZ#602640

* Wed Dec 22 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-34.el6
- BZ#664947 - remove misc.logException, it is deprecated
- BZ#602685 use correct libvirt flag name for forced eject
- BZ#602640 chown volume on HSM, too.
Resolves: BZ#602640 BZ#602685 BZ#664947

* Tue Dec 21 2010 Igor Lvovsky <ilvovsky@redhat.com> - 4.9-33.el6
- RUTH: test running VM with sysprep floppy
- RUTH: test concurrent hibernations
- RUTH: add repeated hibernatio test
- BZ#602640 sysprep.vfd: chmod to readonly
- BZ#660596 - Fix exception parameter in master domain verification
- GCDisablerBlock shold not hide exceptions
- BZ#618120 - getMetaparm - remove redundant search
- BZ#618120 - Fix deleteImage tests
- BZ#618120 - Get rid of duplicate volume metadata
- BZ#613922 - Catch exceptions during volume/image delete validation
- BZ#660596 - Changed username constant from disk user to metadata user so super vdsm will keep working when we change disk user
- On rare cases a resource could free itself because of closure semantics
- BZ#664811 - cleanup domains doesn't crash on non critical errors
- BZ#576065 - Clear only what is necessary when creating new domain
- BZ#654123 - Support for multiple RHEL and Fc vendors in  getFCinitiators.
- safelease: check return value of posix_memalign
- BZ#664518 commit f8e1b6b653350 removed getStorageStats
- BZ#640339 report only last appsList & guestIPs for nonresponsive agents
- RHEV-M wants getVdsCaps.packages to be a map, not list.
- BZ#655839 report running kernel, not installed ones
- since d88f4f914 we don't include tunctl
- RUTH: multiVmTests: run spice VMs, too.
- BZ#664477 honor RHEV-M's soundDevice param
- BZ#664472 - bootstrap: save network after installation
- set spice_tls=1 only if ssl=true
- BZ#602640 keep keys ownership in RHEL5
- BZ#652226 listen only to address of rhevm bridge
- BZ#651345 - do not poll VMs before incoming migration ends
Resolves: BZ#576065 BZ#602640 BZ#613922 BZ#618120 BZ#664811 BZ#640339 BZ#651345 BZ#652226 BZ#654123 BZ#655839 BZ#660596 BZ#664472 BZ#664477

* Thu Dec 16 2010 Igor Lvovsky <ilvovsky@redhat.com> - 4.9-32.el6
- BZ#602640 make vsdm keys qemu-readable
- BZ#660596 - getVGInfo() returns response in old format
Resolves: BZ#602640 BZ#660596

* Thu Dec 16 2010 Igor Lvovsky <ilvovsky@redhat.com> - 4.9-31.el6
- BZ#663389 - Metadata cache clear is now done under exclusive lock
- BZ#660596 - getDeviceCapacities() now returns size in bytes instead of kb
- BZ#660596 - getDeviceInfo works again
- BZ#660596 - getVGInfo uses UUIDs again and getDeviceList returns VGs correctly
Resolves: BZ#660596 BZ#663389

* Tue Dec 14 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-30.el6
- BZ#662942 set virt_use_nfs in policy even if selinux is disabled
- BZ#602640 - chown volumes to vdsm:qemu
- BZ#602640 - run qemu as "qemu" user
- Related to BZ#647155 - do not add already-existing bond devices
- Related to BZ#647155 - do not assume bondX exists before addNetwork
- BZ#662409 report hwaddr of bonding devices
- vdsmd: requires no spice_tls if ssl=false
- Beware of upgrade of libvirt-guests
- bootstrap: remove /var/lib/vdsm/netconfback/*, keep dir itself
- Fixed circular dependency in AsyncProc
- BZ#622438 - Test to make sure we really handle more than 8 domains on file pools
- BZ#622438 - Don't limit amount of domains to non block domains
- Catch StopIteration so that the line iterator wouldn't stop multipath iterator
- Made watchcmd use the AsyncProc class. Now it can be used with minimal performance overhead
- Add 'type' for FCP devices to pathInfo during multipath scanning
- BZ#657854 - Remove unused imports.
- BZ#657854 - Fix exception value.
- Fix makefile and spec. file
- Fix typo
- Removed old rwlock. No need to have 2 classes doing the same thing
- BZ#661683 - Cleanup doesn't unmount anymore
- BZ#651441- Fix AttributeError: 'Stub' object has no attribute...
- Fix minor issues
- Errors in limit functions now state the name of the storage type and instead of the storage type id
- Removed some unused imports
- BZ#660596 - Moved some logic to SuperVdsm to speed things up
- Added chown and direct file
- BZ#647217 - Added logging to new locking stuff
- BZ#639878 - Reordered refreshDirTree() because isIso() expects some links to be there but refreshDirTree has not created them yet
- BZ#646072 - Complex destructors spwan threads instead of running the opeation in the context of the gc run in a different thread
- BZ#646072 - Added blocked gc when subprocess starts to prevent bugz
- BZ#639878 - Remove redundant refresh
- BZ#639878 - Made resource timeout configurable
- no more findMaster
- invalidate chache instead of reloading it
- cache.py is just a dict
- forward port 636426 - Made ruths cleanup work for FCP
- If factory fails in resource creation don't say the client needs to bother support
- BZ#660596 - Added super vdsm and made getDeviceList better
- BZ#638099 - The sha module is deprecated; use the hashlib module instead
- BZ#638099 - Added dynamic barrier and samplemethod
- forward port 591641 - Removed deprecated bug workaround for portal port handling
- forward port 591641 - Added password only logins
- forward port 591641 - Removed useless footers from iscsi.py
- BZ#638099 - Removed getSessionList from the public API
- forward port 636426 - Added pool refresh only on miss for attach or activate domain
- Added sync param to execCmd and created an adapter to safely read process streams
- BZ#658370 - Set fast_io_fail_tmo, no_path_retry and flush_on_last_del in multipath.conf - cont
- BZ#572050 - Added the @logskip decorator. It will cause the method to be skipped when the log tries to find the caller. Currently only turned on for execCmd.
- related to BZ#617982 - removed  nic number limit
- BZ#660297 do not include isoUploader dir in sosreport
- SecureXMLRPCServer: avoid redundent Connection object
- no need to make regular volumes executable
- libvirtvm: eject sysprep floppy on guest reboot
- BZ#626334 force guest to eject CD
Resolves: BZ#572050 BZ#602640 BZ#622438 BZ#626334 BZ#638099 BZ#639878 BZ#646072 BZ#647155 BZ#647217 BZ#651441 BZ#657854 BZ#658370 BZ#660297 BZ#660596 BZ#661683 BZ#662409 BZ#662942

* Mon Dec 06 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-29.el6
- BZ#658895: Fix reference before assignment.
- BZ#658370 - Set fast_io_fail_tmo, no_path_retry and flush_on_last_del in multipath.conf
- BZ#652675 - Rollback spm stop in case of resource timeout
- Get rid from the pool lock in spmStatus
- BZ#647229 - hide wlan and usb nics
- read vdsm.conf in config.py
- BZ#657848 bootstrap: fix os name on RHEL-6
- BZ#655476 - avoid fence loop by not starting vdsm w/ low disk space
- BZ#644852 - getIsoList should return empty dictionary instead of empty list in case that data SD is disconnected
Resolves: BZ#644852 BZ#647229 BZ#652675 BZ#655476 BZ#657848 BZ#658370 BZ#658895

* Wed Dec 01 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-28.el6
- BZ#644852 - getIsoList should return empty dictionary instead of empty list in case that data SD is disconnected
Resolves: BZ#644852

* Tue Nov 30 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-27.el6
- BZ#643861 smbios: add required attribute to sysinfo element
Resolves: BZ#643861

* Tue Nov 30 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-26.el6
- BZ#575720 - CR changes.
- BZ#575720 - lvm.getLV raises instead return None.
- BZ#575720 - Rename lvm.getLVInfo to lvm.getLVSize.
- BZ#575720 - Add LogicalVolumeDoesNotExistError and LogicalVolumeCachingError.
- BZ#575720 - Replace _reloadlvs for reduce lv = None raises.
- BZ#575720 - Simplified Stub class.
- BZ#575720 - whitespace changes.
- BZ#575720 - Catch ConfigParser Exceptions only in buildFilter function.
- Fix regressions: Invalid parameter: 'masterDom=00000000-0000-0000-0000-000000000000'
- Regressions: Wait for delete.
- BZ#575720 - Remove rw permission check during LV activation.
- vdsm_reg Makefile: actually call fixpaths target
- BZ#651335 - do not trust libvirt's blockInfo's physical size
- make: permissive_pyflakes should return true on no errors
- Typo in Ruth's test
- sync_manager is developed in http://git.fedorahosted.org/git/?p=sync_manager.git
- BZ#653875 - sysprep: use same vfd path on destination
Resolves: BZ#575720 BZ#651335 BZ#653875

* Wed Nov 24 2010 Igor Lvovsky <ilvovsky@redhat.com> - 4.9-25.el6
- Fix _fillPVDict missing fields.
Resolves: BZ#575720

* Mon Nov 22 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-24.el6
- Fix typo (opration -> operation) in ut/testUtils.py
- Fix getting SPM status
- Fix empty connection list clean up bug
- BZ#565481 - Can't format domain if only part of it exist
- BZ#654207 - Set dev_loss_tmo to 30 sec, polling_interval to 5 sec in multipath.conf.
- BZ#644852 - In case that data SD is disconnected getIsoList returns empty list
- BZ#642344 - vdsm parse rhel6 storage server vendor incorrectly
- BZ#607468 - Return full ISO list with any permissions
- BZ#575720 - Add (add|rem|replace)VGTags mutiple VG tag functions.
- BZ#575720 - Add test(Add|Rem|Replace)VGTags tests.
- BZ#575720 - Remove validation from (add|rem)VGTag functions. UT updated.
- BZ#575720 - Fix: getVGInfo fuction in hsm.
- BZ#575720 - Add getVGInfo regression test.
- Fix to  __fillPVDict function in hsm.
- Fix: Typo (key).
- Remove unused imports.
- Minor fixes to lvm.
- BZ#606058 - Delete volume: exception in vdsm log when parent volume was removed before its child
- BZ#575720 - CR style changes
- BZ#575720 - _setLVAvailability function accepts a list of lvs.
- BZ#575720 - Remove (add|rem)SDTags, replaced by (add|rem)VGTag. hasSDTag removed. Remove filtering param in getVG. UT updated.
- BZ#575720 - Changed raise type in changeLVTag function.
- BZ#575720 - removeLV function raises CannotRemoveLogicalVolume again.
- BZ#575720 - chown only if needed in createLV function.
- BZ#575720 - Inverted if-else logic in setrwLV function.
- BZ#575720 - Remove unused parameter from getAllVgs.
- BZ#575720 - Remove unused parameters after invalidate functions split.
-  BZ#575720 - Fix: VolumeGroup Exceptions are StorageException based.
- BZ#575720 - Split LVInfo getLv function.
- BZ#575720 - Split LVInfo getPv and getVg functions.
- BZ#575720 - Split LVInfo._invalidate functions.
- BZ#575720 - Remove internal replaceVGTag function.
- BZ#575720 - Added addVGTag and replaceVGTag tests.
- BZ#575720 - Remove internal delVGTag function.
- BZ#575720 - Remove internal addVGTag function.
- BZ#575720 - (Re)move internal _changelv function.
- BZ#575720 - (Re)move internal setLVAvailability function.
- BZ#575720 - Remove internal setLVpermission function.
- BZ#575720 - Remove interface activateVG function. UT updated.
- BZ#575720 - Remove internal setVgAvailability function.
- BZ#575720 - (Re)move internal vgmknodes function.
- BZ#575720 - Remove internal replaceLVTag function.
- BZ#575720 - Remove delTag related tests.
- BZ#575720 - Remove internal delLVTag and interface deltag (LV) functions.
- BZ#575720 - Remove internal addLVTag function.
- BZ#575720 - Remove internal initpv function.
- BZ#575720 - Remove internal refreshlv function.
- BZ#575720 - Remove internal renamelv function.
- BZ#575720 - Remove internal extendlv function.
- BZ#575720 - Remove internal removelv function.
- BZ#575720 - Remove internal createlv function.
- Made ruth batch storage connections of the same type
- BZ#646438 - vdsm doesn't perform necessary checks on validate storage server connection
- BZ#646438 - Test validation flow before storage server connection itself
- BZ#612983 - Add volume type/format validation
- BZ#612983 - Transform local storage from bind-mounts to symlinks
- throw image uid:gid to constants
- replace all @CONSTANT@s in a source file
- vdsm for rhev-h-2.3 builds only for RHEL-6
- Fix typo in spm stop error handling
- BZ#641355 -  After changing multipathd user_friendly_names param, need to run multipath -F and then multipathd restart
- BZ#615753 - Force multipath debug verbosity levels to -v2
- BZ#572045 - iSCSI discovery: Incorrect error message when address cannot be translated into IP
- This patch fixes a potential deadlock and data loss.
- BZ#653904 logCollector: support RHEL6's sosreport
- BZ#650977 vdsClient: getVolumeSize: report apparentsize too.
- RUTH: notice failed vm launch earlier
- RUTH: fix geck.py doc
- Fixed wrong import name
- Put vdsm in its own pgrp
- BZ#618986 - stop vmStats thread: fix typo
- BZ#646780 - netinfo's mac address should not include model type
- make: add a permissive pyflakes test by default
- avoid a couple of pyflakes warnings
- VolumeError: fix silly copy-paste error
- drop redundant connection to migration destination's libvirtd
- BZ#643861 - pass host uuid to guest bios
Resolves: BZ#565481 BZ#572045 BZ#575720 BZ#606058 BZ#607468 BZ#612983 BZ#615753 BZ#618986 BZ#641355 BZ#642344 BZ#643861 BZ#644852 BZ#646438 BZ#646780 BZ#650977 BZ#653904 BZ#654207

* Mon Nov 08 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-23.el6
- BZ#575720 - Remove internal refreshvg function.
- BZ#575720 - Remove internal extendvg function.
- BZ#575720 - Remove internal renamevg function.
- BZ#575720 - Remove internal removevg function.
- BZ#575720 - Remove internal createvg function.
- BZ#575720 - Replaced _normalizeargs function.
- Remove unnecessary tuple conversion.
- BZ#575720 - Introduce LVM Cache to remove redundant LVM calls         replaceVGTag() workaround for BZ#647167
- BZ#575720 - Introduce LVM Cache to remove redundant LVM calls         Add VG tag manipulation to LVM cache
- BZ#613928 - Fix tests for task stop flow
- BZ#613928 - Storage: Tasks are started under sudo but are killed without sudo
- BZ#649742 teardown hibernation volumes after use
- BZ#649742 move _prepareVolumePath/_teardownVolumePath to clientIF
- BZ#626475 require traceroute as long as vdsm-reg needs it
Resolves: BZ#575720 BZ#613928 BZ#626475 BZ#649742

* Wed Nov 03 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-22.el6
- BZ#646809 - getStoragePoolInfo should use repoStats to get domain's statistics (cont.)
- BZ#646809 - getStoragePoolInfo should use repoStats to get domain's statistics.
- BZ#591506 - prepareForShutdown should ask all WorkerThread to goAway()
- BZ#575720 - Typo in log usage
- prepareForShutdown: avoid exception if called before initIRS.
- Do not report hooks with zero script
- RUTH: add migration test
- RUTH: multiVmTests: test concurrent creation of many Vms
- RUTH: hiberTest: encapsulate Vm
- RUTH: hiberTest: implicit runVolume for single-volume images
- BZ#575720 - Introduce LVM Cache to remove redundant LVM calls - tests
- BZ#575720 - Introduce LVM Cache to remove redundant LVM calls
- BZ#646880 - call after_vm_start hook after we surely have Vm._dom
- netinfo: strip quotes from reproted cfg values
- Do not silently swallow libvirt's exception
- no need to log errors after server is stopped.
- stop guestIF before VM is destroyed
- guestIF: do not probe guest socket if _connect() failed
- BZ#618986 - stop vm stats thread asap
Resolves: BZ#575720 BZ#591506 BZ#618986 BZ#646809 BZ#646880

* Wed Oct 27 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-21.el6
- BZ#643699 - 'discoverSendTargets' did not return IP info to backend
- BZ#644847 - fix _initVmStats/saveState race
- BZ#644253 - vdsm needs an up-to-date libvirt
Resolves: BZ#643699 BZ#644253 BZ#644847

* Wed Oct 20 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-20.el6
- BZ#635385 - we stop/start service ksm, too.
- disable libvirt's tls if no key/cert
- fix error messages: Desktop -> Virtual Machine
- BZ#614392 - make prepareVolume timeout proportional to Vm load
- BZ#614392 - move load-correction of timeout to a specialized function
- add man pages to rpm
- handle create-destroy race condition
- RUTH: hiberTest: avoid repeating vmParams
Resolves: BZ#614392 BZ#635385

* Tue Oct 19 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-19.el6
- BZ#642991 -  Log in exception during delete volume has uninitialized parameter
- BZ#617253 - Now regression tests should pass parameters to connectStoragePool. Add masterDomain parsing to YAML.
- BZ#617253 -getMasterDomain should return value from the cache or got master UUID explicitly.
- BZ#617253 - confusing debug log: Cannot find master domain: ('e9971608-9d66-4859-b763-f0f214b94e91',)    In several cases during pool operations we failing on access to master domain.    In that cases we should raise exception 'Cannot find master domain:' with pool and domain UUIDs.
- BZ#603472 - Add mixed domains (NFS/SAN) to ruth config files.   Change limitTestToConnectionType to limitTestToDomainType that will filter test according   to relevant domain types only and not according to connections
- BZ#640367 - getVGList should return VG state 'PARTIAL' for partial VG, 'OK' for normal VG and 'UNKNOWN' otherwise.  - We need to collect VG's attributes with all other VG's parameters during 'vgs' to get VG state.  - getVGInfo should return the partial VG with all its info  as well as normal VG.  - getStorageDomainInfo should return additional key 'state' for OK/PARTIAL/UNKNOWN according to proper VG
- Made fuser handling in unmountMaster thread safe
- BZ#620097 - start vhostmd if sap_agent==True
- RUTH: test hibernate/restore
- RUTH: imporve an exception's readability
- Libvirt log rotate configuration
- BZ#602211 - fix a little blunder
- BZ#627661 - bootstrap: put certs/key where libvirt expects them.
- vdsmd and vdsClient man pages
- use proper operator for string formatting
- BZ#616425 - check if another migration has taken a Vm down
- remove _srcDomXML from Vm.conf once it is no longer needed
- do not freak out if AnonHugePages is unsupported
- BZ#602211 - dig in libvirt to report vm pid
- BZ#570192 - set cpu shares according to requested nice level
- BZ#619783 - log hook stderr into vdsm.log
- remove @ games from hooks.py
- BZ#619783 - report installed hooks in getVdsCaps
Resolves: BZ#570192 BZ#602211 BZ#603472 BZ#616425 BZ#617253 BZ#619783 BZ#620097 BZ#627661 BZ#640367 BZ#642991

* Mon Oct 04 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-18.el6
- BZ#638324 - Fix race between spmStart and disconnectPool
- BZ#638324 - Test race between spmStart and disconnectPool
- BZ#612983 - Add unit test for storage server connection
- BZ#612983 - Refactor storage server connections code for nfs/local connections
- BZ#566433 - fix errors and remove unnecessary code
- BZ#572052 - fixed error in using the 'exc_info' arg
- BZ#603472 - Add retry method to misc. Use it when you want to retry to run a method for certain amount of tries or until a specific timeout
- BZ#603472 - Updated SPM tests to use the env builder
- Object visualizer fix to really use the `targetFile` param
- BZ#616762 - error code is not clear when nfs.mount failed in time-out
- BZ#591641 BZ#595443 - Added support for chap auth in target discovery
- BZ#596078 - Get rid of redundant 'int_max_tasks' in config.py
- BZ#633882 - Tests for multiple copy->delete sequence of the same image
- BZ#633882 - Support multiple copy->delete sequence of the same image
- BZ#616075 - Add traceback in getBlockStorageDomainList failure flow
- BZ#636070 - vdsm client missing parameter in help for command createStoragePool
- BZ#635600 - remove vdsm even selinux is disabled, v2
- BZ#618986 - stop VmStatsThread when Vm goes down
- BZ#602685 - diskChange: no string formatting here
- BZ#634572 - continue sampling Vm after timeout error
- BZ#626324 - vdsm-4.9 should live in a pure 2.3 cluster
- vdsmd.init fix !&& precedence order
- BZ#635385 - disable ksm on speed-optimized hosts
- BZ#633820 - no need to remove vdsm from RHEL-6
- BZ#636075 - ifconfig output is locale-dependent, use LC_ALL=C
Resolves: BZ#566433 BZ#572052 BZ#595443 BZ#596078 BZ#602685 BZ#603472 BZ#612983 BZ#616075 BZ#616762 BZ#618986 BZ#626324 BZ#633820 BZ#633882 BZ#634572 BZ#635385 BZ#635600 BZ#636070 BZ#636075 BZ#638324 BZ#638324

* Tue Sep 21 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-17.el6
- BZ#635687 - request vmc channel when using spice
- guestIF: exit politely if stopped while not connected
- BZ#635600 - remove vdsm even selinux is disabled
- BZ#635429 - set VM nic speed according to its model
- BZ#623245 - update link speed on any state change
- BZ#633770 - invent vmName if not provided
- BuildRequire redhat-rpm-config to bytecompile all .py
- BZ#633296 - bootstrap: peal off quotes from ifcfg-* values
Resolves: BZ#623245 BZ#624661 BZ#633296 BZ#633770 BZ#635429 BZ#635600 BZ#635687

* Mon Sep 13 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-16.el6
- BZ#516712 - generate vdsm.conf.sample from config.py
- no need to explicitly call ksm.adjust every minute
- BZ#603472 - Ruth upgrade (cont.): transfer import/export suite to YAML
- BZ#626727 - Tests for move/copy images on SAN domains
- BZ#626727 - Fix move/copy images on SAN domains after RAW+Sparse volumes preventing.   During move/copy images we should first create destination volume with minimal size   and extend it after that.
- BZ#628625 - Add test for export/import large VM from blank
- BZ#628625 -  VDSM: Importing sparse images larger than 1 G failed `No space left on device.   The reason was a wrong volume size conversion from bytes to sectors.   It failed out from 2.3 during one of code refactorings.
- BZ#626727 - Test for creation RAW+Sparse volumes
- BZ#628491 - connectStoragePool will fail, if it's not possible to refresh /sys/class/scsi_host/hostX/scan 	Log the exception condition, instead of re-raising it
- BZ#626727 - Creation RAW+Sparse volumes on block domain should be prevented
- BZ#612983 - Code refactoring around local storage (cont)
- BZ#603472 - Fixed touch verb in ruthAgent, use impersonation stack instead of su
- BZ#581243 - Updated the resource manager tests to use  module instead of the deprecated  module
- BZ#572052 - Added method to the logging format. No need to write it manually any more.
- BZ#572050 - excCmd now logs where it was called from. 	- Moved findCaller from the resource system to misc 	- Added more filtering options to findCaller 	- Replaced uses of the `new` module with the `types` module
- BZ#572050 - Added a logging filter that makes sure that any exceptions is logged only once
- BZ#572052 - Made it so that storage loggers are created with meaningful names
- BZ#572052 - Removed misc.logException, using the 'exc_info' arg instead.
- BZ#572052 - Removed misc.propegateError.
- BZ#572050 - Moved logging code from misc to codeUtils
- BZ#624265 - Add sleep between every two NFS tests during regression
- BZ#566433 - 'makedirs' refactoring
- BZ#612983 - Code refactoring around local storage
- BZ#566433 - Code refactoring: rename nfs.py -> fileUtils.py, move file functionality from misc to fileUtils
- BZ#612983 - Tests update for local storage
- BZ#612983 - Local storage support
- BZ#597783 - Allow VerifyDevicesFilter test for block devices only
- BZ#624368 - spmstop.sh script fails on syntax error
- BZ#619079 - VDS HBA inventory should be easily retrievable
Resolves: BZ#516712 BZ#566433 BZ#572050 BZ#572052 BZ#581243 BZ#597783 BZ#603472 BZ#612983 BZ#619079 BZ#624265 BZ#624368 BZ#626727 BZ#628491 BZ#628625

* Sun Sep 12 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-15.el6
- configure libvirt on RHEL w/o ssl
- BZ#627661 - configure libvirt on vdsm startup
- BZ#631711 - run hooks.pyc on vdsmd start, as .py are missing from RHEV-H
- fill in BONDING_OPTS whenever creating a bond
- ask guest agent to refresh data only after connecting to it
- never pass unfiltered guest genrated date to rhev-m
- honor emulatedMachine param
- BZ#626324 - only RHEV-2.3 are supported
- BZ#626334 - return error on failure to change/eject CD
- hooks: vmconf is assumed to be a dictionary
- BZ#620991 - read host thp state and report it in getVdsStast
- Use vhostmd if available, but do not require it.
- rename vmchannel to com.redhat.rhevm.vdsm
- BZ#627131 - Tell if it's ovirt w/o rpm
- BZ#627661 - configure libvirt on vdsm startup
- clean sysprep floppy image after use
Resolves: BZ#620991 BZ#626324 BZ#626334 BZ#627131 BZ#627661 BZ#631711

* Wed Sep 01 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-14.el6
- BZ#581243 - Refactored resource system to more easily allow new features
- 2.2.3 - VDSM should report the LUN serial number,vendor and LUN mapping 	Extract the LUN and its serial number from device when collecting path info
- NFS commands are now aware of stale mounts and will try to handle it gracefully
- Changed misc.fileexists to misc.pathExists. It validates directories as well as files, furthermore the name was not in camleCase
- BZ#620710 - Fixed the message field in task status.
- BZ#581243 - added a deferrable context
- BZ#581243 - Added an RWLock to be used for low level locking
- BZ#581243 - Added a simple vdsm specific log adapter to misc
- BZ#620137 - replace custom uuid generator with python's built-in one
- ruth 2.3 adaptations           - better logging in testutils           - print log even if first error
- Add test for double image delete operation
- BZ#624415 - pass <readonly> to sysprep vfd
- report true accumulated rx/txDropped
- do not send insignificant digits of rx/tx rates
- no need to play with /dev/net/tun ownership
- Related to BZ#627661 - vdsm's truststore has moved
- limit concurrent migrations
- log spice/vnc connect/disconnect events (bug 619-379)
- BZ#626179 - Spawn a dummy task to keep the delete image interface consistent
- getVdsStats: report anonHugePages
- BZ#620991 - disable thp according to rhev-m's last request.
- /etc/sysconfig/libvirtd: remove vdsm changes on uninstall
- BZ#624432 - set selinux boolean virt_use_nfs
- BZ#624645 - stop libvirt-guests service on vdsm startup
- BZ#624744 - log guest agent events in TRACE level
- connect to guest agent socket on a new thread
- BZ#620097 - always install vhostmd hook
- BZ#620097 - vhostmd hook
- BZ#620951 - log-collect hooks
- BZ#619783 - hook mechanism
Resolves: BZ#581243 BZ#619783 BZ#620097 BZ#620137 BZ#620710 BZ#620951 BZ#620991 BZ#624415 BZ#624432 BZ#624645 BZ#624744 BZ#626179
Related:  BZ#627661

* Wed Aug 18 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-13.el6
- BZ#624957 - pass correct cmdline param to libvirt
- BZ#624273 - update qemu exec (used for -M ?)
- BZ#624272 - report new names of key packages
- BZ#624430 - create recovery file upon vm creation
- BZ#624415 - libvirtvm: honor 'floppy' param
- BZ#619359 - stop a finished task works
- BZ#609148 - Export VM Failed when removing VM from export domain and exporting it for the second
- BZ#619029 - Reconstruct master domain fail for block SD
- LVM2 temporary compatibility fix
- BZ#623633 - supportedRHEVMs should list vdc versions
- handle stray qemu w/o -uuid
- BZ#622498 - unlimit default qemu coredump size
- BZ#621111 - always create cdrom device (which can be w/o media)
- BZ#622752 - disable balloon device
- BZ#622401 - notice libvirtd disconnection w/o domains
- BZ#623042 - allow iscsid stop/start in sudoers
- BZ#622274 - check path existence before calling blockInfo()
- must not call pgrep with sudo
- BZ#622265 - must not call prepareVolume on recovery
- BZ#620386 - catch ordered shutdown event
- libvirtvm: connect to guest agent socket
- compress before storing VM RAM
- BZ#620329 - report pauseCode==NOERR on launchPaused
- BZ#615733 - vdsmd.init: warn if no free disk space
- vdsmd.init: clean up init script
Resolves: BZ#615733 BZ#619029 BZ#619359 BZ#620329 BZ#620386 BZ#621111 BZ#622265 BZ#622274 BZ#622401 BZ#622498 BZ#622752 BZ#623042 BZ#624272 BZ#624273 BZ#624415 BZ#624430 BZ#624957
Related: BZ#623633

* Sun Aug 01 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-12.el6
- report sign of rtc offset to rhev-m
- make hibernation timeout half of migration's, not fifth
- Drop unneeded upgrade script
- ask another rpm to be --quiet
- Report that we support RHEV-M 2.3 but not 2.1
- respawn: nicer log messages
- respawn: do not exit if slave returns error
- BZ#619035 - pass error_policy to libvirt
- log noisy verbs in TRACE level
- BZ#565416 - never use storage.misc.execCmd(shell=True)
- BZ#606042 - log client IP before each storage API call
- BZ#606042 - log client IP on each API call
- BZ#615444 - report supported cpu models
- ask rpm to be quiet
Resolves: BZ#565416 BZ#606042 BZ#615444 BZ#619035

* Tue Jul 27 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-11.el6
- last rebase over zstream-2.2.2
- BZ#614768 - pass guest kernel and initrd to libvirt
- BZ#565416 use sudo -n, courtesy of storage.misc.execCmd
- fix testHighWrite
- libvirt's IO_ERROR gives alias of drive, not its name
- allow sudo on MK_SYSPREP_FLOPPY
Resolves: BZ#565416 BZ#614768

* Tue Jul 13 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-10.el6
- significant rebase over 4.5-62.8.el5_5rhev2_2
- when killing watchdog kill respawn process
- drop long-unused script
- clean SecureXMLRPCServer.py up
- BZ#523712 - call ssl_accept() in a new thread
- synchronize Vm._refreshLV() and LibvirtVm.cont()
- BZ#609417 - maintain Vm._guestCpuRunning according to SUSPENDED/RESUMED events
- utcoffset is an int, not a tupple
- vdsmd: always source /etc/init.d/functions
- BZ#603793 - Add support for RHEL 6 multipath -ll output format
- libvirtvm: _getQemuError
- listen to IO_ERROR_REASON event
- BZ#571348 - expose iscsi initiator name
- move watchdog out of vdsm, use external respawn instead
Resolves: BZ#523712 BZ#571348 BZ#603793 BZ#609417

* Tue Jun 22 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-9.1.el6
- vdsm.spec-only change: quote spaces in log_filters
Related: BZ#554961

* Tue Jun 22 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-9.el6
- BZ#604708 - test if libvirt supports plaintext tcp if ssl=false
- BZ#523019 - new API setLogLevel
- No longer need to eject CD before replacing it w/ WinXP
- BZ#602685 - changeCD: ignore ret val of attachDevice
- BZ#598906 - vdsClient: list table: show non-responsiveness in state
- FIXME Temporarily add verbosity to libvirt logs
- BZ#604996 - do not try to sample VM while hibernating
- BZ#580154 - demote an unsurprising error to debug level
- BZ#595347 - handle hibernation source finish only in MigrationSourceThread
- clear reference to _dom to avoid BZ#603494
- no longer need to restart multipathd after umount
- no longer need to chmod /etc/pki/CA
- blockStats now expects hda, not ide-0-0-0
- BZ#602199 - make the logs prettier, use a calming password.
- BZ#589458 - libvirtvm: honor launchPaused
Resolves: BZ#523019 BZ#580154 BZ#589458 BZ#595347 BZ#598906 BZ#602199 BZ#602685  BZ#604708 BZ#604996

* Sun Jun 06 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-8.el6
- BZ#599421 - rate errors due to empty pseudo files as debug
- BZ#595347 - handle migration source finish only in MigrationSourceThread
- BZ#598522 - filter out unsurprising error message
- BZ#598533 - place guest agent socket where libvirt's selinux policy expects it
- honor vmchannel=false
- we need mkfs.msdos for creating sysprep floppy
- BZ#588650 - return a clearer error when asked to boot a VM from a missing disk
- BZ#595106 - libvirtvm: recognize old vdsm params of hda hdb etc
- Expect redhat-release-server
Resolves: BZ#588650 BZ#595106 BZ#595347 BZ#598522 BZ#598533 BZ#599421

* Sun May 30 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-7.el6
- BZ#566164 - drop binary blob from vds_bootstrap
- BZ#595347 - do not change Down reason after it was set
- drop unneeded warning
- Related to BZ#595243 - add vmchannel device
- BZ#595056 - rhevm is the name of our default bridge
- BZ#595237 - report latest timeOffset if VM state is Down
- convert Linux sse4_1 to libvirt sse4.1
- BZ#584439 - report bonding configuration options
- BZ#593953 - honor kvmEnable, to run VM in emulation mode
- BZ#593004 - addNetwork make ifcfg-* world-readable
- BZ#505316 - more consistent copyright notice
- pass qemu_drive_cache to libvirt
- BZ#593216 - setVmTicket: 0 timeout means no timeout
- BZ#575577 - die on libvirt system error
- Related to BZ#589458 - launchPaused: pause VM right after creation
- poll for high writes on block devices.
- honor spiceDisableTicketing
- drop spurious <clock> element
- set error_policy according to drive.propagateErrors
- BZ#579366 - let running virtio disk even if they have no index
- logCollector: use the new sos plugin for libvirt
- BZ#580167 - Do not set Down reason if vm is already destroyed
Resolves: BZ#505316 BZ#566164 BZ#575577 BZ#579366 BZ#580167 BZ#584439 BZ#589458 BZ#593004 BZ#593216 BZ#593953 BZ#595056 BZ#595237 BZ#595347
Related: BZ#595243

* Thu Apr 29 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-6.el5rhev
- handle new libvirt events
- getattr() is slightly nicer than dir()
- pretty print domxml for log readability
- libvirtev.py: rebase to libvirt 0.8.0
- BZ#579705 - add boot option max_cstate=1
- BZ#577636 - force-start iscsid as long as we mess with iscsid.conf
- Related to BZ#579762 - avoid logging an exception after a vm is destroyed
- do not report false display{,Secure}Port
- pickle Vm config as soon as vm.Vm() is initialized
- enable spice encryption in libvirt
- use new py_sitedir macro definition
- drop two redundant logs
- set up spice cert/key during rpm installation
- convince RHEV-M 2.2 that it can work with vdsm 4.9
- fix migration to iscsi
- allow vdsm user to connect to remote libvirt over tls
- compute <topology> socket attribute and pass it to libvirt
- Related to BZ#581221 - keep vdsm name in RHEL6 (for now)
#Resolves: BZ#577636 BZ#579705
Related: BZ#543948

* Mon Apr 12 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-5.el5rhev
- Related to BZ#579705 - cannot set max_cstate after boot
- pass cpu model to libvirt
- BZ#577758 - implement setVmTicket
- pass spiceSecureChannels to libvirt
- make sure iscsid is running
- BZ#580460 - remove netconf backup dir in its new location
- add file/dev attrib to <disk><source> according to its type
- fix disk's <serial> element and doc it
- pass timeOffset to <clock> element, and place the latter properly
- lsb_release needed during build
- BZ#579104 - specify unit for qemu-img create size
#Resolves: BZ#577758 BZ#579104 BZ#580460
#Related: BZ#579705
Related: BZ#543948

* Thu Apr 01 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-3.el5rhev
- log vdsm version on startup
- BZ#577617 - ksmtuned can be notified only /after/ qemu starts
- Related to BZ#578551 - mount as nfs v3
- BZ#567572 - store net conf backup under /var/lib/vdsm
- make PW reviewer happy
- BZ#577786 - createXML: use domain <boot> element correctly
- BZ#577618 - use_libvirt by default
- BZ#577617 - translate ksm.py to rhel6
- simplify protection against using api while recovering VMs
- catch unexpected exceptions in a single place
#Resolves: BZ#567572 BZ#577617 BZ#577618 BZ#577786
Related: BZ#543948

* Wed Mar 24 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-2.el6
- plenty of clean-ups an refactoring
- rudimentary integration with libvirt
Related: BZ#543948

* Sun Mar 21 2010 Dan Kenigsberg <danken@redhat.com> - 4.9-1.el6
- First build for RHEL-6.
Related: BZ#543948
