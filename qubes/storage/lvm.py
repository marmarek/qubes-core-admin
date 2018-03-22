#
# The Qubes OS Project, http://www.qubes-os.org
#
# Copyright (C) 2016 Bahtiar `kalkin-` Gadimov <bahtiar@gadimov.de>
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, see <https://www.gnu.org/licenses/>.
#

''' Driver for storing vm images in a LVM thin pool '''

import logging
import os
import re
import subprocess

import time

import asyncio

import qubes
import qubes.storage
import qubes.utils


def check_lvm_version():
    #Check if lvm is very very old, like in Travis-CI
    try:
        lvm_help = subprocess.check_output(['lvm', 'lvcreate', '--help'],
            stderr=subprocess.DEVNULL).decode()
        return '--setactivationskip' not in lvm_help
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

lvm_is_very_old = check_lvm_version()


class ThinPool(qubes.storage.Pool):
    ''' LVM Thin based pool implementation

    Volumes are stored as LVM thin volumes, in thin pool specified by
    *volume_group*/*thin_pool* arguments. LVM volume naming scheme:

        vm-{vm_name}-{volume_name}[-suffix]

    Where suffix can be one of:
        "-snap" - snapshot for currently running VM, at VM shutdown will be
        either discarded (if save_on_stop=False), or committed
        (if save_on_stop=True)
        "-{revision_id}" - volume revision - new revision is automatically
        created at each VM shutdown, the newest one is the volume current state;
        *revisions_to_keep* control how many old revisions
        (in addition to the current one) should be stored
        "" (no suffix) - volatile volume (snap_on_start=False,
        save_on_stop=False); also a deprecated name for the most recent
        committed revision

    On VM startup, new volume is created, depending on volume type,
    according to the table below:

    snap_on_start, save_on_stop
    False,         False,        - no suffix, fresh empty volume
    False,         True,         - "-snap", snapshot of last committed revision
    True ,         False,        - "-snap", snapshot of last committed revision
                                   of source volume (from VM's template)
    True,          True,         - unsupported configuration

    Volume's revision_id is either:
     - "{revision_number}.{timestamp}", where revision number is monotonic
       number and a timestamp is in '%s' format, or
     - "{timestamp}", where timestamp is in '%s' format

    The second format is old one, and all revisions in old format are considered
    older than any revision in new format.
    '''  # pylint: disable=protected-access

    size_cache = None

    driver = 'lvm_thin'

    def __init__(self, volume_group, thin_pool, revisions_to_keep=1, **kwargs):
        super(ThinPool, self).__init__(revisions_to_keep=revisions_to_keep,
                                       **kwargs)
        self.volume_group = volume_group
        self.thin_pool = thin_pool
        self._pool_id = "{!s}/{!s}".format(volume_group, thin_pool)
        self.log = logging.getLogger('qubes.storage.lvm.%s' % self._pool_id)

        self._volume_objects_cache = {}

    @property
    def config(self):
        return {
            'name': self.name,
            'volume_group': self.volume_group,
            'thin_pool': self.thin_pool,
            'driver': ThinPool.driver
        }

    def destroy(self):
        pass  # TODO Should we remove an existing pool?

    def init_volume(self, vm, volume_config):
        ''' Initialize a :py:class:`qubes.storage.Volume` from `volume_config`.
        '''

        if 'revisions_to_keep' not in volume_config.keys():
            volume_config['revisions_to_keep'] = self.revisions_to_keep
        if 'vid' not in volume_config.keys():
            if vm and hasattr(vm, 'name'):
                vm_name = vm.name
            else:
                # for the future if we have volumes not belonging to a vm
                vm_name = qubes.utils.random_string()

            assert self.name

            volume_config['vid'] = "{!s}/vm-{!s}-{!s}".format(
                self.volume_group, vm_name, volume_config['name'])

        volume_config['volume_group'] = self.volume_group
        volume_config['pool'] = self
        volume = ThinVolume(**volume_config)
        self._volume_objects_cache[volume_config['vid']] = volume
        return volume

    def setup(self):
        reset_cache()
        cache_key = self.volume_group + '/' + self.thin_pool
        if cache_key not in size_cache:
            raise qubes.storage.StoragePoolException(
                'Thin pool {} does not exist'.format(cache_key))
        if size_cache[cache_key]['attr'][0] != 't':
            raise qubes.storage.StoragePoolException(
                'Volume {} is not a thin pool'.format(cache_key))
        # TODO Should we create a non existing pool?

    def get_volume(self, vid):
        ''' Return a volume with given vid'''
        if vid in self._volume_objects_cache:
            return self._volume_objects_cache[vid]

        config = {
                'pool': self,
                'vid': vid,
                'name': vid,
                'volume_group': self.volume_group,
            }
        # don't cache this object, as it doesn't carry full configuration
        return ThinVolume(**config)

    def list_volumes(self):
        ''' Return a list of volumes managed by this pool '''
        volumes = []
        revision_suffix_re = re.compile(r'.*-[0-9]+\.[0-9]+$')
        for vid, vol_info in size_cache.items():
            if not vid.startswith(self.volume_group + '/'):
                continue
            if vol_info['pool_lv'] != self.thin_pool:
                continue
            if vid.endswith('-snap') or vid.endswith('-import'):
                # implementation detail volume
                continue
            if vid.endswith('-back'):
                # old revisions
                continue
            if revision_suffix_re.match(vid):
                # some revision, split its number to get vid
                vid = vid.rsplit('-', 1)[0]
            volume = self.get_volume(vid)
            if volume in volumes:
                continue
            volumes.append(volume)
        return volumes

    @property
    def size(self):
        try:
            return qubes.storage.lvm.size_cache[
                self.volume_group + '/' + self.thin_pool]['size']
        except KeyError:
            return 0

    @property
    def usage(self):
        try:
            return qubes.storage.lvm.size_cache[
                self.volume_group + '/' + self.thin_pool]['usage']
        except KeyError:
            return 0


def init_cache(log=logging.getLogger('qubes.storage.lvm')):
    cmd = ['lvs', '--noheadings', '-o',
           'vg_name,pool_lv,name,lv_size,data_percent,lv_attr,origin',
           '--units', 'b', '--separator', ';']
    if os.getuid() != 0:
        cmd.insert(0, 'sudo')
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        close_fds=True)
    out, err = p.communicate()
    return_code = p.returncode
    if return_code == 0 and err:
        log.warning(err)
    elif return_code != 0:
        raise qubes.storage.StoragePoolException(err)

    result = {}

    for line in out.splitlines():
        line = line.decode().strip()
        pool_name, pool_lv, name, size, usage_percent, attr, \
            origin = line.split(';', 6)
        if '' in [pool_name, name, size, usage_percent]:
            continue
        name = pool_name + "/" + name
        size = int(size[:-1])  # Remove 'B' suffix
        usage = int(size / 100 * float(usage_percent))
        result[name] = {'size': size, 'usage': usage, 'pool_lv': pool_lv,
            'attr': attr, 'origin': origin}

    return result


size_cache = init_cache()


def _revision_sort_key(revision):
    '''Sort key for revisions. For old revisions (just timestamp),
    sort them by time; for new ones (number.timestamp), sort by number.
    All new revisions are considered newer by all old ones.

    :returns tuple(number, timestamp)
    '''
    if isinstance(revision, tuple):
        revision = revision[0]
    if '-' in revision:
        revision = revision.split('-')[0]
    if '.' in revision:
        number, timestamp = revision.split('.', 1)
        return int(number), int(timestamp)
    return 0, int(revision)


class ThinVolume(qubes.storage.Volume):
    ''' Default LVM thin volume implementation
    '''  # pylint: disable=too-few-public-methods


    def __init__(self, volume_group, size=0, **kwargs):
        self.volume_group = volume_group
        super(ThinVolume, self).__init__(size=size, **kwargs)
        self.log = logging.getLogger('qubes.storage.lvm.%s' % str(self.pool))

        if self.snap_on_start or self.save_on_stop:
            self._vid_snap = self.vid + '-snap'
        if self.save_on_stop:
            self._vid_import = self.vid + '-import'

        self.__vid_current = None

        self._size = size

    def _get_next_revision_number(self):
        revisions = self._get_revisions('(|-back)')
        if not revisions:
            return 1
        max_revision = max([
            int(r.split('.')[0]) if '.' in r else 0
            for r in revisions
        ])
        return max_revision + 1

    @property
    def _vid_current(self):
        if self.__vid_current is None:
            self._refresh_vid_current()
        return self.__vid_current

    def _refresh_vid_current(self):
        revisions = self._get_revisions('')
        if not revisions:
            # fallback to old format - possibly pool
            # not connected, don't crash caller
            # of 'path' attribute access
            self.__vid_current = self.vid
        else:
            last_revision = \
                max(revisions.items(), key=_revision_sort_key)[0]
            self.__vid_current = self.vid + '-' + last_revision

    @property
    def path(self):
        return '/dev/' + self._vid_current

    def _get_revisions(self, suffix):
        name_re = re.compile(r'{}-[0-9.]*{}'.format(self.vid, suffix))
        name_prefix = self.vid + '-'
        revisions = {}
        for revision_vid in size_cache:
            if not name_re.fullmatch(revision_vid):
                continue
            revision_vid = revision_vid[len(name_prefix):]
            # get revision without suffix
            if '.' in revision_vid:
                seconds = int(revision_vid.split('-')[0].split('.')[1])
            else:
                seconds = int(revision_vid.split('-')[0])
            iso_date = qubes.storage.isodate(seconds).split('.', 1)[0]
            revisions[revision_vid] = iso_date
        return revisions

    @property
    def revisions(self):
        revisions = self._get_revisions('(-back|)')
        # return early if no revisions found - probably pool detached
        if not revisions:
            return revisions
        # skip the current one
        assert self._vid_current is not None
        if self._vid_current != self.vid:
            del revisions[self._vid_current[len(self.vid)+1:]]
        return revisions

    @property
    def size(self):
        try:
            if self.is_dirty():
                return qubes.storage.lvm.size_cache[self._vid_snap]['size']
            return qubes.storage.lvm.size_cache[self._vid_current]['size']
        except KeyError:
            return self._size

    @size.setter
    def size(self, _):
        raise qubes.storage.StoragePoolException(
            "You shouldn't use lvm size setter")

    def _reset(self):
        ''' Resets a volatile volume '''
        assert not self.snap_on_start and not self.save_on_stop, \
            "Not a volatile volume"
        self.log.debug('Resetting volatile %s', self.vid)
        try:
            cmd = ['remove', self.vid]
            qubes_lvm(cmd, self.log)
        except qubes.storage.StoragePoolException:
            pass
        # pylint: disable=protected-access
        cmd = ['create', self.pool._pool_id, self.vid.split('/')[1],
               str(self.size)]
        qubes_lvm(cmd, self.log)

    def _remove_revisions(self, revisions=None):
        '''Remove old volume revisions.

        If no revisions list is given, it removes old revisions according to
        :py:attr:`revisions_to_keep`

        :param revisions: list of revisions to remove
        '''
        if revisions is None:
            revisions = sorted(self.revisions.items(),
                key=_revision_sort_key)
            # pylint: disable=invalid-unary-operand-type
            revisions = revisions[:(-self.revisions_to_keep) or None]
            revisions = [rev_id for rev_id, _ in revisions]

        for rev_id in revisions:
            # safety check
            assert rev_id != self._vid_current
            try:
                cmd = ['remove', self.vid + '-' + rev_id]
                qubes_lvm(cmd, self.log)
            except qubes.storage.StoragePoolException:
                pass

    def _commit(self, vid_to_commit=None, keep=False):
        '''
        Commit temporary volume into current one. By default
        :py:attr:`_vid_snap` is used (which is created by :py:meth:`start()`),
        but can be overriden by *vid_to_commit* argument.

        :param vid_to_commit: LVM volume ID to commit into this one
        :param keep: whether to keep or not *vid_to_commit*.
          IOW use 'clone' or 'rename' methods.
        :return: None
        '''
        msg = "Trying to commit {!s}, but it has save_on_stop == False"
        msg = msg.format(self)
        assert self.save_on_stop, msg

        msg = "Trying to commit {!s}, but it has rw == False"
        msg = msg.format(self)
        assert self.rw, msg
        if vid_to_commit is None:
            assert hasattr(self, '_vid_snap')
            vid_to_commit = self._vid_snap

        # TODO: when converting this function to coroutine, this _must_ be
        # under a lock
        if not os.path.exists('/dev/' + vid_to_commit):
            # nothing to commit
            return

        previous_vid_current = self._vid_current
        if previous_vid_current == self.vid:
            # VM started with old code, need to rename volume
            # to contain revision_id
            cmd = ['rename', previous_vid_current,
                   '{}-{}.{}'.format(
                    self.vid, self._get_next_revision_number(),
                    int(time.time()))]
            qubes_lvm(cmd, self.log)
            reset_cache()

        # then assign new revision number to the volume we're committing right
        # now
        cmd = ['clone' if keep else 'rename',
               vid_to_commit,
               '{}-{}.{}'.format(
                self.vid, self._get_next_revision_number(),
                int(time.time()))]
        qubes_lvm(cmd, self.log)
        reset_cache()
        self._refresh_vid_current()
        # make sure the one we've committed right now is properly
        # detected as the current one - before removing anything
        assert self._vid_current == cmd[2]

        # and remove old snapshots, if needed
        self._remove_revisions()

    def create(self):
        assert self.vid
        assert self.size
        if self.save_on_stop:
            revision_suffix = '-1.{}'.format(int(time.time()))
            if self.source:
                cmd = ['clone', self.source.path,
                       self.vid + revision_suffix]
            else:
                cmd = [
                    'create',
                    self.pool._pool_id,  # pylint: disable=protected-access
                    self.vid.split('/', 1)[1] + revision_suffix,
                    str(self.size)
                ]
            qubes_lvm(cmd, self.log)
            reset_cache()
            self._refresh_vid_current()
        return self

    def remove(self):
        assert self.vid
        try:
            if os.path.exists('/dev/' + self._vid_snap):
                cmd = ['remove', self._vid_snap]
                qubes_lvm(cmd, self.log)
        except AttributeError:
            pass

        try:
            if os.path.exists('/dev/' + self._vid_import):
                cmd = ['remove', self._vid_import]
                qubes_lvm(cmd, self.log)
        except AttributeError:
            pass

        self._remove_revisions(self.revisions.keys())
        if not os.path.exists(self.path):
            return
        cmd = ['remove', self.path]
        qubes_lvm(cmd, self.log)
        reset_cache()
        # pylint: disable=protected-access
        self.pool._volume_objects_cache.pop(self.vid, None)

    def export(self):
        ''' Returns an object that can be `open()`. '''
        # make sure the device node is available
        qubes_lvm(['activate', self.path], self.log)
        devpath = self.path
        return devpath

    @asyncio.coroutine
    def import_volume(self, src_volume):
        if not src_volume.save_on_stop:
            return self

        if self.is_dirty():
            raise qubes.storage.StoragePoolException(
                'Cannot import to dirty volume {} -'
                ' start and stop a qube to cleanup'.format(self.vid))
        self.abort_if_import_in_progress()
        # HACK: neat trick to speed up testing if you have same physical thin
        # pool assigned to two qubes-pools i.e: qubes_dom0 and test-lvm
        # pylint: disable=line-too-long
        if isinstance(src_volume.pool, ThinPool) and \
                src_volume.pool.thin_pool == self.pool.thin_pool:  # NOQA
            self._commit(src_volume.path[len('/dev/'):], keep=True)
        else:
            cmd = ['create',
                   self.pool._pool_id,  # pylint: disable=protected-access
                   self._vid_import.split('/')[1],
                   str(src_volume.size)]
            qubes_lvm(cmd, self.log)
            src_path = src_volume.export()
            cmd = ['dd', 'if=' + src_path, 'of=/dev/' + self._vid_import,
                'conv=sparse', 'status=none']
            if not os.access('/dev/' + self._vid_import, os.W_OK) or \
                    not os.access(src_path, os.R_OK):
                cmd.insert(0, 'sudo')

            p = yield from asyncio.create_subprocess_exec(*cmd)
            yield from p.wait()
            if p.returncode != 0:
                cmd = ['remove', self._vid_import]
                qubes_lvm(cmd, self.log)
                raise qubes.storage.StoragePoolException(
                    'Failed to import volume {!r}, dd exit code: {}'.format(
                        src_volume, p.returncode))
            self._commit(self._vid_import)

        return self

    def import_data(self):
        ''' Returns an object that can be `open()`. '''
        if self.is_dirty():
            raise qubes.storage.StoragePoolException(
                'Cannot import data to dirty volume {}, stop the qube first'.
                format(self.vid))
        self.abort_if_import_in_progress()
        # pylint: disable=protected-access
        cmd = ['create', self.pool._pool_id, self._vid_import.split('/')[1],
               str(self.size)]
        qubes_lvm(cmd, self.log)
        reset_cache()
        devpath = '/dev/' + self._vid_import
        return devpath

    def import_data_end(self, success):
        '''Either commit imported data, or discard temporary volume'''
        if not os.path.exists('/dev/' + self._vid_import):
            raise qubes.storage.StoragePoolException(
                'No import operation in progress on {}'.format(self.vid))
        if success:
            self._commit(self._vid_import)
        else:
            cmd = ['remove', self._vid_import]
            qubes_lvm(cmd, self.log)

    def abort_if_import_in_progress(self):
        try:
            devpath = '/dev/' + self._vid_import
            if os.path.exists(devpath):
                raise qubes.storage.StoragePoolException(
                    'Import operation in progress on {}'.format(self.vid))
        except AttributeError:  # self._vid_import
            # no vid_import - import definitely not in progress
            pass

    def is_dirty(self):
        if self.save_on_stop:
            return os.path.exists('/dev/' + self._vid_snap)
        return False

    def is_outdated(self):
        if not self.snap_on_start:
            return False
        if self._vid_snap not in size_cache:
            return False
        return (size_cache[self._vid_snap]['origin'] !=
               self.source.path.split('/')[-1])

    def revert(self, revision=None):
        if self.is_dirty():
            raise qubes.storage.StoragePoolException(
                'Cannot revert dirty volume {}, stop the qube first'.format(
                    self.vid))
        self.abort_if_import_in_progress()
        if revision is None:
            revision = \
                max(self.revisions.items(), key=_revision_sort_key)[0]
        old_path = '/dev/' + self.vid + '-' + revision
        if not os.path.exists(old_path):
            msg = "Volume {!s} has no {!s}".format(self, old_path)
            raise qubes.storage.StoragePoolException(msg)

        back_vid = self._vid_current
        new_current_revision = '{}-{}.{}'.format(
            self.vid, self._get_next_revision_number(), int(time.time()))
        cmd = ['clone', self.vid + '-' + revision, new_current_revision]
        qubes_lvm(cmd, self.log)
        cmd = ['remove', back_vid]
        qubes_lvm(cmd, self.log)
        reset_cache()
        self._refresh_vid_current()
        return self

    def resize(self, size):
        ''' Expands volume, throws
            :py:class:`qubst.storage.qubes.storage.StoragePoolException` if
            given size is less than current_size
        '''
        if not self.rw:
            msg = 'Can not resize reađonly volume {!s}'.format(self)
            raise qubes.storage.StoragePoolException(msg)

        if size < self.size:
            raise qubes.storage.StoragePoolException(
                'For your own safety, shrinking of %s is'
                ' disabled (%d < %d). If you really know what you'
                ' are doing, use `lvresize` on %s manually.' %
                (self.name, size, self.size, self.vid))

        if size == self.size:
            return

        if self.is_dirty():
            cmd = ['extend', self._vid_snap, str(size)]
            qubes_lvm(cmd, self.log)
        elif hasattr(self, '_vid_import') and \
                os.path.exists('/dev/' + self._vid_import):
            cmd = ['extend', self._vid_import, str(size)]
            qubes_lvm(cmd, self.log)
        elif self.save_on_stop or not self.snap_on_start:
            cmd = ['extend', self._vid_current, str(size)]
            qubes_lvm(cmd, self.log)
        reset_cache()

    def _snapshot(self):
        try:
            cmd = ['remove', self._vid_snap]
            qubes_lvm(cmd, self.log)
        except:  # pylint: disable=bare-except
            pass

        if self.source is None:
            cmd = ['clone', self._vid_current, self._vid_snap]
        else:
            cmd = ['clone', self.source.path, self._vid_snap]
        qubes_lvm(cmd, self.log)

    def start(self):
        self.abort_if_import_in_progress()
        try:
            if self.snap_on_start or self.save_on_stop:
                if not self.save_on_stop or not self.is_dirty():
                    self._snapshot()
            else:
                self._reset()
        finally:
            reset_cache()
        return self

    def stop(self):
        try:
            if self.save_on_stop:
                self._commit()
            if self.snap_on_start and not self.save_on_stop:
                cmd = ['remove', self._vid_snap]
                qubes_lvm(cmd, self.log)
            elif not self.snap_on_start and not self.save_on_stop:
                cmd = ['remove', self.vid]
                qubes_lvm(cmd, self.log)
        finally:
            reset_cache()
        return self

    def verify(self):
        ''' Verifies the volume. '''
        if not self.save_on_stop and not self.snap_on_start:
            # volatile volumes don't need any files
            return True
        if self.source is not None:
            vid = self.source.path[len('/dev/'):]
        else:
            vid = self._vid_current
        try:
            vol_info = size_cache[vid]
            if vol_info['attr'][4] != 'a':
                raise qubes.storage.StoragePoolException(
                    'volume {} not active'.format(vid))
        except KeyError:
            raise qubes.storage.StoragePoolException(
                'volume {} missing'.format(vid))
        return True


    def block_device(self):
        ''' Return :py:class:`qubes.storage.BlockDevice` for serialization in
            the libvirt XML template as <disk>.
        '''
        if self.snap_on_start or self.save_on_stop:
            return qubes.storage.BlockDevice(
                '/dev/' + self._vid_snap, self.name, self.script,
                self.rw, self.domain, self.devtype)

        return super(ThinVolume, self).block_device()

    @property
    def usage(self):  # lvm thin usage always returns at least the same usage as
                      # the parent
        try:
            return qubes.storage.lvm.size_cache[self._vid_current]['usage']
        except KeyError:
            return 0


def pool_exists(pool_id):
    ''' Return true if pool exists '''
    try:
        vol_info = size_cache[pool_id]
        return vol_info['attr'][0] == 't'
    except KeyError:
        return False


def qubes_lvm(cmd, log=logging.getLogger('qubes.storage.lvm')):
    ''' Call :program:`lvm` to execute an LVM operation '''
    action = cmd[0]
    if action == 'remove':
        lvm_cmd = ['lvremove', '-f', cmd[1]]
    elif action == 'clone':
        lvm_cmd = ['lvcreate', '-kn', '-ay', '-s', cmd[1], '-n', cmd[2]]
    elif action == 'create':
        lvm_cmd = ['lvcreate', '-T', cmd[1], '-kn', '-ay', '-n', cmd[2], '-V',
           str(cmd[3]) + 'B']
    elif action == 'extend':
        size = int(cmd[2]) / (1024 * 1024)
        lvm_cmd = ["lvextend", "-L%s" % size, cmd[1]]
    elif action == 'activate':
        lvm_cmd = ['lvchange', '-ay', cmd[1]]
    elif action == 'rename':
        lvm_cmd = ['lvrename', cmd[1], cmd[2]]
    else:
        raise NotImplementedError('unsupported action: ' + action)
    if lvm_is_very_old:
        # old lvm in trusty image used there does not support -k option
        lvm_cmd = [x for x in lvm_cmd if x != '-kn']
    if os.getuid() != 0:
        cmd = ['sudo', 'lvm'] + lvm_cmd
    else:
        cmd = ['lvm'] + lvm_cmd
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        close_fds=True)
    out, err = p.communicate()
    return_code = p.returncode
    if out:
        log.debug(out)
    if return_code == 0 and err:
        log.warning(err)
    elif return_code != 0:
        assert err, "Command exited unsuccessful, but printed nothing to stderr"
        raise qubes.storage.StoragePoolException(err)
    return True


def reset_cache():
    qubes.storage.lvm.size_cache = init_cache()
