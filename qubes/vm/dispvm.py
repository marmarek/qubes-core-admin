#
# The Qubes OS Project, http://www.qubes-os.org
#
# Copyright (C) 2014-2016  Wojtek Porczyk <woju@invisiblethingslab.com>
# Copyright (C) 2016       Marek Marczykowski <marmarek@invisiblethingslab.com>)
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

''' A disposable vm implementation '''

import asyncio

import qubes.vm.qubesvm
import qubes.vm.appvm
import qubes.config

class DispVM(qubes.vm.qubesvm.QubesVM):
    '''Disposable VM'''

    template = qubes.VMProperty('template',
                                load_stage=4,
                                vmclass=qubes.vm.appvm.AppVM,
                                doc='AppVM, on which this DispVM is based.')

    dispid = qubes.property('dispid', type=int, write_once=True,
        clone=False,
        doc='''Internal, persistent identifier of particular DispVM.''')

    auto_cleanup = qubes.property('auto_cleanup', type=bool, default=False,
        doc='automatically remove this VM upon shutdown')

    def __init__(self, app, xml, *args, **kwargs):
        self.volume_config = {
            'root': {
                'name': 'root',
                'snap_on_start': True,
                'save_on_stop': False,
                'rw': False,
                'source': None,
            },
            'private': {
                'name': 'private',
                'snap_on_start': True,
                'save_on_stop': False,
                'rw': True,
                'source': None,
            },
            'volatile': {
                'name': 'volatile',
                'snap_on_start': False,
                'save_on_stop': False,
                'rw': True,
                'size': qubes.config.defaults['root_img_size'] +
                        qubes.config.defaults['private_img_size'],
            },
            'kernel': {
                'name': 'kernel',
                'snap_on_start': False,
                'save_on_stop': False,
                'rw': False,
            }
        }

        template = kwargs.get('template', None)

        if xml is None:
            assert template is not None

            if not template.dispvm_allowed:
                raise qubes.exc.QubesValueError(
                    'template for DispVM ({}) needs to have '
                    'dispvm_allowed=True'.format(template.name))

            if 'dispid' not in kwargs:
                kwargs['dispid'] = app.domains.get_new_unused_dispid()
            if 'name' not in kwargs:
                kwargs['name'] = 'disp' + str(kwargs['dispid'])

        if template is not None:
            # template is only passed if the AppVM is created, in other cases we
            # don't need to patch the volume_config because the config is
            # coming from XML, already as we need it
            for name, config in template.volume_config.items():
                # in case the template vm has more volumes add them to own
                # config
                if name not in self.volume_config:
                    self.volume_config[name] = config.copy()
                    if 'vid' in self.volume_config[name]:
                        del self.volume_config[name]['vid']

        super(DispVM, self).__init__(app, xml, *args, **kwargs)

        if xml is None:
            # by default inherit properties from the DispVM template
            proplist = [prop.__name__ for prop in template.property_list()
                if prop.clone and prop.__name__ not in ['template']]
            self_props = [prop.__name__ for prop in self.property_list()]
            self.clone_properties(template, set(proplist).intersection(
                self_props))

            self.firewall.clone(template.firewall)
            self.features.update(template.features)
            self.tags.update(template.tags)

    @qubes.events.handler('domain-load')
    def on_domain_loaded(self, event):
        ''' When domain is loaded assert that this vm has a template.
        '''  # pylint: disable=unused-argument
        assert self.template

    @qubes.events.handler('property-pre-set:template')
    def on_property_pre_set_template(self, event, name, newvalue,
            oldvalue=None):
        ''' Disposable VM cannot have template changed '''
        # pylint: disable=unused-argument
        raise qubes.exc.QubesValueError(self,
            'Cannot change template of Disposable VM')

    @asyncio.coroutine
    def on_domain_shutdown_coro(self):
        '''Coroutine for executing cleanup after domain shutdown.

        This override default action defined in QubesVM.on_domain_shutdown_coro
        '''
        with (yield from self.startup_lock):
            yield from self.storage.stop()
            if self.auto_cleanup:
                yield from self.remove_from_disk()
                del self.app.domains[self]
                self.app.save()

    @classmethod
    @asyncio.coroutine
    def from_appvm(cls, appvm, **kwargs):
        '''Create a new instance from given AppVM

        :param qubes.vm.appvm.AppVM appvm: template from which the VM should \
            be created
        :returns: new disposable vm

        *kwargs* are passed to the newly created VM

        >>> import qubes.vm.dispvm.DispVM
        >>> dispvm = qubes.vm.dispvm.DispVM.from_appvm(appvm).start()
        >>> dispvm.run_service('qubes.VMShell', input='firefox')
        >>> dispvm.cleanup()

        This method modifies :file:`qubes.xml` file.
        The qube returned is not started.
        '''
        if not appvm.dispvm_allowed:
            raise qubes.exc.QubesException(
                'Refusing to create DispVM out of this AppVM, because '
                'dispvm_allowed=False')
        app = appvm.app
        dispvm = app.add_new_vm(
            cls,
            template=appvm,
            auto_cleanup=True,
            **kwargs)
        yield from dispvm.create_on_disk()
        app.save()
        return dispvm

    @asyncio.coroutine
    def cleanup(self):
        '''Clean up after the DispVM

        This stops the disposable qube and removes it from the store.
        This method modifies :file:`qubes.xml` file.
        '''
        try:
            # pylint: disable=not-an-iterable
            yield from self.kill()
        except qubes.exc.QubesVMNotStartedError:
            pass
        # if auto_cleanup is set, this will be done automatically
        if not self.auto_cleanup:
            yield from self.remove_from_disk()
            del self.app.domains[self]
            self.app.save()

    @asyncio.coroutine
    def start(self, **kwargs):

        # sanity check, if template_for_dispvm got changed in the meantime
        if not self.template.dispvm_allowed:
            raise qubes.exc.QubesException(
                'template for DispVM ({}) needs to have '
                'dispvm_allowed=True'.format(self.template.name))

        yield from super(DispVM, self).start(**kwargs)
