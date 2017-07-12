#
# The Qubes OS Project, http://www.qubes-os.org
#
# Copyright (C) 2014-2016  Wojtek Porczyk <woju@invisiblethingslab.com>
# Copyright (C) 2016       Marek Marczykowski <marmarek@invisiblethingslab.com>)
# Copyright (C) 2016       Bahtiar `kalkin-` Gadimov <bahtiar@gadimov.de>
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
''' This module contains the AppVM implementation '''

import copy

import qubes.events
import qubes.vm.qubesvm
from qubes.config import defaults


class AppVM(qubes.vm.qubesvm.QubesVM):
    '''Application VM'''

    template = qubes.VMProperty('template',
                                load_stage=4,
                                vmclass=qubes.vm.templatevm.TemplateVM,
                                doc='Template, on which this AppVM is based.')

    dispvm_allowed = qubes.property('dispvm_allowed',
        type=bool,
        default=False,
        doc='Should this VM be allowed to start as Disposable VM')

    default_volume_config = {
            'root': {
                'name': 'root',
                'pool': 'default',
                'snap_on_start': True,
                'save_on_stop': False,
                'rw': False,
                'source': None,
            },
            'private': {
                'name': 'private',
                'pool': 'default',
                'snap_on_start': False,
                'save_on_stop': True,
                'rw': True,
                'size': defaults['private_img_size'],
            },
            'volatile': {
                'name': 'volatile',
                'pool': 'default',
                'snap_on_start': False,
                'save_on_stop': False,
                'size': defaults['root_img_size'],
                'rw': True,
            },
            'kernel': {
                'name': 'kernel',
                'pool': 'linux-kernel',
                'snap_on_start': False,
                'save_on_stop': False,
                'rw': False,
            }
        }

    def __init__(self, app, xml, **kwargs):
        self.volume_config = copy.deepcopy(self.default_volume_config)
        template = kwargs.get('template', None)

        if template is not None:
            # template is only passed if the AppVM is created, in other cases we
            # don't need to patch the volume_config because the config is
            # coming from XML, already as we need it

            for name, conf in self.volume_config.items():
                tpl_volume = template.volumes[name]

                self.config_volume_from_source(conf, tpl_volume)

            for name, config in template.volume_config.items():
                # in case the template vm has more volumes add them to own
                # config
                if name not in self.volume_config:
                    self.volume_config[name] = config.copy()
                    if 'vid' in self.volume_config[name]:
                        del self.volume_config[name]['vid']

        super(AppVM, self).__init__(app, xml, **kwargs)

    @qubes.events.handler('domain-load')
    def on_domain_loaded(self, event):
        ''' When domain is loaded assert that this vm has a template.
        '''  # pylint: disable=unused-argument
        assert self.template

    @qubes.events.handler('property-pre-set:template')
    def on_property_pre_set_template(self, event, name, newvalue,
            oldvalue=None):
        '''Forbid changing template of running VM
        '''  # pylint: disable=unused-argument
        if not self.is_halted():
            raise qubes.exc.QubesVMNotHaltedError(self,
                'Cannot change template while qube is running')

    @qubes.events.handler('property-set:template')
    def on_property_set_template(self, event, name, newvalue, oldvalue=None):
        ''' Adjust root (and possibly other snap_on_start=True) volume
        on template change.
        '''  # pylint: disable=unused-argument

        for volume_name, conf in self.default_volume_config.items():
            if conf.get('snap_on_start', False) and \
                    conf.get('source', None) is None:
                config = conf.copy()
                template_volume = newvalue.volumes[volume_name]
                self.volume_config[volume_name] = \
                    self.config_volume_from_source(
                        config,
                        template_volume)
                self.storage.init_volume(volume_name, config)
