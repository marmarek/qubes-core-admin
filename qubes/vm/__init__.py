#!/usr/bin/python2 -O
# vim: fileencoding=utf-8

#
# The Qubes OS Project, https://www.qubes-os.org/
#
# Copyright (C) 2010-2015  Joanna Rutkowska <joanna@invisiblethingslab.com>
# Copyright (C) 2011-2015  Marek Marczykowski-Górecki
#                              <marmarek@invisiblethingslab.com>
# Copyright (C) 2014-2015  Wojtek Porczyk <woju@invisiblethingslab.com>
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

'''Qubes Virtual Machines

'''

import datetime
import os
import subprocess
import sys
import xml.parsers.expat

import lxml.etree

import qubes
import qubes.devices
import qubes.events
import qubes.log
import qubes.tools.qvm_ls


class Features(dict):
    '''Manager of the features.

    Features can have three distinct values: no value (not present in mapping,
    which is closest thing to :py:obj:`None`), empty string (which is
    interpreted as :py:obj:`False`) and non-empty string, which is
    :py:obj:`True`. Anything assigned to the mapping is coerced to strings,
    however if you assign instances of :py:class:`bool`, they are converted as
    described above. Be aware that assigning the number `0` (which is considered
    false in Python) will result in string `'0'`, which is considered true.

    This class inherits from dict, but has most of the methods that manipulate
    the item disarmed (they raise NotImplementedError). The ones that are left
    fire appropriate events on the qube that owns an instance of this class.
    '''

    #
    # Those are the methods that affect contents. Either disarm them or make
    # them report appropriate events. Good approach is to rewrite them carefully
    # using official documentation, but use only our (overloaded) methods.
    #
    def __init__(self, vm, other=None, **kwargs):
        super(Features, self).__init__()
        self.vm = vm
        self.update(other, **kwargs)

    def __delitem__(self, key):
        super(Features, self).__delitem__(key)
        self.vm.fire_event('domain-feature-delete', key)

    def __setitem__(self, key, value):
        if value is None or isinstance(value, bool):
            value = '1' if value else ''
        else:
            value = str(value)
        self.vm.fire_event('domain-feature-set', key, value)
        super(Features, self).__setitem__(key, value)

    def clear(self):
        for key in self:
            del self[key]

    def pop(self):
        '''Not implemented
        :raises: NotImplementedError
        '''
        raise NotImplementedError()

    def popitem(self):
        '''Not implemented
        :raises: NotImplementedError
        '''
        raise NotImplementedError()

    def setdefault(self):
        '''Not implemented
        :raises: NotImplementedError
        '''
        raise NotImplementedError()

    def update(self, other=None, **kwargs):
        if other is not None:
            if hasattr(other, 'keys'):
                for key in other:
                    self[key] = other[key]
            else:
                for key, value in other:
                    self[key] = value

        for key in kwargs:
            self[key] = kwargs[key]

    #
    # end of overriding
    #

    _NO_DEFAULT = object()

    def check_with_template(self, feature, default=_NO_DEFAULT):
        ''' Check if the vm's template has the specified feature. '''
        if feature in self:
            return self[feature]

        if hasattr(self.vm, 'template') and self.vm.template is not None \
                and feature in self.vm.template.features:
            return self.vm.template.features[feature]

        if default is self._NO_DEFAULT:
            raise KeyError(feature)

        return default


class BaseVMMeta(qubes.events.EmitterMeta):
    '''Metaclass for :py:class:`.BaseVM`'''
    def __init__(cls, name, bases, dict_):
        super(BaseVMMeta, cls).__init__(name, bases, dict_)
        qubes.tools.qvm_ls.process_class(cls)


class BaseVM(qubes.PropertyHolder):
    '''Base class for all VMs

    :param app: Qubes application context
    :type app: :py:class:`qubes.Qubes`
    :param xml: xml node from which to deserialise
    :type xml: :py:class:`lxml.etree._Element` or :py:obj:`None`

    This class is responsible for serializing and deserialising machines and
    provides basic framework. It contains no management logic. For that, see
    :py:class:`qubes.vm.qubesvm.QubesVM`.
    '''
    # pylint: disable=no-member

    __metaclass__ = BaseVMMeta

    def __init__(self, app, xml, features=None, devices=None, tags=None,
            **kwargs):
        # pylint: disable=redefined-outer-name

        # self.app must be set before super().__init__, because some property
        # setters need working .app attribute
        #: mother :py:class:`qubes.Qubes` object
        self.app = app

        super(BaseVM, self).__init__(xml, **kwargs)

        #: dictionary of features of this qube
        self.features = Features(self, features)

        #: :py:class:`DeviceManager` object keeping devices that are attached to
        #: this domain
        self.devices = devices or qubes.devices.DeviceManager(self)

        #: user-specified tags
        self.tags = tags or {}

        if self.xml is not None:
            # features
            for node in xml.xpath('./features/feature'):
                self.features[node.get('name')] = node.text

            # devices (pci, usb, ...)
            for parent in xml.xpath('./devices'):
                devclass = parent.get('class')
                for node in parent.xpath('./device'):
                    self.devices[devclass].attach(node.text)

            # tags
            for node in xml.xpath('./tags/tag'):
                self.tags[node.get('name')] = node.text

            # SEE:1815 firewall, policy.

            # check if properties are appropriate
            all_names = set(prop.__name__ for prop in self.property_list())

            for node in self.xml.xpath('./properties/property'):
                name = node.get('name')
                if name not in all_names:
                    raise TypeError(
                        'property {!r} not applicable to {!r}'.format(
                            name, self.__class__.__name__))

        #: logger instance for logging messages related to this VM
        self.log = None

        if hasattr(self, 'name'):
            self.init_log()

    def init_log(self):
        '''Initialise logger for this domain.'''
        self.log = qubes.log.get_vm_logger(self.name)

    def __xml__(self):
        element = lxml.etree.Element('domain')
        element.set('id', 'domain-' + str(self.qid))
        element.set('class', self.__class__.__name__)

        element.append(self.xml_properties())

        features = lxml.etree.Element('features')
        for feature in self.features:
            node = lxml.etree.Element('feature', name=feature)
            node.text = self.features[feature]
            features.append(node)
        element.append(features)

        for devclass in self.devices:
            devices = lxml.etree.Element('devices')
            devices.set('class', devclass)
            for device in self.devices[devclass]:
                node = lxml.etree.Element('device')
                node.text = device
                devices.append(node)
            element.append(devices)

        tags = lxml.etree.Element('tags')
        for tag in self.tags:
            node = lxml.etree.Element('tag', name=tag)
            node.text = self.tags[tag]
            tags.append(node)
        element.append(tags)

        return element

    def __repr__(self):
        proprepr = []
        for prop in self.property_list():
            try:
                proprepr.append('{}={!s}'.format(
                    prop.__name__, getattr(self, prop.__name__)))
            except AttributeError:
                continue

        return '<{} object at {:#x} {}>'.format(
            self.__class__.__name__, id(self), ' '.join(proprepr))

    #
    # xml serialising methods
    #

    def create_config_file(self, prepare_dvm=False):
        '''Create libvirt's XML domain config file

        :param bool prepare_dvm: If we are in the process of preparing \
            DisposableVM
        '''
        domain_config = self.app.env.get_template('libvirt/xen.xml').render(
            vm=self, prepare_dvm=prepare_dvm)
        return domain_config

    #
    # firewall
    # SEE:1815 rewrite it, have <firewall/> node under <domain/>
    # and possibly integrate with generic policy framework.
    #

    def write_firewall_conf(self, conf):
        '''Write firewall config file.
        '''
        defaults = self.get_firewall_conf()
        expiring_rules_present = False
        for item in defaults.keys():
            if item not in conf:
                conf[item] = defaults[item]

        root = lxml.etree.Element(
                "QubesFirewallRules",
                policy=("allow" if conf["allow"] else "deny"),
                dns=("allow" if conf["allowDns"] else "deny"),
                icmp=("allow" if conf["allowIcmp"] else "deny"),
                yumProxy=("allow" if conf["allowYumProxy"] else "deny"))

        for rule in conf["rules"]:
            # For backward compatibility
            if "proto" not in rule:
                if rule["portBegin"] is not None and rule["portBegin"] > 0:
                    rule["proto"] = "tcp"
                else:
                    rule["proto"] = "any"
            element = lxml.etree.Element(
                    "rule",
                    address=rule["address"],
                    proto=str(rule["proto"]),
            )
            if rule["netmask"] is not None and rule["netmask"] != 32:
                element.set("netmask", str(rule["netmask"]))
            if rule.get("portBegin", None) is not None and \
                            rule["portBegin"] > 0:
                element.set("port", str(rule["portBegin"]))
            if rule.get("portEnd", None) is not None and rule["portEnd"] > 0:
                element.set("toport", str(rule["portEnd"]))
            if "expire" in rule:
                element.set("expire", str(rule["expire"]))
                expiring_rules_present = True

            root.append(element)

        tree = lxml.etree.ElementTree(root)

        try:
            old_umask = os.umask(0o002)
            with open(os.path.join(self.dir_path,
                    self.firewall_conf), 'w') as fd:
                tree.write(fd, encoding="UTF-8", pretty_print=True)
            fd.close()
            os.umask(old_umask)
        except EnvironmentError as err:  # pylint: disable=broad-except
            print >> sys.stderr, "{0}: save error: {1}".format(
                    os.path.basename(sys.argv[0]), err)
            return False

        # Automatically enable/disable 'updates-proxy-setup' service based on
        # allowYumProxy
        if conf['allowYumProxy']:
            self.features['updates-proxy-setup'] = '1'
        else:
            try:
                del self.features['updates-proxy-setup']
            except KeyError:
                pass

        if expiring_rules_present:
            subprocess.call(["sudo", "systemctl", "start",
                             "qubes-reload-firewall@%s.timer" % self.name])

        # SEE:1815 any better idea? some arguments?
        self.fire_event('firewall-changed')

        return True

    def has_firewall(self):
        ''' Return `True` if there are some vm specific firewall rules set '''
        return os.path.exists(os.path.join(self.dir_path, self.firewall_conf))

    @staticmethod
    def get_firewall_defaults():
        ''' Returns the default firewall rules '''
        return {
            'rules': list(),
            'allow': True,
            'allowDns': True,
            'allowIcmp': True,
            'allowYumProxy': False}

    def get_firewall_conf(self):
        ''' Returns the firewall config dictionary '''
        conf = self.get_firewall_defaults()

        try:
            tree = lxml.etree.parse(os.path.join(self.dir_path,
                self.firewall_conf))
            root = tree.getroot()

            conf["allow"] = (root.get("policy") == "allow")
            conf["allowDns"] = (root.get("dns") == "allow")
            conf["allowIcmp"] = (root.get("icmp") == "allow")
            conf["allowYumProxy"] = (root.get("yumProxy") == "allow")

            for element in root:
                rule = {}
                attr_list = ("address", "netmask", "proto", "port", "toport",
                             "expire")

                for attribute in attr_list:
                    rule[attribute] = element.get(attribute)

                if rule["netmask"] is not None:
                    rule["netmask"] = int(rule["netmask"])
                else:
                    rule["netmask"] = 32

                if rule["port"] is not None:
                    rule["portBegin"] = int(rule["port"])
                else:
                    # backward compatibility
                    rule["portBegin"] = 0

                # For backward compatibility
                if rule["proto"] is None:
                    if rule["portBegin"] > 0:
                        rule["proto"] = "tcp"
                    else:
                        rule["proto"] = "any"

                if rule["toport"] is not None:
                    rule["portEnd"] = int(rule["toport"])
                else:
                    rule["portEnd"] = None

                if rule["expire"] is not None:
                    rule["expire"] = int(rule["expire"])
                    if rule["expire"] <= int(datetime.datetime.now().strftime(
                            "%s")):
                        continue
                else:
                    del rule["expire"]

                del rule["port"]
                del rule["toport"]

                conf["rules"].append(rule)

        except EnvironmentError as err:  # pylint: disable=broad-except
            # problem accessing file, like ENOTFOUND, EPERM or sth
            # return default config
            return conf

        except (xml.parsers.expat.ExpatError,
                ValueError, LookupError) as err:
            # config is invalid
            print("{0}: load error: {1}".format(
                os.path.basename(sys.argv[0]), err))
            return None

        return conf


class VMProperty(qubes.property):
    '''Property that is referring to a VM

    :param type vmclass: class that returned VM is supposed to be instance of

    and all supported by :py:class:`property` with the exception of ``type`` \
        and ``setter``
    '''

    _none_value = ''

    def __init__(self, name, vmclass=BaseVM, allow_none=False,
            **kwargs):
        if 'type' in kwargs:
            raise TypeError(
                "'type' keyword parameter is unsupported in {}".format(
                    self.__class__.__name__))
        if 'setter' in kwargs:
            raise TypeError(
                "'setter' keyword parameter is unsupported in {}".format(
                    self.__class__.__name__))
        if not issubclass(vmclass, BaseVM):
            raise TypeError(
                "'vmclass' should specify a subclass of qubes.vm.BaseVM")

        super(VMProperty, self).__init__(name,
            saver=(lambda self_, prop, value:
                self._none_value if value is None else value.name),
            **kwargs)
        self.vmclass = vmclass
        self.allow_none = allow_none

    def __set__(self, instance, value):
        if value is self.__class__.DEFAULT:
            self.__delete__(instance)
            return

        if value == self._none_value:
            value = None
        if value is None:
            if self.allow_none:
                super(VMProperty, self).__set__(instance, value)
                return
            else:
                raise ValueError(
                    'Property {!r} does not allow setting to {!r}'.format(
                        self.__name__, value))

        app = instance if isinstance(instance, qubes.Qubes) else instance.app

        try:
            vm = app.domains[value]
        except KeyError:
            raise qubes.exc.QubesVMNotFoundError(value)

        if not isinstance(vm, self.vmclass):
            raise TypeError('wrong VM class: domains[{!r}] if of type {!s} '
                'and not {!s}'.format(value,
                    vm.__class__.__name__,
                    self.vmclass.__name__))

        super(VMProperty, self).__set__(instance, vm)