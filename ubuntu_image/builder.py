"""Flow for building a disk image."""


import os
import shutil
import logging

from contextlib import ExitStack, contextmanager
from operator import attrgetter
from tempfile import TemporaryDirectory
from ubuntu_image.helpers import GiB, MiB, run, weld
from ubuntu_image.image import Image
from ubuntu_image.parser import parse as parse_yaml
from ubuntu_image.state import State


SPACE = ' '
_logger = logging.getLogger('ubuntu-image')


@contextmanager
def mount(img):
    with ExitStack() as resources:
        tmpdir = resources.enter_context(TemporaryDirectory())
        mountpoint = os.path.join(tmpdir, 'root-mount')
        os.makedirs(mountpoint)
        run('sudo mount -oloop {} {}'.format(img, mountpoint))
        resources.callback(run, 'sudo umount {}'.format(mountpoint))
        yield mountpoint


def _mkfs_ext4(img_file, contents_dir):
    """Encapsulate the `mkfs.ext4` invocation.

    As of e2fsprogs 1.43.1, mkfs.ext4 supports a -d option which allows
    you to populate the ext4 partition at creation time, with the
    contents of an existing directory.  Unfortunately, we're targeting
    Ubuntu 16.04, which has e2fsprogs 1.42.X without the -d flag.  In
    that case, we have to sudo loop mount the ext4 file system and
    populate it that way.  Which sucks because sudo.
    """
    proc = run('mkfs.ext4 -L writable {} -d {}'.format(img_file, contents_dir),
               check=False)
    if proc.returncode == 0:
        # We have a new enough e2fsprogs, so we're done.
        return
    run('mkfs.ext4 -L writable {}'.format(img_file))
    with mount(img_file) as mountpoint:
        # fixme: everything is terrible.
        run('sudo cp -dR --preserve=mode,timestamps {}/* {}'.format(
            contents_dir, mountpoint), shell=True)


class BaseImageBuilder(State):
    def __init__(self, workdir=None, output=None):
        super().__init__()
        if workdir is None:
            self.workdir = self.resources.enter_context(TemporaryDirectory())
        else:
            self.workdir = workdir
        # Where the disk.img file ends up.
        self.output = (
            os.path.join(self.workdir, 'disk.img')
            if output is None
            else output)
        # Information passed between states.
        self.rootfs = None
        self.rootfs_size = 0
        self.bootfs = None
        self.bootfs_size = 0
        self.images = None
        self.boot_img = None
        self.root_img = None
        self.disk_img = None
        # Currently unused in the base class, but defined because we should
        # use this same abstraction for non-snappy images.
        self.gadget = None
        self._next.append(self.make_temporary_directories)

    def __getstate__(self):
        state = super().__getstate__()
        state.update(
            rootfs=self.rootfs,
            rootfs_size=self.rootfs_size,
            bootfs=self.bootfs,
            bootfs_size=self.bootfs_size,
            gadget=self.gadget,
            images=self.images,
            boot_img=self.boot_img,
            root_img=self.root_img,
            disk_img=self.disk_img,
            output=self.output,
            workdir=self.workdir,
            )
        return state

    def __setstate__(self, state):
        super().__setstate__(state)
        # Fail if the temporary directory no longer exists.
        self.workdir = state['workdir']
        if not os.path.isdir(self.workdir):
            raise FileNotFoundError(self.workdir)
        self.rootfs = state['rootfs']
        self.rootfs_size = state['rootfs_size']
        self.bootfs = state['bootfs']
        self.bootfs_size = state['bootfs_size']
        self.gadget = state['gadget']
        self.images = state['images']
        self.boot_img = state['boot_img']
        self.root_img = state['root_img']
        self.disk_img = state['disk_img']
        self.output = state['output']

    def make_temporary_directories(self):
        self.rootfs = os.path.join(self.workdir, 'root')
        self.bootfs = os.path.join(self.workdir, 'boot')
        os.makedirs(self.rootfs)
        os.makedirs(self.bootfs)
        self._next.append(self.populate_rootfs_contents)

    def populate_rootfs_contents(self):
        # XXX For now just put some dummy contents there to verify the basic
        # approach.
        for path, contents in {
                'foo': 'this is foo',
                'bar': 'this is bar',
                'baz/buz': 'some bazz buzz',
                }.items():
            rooted_path = os.path.join(self.rootfs, path)
            dirname = os.path.dirname(path)
            if len(dirname) > 0:
                os.makedirs(os.path.dirname(rooted_path), exist_ok=True)
            with open(rooted_path, 'w', encoding='utf-8') as fp:
                fp.write(contents)
        # Mount point for /boot.
        os.mkdir(os.path.join(self.rootfs, 'boot'))
        self._next.append(self.calculate_rootfs_size)

    def _calculate_dirsize(self, path):
        total = 0
        for dirpath, dirnames, filenames in os.walk(path):
            for filename in filenames:
                total += os.path.getsize(os.path.join(dirpath, filename))
        # Fudge factor for incidentals.
        total *= 1.5
        return total

    def calculate_rootfs_size(self):
        # Calculate the size of the root file system.  Basically, I'm trying
        # to reproduce du(1) close enough without having to call out to it and
        # parse its output.
        self.rootfs_size = self._calculate_dirsize(self.rootfs)
        self._next.append(self.populate_bootfs_contents)

    def populate_bootfs_contents(self):
        for path, contents in {
                'other': 'other',
                'boot/qux': 'boot qux',
                'boot/qay': 'boot qay',
                }.items():
            booted_path = os.path.join(self.bootfs, path)
            dirname = os.path.dirname(path)
            if len(dirname) > 0:
                os.makedirs(os.path.dirname(booted_path), exist_ok=True)
            with open(booted_path, 'w', encoding='utf-8') as fp:
                fp.write(contents)
        self._next.append(self.calculate_bootfs_size)

    def calculate_bootfs_size(self):
        self.bootfs_size = self._calculate_dirsize(self.bootfs)
        self._next.append(self.prepare_filesystems)

    def prepare_filesystems(self):
        self.images = os.path.join(self.workdir, '.images')
        os.makedirs(self.images)
        # The image for the boot partition.
        self.boot_img = os.path.join(self.images, 'boot.img')
        run('dd if=/dev/zero of={} count=0 bs=64M seek=1'.format(
            self.boot_img))
        run('mkfs.vfat {}'.format(self.boot_img))
        # The image for the root partition.
        self.root_img = os.path.join(self.images, 'root.img')
        run('dd if=/dev/zero of={} count=0 bs=1GB seek=2'.format(
            self.root_img))
        # We defer creating the root file system image because we have to
        # populate it at the same time.  See mkfs.ext4(8) for details.
        self._next.append(self.populate_filesystems)

    def populate_filesystems(self):
        # The boot file system is VFAT.
        sourcefiles = SPACE.join(
            os.path.join(self.bootfs, filename)
            for filename in os.listdir(self.bootfs)
            )
        run('mcopy -s -i {} {} ::'.format(self.boot_img, sourcefiles),
            env=dict(MTOOLS_SKIP_CHECK='1'))
        # The root partition needs to be ext4, which may or may not be
        # populated at creation time, depending on the version of e2fsprogs.
        _mkfs_ext4(self.root_img, self.rootfs)
        self._next.append(self.make_disk)

    def make_disk(self):
        if self.gadget and self.gadget.scheme != 'GPT':
            raise ValueError('DOS partition tables not yet supported')
        self.disk_img = os.path.join(self.images, 'disk.img')
        image = Image(self.disk_img, GiB(4))
        # Create BIOS boot partition
        #
        # The partition is 1MiB in size, as recommended by various
        # partitioning guides.  The actual required size is much, much
        # smaller.
        #
        # https://www.gnu.org/software/grub/manual/html_node/BIOS-installation.html#BIOS-installation
        # image.partition(new='1:4MiB:+1MiB')
        # image.partition(typecode='1:21686148-6449-6E6F-744E-656564454649')
        # image.partition(change_name='1:grub')
        # image.copy_blob(self.boot_img,
        #                 bs='1MiB', seek=4, count=1, conv='notrunc')
        #
        # Create EFI system partition
        #
        part_id = 1
        offset = MiB(4)
        if self.gadget:
            # walk through all partitions, and write them to the disk image
            # at the lowest permissible offset.  We should not have any
            # overlapping partitions, the parser should have already rejected
            # such as invalid.
            # XXX: the parser should sort these partitions for us in disk
            # order as part of checking for overlaps, so we should not need
            # to sort them here.
            for part in sorted(self.gadget.partitions,
                               key=attrgetter('offset')):
                size = part.size
                if not part.offset:
                    part.offset = offset
                # sgdisk takes either a sector or a KiB/MiB argument; assume
                # that the offset and size are always multiples of 1MiB.  We
                # should actually prefer multiples of 4MiB for optimal
                # performance on modern disks.
                partdef = '{}:{}M:+{}M'.format(
                    part_id, offset // MiB(1), size // MiB(1))
                image.partition(new=partdef)
                image.partition(typecode='{}:{}'.format(
                    part_id, part.type_id))
                if part.role == 'ESP':
                    # XXX: this should be part of the parser defaults.
                    image.partition(change_name='{}:system-boot'
                                                .format(part_id))
                    # assume that the offset and size are always multiples of
                    # 1MiB.  (XXX: but this should be enforced elsewhere.)
                    image.copy_blob(self.boot_img,
                                    bs='1M', seek=offset // 1024 // 1024,
                                    count=part.size // 1024 // 1024,
                                    conv='notrunc')
                offset = part.offset + size
                part_id += 1
        else:
            # XXX: there should be no 'else'
            image.partition(new='2:5MiB:+64MiB')
            image.partition(typecode='2:C12A7328-F81F-11D2-BA4B-00A0C93EC93B')
            image.partition(change_name='2:system-boot')
            image.copy_blob(self.boot_img,
                            bs='1M', seek=5, count=64, conv='notrunc')
            part_id += 2
        # Create main snappy writable partition
        # XXX: remove the fixed offset
        image.partition(new='{}:72MiB:+3646MiB'.format(part_id))
        image.partition(typecode='{}:0FC63DAF-8483-4772-8E79-3D69D8477DE4'
                                 .format(part_id))
        image.partition(change_name='{}:writable'.format(part_id))
        image.copy_blob(self.root_img,
                        bs='1M', seek=72, count=3646, conv='notrunc')
        self._next.append(self.finish)

    def finish(self):
        # Move the completed disk image to destination location, since the
        # temporary scratch directory is about to get removed.
        shutil.move(self.disk_img, self.output)
        self._next.append(self.close)


class ModelAssertionBuilder(BaseImageBuilder):
    def __init__(self, args):
        # Where should the image file end up?
        output = (
            os.path.abspath('disk.img')
            if args.output is None
            else args.output)
        super().__init__(workdir=args.workdir, output=output)
        self.args = args
        self.unpackdir = None

    def __getstate__(self):
        state = super().__getstate__()
        state.update(
            args=self.args,
            unpackdir=self.unpackdir,
            )
        return state

    def __setstate__(self, state):
        super().__setstate__(state)
        self.args = state['args']
        self.unpackdir = state['unpackdir']

    def make_temporary_directories(self):
        self.unpackdir = os.path.join(self.workdir, 'unpack')
        os.makedirs(self.unpackdir)
        super().make_temporary_directories()

    def populate_rootfs_contents(self):
        weld(self.args.model_assertion,
             self.rootfs, self.unpackdir,
             self.args.channel)
        # XXX For testing purposes, these files can't be owned by root.  Blech
        # blech blech.
        run('sudo chown -R {} {}'.format(os.getuid(), self.rootfs))
        run('sudo chown -R {} {}'.format(os.getuid(), self.bootfs))
        run('sudo chown -R {} {}'.format(os.getuid(), self.unpackdir))
        self._next.append(self.load_gadget_yaml)

    def load_gadget_yaml(self):
        yaml_file = os.path.join(self.unpackdir, 'meta', 'image.yaml')
        with open(yaml_file, 'r', encoding='utf-8') as fp:
            self.gadget = parse_yaml(fp)
        self._next.append(self.calculate_rootfs_size)

    def populate_bootfs_contents(self):
        # The --root-dir directory has a boot/ directory inside it.  The
        # contents of this directory (but not the parent <root-dir>/boot
        # directory itself) needs to be moved to the bootfs directory.  Leave
        # <root-dir>/boot as a future mount point.
        boot = os.path.join(self.rootfs, 'system-data/boot')
        # XXX: bad special-casing.  snap weld currently installs to /boot/grub,
        # but we need to map this to /EFI/ubuntu.
        os.makedirs(os.path.join(self.bootfs, 'EFI'), exist_ok=True)
        for filename in os.listdir(boot):
            src = os.path.join(boot, filename)
            dst = os.path.join(self.bootfs, 'EFI', 'ubuntu')
            shutil.copytree(src, dst)
            shutil.rmtree(src)
        self._next.append(self.calculate_bootfs_size)
        for part in self.gadget.partitions:
            if part.role == 'ESP':
                for file in part.files:
                    src = os.path.join(self.unpackdir, file[0])
                    dst = os.path.join(self.bootfs, file[1])
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy(src, dst)
                # XXX: there should only be one ESP
                break
