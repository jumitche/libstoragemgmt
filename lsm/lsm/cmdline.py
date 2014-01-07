# Copyright (C) 2012 Red Hat, Inc.
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#
# Author: tasleson
from argparse import ArgumentParser
import optparse
import os
import textwrap
import sys
import getpass
import time

import common
import client
import data
from version import VERSION
from data import Capabilities

##@package lsm.cmdline


## Users are reporting errors with broken pipe when piping output
# to another program.  This appears to be related to this issue:
# http://bugs.python.org/issue11380
# Unable to reproduce, but hopefully this will address it.
# @param msg    The message to be written to stdout
def out(msg):
    try:
        sys.stdout.write(str(msg))
        sys.stdout.write("\n")
        sys.stdout.flush()
    except IOError:
        sys.exit(1)


## Wraps the invocation to the command line
# @param    client  Object to invoke calls on (optional)
def cmd_line_wrapper(c=None):
    """
    Common command line code, called.
    """
    try:
        cli = CmdLine()
        cli.process(c)
    except ArgError as ae:
        sys.stderr.write(str(ae))
        sys.stderr.flush()
        sys.exit(2)
    except common.LsmError as le:
        sys.stderr.write(str(le) + "\n")
        sys.stderr.flush()
        sys.exit(4)
    except KeyboardInterrupt:
        sys.exit(1)


## Simple class used to handle \n in optparse output
class MyWrapper(object):
    """
    Handle \n in text for the command line help etc.
    """

    def __init__(self):
        pass

    @staticmethod
    def wrap(text, width=70, **kw):
        rc = []
        for line in text.split("\n"):
            rc.extend(textwrap.wrap(line, width, **kw))
        return rc

    @staticmethod
    def fill(text, width=70, **kw):
        rc = []
        for line in text.split("\n"):
            rc.append(textwrap.fill(line, width, **kw))
        return "\n".join(rc)


## This class represents a command line argument error
class ArgError(Exception):
    def __init__(self, message, *args, **kwargs):
        """
        Class represents an error.
        """
        Exception.__init__(self, *args, **kwargs)
        self.msg = message

    def __str__(self):
        return "%s: error: %s\n" % (os.path.basename(sys.argv[0]), self.msg)


## Prefixes cmd with "cmd_"
# @param    cmd     The command to prefix with cmd_"
# @return   The cmd string prefixed with "cmd_"
def _c(cmd):
    return "cmd_" + cmd


## Prefixes option with "opt_"
# @param    option  The option to prefix with "opt_"
# @return   The option string prefixed with "opt_"
def _o(option):
    return "opt_" + option


## Finds an item based on the id.  Each list item requires a member "id"
# @param    l       list to search
# @param    the_id  the id to match
def _get_item(l, the_id):
    for i in l:
        if i.id == the_id:
            return i
    return None


## Class that encapsulates the command line arguments for lsmcli
# Note: This class is used by lsmcli and any python plug-ins.
class CmdLine:
    """
    Command line interface class.
    """

    ##
    # Warn of imminent data loss
    # @param    deleting    Indicate data will be lost vs. may be lost
    #                       (re-size)
    # @return True if operation confirmed, else False
    def confirm_prompt(self, deleting):
        """
        Give the user a chance to bail.
        """
        if not self.args.force:
            msg = "will" if deleting else "may"
            out("Warning: You are about to do an operation that %s cause data "
                "to be lost!\nPress [Y|y] to continue, any other key to abort"
                % msg)

            pressed = common.getch()
            if pressed.upper() == 'Y':
                return True
            else:
                out('Operation aborted!')
                return False
        else:
            return True

    ##
    # Tries to make the output better when it varies considerably from
    # plug-in to plug-in.
    # @param    rows    Data, first row is header all other data.
    def display_table(self, rows):
        """
        Creates a nicer text dump of tabular data.  First row should be the
        column headers.
        """
        #If any of the table cells is another list, lets flatten using the sep
        for i in range(len(rows)):
            for j in range(len(rows[i])):
                if isinstance(rows[i][j], list):
                    rows[i][j] = self._list(rows[i][j])

        if self.args.sep is not None:
            s = self.args.sep

            #See if we want to display the header or not!
            start = 1
            if self.args.header:
                start = 0

            for i in range(start, len(rows)):
                out(s.join([str(x) for x in rows[i]]))

        else:
            if len(rows) >= 2:
                #Get the max length of each column
                lens = []
                for l in zip(*rows):
                    lens.append(max(len(str(x)) for x in l))
                data_formats = []
                header_formats = []

                #Build the needed format
                for i in range(len(rows[0])):
                    header_formats.append("%%-%ds" % lens[i])

                    #If the row contains numerical data we will right justify.
                    if isinstance(rows[1][i], int):
                        data_formats.append("%%%dd" % lens[i])
                    else:
                        data_formats.append("%%-%ds" % lens[i])

                #Print the header, header separator and then row data.
                header_pattern = " | ".join(header_formats)
                out(header_pattern % tuple(rows[0]))
                out("-+-".join(['-' * n for n in lens]))
                data_pattern = " | ".join(data_formats)

                for i in range(1, len(rows)):
                    out(data_pattern % tuple(rows[i]))

    def display_data(self, d):

        if d and len(d):

            rows = d[0].column_headers()

            for r in d:
                rows.extend(
                    r.column_data(self.args.human, self.args.enum))

            self.display_table(rows)

    def display_available_plugins(self):
        d = []
        sep = '<}{>'
        plugins = client.Client.get_available_plugins(sep)

        # Nested class for the sole purpose of table display
        class PlugData(data.IData):

            def __init__(self, description, plugin_version):
                self.desc = description
                self.version = plugin_version

            def column_data(self, human=False, enum_as_number=False):
                return [[self.desc, self.version]]

            def column_headers(self):
                return [["Description", "Version"]]

        for p in plugins:
            desc, version = p.split(sep)
            d.append(PlugData(desc, version))

        self.display_data(d)

    ## All the command line arguments and options are created in this method
    @staticmethod
    def cli():
        """
        Command line interface parameters
        """
        optparse.textwrap = MyWrapper
        parser = ArgumentParser()
        parser.description = 'libStorageMgmt command line interface. \n'

        parser.epilog = ('Copyright 2012-2013 Red Hat, Inc.\n'
                         'Please report bugs to '
                         '<libstoragemgmt-devel@lists.sourceforge.net>\n')

        parser.add_argument('-v', '--version', action='version',
                            version="%s %s" % (sys.argv[0], VERSION))
        parser.add_argument('-u', '--uri', action="store", type=str,
                          dest="uri",
                          help='uniform resource identifier (env LSMCLI_URI)')
        parser.add_argument('-P', '--prompt', action="store_true", dest="prompt",
                          help='prompt for password (env LSMCLI_PASSWORD)')
        parser.add_argument('-H', '--human', action="store_true", dest="human",
                          help='print sizes in human readable format\n'
                               '(e.g., MiB, GiB, TiB)')
        parser.add_argument('-t', '--terse', action="store", dest="sep",
                          help='print output in terse form with "SEP" '
                               'as a record separator')

        parser.add_argument('-e', '--enum', action="store_true", dest="enum",
                          default=False,
                          help='display enumerated types as numbers '
                               'instead of text')

        parser.add_argument('-f', '--force', action="store_true", dest="force",
                          default=False,
                          help='bypass confirmation prompt for data '
                               'loss operations')

        parser.add_argument('-w', '--wait', action="store", type=int,
                          dest="wait", default=30000,
                          help="command timeout value in ms (default = 30s)")

        parser.add_argument('--header', action="store_true", dest="header",
                          help='include the header with terse')

        parser.add_argument('-b', action="store_true", dest="async",
                          default=False,
                          help='run the command async. instead of waiting '
                               'for completion. '
                               'Command will exit(7) and job id written '
                               'to stdout.')

        #What action we want to take
        commands = parser.add_argument_group('Commands')

        list_choices = ['VOLUMES', 'INITIATORS', 'POOLS', 'FS', 'SNAPSHOTS',
                        'EXPORTS', "NFS_CLIENT_AUTH", 'ACCESS_GROUPS',
                        'SYSTEMS', 'DISKS', 'PLUGINS']

        commands.add_argument('-l', '--list', action="store",
                            dest=_c("list"),
                            #metavar='<'+ ",".join(list_choices) + '>',
                            metavar='<type>',
                            choices=list_choices,
                            help='List records of type: %s \n' %
                                 ", ".join(list_choices) +
                                 'Note: SNAPSHOTS requires --fs <fs id>.\n' +
                                 '      POOLS can have -o switch.')

        commands.add_argument('--capabilities', action="store",
                            type=str,
                            dest=_c("capabilities"),
                            metavar='<system id>',
                            help='Retrieves array capabilities')

        commands.add_argument('--plugin-info', action="store",
                            metavar='<plugin>',
                            dest=_c("plugin-info"),
                            help='Retrieves plugin description and version')

        commands.add_argument('--delete-fs', action="store", type=str,
                            dest=_c("delete-fs"),
                            metavar='<fs id>',
                            help='Delete a filesystem')

        commands.add_argument('--delete-access-group', action="store",
                            type=str,
                            dest=_c("delete-access-group"),
                            metavar='<group id>',
                            help='Deletes an access group')

        commands.add_argument('--access-group-add', action="store",
                            type=str,
                            dest=_c("access-group-add"),
                            metavar='<access group id>',
                            help='Adds an initiator to an access group, '
                                 'requires:\n'
                                 '--id <initiator id\n'
                                 '--type <initiator type>')

        commands.add_argument('--access-group-remove', action="store",
                            type=str,
                            dest=_c("access-group-remove"),
                            metavar='<access group id>',
                            help='Removes an initiator from an access group, '
                                 'requires:\n'
                                 '--id <initiator id>')

        commands.add_argument('--create-volume', action="store",
                            type=str,
                            dest=_c("create-volume"),
                            metavar='<volume name>',
                            help="Creates a volume (logical unit) requires:\n"
                                 "--size <volume size>\n"
                                 "--pool <pool id>\n"
                                 "--provisioning (optional) "
                                 "[DEFAULT|THIN|FULL]\n")

        commands.add_argument('--create-fs', action="store", type=str,
                            dest=_c("create-fs"),
                            metavar='<fs name>',
                            help="Creates a file system requires:\n"
                                 "--size <fs size>\n"
                                 "--pool <pool id>")

        commands.add_argument('--create-ss', action="store", type=str,
                            dest=_c("create-ss"),
                            metavar='<snapshot name>',
                            help="Creates a snapshot, requires:\n"
                                 "--file <repeat for each file>(default "
                                 "is all files)\n"
                                 "--fs <file system id>")

        commands.add_argument('--create-access-group', action="store",
                            type=str,
                            dest=_c("create-access-group"),
                            metavar='<Access group name>',
                            help="Creates an access group, requires:\n"
                                 "--id <initiator id>\n"
                                 '--type [WWPN|WWNN|ISCSI|HOSTNAME|SAS]\n'
                                 '--system <system id>')

        commands.add_argument('--access-group-volumes', action="store",
                            type=str,
                            dest=_c("access-group-volumes"),
                            metavar='<access group id>',
                            help='Lists the volumes that the access group has'
                                 ' been granted access to')

        commands.add_argument('--volume-access-group', action="store",
                            type=str,
                            dest=_c("volume-access-group"),
                            metavar='<volume id>',
                            help='Lists the access group(s) that have access'
                                 ' to volume')

        commands.add_argument('--volumes-accessible-initiator',
                            action="store", type=str,
                            dest=_c("volumes-accessible-initiator"),
                            metavar='<initiator id>',
                            help='Lists the volumes that are accessible '
                                 'by the initiator')

        commands.add_argument('--initiators-granted-volume', action="store",
                            type=str,
                            dest=_c("initiators-granted-volume"),
                            metavar='<volume id>',
                            help='Lists the initiators that have been '
                                 'granted access to specified volume')

        commands.add_argument('--restore-ss', action="store", type=str,
                            dest=_c("restore-ss"),
                            metavar='<snapshot id>',
                            help="Restores a FS or specified files to "
                                 "previous snapshot state, requires:\n"
                                 "--fs <file system>\n"
                                 "--file <repeat for each file (optional)>\n"
                                 "--fileas <restore file name (optional)>\n"
                                 "--all (optional, exclusive option, "
                                 "restores all files in snapshot other "
                                 "options must be absent)")

        commands.add_argument('--clone-fs', action="store", type=str,
                            dest=_c("clone-fs"),
                            metavar='<source file system id>',
                            help="Creates a file system clone requires:\n"
                                 "--name <file system clone name>\n"
                                 "--backing-snapshot <backing snapshot id> "
                                 "(optional)")

        commands.add_argument('--clone-file', action="store", type=str,
                            dest=_c("clone-file"),
                            metavar='<file system>',
                            help="Creates a clone of a file (thin "
                                 "provisioned):\n"
                                 "--src  <source file to clone "
                                 "(relative path)>\n"
                                 "--dest <destination file (relative path)>\n"
                                 "--backing-snapshot <backing snapshot id> "
                                 "(optional)")

        commands.add_argument('--delete-volume', action="store",
                            type=str,
                            metavar='<volume id>',
                            dest=_c("delete-volume"),
                            help='Deletes a volume given its id')

        commands.add_argument('--delete-ss', action="store", type=str,
                            metavar='<snapshot id>',
                            dest=_c("delete-ss"),
                            help='Deletes a snapshot requires --fs')

        commands.add_argument('-r', '--replicate-volume', action="store",
                            type=str,
                            metavar='<volume id>',
                            dest=_c("replicate-volume"),
                            help='replicates a volume, requires:\n'
                                 "--type [SNAPSHOT|CLONE|COPY|MIRROR_ASYNC|"
                                 "MIRROR_SYNC]\n"
                                 "--name <human name>\n"
                                 "Optional:\n"
                                 "--pool <pool id>\n")

        commands.add_argument('--replicate-volume-range-block-size',
                            action="store", type=str,
                            metavar='<system id>',
                            dest=_c("replicate-volume-range-block-size"),
                            help='size of each replicated block in bytes')

        commands.add_argument(
            '--replicate-volume-range', action="store",
            type=str,
            metavar='<volume id>',
            dest=_c("replicate-volume-range"),
            help='replicates a portion of a volume, requires:\n'
                 "--type [SNAPSHOT|CLONE|COPY|MIRROR]\n"
                 "--dest <destination volume>\n"
                 "--src_start <source block start number>\n"
                 "--dest_start <destination block start>\n"
                 "--count <number of blocks to replicate>")

        commands.add_argument(
            '--iscsi-chap', action="store", type=str,
            metavar='<initiator id>',
            dest=_c("iscsi-chap"),
            help='configures ISCSI inbound/outbound CHAP '
                 'authentication\n'
                 'Optional:\n'
                 '--in-user <inbound chap user name>\n'
                 '--in-password <inbound chap password>\n'
                 '--out-user <outbound chap user name>\n'
                 '--out-password <inbound chap user password\n')

        commands.add_argument(
            '--access-grant', action="store", type=str,
            metavar='<initiator id>',
            dest=_c("access-grant"),
            help='grants access to an initiator to a volume\n'
                 'requires:\n'
                 '--type <initiator id type>\n'
                 '--volume <volume id>\n'
                 '--access [RO|RW], read-only or read-write')

        commands.add_argument('--access-grant-group', action="store",
                            type=str,
                            metavar='<access group id>',
                            dest=_c("access-grant-group"),
                            help='grants access to an access group to a '
                                 'volume\n'
                                 'requires:\n'
                                 '--volume <volume id>\n'
                                 '--access [RO|RW], read-only or read-write')

        commands.add_argument(
            '--access-revoke', action="store",
            type=str,
            metavar='<initiator id>',
            dest=_c("access-revoke"),
            help='removes access for an initiator to a volume\n'
                 'requires:\n'
                 '--volume <volume id>')

        commands.add_argument(
            '--access-revoke-group', action="store",
            type=str,
            metavar='<access group id>',
            dest=_c("access-revoke-group"),
            help='removes access for access group to a volume\n'
                 'requires:\n'
                 '--volume <volume id>')

        commands.add_argument('--resize-volume', action="store",
                            type=str,
                            metavar='<volume id>',
                            dest=_c("resize-volume"),
                            help='re-sizes a volume, requires:\n'
                                 '--size <new size>')

        commands.add_argument('--resize-fs', action="store", type=str,
                            metavar='<fs id>',
                            dest=_c("resize-fs"),
                            help='re-sizes a file system, requires:\n'
                                 '--size <new size>')

        commands.add_argument('--nfs-export-remove', action="store",
                            type=str,
                            metavar='<nfs export id>',
                            dest=_c("nfs-export-remove"),
                            help='removes a nfs export')

        commands.add_argument('--nfs-export-fs', action="store",
                            type=str,
                            metavar='<file system id>',
                            dest=_c("nfs-export-fs"),
                            help='creates a nfs export\n'
                                 'Optional:\n'
                                 '--exportpath e.g. /foo/bar\n'
                                 'Note: root, ro, rw are to be repeated for '
                                 'each host\n'
                                 '--root <no_root_squash host>\n'
                                 '--ro <read only host>\n'
                                 '--rw <read/write host>\n'
                                 '--anonuid <uid to map to anonymous>\n'
                                 '--anongid <gid to map to anonymous>\n'
                                 '--auth-type <NFS client authentication '
                                 'type>\n')

        commands.add_argument('--job-status', action="store", type=str,
                            metavar='<job status id>',
                            dest=_c("job-status"),
                            help='retrieve information about job')

        commands.add_argument(
            '--volume-dependants', action="store",
            type=str,
            metavar='<volume id>',
            dest=_c("volume-dependants"),
            help='Returns True if volume has a dependant child')

        commands.add_argument('--volume-dependants-rm', action="store",
                            type=str,
                            metavar='<volume id>',
                            dest=_c("volume-dependants-rm"),
                            help='Removes dependencies')

        commands.add_argument('--fs-dependants', action="store",
                            type=str,
                            metavar='<fs id>',
                            dest=_c("fs-dependants"),
                            help='Returns true if a child dependency exists.\n'
                                 'Optional:\n'
                                 '--file <file> for File check')

        commands.add_argument('--fs-dependants-rm', action="store",
                            type=str,
                            metavar='<fs id>',
                            dest=_c("fs-dependants-rm"),
                            help='Removes dependencies\n'
                                 'Optional:\n'
                                 '--file <file> for File check')

        commands.add_argument('--create-pool', action="store",
                            type="string",
                            dest=_c("create-pool"),
                            metavar='<pool id>',
                            help="Creates a Pool requires:\n"
                                 "--system <system id>\n"
                                 "Optional:\n"
                                 "--member-id '<first member id>' "
                                 "--member-id '<second member id>' ...\n"
                                 "--member-type [DISK|VOLUME|POOL]\n"
                                 "--raid-type [JBOD|RAID1|RAID3|RAID4|...]\n"
                                 "--size <pool size>\n"
                                 "--thinp-type [THIN|THICK]"
                                 "--member_count [0-9]+")

        commands.add_argument('--delete-pool', action="store",
                            type="string",
                            dest=_c("delete-pool"),
                            metavar='<pool id>',
                            help="Delete a Pool\n")

        parser.add_option_group(commands)

        #Options to the actions
        #We could hide these with help = optparse.SUPPRESS_HELP
        #Should we?
        command_args = parser.add_argument_group('Command options')
        command_args.add_argument('--size', action="store", type=str,
                                metavar='size',
                                dest=_o("size"),
                                help='size (Can use B, K, M, G, T, P postfix '
                                     '(IEC sizing)')
        command_args.add_argument('--pool', action="store", type=str,
                                metavar='pool id',
                                dest=_o("pool"), help='pool ID')
        command_args.add_argument(
            '--provisioning', action="store",
            default='DEFAULT',
            choices=['DEFAULT', 'THIN', 'FULL'],
            dest="provisioning", help='[DEFAULT|THIN|FULL]')

        command_args.add_argument('--type', action="store",
                                choices=['WWPN', 'WWNN', 'ISCSI', 'HOSTNAME',
                                         'SAS', 'SNAPSHOT', 'CLONE', 'COPY',
                                         'MIRROR_SYNC', 'MIRROR_ASYNC'],
                                metavar="type",
                                dest=_o("type"), help='type specifier')

        command_args.add_argument('--name', action="store", type=str,
                                metavar="name",
                                dest=_o("name"),
                                help='human readable name')

        command_args.add_argument('--volume', action="store", type=str,
                                metavar="volume",
                                dest=_o("volume"), help='volume ID')

        command_args.add_argument('--access', action="store",
                                metavar="access",
                                dest=_o("access"), choices=['RO', 'RW'],
                                help='[RO|RW], read-only or read-write access')

        command_args.add_argument('--id', action="store", type=str,
                                metavar="initiator id",
                                dest=_o("id"), help="initiator id")

        command_args.add_argument('--system', action="store", type=str,
                                metavar="system id",
                                dest=_o("system"), help="system id")

        command_args.add_argument('--backing-snapshot', action="store",
                                type=str,
                                metavar="<backing snapshot>", default=None,
                                dest="backing_snapshot",
                                help="backing snap shot name for operation")

        command_args.add_argument('--src', action="store", type=str,
                                metavar="<source file>", default=None,
                                dest=_o("src"), help="source of operation")

        command_args.add_argument('--dest', action="store", type=str,
                                metavar="<source file>", default=None,
                                dest=_o("dest"),
                                help="destination of operation")

        command_args.add_argument('--file', action="append", type=str,
                                metavar="<file>", default=[],
                                dest="file",
                                help="file to include in operation, option "
                                     "can be repeated")

        command_args.add_argument('--fileas', action="append", type=str,
                                metavar="<fileas>", default=[],
                                dest="fileas",
                                help="file to be renamed as, option can "
                                     "be repeated")

        command_args.add_argument('--fs', action="store", type=str,
                                metavar="<file system>", default=None,
                                dest=_o("fs"), help="file system of interest")

        command_args.add_argument('--exportpath', action="store",
                                type=str,
                                metavar="<path for export>", default=None,
                                dest=_o("exportpath"),
                                help="desired export path on array")

        command_args.add_argument('--root', action="append", type=str,
                                metavar="<no_root_squash_host>", default=[],
                                dest="nfs_root",
                                help="list of hosts with no_root_squash")

        command_args.add_argument('--ro', action="append", type=str,
                                metavar="<read only host>", default=[],
                                dest="nfs_ro",
                                help="list of hosts with read/only access")

        command_args.add_argument('--rw', action="append", type=str,
                                metavar="<read/write host>", default=[],
                                dest="nfs_rw",
                                help="list of hosts with read/write access")

        command_args.add_argument('--anonuid', action="store", type=str,
                                metavar="<anonymous uid>", default=None,
                                dest="anonuid", help="uid to map to anonymous")

        command_args.add_argument('--anongid', action="store", type=str,
                                metavar="<anonymous uid>", default=None,
                                dest="anongid", help="gid to map to anonymous")

        command_args.add_argument(
            '--authtype', action="store", type=str,
            metavar="<type>", default=None,
            dest="authtype",
            help="NFS client authentication type")

        command_args.add_argument('--all', action="store_true", dest="all",
                                default=False,
                                help='specify all in an operation')

        command_args.add_argument('--src_start', action="append", type=int,
                                metavar="<source block start>", default=None,
                                dest=_o("src_start"),
                                help="source block address to replicate")

        command_args.add_argument(
            '--dest_start', action="append", type=int,
            metavar="<dest. block start>", default=None,
            dest=_o("dest_start"),
            help="destination block address to replicate")

        command_args.add_argument('--count', action="append", type=int,
                                metavar="<block count>", default=None,
                                dest=_o("count"),
                                help="number of blocks to replicate")

        command_args.add_argument('--in-user', action="store", type=str,
                                metavar="<username>", default=None,
                                dest=_o("username"),
                                help="CHAP inbound user name")

        command_args.add_argument(
            '--in-password', action="store", type=str,
            metavar="<password>", default=None,
            dest=_o("password"),
            help="CHAP inbound password")

        command_args.add_argument(
            '--out-user', action="store", type=str,
            metavar="<out_user>", default=None,
            dest=_o("out_user"),
            help="CHAP outbound user name")

        command_args.add_argument('--out-password', action="store",
                                type=str,
                                metavar="<out_password>", default=None,
                                dest=_o("out_password"),
                                help="CHAP outbound password")

        command_args.add_argument('-o', '--optional', action="store_true",
                                default=None,
                                dest=_o("flag_opt_data"),
                                help="Retrieving optional data also if " +
                                     "available.")

        command_args.add_argument('', '--member-id',
                                action="append",
                                type="string",
                                metavar="<member_id>",
                                dest=_o("member_ids"),
                                help="Pool member ID, could be ID of "
                                     "Disk/Pool/Volume. This option is "
                                     "repeatable")

        command_args.add_argument('', '--member-type', action="store",
                                type="string",
                                metavar="<member_type>",
                                dest=_o("member_type_str"),
                                help="Pool member type, [DISK|POOL|VOLUME]")

        command_args.add_argument('', '--member-count', action="store",
                                type="int",
                                metavar="<member_count>",
                                dest=_o("member_count"),
                                help="Pool member count, " +
                                     "integer bigger than 0")

        command_args.add_argument('', '--raid-type', action="store",
                                type="string",
                                metavar="<raid_type>", default=None,
                                dest=_o("raid_type_str"),
                                help="Pool RAID type, [RAID0|RAID1|RAID5...]")

        command_args.add_argument('', '--thinp-type', action="store",
                                type="string",
                                metavar="<thinp_type>", default=None,
                                dest=_o("thinp_type_str"),
                                help="Thin Provisioning Type, [THICK|THIN]")

        parser.add_option_group(command_args)

        return parser.parse_args()

    ## Checks to make sure only one command was specified on the command line
    # @param    self    The this pointer
    # @return   tuple of command to execute and the value of the command
    #           argument
    def _cmd(self):
        cmds = [e[4:] for e in dir(self.args)
                if e[0:4] == "cmd_" and self.args.__dict__[e] is not None]
        if len(cmds) > 1:
            raise ArgError(
                "More than one command operation specified (" + ",".join(
                    cmds) + ")")

        if len(cmds) == 1:
            return cmds[0], self.args.__dict__['cmd_' + cmds[0]]
        else:
            return None, None

    ## Validates that the required options for a given command are present.
    # @param    self    The this pointer
    # @return   None
    def _validate(self):
        optional_opts = 0
        expected_opts = self.verify[self.cmd]['options']
        actual_ops = [e[4:] for e in dir(self.args)
                      if e[0:4] == "opt_" and
                      self.args.__dict__[e] is not None]

        if 'optional' in self.verify[self.cmd]:
            optional_opts = len(self.verify[self.cmd]['optional'])

        if len(expected_opts):
            if (optional_opts + len(expected_opts)) >= len(actual_ops):
                for e in expected_opts:
                    if e not in actual_ops:
                        out("expected=" + ":".join(expected_opts))
                        out("actual=" + ":".join(actual_ops))
                        raise ArgError("missing option " + e)

            else:
                raise ArgError("expected options = (" +
                               ",".join(expected_opts) + ") actual = (" +
                               ",".join(actual_ops) + ")")

        #Check size
        if self.args.opt_size:
            self._size(self.args.opt_size)

    def _list(self, l):
        if l and len(l):
            if self.args.sep:
                return self.args.sep.join(l)
            else:
                return ", ".join(l)
        else:
            return "None"

    ## Display the types of nfs client authentication that are supported.
    # @param    self    The this pointer
    # @return None
    def display_nfs_client_authentication(self):
        """
        Dump the supported nfs client authentication types
        """
        if self.args.sep:
            out(self.args.sep.join(self.c.export_auth()))
        else:
            out(", ".join(self.c.export_auth()))

    ## Method that calls the appropriate method based on what the cmd_value is
    # @param    self    The this pointer
    def list(self):
        if self.cmd_value == 'VOLUMES':
            self.display_data(self.c.volumes())
        elif self.cmd_value == 'POOLS':
            if self.args.opt_flag_opt_data is True:
                self.display_data(
                    self.c.pools(data.Pool.RETRIEVE_FULL_INFO))
            else:
                self.display_data(self.c.pools())
        elif self.cmd_value == 'FS':
            self.display_data(self.c.fs())
        elif self.cmd_value == 'SNAPSHOTS':
            if self.args.opt_fs is None:
                raise ArgError("--fs <file system id> required")

            fs = _get_item(self.c.fs(), self.args.opt_fs)
            if fs:
                self.display_data(self.c.fs_snapshots(fs))
            else:
                raise ArgError(
                    "filesystem %s not found!" % self.args.opt_fs)
        elif self.cmd_value == 'INITIATORS':
            self.display_data(self.c.initiators())
        elif self.cmd_value == 'EXPORTS':
            self.display_data(self.c.exports())
        elif self.cmd_value == 'NFS_CLIENT_AUTH':
            self.display_nfs_client_authentication()
        elif self.cmd_value == 'ACCESS_GROUPS':
            self.display_data(self.c.access_group_list())
        elif self.cmd_value == 'SYSTEMS':
            self.display_data(self.c.systems())
        elif self.cmd_value == 'DISKS':
            if self.options.opt_flag_opt_data is True:
                self.display_data(
                    self.c.disks(data.Disk.RETRIEVE_FULL_INFO))
            else:
                self.display_data(self.c.disks())
        elif self.cmd_value == 'PLUGINS':
            self.display_available_plugins()
        else:
            raise ArgError(" unsupported listing type=%s", self.cmd_value)

    ## Converts type initiator type to enumeration type.
    # @param    type    String representation of type
    # @returns  Enumerated value
    @staticmethod
    def _init_type_to_enum(init_type):
        if init_type == 'WWPN':
            i = data.Initiator.TYPE_PORT_WWN
        elif init_type == 'WWNN':
            i = data.Initiator.TYPE_NODE_WWN
        elif init_type == 'ISCSI':
            i = data.Initiator.TYPE_ISCSI
        elif init_type == 'HOSTNAME':
            i = data.Initiator.TYPE_HOSTNAME
        elif init_type == 'SAS':
            i = data.Initiator.TYPE_SAS
        else:
            raise ArgError("invalid initiator type " + init_type)
        return i

    ## Creates an access group.
    # @param    self    The this pointer
    def create_access_group(self):
        name = self.cmd_value
        initiator = self.args.opt_id
        i = CmdLine._init_type_to_enum(self.args.opt_type)
        access_group = self.c.access_group_create(name, initiator, i,
                                                  self.args.opt_system)
        self.display_data([access_group])

    def _add_rm_access_grp_init(self, op):
        agl = self.c.access_group_list()
        group = _get_item(agl, self.cmd_value)

        if group:
            if op:
                i = CmdLine._init_type_to_enum(self.args.opt_type)
                self.c.access_group_add_initiator(
                    group, self.args.opt_id, i)
            else:
                i = _get_item(self.c.initiators(), self.args.opt_id)
                if i:
                    self.c.access_group_del_initiator(group, i.id)
                else:
                    raise ArgError(
                        "initiator with id %s not found!" %
                        self.args.opt_id)
        else:
            if not group:
                raise ArgError(
                    'access group with id %s not found!' % self.cmd_value)

    ## Adds an initiator from an access group
    def access_group_add(self):
        self._add_rm_access_grp_init(True)

    ## Removes an initiator from an access group
    def access_group_remove(self):
        self._add_rm_access_grp_init(False)

    def access_group_volumes(self):
        agl = self.c.access_group_list()
        group = _get_item(agl, self.cmd_value)

        if group:
            vols = self.c.volumes_accessible_by_access_group(group)
            self.display_data(vols)
        else:
            raise ArgError(
                'access group with id %s not found!' % self.cmd_value)

    def volume_accessible_init(self):
        i = _get_item(self.c.initiators(), self.cmd_value)

        if i:
            volumes = self.c.volumes_accessible_by_initiator(i)
            self.display_data(volumes)
        else:
            raise ArgError("initiator with id= %s not found!" % self.cmd_value)

    def init_granted_volume(self):
        vol = _get_item(self.c.volumes(), self.cmd_value)

        if vol:
            initiators = self.c.initiators_granted_to_volume(vol)
            self.display_data(initiators)
        else:
            raise ArgError("volume with id= %s not found!" % self.cmd_value)

    def iscsi_chap(self):
        init = _get_item(self.c.initiators(), self.cmd_value)
        if init:
            self.c.iscsi_chap_auth(init, self.args.opt_username,
                                   self.args.opt_password,
                                   self.args.opt_out_user,
                                   self.args.opt_out_password)
        else:
            raise ArgError("initiator with id= %s not found" % self.cmd_value)

    def volume_access_group(self):
        vol = _get_item(self.c.volumes(), self.cmd_value)

        if vol:
            groups = self.c.access_groups_granted_to_volume(vol)
            self.display_data(groups)
        else:
            raise ArgError("volume with id= %s not found!" % self.cmd_value)

    ## Used to delete access group
    # @param    self    The this pointer
    def delete_access_group(self):
        agl = self.c.access_group_list()

        group = _get_item(agl, self.cmd_value)
        if group:
            return self.c.access_group_del(group)
        else:
            raise ArgError(
                "access group with id = %s not found!" % self.cmd_value)

    ## Used to delete a file system
    # @param    self    The this pointer
    def fs_delete(self):

        fs = _get_item(self.c.fs(), self.cmd_value)
        if fs:
            if self.confirm_prompt(True):
                self._wait_for_it("delete-fs", self.c.fs_delete(fs), None)
        else:
            raise ArgError("fs with id = %s not found!" % self.cmd_value)

    ## Used to create a file system
    # @param    self    The this pointer
    def fs_create(self):
        #Need a name, size and pool
        size = self._size(self.args.opt_size)
        p = _get_item(self.c.pools(), self.args.opt_pool)
        name = self.cmd_value
        if p:
            fs = self._wait_for_it("create-fs",
                                   *self.c.fs_create(p, name, size))
            self.display_data([fs])
        else:
            raise ArgError(
                "pool with id = %s not found!" % self.args.opt_pool)

    ## Used to resize a file system
    # @param    self    The this pointer
    def fs_resize(self):
        fs = _get_item(self.c.fs(), self.cmd_value)
        size = self._size(self.args.opt_size)

        if fs and size:
            if self.confirm_prompt(False):
                fs = self._wait_for_it("resize-fs",
                                       *self.c.fs_resize(fs, size))
                self.display_data([fs])
        else:
            if not fs:
                raise ArgError(
                    " filesystem with id= %s not found!" % self.cmd_value)

    ## Used to clone a file system
    # @param    self    The this pointer
    def fs_clone(self):
        src_fs = _get_item(self.c.fs(), self.cmd_value)
        name = self.args.opt_name

        if not src_fs:
            raise ArgError(
                " source file system with id=%s not found!" % self.cmd_value)

        if self.args.backing_snapshot:
            #go get the snapsnot
            ss = _get_item(self.c.fs_snapshots(src_fs),
                           self.args.backing_snapshot)
            if not ss:
                raise ArgError(
                    " snapshot with id= %s not found!" %
                    self.args.backing_snapshot)
        else:
            ss = None

        fs = self._wait_for_it("fs_clone", *self.c.fs_clone(src_fs, name, ss))
        self.display_data([fs])

    ## Used to clone a file(s)
    # @param    self    The this pointer
    def file_clone(self):
        fs = _get_item(self.c.fs(), self.cmd_value)
        src = self.args.opt_src
        dest = self.args.opt_dest

        if self.args.backing_snapshot:
            #go get the snapsnot
            ss = _get_item(self.c.fs_snapshots(fs),
                           self.args.backing_snapshot)
        else:
            ss = None

        self._wait_for_it("file_clone", self.c.file_clone(fs, src, dest, ss),
                          None)

    ##Converts a size parameter into the appropriate number of bytes
    # @param    s   Size to convert to bytes handles B, K, M, G, T, P postfix
    # @return Size in bytes
    @staticmethod
    def _size(s):
        size_bytes = common.size_human_2_size_bytes(s)
        if size_bytes <= 0:
            raise ArgError("Incorrect size argument format: '%s'" % s)
        return size_bytes

    def _cp(self, cap, val):
        if self.args.sep is not None:
            s = self.args.sep
        else:
            s = ':'

        if val == data.Capabilities.SUPPORTED:
            v = "SUPPORTED"
        elif val == data.Capabilities.UNSUPPORTED:
            v = "UNSUPPORTED"
        elif val == data.Capabilities.SUPPORTED_OFFLINE:
            v = "SUPPORTED_OFFLINE"
        elif val == data.Capabilities.NOT_IMPLEMENTED:
            v = "NOT_IMPLEMENTED"
        else:
            v = "UNKNOWN"

        out("%s%s%s" % (cap, s, v))

    def capabilities(self):
        s = _get_item(self.c.systems(), self.cmd_value)

        if s:
            cap = self.c.capabilities(s)
            self._cp("BLOCK_SUPPORT", cap.get(Capabilities.BLOCK_SUPPORT))
            self._cp("FS_SUPPORT", cap.get(Capabilities.FS_SUPPORT))
            self._cp("INITIATORS", cap.get(Capabilities.INITIATORS))
            self._cp("INITIATORS_GRANTED_TO_VOLUME",
                     cap.get(Capabilities.INITIATORS_GRANTED_TO_VOLUME))
            self._cp("VOLUMES", cap.get(Capabilities.VOLUMES))
            self._cp("VOLUME_CREATE", cap.get(Capabilities.VOLUME_CREATE))
            self._cp("VOLUME_RESIZE", cap.get(Capabilities.VOLUME_RESIZE))
            self._cp("VOLUME_REPLICATE",
                     cap.get(Capabilities.VOLUME_REPLICATE))
            self._cp("VOLUME_REPLICATE_CLONE",
                     cap.get(Capabilities.VOLUME_REPLICATE_CLONE))
            self._cp("VOLUME_REPLICATE_COPY",
                     cap.get(Capabilities.VOLUME_REPLICATE_COPY))
            self._cp("VOLUME_REPLICATE_MIRROR_ASYNC",
                     cap.get(Capabilities.VOLUME_REPLICATE_MIRROR_ASYNC))
            self._cp("VOLUME_REPLICATE_MIRROR_SYNC",
                     cap.get(Capabilities.VOLUME_REPLICATE_MIRROR_SYNC))
            self._cp("VOLUME_COPY_RANGE_BLOCK_SIZE",
                     cap.get(Capabilities.VOLUME_COPY_RANGE_BLOCK_SIZE))
            self._cp("VOLUME_COPY_RANGE",
                     cap.get(Capabilities.VOLUME_COPY_RANGE))
            self._cp("VOLUME_COPY_RANGE_CLONE",
                     cap.get(Capabilities.VOLUME_COPY_RANGE_CLONE))
            self._cp("VOLUME_COPY_RANGE_COPY",
                     cap.get(Capabilities.VOLUME_COPY_RANGE_COPY))
            self._cp("VOLUME_DELETE", cap.get(Capabilities.VOLUME_DELETE))
            self._cp("VOLUME_ONLINE", cap.get(Capabilities.VOLUME_ONLINE))
            self._cp("VOLUME_OFFLINE", cap.get(Capabilities.VOLUME_OFFLINE))
            self._cp("VOLUME_INITIATOR_GRANT",
                     cap.get(Capabilities.VOLUME_INITIATOR_GRANT))
            self._cp("VOLUME_INITIATOR_REVOKE",
                     cap.get(Capabilities.VOLUME_INITIATOR_REVOKE))
            self._cp("VOLUME_THIN",
                     cap.get(Capabilities.VOLUME_THIN))
            self._cp("VOLUME_ISCSI_CHAP_AUTHENTICATION",
                     cap.get(Capabilities.VOLUME_ISCSI_CHAP_AUTHENTICATION))
            self._cp("ACCESS_GROUP_GRANT",
                     cap.get(Capabilities.ACCESS_GROUP_GRANT))
            self._cp("ACCESS_GROUP_REVOKE",
                     cap.get(Capabilities.ACCESS_GROUP_REVOKE))
            self._cp("ACCESS_GROUP_LIST",
                     cap.get(Capabilities.ACCESS_GROUP_LIST))
            self._cp("ACCESS_GROUP_CREATE",
                     cap.get(Capabilities.ACCESS_GROUP_CREATE))
            self._cp("ACCESS_GROUP_DELETE",
                     cap.get(Capabilities.ACCESS_GROUP_DELETE))
            self._cp("ACCESS_GROUP_ADD_INITIATOR",
                     cap.get(Capabilities.ACCESS_GROUP_ADD_INITIATOR))
            self._cp("ACCESS_GROUP_DEL_INITIATOR",
                     cap.get(Capabilities.ACCESS_GROUP_DEL_INITIATOR))
            self._cp("VOLUMES_ACCESSIBLE_BY_ACCESS_GROUP",
                     cap.get(Capabilities.VOLUMES_ACCESSIBLE_BY_ACCESS_GROUP))
            self._cp("VOLUME_ACCESSIBLE_BY_INITIATOR",
                     cap.get(Capabilities.VOLUME_ACCESSIBLE_BY_INITIATOR))
            self._cp("ACCESS_GROUPS_GRANTED_TO_VOLUME",
                     cap.get(Capabilities.ACCESS_GROUPS_GRANTED_TO_VOLUME))
            self._cp("VOLUME_CHILD_DEPENDENCY",
                     cap.get(Capabilities.VOLUME_CHILD_DEPENDENCY))
            self._cp("VOLUME_CHILD_DEPENDENCY_RM",
                     cap.get(Capabilities.VOLUME_CHILD_DEPENDENCY_RM))
            self._cp("FS", cap.get(Capabilities.FS))
            self._cp("FS_DELETE", cap.get(Capabilities.FS_DELETE))
            self._cp("FS_RESIZE", cap.get(Capabilities.FS_RESIZE))
            self._cp("FS_CREATE", cap.get(Capabilities.FS_CREATE))
            self._cp("FS_CLONE", cap.get(Capabilities.FS_CLONE))
            self._cp("FILE_CLONE", cap.get(Capabilities.FILE_CLONE))
            self._cp("FS_SNAPSHOTS", cap.get(Capabilities.FS_SNAPSHOTS))
            self._cp("FS_SNAPSHOT_CREATE",
                     cap.get(Capabilities.FS_SNAPSHOT_CREATE))
            self._cp("FS_SNAPSHOT_CREATE_SPECIFIC_FILES",
                     cap.get(Capabilities.FS_SNAPSHOT_CREATE_SPECIFIC_FILES))
            self._cp("FS_SNAPSHOT_DELETE",
                     cap.get(Capabilities.FS_SNAPSHOT_DELETE))
            self._cp("FS_SNAPSHOT_REVERT",
                     cap.get(Capabilities.FS_SNAPSHOT_REVERT))
            self._cp("FS_SNAPSHOT_REVERT_SPECIFIC_FILES",
                     cap.get(Capabilities.FS_SNAPSHOT_REVERT_SPECIFIC_FILES))
            self._cp("FS_CHILD_DEPENDENCY",
                     cap.get(Capabilities.FS_CHILD_DEPENDENCY))
            self._cp("FS_CHILD_DEPENDENCY_RM",
                     cap.get(Capabilities.FS_CHILD_DEPENDENCY_RM))
            self._cp("FS_CHILD_DEPENDENCY_RM_SPECIFIC_FILES", cap.get(
                Capabilities.FS_CHILD_DEPENDENCY_RM_SPECIFIC_FILES))
            self._cp("EXPORT_AUTH", cap.get(Capabilities.EXPORT_AUTH))
            self._cp("EXPORTS", cap.get(Capabilities.EXPORTS))
            self._cp("EXPORT_FS", cap.get(Capabilities.EXPORT_FS))
            self._cp("EXPORT_REMOVE", cap.get(Capabilities.EXPORT_REMOVE))
            self._cp("EXPORT_CUSTOM_PATH",
                     cap.get(Capabilities.EXPORT_CUSTOM_PATH))
        else:
            raise ArgError("system with id= %s not found!" % self.cmd_value)

    def plugin_info(self):
        desc, version = self.c.plugin_info()

        if self.args.sep:
            out("%s%s%s" % (desc, self.args.sep, version))
        else:
            out("Description: %s Version: %s" % (desc, version))

    ## Creates a volume
    # @param    self    The this pointer
    def create_volume(self):
        #Get pool
        p = _get_item(self.c.pools(), self.args.opt_pool)
        if p:
            vol = self._wait_for_it("create-volume",
                                    *self.c.volume_create(
                                        p,
                                        self.cmd_value,
                                        self._size(self.args.opt_size),
                                        data.Volume.prov_string_to_type(
                                            self.args.provisioning)))

            self.display_data([vol])
        else:
            raise ArgError(
                " pool with id= %s not found!" % self.args.opt_pool)

    ## Creates a snapshot
    # @param    self    The this pointer
    def create_ss(self):
        #Get fs
        fs = _get_item(self.c.fs(), self.args.opt_fs)
        if fs:
            ss = self._wait_for_it("snapshot-create",
                                   *self.c.fs_snapshot_create(
                                       fs,
                                       self.cmd_value,
                                       self.args.file))

            self.display_data([ss])
        else:
            raise ArgError("fs with id= %s not found!" % self.args.opt_fs)

    ## Restores a snap shot
    # @param    self    The this pointer
    def restore_ss(self):
        #Get snapshot
        fs = _get_item(self.c.fs(), self.args.opt_fs)
        ss = _get_item(self.c.fs_snapshots(fs), self.cmd_value)

        if ss and fs:

            if self.args.file:
                if self.args.fileas:
                    if len(self.args.file) != len(self.args.fileas):
                        raise ArgError(
                            "number of --files not equal to --fileas")

            if self.args.all:
                if self.args.file or self.args.fileas:
                    raise ArgError(
                        "Unable to specify --all and --files or --fileas")

            if self.args.all is False and self.args.file is None:
                raise ArgError("Need to specify --all or at least one --file")

            if self.confirm_prompt(True):
                self._wait_for_it('restore-ss',
                                  self.c.fs_snapshot_revert(
                                      fs, ss,
                                      self.args.file,
                                      self.args.fileas,
                                      self.args.all),
                                  None)
        else:
            if not ss:
                raise ArgError("ss with id= %s not found!" % self.cmd_value)
            if not fs:
                raise ArgError(
                    "fs with id= %s not found!" % self.args.opt_fs)

    ## Deletes a volume
    # @param    self    The this pointer
    def delete_volume(self):
        v = _get_item(self.c.volumes(), self.cmd_value)

        if v:
            if self.confirm_prompt(True):
                self._wait_for_it("delete-volume", self.c.volume_delete(v),
                                  None)
        else:
            raise ArgError(" volume with id= %s not found!" % self.cmd_value)

    ## Deletes a snap shot
    # @param    self    The this pointer
    def delete_ss(self):
        fs = _get_item(self.c.fs(), self.args.opt_fs)
        if fs:
            ss = _get_item(self.c.fs_snapshots(fs), self.cmd_value)
            if ss:
                if self.confirm_prompt(True):
                    self._wait_for_it("delete-snapshot",
                                      self.c.fs_snapshot_delete(fs, ss), None)
            else:
                raise ArgError(
                    " snapshot with id= %s not found!" % self.cmd_value)
        else:
            raise ArgError(
                " file system with id= %s not found!" % self.args.opt_fs)

    ## Waits for an operation to complete by polling for the status of the
    # operations.
    # @param    self    The this pointer
    # @param    msg     Message to display if this job fails
    # @param    job     The job id to wait on
    # @param    item    The item that could be available now if there is no job
    def _wait_for_it(self, msg, job, item):
        if not job:
            return item
        else:
            #If a user doesn't want to wait, return the job id to stdout
            #and exit with job in progress
            if self.args.async:
                out(job)
                self.shutdown(common.ErrorNumber.JOB_STARTED)

            while True:
                (s, percent, i) = self.c.job_status(job)

                if s == common.JobStatus.INPROGRESS:
                    #Add an option to spit out progress?
                    #print "%s - Percent %s complete" % (job, percent)
                    time.sleep(0.25)
                elif s == common.JobStatus.COMPLETE:
                    self.c.job_free(job)
                    return i
                else:
                    #Something better to do here?
                    raise ArgError(msg + " job error code= " + str(s))

    ## Retrieves the status of the specified job
    # @param    self    The this pointer
    def job_status(self):
        (s, percent, i) = self.c.job_status(self.cmd_value)

        if s == common.JobStatus.COMPLETE:
            if i:
                self.display_data([i])

            self.c.job_free(self.cmd_value)
        else:
            out(str(percent))
            self.shutdown(common.ErrorNumber.JOB_STARTED)

    ## Replicates a volume
    # @param    self    The this pointer
    def replicate_volume(self):
        p = None

        if self.args.opt_pool:
            p = _get_item(self.c.pools(), self.args.opt_pool)

        v = _get_item(self.c.volumes(), self.cmd_value)

        if v:

            rep_type = data.Volume.rep_String_to_type(self.args.opt_type)
            if rep_type == data.Volume.REPLICATE_UNKNOWN:
                raise ArgError("invalid replication type= %s" % rep_type)

            vol = self._wait_for_it("replicate volume",
                                    *self.c.volume_replicate(
                                        p, rep_type, v, self.args.opt_name))
            self.display_data([vol])
        else:
            if not p:
                raise ArgError(
                    "pool with id= %s not found!" % self.args.opt_pool)
            if not v:
                raise ArgError("Volume with id= %s not found!" %
                               self.cmd_value)

    ## Replicates a range of a volume
    # @param    self    The this pointer
    def replicate_vol_range(self):
        src = _get_item(self.c.volumes(), self.cmd_value)
        dest = _get_item(self.c.volumes(), self.args.opt_dest)

        if src and dest:
            rep_type = data.Volume.rep_String_to_type(self.args.opt_type)
            if rep_type == data.Volume.REPLICATE_UNKNOWN:
                raise ArgError("invalid replication type= %s" % rep_type)

            src_starts = self.args.opt_src_start
            dest_starts = self.args.opt_dest_start
            counts = self.args.opt_count

            if (0 < len(src_starts) == len(dest_starts)
                    and len(dest_starts) == len(counts)):
                ranges = []

                for i in range(len(src_starts)):
                    ranges.append(data.BlockRange(src_starts[i],
                                                  dest_starts[i],
                                                  counts[i]))

                if self.confirm_prompt(False):
                    self.c.volume_replicate_range(rep_type, src, dest, ranges)
        else:
            if not src:
                raise ArgError(
                    "src volume with id= %s not found!" % self.cmd_value)
            if not dest:
                raise ArgError(
                    "dest volume with id= %s not found!" %
                    self.args.opt_dest)

    ##
    # Returns the block size in bytes for each block represented in
    # volume_replicate_range
    # @param    self    The this pointer
    def replicate_vol_range_bs(self):
        s = _get_item(self.c.systems(), self.cmd_value)
        if s:
            out(self.c.volume_replicate_range_block_size(s))
        else:
            raise ArgError("system with id= %s not found" % self.cmd_value)

    ## Used to grant or revoke access to a volume to an initiator.
    # @param    self    The this pointer
    # @param    grant   bool, if True we grant, else we un-grant.
    def _access(self, grant):
        v = _get_item(self.c.volumes(), self.args.opt_volume)
        if not v:
            raise ArgError(
                "volume with id= %s not found" % self.args.opt_volume)

        initiator_id = self.cmd_value

        if grant:
            i_type = CmdLine._init_type_to_enum(self.args.opt_type)
            access = data.Volume.access_string_to_type(self.args.opt_access)

            self.c.initiator_grant(initiator_id, i_type, v, access)
        else:
            initiator = _get_item(self.c.initiators(), initiator_id)
            if not initiator:
                raise ArgError("initiator with id= %s not found" %
                               initiator_id)

            self.c.initiator_revoke(initiator, v)

    ## Grant access to volume to an initiator
    # @param    self    The this pointer
    def access_grant(self):
        return self._access(True)

    ## Revoke access to volume to an initiator
    # @param    self    The this pointer
    def access_revoke(self):
        return self._access(False)

    def _access_group(self, grant=True):
        agl = self.c.access_group_list()
        group = _get_item(agl, self.cmd_value)
        v = _get_item(self.c.volumes(), self.args.opt_volume)

        if group and v:
            if grant:
                access = data.Volume.access_string_to_type(
                    self.args.opt_access)
                self.c.access_group_grant(group, v, access)
            else:
                self.c.access_group_revoke(group, v)
        else:
            if not group:
                raise ArgError(
                    "access group with id= %s not found!" % self.cmd_value)
            if not v:
                raise ArgError(
                    "volume with id= %s not found!" % self.args.opt_volume)

    def access_grant_group(self):
        return self._access_group(True)

    def access_revoke_group(self):
        return self._access_group(False)

    ## Re-sizes a volume
    # @param    self    The this pointer
    def resize_volume(self):
        v = _get_item(self.c.volumes(), self.cmd_value)
        if v:
            size = self._size(self.args.opt_size)

            if self.confirm_prompt(False):
                vol = self._wait_for_it("resize",
                                        *self.c.volume_resize(v, size))
                self.display_data([vol])
        else:
            raise ArgError("volume with id= %s not found!" % self.cmd_value)

    ## Removes a nfs export
    # @param    self    The this pointer
    def nfs_export_remove(self):
        export = _get_item(self.c.exports(), self.cmd_value)
        if export:
            self.c.export_remove(export)
        else:
            raise ArgError("nfs export with id= %s not found!" %
                           self.cmd_value)

    ## Exports a file system as a NFS export
    # @param    self    The this pointer
    def nfs_export_fs(self):
        fs = _get_item(self.c.fs(), self.cmd_value)

        if fs:
            #Check to see if we have some type of access specified
            if len(self.args.nfs_rw) == 0 \
                    and len(self.args.nfs_ro) == 0:
                raise ArgError(" please specify --ro or --rw access")

            export = self.c.export_fs(
                fs.id,
                self.args.opt_exportpath,
                self.args.nfs_root,
                self.args.nfs_rw, self.args.nfs_ro,
                self.args.anonuid,
                self.args.anongid,
                self.args.authtype, None)
            self.display_data([export])
        else:
            raise ArgError(
                " file system with id=%s not found!" % self.cmd_value)

    ## Displays volume dependants.
    # @param    self    The this pointer
    def vol_dependants(self):
        v = _get_item(self.c.volumes(), self.cmd_value)

        if v:
            rc = self.c.volume_child_dependency(v)
            out(rc)
        else:
            raise ArgError("volume with id= %s not found!" % self.cmd_value)

    ## Removes volume dependants.
    # @param    self    The this pointer
    def vol_dependants_rm(self):
        v = _get_item(self.c.volumes(), self.cmd_value)

        if v:
            self._wait_for_it("volume-dependant-rm",
                              self.c.volume_child_dependency_rm(v), None)
        else:
            raise ArgError("volume with id= %s not found!" % self.cmd_value)

    ## Displays file system dependants
    # @param    self    The this pointer
    def fs_dependants(self):
        fs = _get_item(self.c.fs(), self.cmd_value)

        if fs:
            rc = self.c.fs_child_dependency(fs, self.args.file)
            out(rc)
        else:
            raise ArgError(
                "File system with id= %s not found!" % self.cmd_value)

    ## Removes file system dependants
    # @param    self    The this pointer
    def fs_dependants_rm(self):
        fs = _get_item(self.c.fs(), self.cmd_value)

        if fs:
            self._wait_for_it("fs-dependants-rm",
                              self.c.fs_child_dependency_rm(fs,
                                                            self.args.file),
                              None)
        else:
            raise ArgError(
                "File system with id= %s not found!" % self.cmd_value)

    ## Deletes a pool
    # @param    self    The this pointer
    def delete_pool(self):
        pool = _get_item(self.c.pools(), self.cmd_value)
        if pool:
            if self.confirm_prompt(True):
                self._wait_for_it("delete-pool",
                                  self.c.pool_delete(pool),
                                  None)
                out("Pool %s deleted" % pool.id)
        else:
            raise ArgError("pool with id= %s not found!" % self.cmd_value)

    ## Creates a pool
    # @param    self    The this pointer
    def create_pool(self):
        if not self.args.opt_system:
            raise ArgError("System ID not defined")

        pool_name = self.cmd_value
        raid_type = data.Pool.RAID_TYPE_UNKNOWN
        member_ids = []
        member_type = data.Pool.MEMBER_TYPE_UNKNOWN
        member_count = 0
        thinp_type = data.Pool.THINP_TYPE_UNKNOWN
        size_bytes = 0

        if self.args.opt_raid_type_str:
            raid_type = data.Pool.raid_type_str_to_type(
                self.args.opt_raid_type_str)
            if raid_type == data.Pool.RAID_TYPE_UNKNOWN or \
               raid_type == data.Pool.RAID_TYPE_NOT_APPLICABLE:
                raise ArgError("Unknown RAID type specified: %s" %
                               self.args.opt_raid_type_str)

        if len(self.args.opt_member_ids) >= 1:
            member_ids = self.args.opt_member_ids

        if self.args.opt_size:
            size_bytes = self._size(self.args.opt_size)
            if size_bytes <= 0:
                raise ArgError("Incorrect size argument format: '%s'" %
                               self.args.opt_size)

        if self.args.opt_member_type_str:
            member_type = data.Pool.member_type_str_to_type(
                self.args.opt_member_type_str)

        if member_ids and member_type != data.Pool.MEMBER_TYPE_UNKNOWN:
            if (member_type == data.Pool.MEMBER_TYPE_DISK):
                disks = self.c.disks()
                for member_id in member_ids:
                    flag_found = False
                    for disk in disks:
                        if disk.id == member_id:
                            flag_found = True
                            break
                    if not flag_found:
                        raise ArgError("Invalid Disk ID specified in " +
                                       "--member-id %s " % member_id)
            elif (member_type == data.Pool.MEMBER_TYPE_VOLUME):
                volumes = self.c.volumes()
                for member_id in member_ids:
                    flag_found = False
                    for volume in volumes:
                        if volume.id == member_id:
                            flag_found = True
                            break
                    if not flag_found:
                        raise ArgError("Invalid Volume ID specified in " +
                                       "--member-ids %s " % member_id)
            elif (member_type == data.Pool.MEMBER_TYPE_POOL):
                if not self.args.opt_size:
                    raise ArgError("--size is mandatory when creating Pool " +
                                   "against another Pool")
                pools = self.c.pools()
                for member_id in member_ids:
                    flag_found = False
                    for pool in pools:
                        if pool.id == member_id:
                            flag_found = True
                            break
                    if not flag_found:
                        raise ArgError("Invalid Pool ID specified in " +
                                       "--member-ids %s " % member_id)
            else:
                raise ArgError("Unkown pool member-type %s, should be %s" %
                               (self.args.opt_member_type_str,
                                '[DISK/VOLUME/POOL]'))

        if self.args.opt_thinp_type_str:
            thinp_type_str = self.args.opt_thinp_type_str
            thinp_type = data.Pool.thinp_type_str_to_type(thinp_type_str)

        pool = self._wait_for_it("create-pool",
                                 *self.c.pool_create(self.args.opt_system,
                                                     pool_name,
                                                     raid_type,
                                                     member_type,
                                                     member_ids,
                                                     member_count,
                                                     size_bytes,
                                                     thinp_type,
                                                     0))
        self.display_data([pool])

    def _read_configfile(self):
        """
        Set uri from config file. Will be overridden by cmdline option or
        env var if present.
        """

        allowed_config_options = ("uri",)

        config_path = os.path.expanduser("~") + "/.lsmcli"
        if not os.path.exists(config_path):
            return

        with open(config_path) as f:
            for line in f:

                if line.lstrip().startswith("#"):
                    continue

                try:
                    name, val = [x.strip() for x in line.split("=", 1)]
                    if name in allowed_config_options:
                        setattr(self, name, val)
                except ValueError:
                    pass

    ## Class constructor.
    # @param    self    The this pointer
    def __init__(self):
        self.uri = None
        self.c = None
        self.args = CmdLine.cli()

        self.cleanup = None

        #Get and set the command and command value we will be executing
        (self.cmd, self.cmd_value) = self._cmd()

        if self.cmd is None:
            raise ArgError("no command specified, try --help")

        #Data driven validation
        self.verify = {'list': {'options': [], 'method': self.list},
                       'delete-fs': {'options': [],
                                     'method': self.fs_delete},
                       'delete-access-group':
                       {'options': [], 'method': self.delete_access_group},
                       'capabilities': {'options': [],
                                        'method': self.capabilities},

                       'plugin-info': {'options': [],
                                       'method': self.plugin_info},

                       'create-volume': {'options': ['size', 'pool'],
                                         'method': self.create_volume},
                       'create-fs': {'options': ['size', 'pool'],
                                     'method': self.fs_create},
                       'clone-fs': {'options': ['name'],
                                    'method': self.fs_clone},
                       'create-access-group': {
                       'options': ['id', 'type', 'system'],
                       'method': self.create_access_group},
                       'access-group-add': {'options': ['id', 'type'],
                                            'method': self.access_group_add},
                       'access-group-remove':
                       {'options': ['id'], 'method': self.access_group_remove},
                       'access-group-volumes':
                       {'options': [], 'method': self.access_group_volumes},
                       'volume-access-group':
                       {'options': [],
                       'method': self.volume_access_group},
                       'volumes-accessible-initiator':
                       {'options': [], 'method': self.volume_accessible_init},

                       'initiators-granted-volume':
                       {'options': [], 'method': self.init_granted_volume},

                       'iscsi-chap': {'options': [],
                                      'method': self.iscsi_chap},

                       'create-ss': {'options': ['fs'],
                                     'method': self.create_ss},
                       'clone-file': {'options': ['src', 'dest'],
                                      'method': self.file_clone},
                       'delete-volume': {'options': [],
                                         'method': self.delete_volume},
                       'delete-ss': {'options': ['fs'],
                                     'method': self.delete_ss},
                       'replicate-volume': {'options': ['type', 'name'],
                                            'optional': ['pool'],
                                            'method': self.replicate_volume},
                       'access-grant':
                                    {'options': ['volume', 'access', 'type'],
                                     'method': self.access_grant},
                       'access-grant-group':
                       {'options': ['volume', 'access'],
                       'method': self.access_grant_group},
                       'access-revoke': {'options': ['volume'],
                                         'method': self.access_revoke},
                       'access-revoke-group':
                       {'options': ['volume'],
                       'method': self.access_revoke_group},
                       'resize-volume': {'options': ['size'],
                                         'method': self.resize_volume},
                       'resize-fs': {'options': ['size'],
                                     'method': self.fs_resize},
                       'nfs-export-remove': {'options': [],
                                             'method': self.nfs_export_remove},
                       'nfs-export-fs': {'options': [],
                                         'method': self.nfs_export_fs},
                       'restore-ss': {'options': ['fs'],
                                      'method': self.restore_ss},
                       'job-status': {'options': [],
                                      'method': self.job_status},
                       'replicate-volume-range':
                       {'options':
                       ['type', 'dest', 'src_start', 'dest_start', 'count'],
                       'method': self.replicate_vol_range},
                       'replicate-volume-range-block-size':
                       {'options': [],
                       'method': self.replicate_vol_range_bs},
                       'volume-dependants': {'options': [],
                                             'method': self.vol_dependants},
                       'volume-dependants-rm':
                       {'options': [], 'method': self.vol_dependants_rm},
                       'fs-dependants': {'options': [],
                                         'method': self.fs_dependants},
                       'fs-dependants-rm': {'options': [],
                                            'method': self.fs_dependants_rm},
            'create-pool': {
                'options': ['system'],
                'optional': ['size', 'member_ids', 'raid_type_str',
                             'member_type_str', 'member_count', 'thinp_type'],
                'method': self.create_pool
            },
            'delete-pool': {
                'options': [],
                'method': self.delete_pool
            },
        }
        self._validate()

        self.tmo = int(self.args.wait)
        if not self.tmo or self.tmo < 0:
            raise ArgError("[-w|--wait] reguires a non-zero positive integer")

        self._read_configfile()
        if os.getenv('LSMCLI_URI') is not None:
            self.uri = os.getenv('LSMCLI_URI')
        self.password = os.getenv('LSMCLI_PASSWORD')
        if self.args.uri is not None:
            self.uri = self.args.uri

        # We need a valid plug-in to instantiate even if all we are trying
        # to do is list the plug-ins at the moment to keep that code
        # the same in all cases, even though it isn't technically
        # required for the client library (static method)
        # TODO: Make this not necessary.
        if (self.cmd == 'list' and self.args.cmd_list == "PLUGINS"):
            self.uri = "sim://"
            self.password = None

        if self.uri is None:
            raise ArgError("--uri missing or export LSMCLI_URI")

        #Lastly get the password if requested.
        if self.args.prompt is not None:
            self.password = getpass.getpass()

        if self.password is not None:
            #Check for username
            u = common.uri_parse(self.uri)
            if u['username'] is None:
                raise ArgError("password specified with no user name in uri")

    ## Does appropriate clean-up
    # @param    self    The this pointer
    # @param    ec      The exit code
    def shutdown(self, ec=None):
        if self.cleanup:
            self.cleanup()

        if ec:
            sys.exit(ec)

    ## Process the specified command
    # @param    self    The this pointer
    # @param    cli     The object instance to invoke methods on.
    def process(self, cli=None):
        """
        Process the parsed command.
        """
        if cli:
            #Directly invoking code though a wrapper to catch unsupported
            #operations.
            self.c = common.Proxy(cli())
            self.c.startup(self.uri, self.password, self.tmo)
            self.cleanup = self.c.shutdown
        else:
            #Going across the ipc pipe
            self.c = common.Proxy(
                client.Client(self.uri, self.password, self.tmo))

            if os.getenv('LSM_DEBUG_PLUGIN'):
                raw_input(
                    "Attach debugger to plug-in, press <return> when ready...")

            self.cleanup = self.c.close

        self.verify[self.cmd]['method']()
        self.shutdown()
