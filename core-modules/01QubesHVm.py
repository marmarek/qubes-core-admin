#!/usr/bin/python2
# -*- coding: utf-8 -*-
#
# The Qubes OS Project, http://www.qubes-os.org
#
# Copyright (C) 2010  Joanna Rutkowska <joanna@invisiblethingslab.com>
# Copyright (C) 2013  Marek Marczykowski <marmarek@invisiblethingslab.com>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
#
#

import os
import os.path
import signal
import subprocess
import sys
import shutil
from xml.etree import ElementTree

from qubes.qubes import (
    dry_run,
    defaults,
    register_qubes_vm_class,
    system_path,
    vmm,
    QubesException,
    QubesResizableVm,
)


system_path["config_template_hvm"] = '/usr/share/qubes/vm-template-hvm.xml'

defaults["hvm_disk_size"] = 20*1024*1024*1024
defaults["hvm_private_img_size"] = 2*1024*1024*1024
defaults["hvm_memory"] = 512


class QubesHVm(QubesResizableVm):
    """
    A class that represents an HVM. A child of QubesVm.
    """

    # FIXME: logically should inherit after QubesAppVm, but none of its methods
    # are useful for HVM

    def get_attrs_config(self):
        attrs = super(QubesHVm, self).get_attrs_config()
        attrs.pop('kernel')
        attrs.pop('kernels_dir')
        attrs.pop('kernelopts')
        attrs.pop('uses_default_kernel')
        attrs.pop('uses_default_kernelopts')
        attrs['virt_mode']['func'] = lambda value: 'hvm'
        attrs['dir_path']['func'] = lambda value: value if value is not None \
                else os.path.join(system_path["qubes_appvms_dir"], self.name)
        attrs['config_file_template']['func'] = \
            lambda x: system_path["config_template_hvm"]
        attrs['drive'] = { 'attr': '_drive',
                           'save': lambda: str(self.drive) }
        # Remove this two lines when HVM will get qmemman support
        attrs['maxmem'].pop('save')
        attrs['maxmem']['func'] = lambda x: self.memory
        attrs['timezone'] = { 'default': 'localtime',
                              'save': lambda: str(self.timezone) }
        attrs['qrexec_installed'] = { 'default': False,
            'attr': '_qrexec_installed',
            'save': lambda: str(self._qrexec_installed) }
        attrs['guiagent_installed'] = { 'default' : False,
            'attr': '_guiagent_installed',
            'save': lambda: str(self._guiagent_installed) }
        attrs['seamless_gui_mode'] = { 'default': False,
                              'attr': '_seamless_gui_mode',
                              'save': lambda: str(self._seamless_gui_mode) }
        attrs['services']['default'] = "{'meminfo-writer': False}"

        attrs['memory']['default'] = defaults["hvm_memory"]

        return attrs

    def __init__(self, **kwargs):

        super(QubesHVm, self).__init__(**kwargs)

        # Default for meminfo-writer have changed to (correct) False in the
        # same version as introduction of guiagent_installed, so for older VMs
        # with wrong setting, change is based on 'guiagent_installed' presence
        if "guiagent_installed" not in kwargs and \
            (not 'xml_element' in kwargs or kwargs['xml_element'].get('guiagent_installed') is None):
            self.services['meminfo-writer'] = False

    @property
    def type(self):
        return "HVM"

    def is_appvm(self):
        return True

    @property
    def virt_mode(self):
        return self._virt_mode

    @virt_mode.setter
    def virt_mode(self, value):
        if value not in ["default", "hvm"]:
            raise QubesException("Invalid virt_mode - only hvm supported for this VM")
        self._virt_mode = "hvm"

    @classmethod
    def is_template_compatible(cls, template):
        if template and (not template.is_template() or template.type != "TemplateHVM"):
            return False
        return True

    def get_clone_attrs(self):
        attrs = super(QubesHVm, self).get_clone_attrs()
        attrs.remove('kernel')
        attrs.remove('uses_default_kernel')
        attrs.remove('kernelopts')
        attrs.remove('uses_default_kernelopts')
        attrs += [ 'timezone' ]
        attrs += [ 'qrexec_installed' ]
        attrs += [ 'guiagent_installed' ]
        return attrs

    @property
    def qrexec_installed(self):
        return self._qrexec_installed or \
            bool(self.template and self.template.qrexec_installed)

    @qrexec_installed.setter
    def qrexec_installed(self, value):
        if self.template and self.template.qrexec_installed and not value:
            print >>sys.stderr, "WARNING: When qrexec_installed set in template, it will be propagated to the VM"
        self._qrexec_installed = value

    @property
    def guiagent_installed(self):
        return self._guiagent_installed or \
            bool(self.template and self.template.guiagent_installed)

    @guiagent_installed.setter
    def guiagent_installed(self, value):
        if self.template and self.template.guiagent_installed and not value:
            print >>sys.stderr, "WARNING: When guiagent_installed set in template, it will be propagated to the VM"
        self._guiagent_installed = value

    @property
    def seamless_gui_mode(self):
        if not self.guiagent_installed:
            return False
        return self._seamless_gui_mode

    @seamless_gui_mode.setter
    def seamless_gui_mode(self, value):
        if self._seamless_gui_mode == value:
            return
        if not self.guiagent_installed and value:
            raise ValueError("Seamless GUI mode requires GUI agent installed")

        self._seamless_gui_mode = value
        if self.is_running():
            self.send_gui_mode()

    @property
    def drive(self):
        return self._drive

    @drive.setter
    def drive(self, value):
        if value is None:
            self._drive = None
            return

        # strip type for a moment
        drv_type = "cdrom"
        if value.startswith("hd:") or value.startswith("cdrom:"):
            (drv_type, unused, value) = value.partition(":")
            drv_type = drv_type.lower()

        # sanity check
        if drv_type not in ['hd', 'cdrom']:
            raise QubesException("Unsupported drive type: %s" % type)

        if value.count(":") == 0:
            value = "dom0:" + value
        if value.count(":/") == 0:
            # FIXME: when Windows backend will be supported, improve this
            raise QubesException("Drive path must be absolute")

        self._drive = drv_type + ":" + value

    def create_on_disk(self, verbose, source_template = None):
        self.log.debug('create_on_disk(source_template={!r})'.format(
            source_template))
        if dry_run:
            return

        if source_template is None:
            source_template = self.template

        # create empty disk
        self.storage.private_img_size = defaults["hvm_private_img_size"]
        self.storage.root_img_size = defaults["hvm_disk_size"]
        self.storage.create_on_disk(verbose, source_template)

        if verbose:
            print >> sys.stderr, "--> Creating icon symlink: {0} -> {1}".format(self.icon_path, self.label.icon_path)

        try:
            if hasattr(os, "symlink"):
                os.symlink (self.label.icon_path, self.icon_path)
            else:
                shutil.copy(self.label.icon_path, self.icon_path)
        except Exception as e:
            print >> sys.stderr, "WARNING: Failed to set VM icon: %s" % str(e)

        # Make sure that we have UUID allocated
        self._update_libvirt_domain()

        # fire hooks
        for hook in self.hooks_create_on_disk:
            hook(self, verbose, source_template=source_template)

    def get_private_img_sz(self):
        if not os.path.exists(self.private_img):
            return 0

        return os.path.getsize(self.private_img)

    def resize_private_img(self, size):
        assert size >= self.get_private_img_sz(), "Cannot shrink private.img"

        if self.is_running():
            raise NotImplementedError("Online resize of HVM's private.img not implemented, shutdown the VM first")

        self.storage.resize_private_img(size)

    def get_config_params(self):

        params = super(QubesHVm, self).get_config_params()

        self.storage.drive = self.drive
        params.update(self.storage.get_config_params())
        params['volatiledev'] = ''

        if self.timezone.lower() == 'localtime':
            params['time_basis'] = 'localtime'
            params['timeoffset'] = '0'
        elif self.timezone.isdigit():
            params['time_basis'] = 'UTC'
            params['timeoffset'] = self.timezone
        else:
            print >>sys.stderr, "WARNING: invalid 'timezone' value: %s" % self.timezone
            params['time_basis'] = 'UTC'
            params['timeoffset'] = '0'
        return params

    def verify_files(self):
        if dry_run:
            return

        self.storage.verify_files()

        # fire hooks
        for hook in self.hooks_verify_files:
            hook(self)

        return True

    @property
    def vif(self):
        if self.xid < 0:
            return None
        if self.netvm is None:
            return None
        return "vif{0}.+".format(self.stubdom_xid)

    @property
    def mac(self):
        if self._mac is not None:
            return self._mac
        elif self.template is not None:
            return self.template.mac
        else:
            return "00:16:3E:5E:6C:{qid:02X}".format(qid=self.qid)

    @mac.setter
    def mac(self, value):
        self._mac = value

    def run(self, command, **kwargs):
        if self.qrexec_installed:
            if 'gui' in kwargs and kwargs['gui']==False:
                command = "nogui:" + command
            return super(QubesHVm, self).run(command, **kwargs)
        else:
            raise QubesException("Needs qrexec agent installed in VM to use this function. See also qvm-prefs.")

    @property
    def stubdom_xid(self):
        if self.xid < 0:
            return -1

        if vmm.xs is None:
            return -1

        stubdom_xid_str = vmm.xs.read('', '/local/domain/%d/image/device-model-domid' % self.xid)
        if stubdom_xid_str is not None:
            return int(stubdom_xid_str)
        else:
            return -1

    def validate_drive_path(self, drive):
        drive_type, drive_domain, drive_path = drive.split(':', 2)
        if drive_domain == 'dom0':
            if not os.path.exists(drive_path):
                raise QubesException("Invalid drive path '{}'".format(
                    drive_path))

    def start(self, *args, **kwargs):
        if self.drive:
            self.validate_drive_path(self.drive)
        # make it available to storage.prepare_for_vm_startup, which is
        # called before actually building VM libvirt configuration
        self.storage.drive = self.drive

        if self.template and self.template.is_running():
            raise QubesException("Cannot start the HVM while its template is running")
        try:
            if 'mem_required' not in kwargs:
                # Reserve 44MB for stubdomain
                kwargs['mem_required'] = (self.memory + 44) * 1024 * 1024
            return super(QubesHVm, self).start(*args, **kwargs)
        except QubesException as e:
            capabilities = vmm.libvirt_conn.getCapabilities()
            tree = ElementTree.fromstring(capabilities)
            os_types = tree.findall('./guest/os_type')
            if 'hvm' not in map(lambda x: x.text, os_types):
                raise QubesException("Cannot start HVM without VT-x/AMD-v enabled")
            else:
                raise

    def start_stubdom_guid(self, verbose=True):

        guid_cmd = [system_path["qubes_guid_path"],
                    "-d", str(self.stubdom_xid),
                    "-t", str(self.xid),
                    "-N", self.name,
                    "-c", self.label.color,
                    "-i", self.label.icon_path,
                    "-l", str(self.label.index)]
        if self.debug:
            guid_cmd += ['-v', '-v']
        elif not verbose:
            guid_cmd += ['-q']
        retcode = subprocess.call (guid_cmd)
        if (retcode != 0) :
            raise QubesException("Cannot start qubes-guid!")

    def start_guid(self, verbose=True, notify_function=None,
                   before_qrexec=False, **kwargs):
        if not before_qrexec:
            return

        if not self.guiagent_installed or self.debug:
            if verbose:
                print >> sys.stderr, "--> Starting Qubes GUId (full screen)..."
            self.start_stubdom_guid(verbose=verbose)

        kwargs['extra_guid_args'] = kwargs.get('extra_guid_args', []) + \
            ['-Q', '-n']

        stubdom_guid_pidfile = \
            '/var/run/qubes/guid-running.%d' % self.stubdom_xid
        if not self.debug and os.path.exists(stubdom_guid_pidfile):
            # Terminate stubdom guid once "real" gui agent connects
            stubdom_guid_pid = int(open(stubdom_guid_pidfile, 'r').read())
            kwargs['extra_guid_args'] += ['-K', str(stubdom_guid_pid)]

        super(QubesHVm, self).start_guid(verbose, notify_function, **kwargs)

    def start_qrexec_daemon(self, **kwargs):
        if not self.qrexec_installed:
            if kwargs.get('verbose', False):
                print >> sys.stderr, "--> Starting the qrexec daemon..."
            xid = self.get_xid()
            qrexec_env = os.environ.copy()
            qrexec_env['QREXEC_STARTUP_NOWAIT'] = '1'
            retcode = subprocess.call ([system_path["qrexec_daemon_path"], str(xid), self.name, self.default_user], env=qrexec_env)
            if (retcode != 0) :
                self.force_shutdown(xid=xid)
                raise OSError ("ERROR: Cannot execute qrexec-daemon!")
        else:
            super(QubesHVm, self).start_qrexec_daemon(**kwargs)

            if self.guiagent_installed:
                if kwargs.get('verbose'):
                    print >> sys.stderr, "--> Waiting for user '%s' login..." % self.default_user

                self.wait_for_session(notify_function=kwargs.get('notify_function', None))
                self.send_gui_mode()

    def send_gui_mode(self):
        if self.seamless_gui_mode:
            service_input = "SEAMLESS"
        else:
            service_input = "FULLSCREEN"

        self.run_service("qubes.SetGuiMode", input=service_input)

    def _cleanup_zombie_domains(self):
        super(QubesHVm, self)._cleanup_zombie_domains()
        if not self.is_running():
            xc_stubdom = self.get_xc_dominfo(name=self.name+'-dm')
            if xc_stubdom is not None:
                if xc_stubdom['paused'] == 1:
                    subprocess.call(['xl', 'destroy', str(xc_stubdom['domid'])])
                if xc_stubdom['dying'] == 1:
                    # GUID still running?
                    guid_pidfile = \
                        '/var/run/qubes/guid-running.%d' % xc_stubdom['domid']
                    if os.path.exists(guid_pidfile):
                        guid_pid = open(guid_pidfile).read().strip()
                        os.kill(int(guid_pid), 15)

    def suspend(self):
        if dry_run:
            return

        if not self.is_running() and not self.is_paused():
            raise QubesException ("VM not running!")

        self.pause()

    def is_guid_running(self):
        # If user force the guiagent, is_guid_running will mimic a standard QubesVM
        if self.guiagent_installed:
            return super(QubesHVm, self).is_guid_running()
        else:
            xid = self.stubdom_xid
            if xid < 0:
                return False
            if not os.path.exists('/var/run/qubes/guid-running.%d' % xid):
                return False
            return True

    def is_fully_usable(self):
        # Running gui-daemon implies also VM running
        if not self.is_guid_running():
            return False
        if self.qrexec_installed and not self.is_qrexec_running():
            return False
        return True

register_qubes_vm_class(QubesHVm)
