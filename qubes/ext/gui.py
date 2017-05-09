#
# The Qubes OS Project, https://www.qubes-os.org/
#
# Copyright (C) 2010-2016  Joanna Rutkowska <joanna@invisiblethingslab.com>
# Copyright (C) 2013-2016  Marek Marczykowski-Górecki
#                              <marmarek@invisiblethingslab.com>
# Copyright (C) 2014-2016  Wojtek Porczyk <woju@invisiblethingslab.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#

import os

import qubes.config
import qubes.ext



class GUI(qubes.ext.Extension):
    @staticmethod
    def send_gui_mode(vm):
        vm.run_service('qubes.SetGuiMode',
            input=('SEAMLESS'
            if vm.features.get('gui-seamless', False)
            else 'FULLSCREEN'))


    @staticmethod
    def is_guid_running(vm):
        '''Check whether gui daemon for this domain is available.

        Notice: this will be irrelevant here, after real splitting GUI/Admin.

        :returns: :py:obj:`True` if guid is running, \
            :py:obj:`False` otherwise.
        :rtype: bool
        '''
        xid = vm.xid
        if xid < 0:
            return False
        if not os.path.exists('/var/run/qubes/guid-running.{}'.format(xid)):
            return False
        return True


    @qubes.ext.handler('domain-is-fully-usable')
    def on_domain_is_fully_usable(self, vm, event):
        # pylint: disable=unused-argument
        if not self.is_guid_running(vm):
            yield False
