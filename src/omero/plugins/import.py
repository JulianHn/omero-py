#!/usr/bin/env python
# -*- coding: utf-8 -*-

#
# Copyright (C) 2009-2016 Glencoe Software, Inc. All Rights Reserved.
# Use is subject to license terms supplied in LICENSE.txt
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

"""
   Startup plugin for command-line importer.

"""
from __future__ import division
from __future__ import print_function

from builtins import str
from past.utils import old_div
from past.builtins import basestring
from builtins import object
from io import BytesIO
import os
import csv
import sys
import shlex
import requests
import re
from zipfile import ZipFile


from omero.cli import BaseControl, CLI
import omero.java
from omero.util import get_omero_user_cache_dir
from omero_ext.argparse import SUPPRESS
from omero_ext.path import path

START_CLASS = "ome.formats.importer.cli.CommandLineImporter"
TEST_CLASS = "ome.formats.test.util.TestEngine"

HELP = """Run the Java-based command-line importer

This is a Python wrapper around the Java importer. Login is handled by Python
OMERO.cli. To see more options, use "--javahelp".

Options marked with "**" are passed strictly to Java. If they interfere with
any of the Python arguments, you may need to end precede your arguments with a
"--".

Bulk imports:

Rather than passing one or more files to the import command, a single
dictionary-like file (e.g. yml or json) can be passed to the `--bulk`
argument. Most keys in the bulk file will be treated like additional
command-line arguments. Special keys include:

 * columns      A list of columns for parsing the value of path
 * continue     Like the "-c" changes error handling
 * dry_run      If true, print out additional arguments rather than run them.
                If another string other than false, use as a template for
                storing the import commands. (e.g. /tmp/%s.sh)
 * include      Relative path (from the bulk file) of a parent bulk file
 * path         A file which will be parsed line by line based on its file
                ending. Lines containing zero or more keys along with a
                single file to be imported. Options for formats include:
                  - .tsv and .csv files will be parsed by the existing library
                  - other files will be parsed with shlex
                  - unless no columns are specified, in which case each line
                    is treated as a file

"""
EXAMPLES = """
Examples:

  # Display help
  $ omero import -h
  # Import foo.tiff using current login
  $ omero import ~/Data/my_file.dv
  # Import foo.tiff using input credentials
  $ omero import -s localhost -u user -w password foo.tiff
  # Set Java debugging level to ALL
  $ omero import foo.tiff -- --debug=ALL
  # Display used files for importing foo.tiff
  $ omero import foo.tiff -f
  # Limit debugging output
  $ omero import -- --debug=ERROR foo.tiff

For additional information, see:
https://docs.openmicroscopy.org/latest/omero/users/cli/import.html
Report bugs at https://forum.image.sc/
"""
TESTHELP = """Run the Importer TestEngine suite (devs-only)"""
DEBUG_CHOICES = ["ALL", "DEBUG", "ERROR", "FATAL", "INFO", "TRACE", "WARN"]
OUTPUT_CHOICES = ["ids", "legacy", "yaml"]
SKIP_CHOICES = ['all', 'checksum', 'minmax', 'thumbnails', 'upgrade']
NO_ARG = object()

OMERO_JAVA_ZIP = (
    'https://downloads.openmicroscopy.org/omero/{version}/OMERO.java.zip'
)


class CommandArguments(object):

    def __init__(self, ctx, args):
        self.__ctx = ctx
        self.__args = args
        self.__accepts = set()
        self.__added = dict()
        self.__java_initial = list()
        self.__java_additional = list()
        self.__py_initial = list()
        self.__py_additional = list()
        # Python arguments
        self.__py_keys = (
            "javahelp", "skip", "file", "errs", "logback",
            "port", "password", "group", "create", "func",
            "bulk", "prog", "user", "key", "path", "logprefix",
            "JAVA_DEBUG", "quiet", "server", "depth", "clientdir",
            "fetch_jars",
            "sudo")
        self.set_login_arguments(ctx, args)
        self.set_skip_arguments(args)

        for key in vars(args):
            self.__accepts.add(key)
            val = getattr(args, key)
            if key in self.__py_keys:
                # Place the Python elements on the CommandArguments
                # instance so that it behaves like `args`
                setattr(self, key, val)
                self.append_arg(self.__py_initial, key, val)

            elif not val:
                # If there's no value, do nothing
                pass

            else:
                self.append_arg(self.__java_initial, key, val)

    def append_arg(self, cmd_list, key, val=NO_ARG):
        arg_list = self.build_arg_list(key, val)
        cmd_list.extend(arg_list)

    def reset_arg(self, cmd_list, idx, key, val=NO_ARG):
        arg_list = self.build_arg_list(key, val)
        cmd_list[idx:idx+len(arg_list)] = arg_list

    def build_arg_list(self, key, val=NO_ARG):
        arg_list = []
        if len(key) == 1:
            arg_list.append("-"+key)
            if val != NO_ARG:
                if isinstance(val, basestring):
                    arg_list.append(val)
        else:
            key = key.replace("_", "-")
            if val == NO_ARG:
                arg_list.append("--%s" % key)
            elif isinstance(val, basestring):
                arg_list.append(
                    "--%s=%s" % (key, val))
            else:
                arg_list.append("--%s" % key)
        return arg_list

    def set_path(self, path):
        if not isinstance(path, list):
            self.__ctx.die(202, "Path is not a list")
        else:
            self.path = path

    def java_args(self):
        rv = list()
        rv.extend(self.__java_initial)
        rv.extend(self.__java_additional)
        rv.extend(self.path)
        if self.JAVA_DEBUG:
            # Since "args.debug" is used by omero/cli.py itself,
            # uses of "--debug" *after* the `import` command are
            # handled by placing them in this special variable.
            rv.append("--debug=%s" % self.JAVA_DEBUG)
        return rv

    def initial_args(self):
        rv = list()
        rv.extend(self.__py_initial)
        rv.extend(self.__java_initial)
        return rv

    def added_args(self):
        rv = list()
        rv.extend(self.__py_additional)
        rv.extend(self.__java_additional)
        rv.extend(self.path)
        return rv

    def accepts(self, key):
        return key in self.__accepts

    def add(self, key, val=NO_ARG):

        idx = None
        if key in self.__added:
            idx = self.__added[key]

        if key in self.__py_keys:
            # First we check if this is a Python argument, in which
            # case it's set directly on the instance itself. This
            # may need to be later set elsewhere if multiple bulk
            # files are supported.
            setattr(self, key, val)
            where = self.__py_additional
        elif not self.accepts(key):
            self.__ctx.die(200, "Unknown argument: %s" % key)
        else:
            where = self.__java_additional

        if idx is None:
            idx = len(where)
            self.append_arg(where, key, val)
            self.__added[key] = idx
        else:
            self.reset_arg(where, idx, key, val)

    def set_login_arguments(self, ctx, args):
        """Set the connection arguments"""

        if args.javahelp:
            self.__java_initial.append("-h")

        # Connection is required unless help arguments or -f is passed
        connection_required = ("-h" not in self.__java_initial and
                               not args.f and
                               not args.advanced_help)
        if connection_required:
            client = ctx.conn(args)
            host = client.getProperty("omero.host")
            port = client.getProperty("omero.port")
            session = client.getSessionId()
            self.__java_initial.extend(["-s", host])
            self.__java_initial.extend(["-p", port])
            self.__java_initial.extend(["-k", session])

    def set_skip_arguments(self, args):
        """Set the arguments to skip steps during import"""
        if not args.skip:
            return
        self.set_skip_values(args.skip)

    def set_skip_values(self, skip):
        """Set the arguments to skip steps during import"""

        if ('all' in skip or 'checksum' in skip):
            self.__java_initial.append("--checksum-algorithm=File-Size-64")
        if ('all' in skip or 'thumbnails' in skip):
            self.__java_initial.append("--no-thumbnails")
        if ('all' in skip or 'minmax' in skip):
            self.__java_initial.append("--no-stats-info")
        if ('all' in skip or 'upgrade' in skip):
            self.__java_initial.append("--no-upgrade-check")

    def open_files(self, mode="w"):
        # Open file handles for stdout/stderr if applicable
        out = self.open_log(self.__args.file, self.__args.logprefix, mode=mode)
        err = self.open_log(self.__args.errs, self.__args.logprefix, mode=mode)
        return out, err

    def open_log(self, file, prefix=None, mode="w"):
        if not file:
            return None
        if prefix:
            file = os.path.sep.join([prefix, file])
        file = os.path.abspath(file)
        dir = os.path.dirname(file)
        if not os.path.exists(dir):
            os.makedirs(dir)
        return open(file, mode)


class ImportControl(BaseControl):

    COMMAND = [START_CLASS]

    def _configure(self, parser):

        parser.add_login_arguments()

        parser.add_argument(
            "--javahelp", "--java-help",
            action="store_true", help="Show the Java help text")

        # The following arguments are strictly used by Python
        # The "---" form is kept for backwards compatibility.
        py_group = parser.add_argument_group(
            'Python arguments',
            'Optional arguments which are used to configure import.')

        def add_python_argument(*args, **kwargs):
            py_group.add_argument(*args, **kwargs)

        for name, help in (
            ("bulk", "Bulk YAML file for driving multiple imports"),
            ("logprefix", "Directory or file prefix for --file and --errs"),
            (
                "file",
                "File for storing the standard output from the Java process"
            ),
            (
                "errs",
                "File for storing the standard error from the Java process"
            )
        ):
            add_python_argument("--%s" % name, nargs="?", help=help)
            add_python_argument("---%s" % name, nargs="?", help=SUPPRESS)

        add_python_argument(
            "--clientdir", type=str,
            help="Path to the directory containing the client JARs. "
            " Default: lib/client")
        add_python_argument(
            "--logback", type=str,
            help="Path to a logback xml file. "
            " Default: etc/logback-cli.xml")
        add_python_argument(
            "--fetch-jars", type=str,
            help="Download OMERO.java jars by version or URL, then exit")

        # The following arguments are strictly passed to Java
        name_group = parser.add_argument_group(
            'Naming arguments', 'Optional arguments passed strictly to Java.')

        def add_java_name_argument(*args, **kwargs):
            name_group.add_argument(*args, **kwargs)

        add_java_name_argument(
            "-n", "--name",
            help="Image or plate name to use (**)",
            metavar="NAME")
        add_java_name_argument(
            "-x", "--description",
            help="Image or plate description to use (**)",
            metavar="DESCRIPTION")
        # Deprecated naming arguments
        add_java_name_argument(
            "--plate_name",
            help=SUPPRESS)
        add_java_name_argument(
            "--plate_description",
            help=SUPPRESS)

        # Feedback options
        feedback_group = parser.add_argument_group(
            'Feedback arguments',
            'Optional arguments passed strictly to Java allowing to report'
            ' errors to the OME team.')

        def add_feedback_argument(*args, **kwargs):
            feedback_group.add_argument(*args, **kwargs)

        add_feedback_argument(
            "--report", action="store_true",
            help="Report errors to the OME team (**)")
        add_feedback_argument(
            "--upload", action="store_true",
            help=("Upload broken files and log file (if any) with report."
                  " Required --report (**)"))
        add_feedback_argument(
            "--logs", action="store_true",
            help=("Upload log file (if any) with report."
                  " Required --report (**)"))
        add_feedback_argument(
            "--email",
            help="Email for reported errors. Required --report (**)",
            metavar="EMAIL")
        add_feedback_argument(
            "--qa-baseurl",
            help=SUPPRESS)

        # Annotation options
        annotation_group = parser.add_argument_group(
            'Annotation arguments',
            'Optional arguments passed strictly to Java allowing to annotate'
            ' imports.')

        def add_annotation_argument(*args, **kwargs):
            annotation_group.add_argument(*args, **kwargs)

        add_annotation_argument(
            "--annotation-ns", metavar="ANNOTATION_NS",
            help="Namespace to use for subsequent annotation (**)")
        add_annotation_argument(
            "--annotation-text", metavar="ANNOTATION_TEXT",
            help="Content for a text annotation (**)")
        add_annotation_argument(
            "--annotation-link",
            metavar="ANNOTATION_LINK",
            help="Comment annotation ID to link all images to (**)")
        add_annotation_argument(
            "--annotation_ns", metavar="ANNOTATION_NS",
            help=SUPPRESS)
        add_annotation_argument(
            "--annotation_text", metavar="ANNOTATION_TEXT",
            help=SUPPRESS)
        add_annotation_argument(
            "--annotation_link", metavar="ANNOTATION_LINK",
            help=SUPPRESS)

        java_group = parser.add_argument_group(
            'Java arguments', 'Optional arguments passed strictly to Java.')

        def add_java_argument(*args, **kwargs):
            java_group.add_argument(*args, **kwargs)

        add_java_argument(
            "-f", action="store_true",
            help="Display the used files and exit (**)")
        add_java_argument(
            "-c", action="store_true",
            help="Continue importing after errors (**)")
        add_java_argument(
            "-l", "--readers",
            help="Use the list of readers rather than the default (**)",
            metavar="READER_FILE")
        add_java_argument(
            "-d",
            help="OMERO dataset ID to import image into (**)",
            metavar="DATASET_ID")
        add_java_argument(
            "-r",
            help="OMERO screen ID to import plate into (**)",
            metavar="SCREEN_ID")
        add_java_argument(
            "-T", "--target",
            help="OMERO target specification (**)",
            metavar="TARGET")
        add_java_argument(
            "--debug", choices=DEBUG_CHOICES,
            help="Turn debug logging on (**)",
            metavar="LEVEL", dest="JAVA_DEBUG")
        add_java_argument(
            "--output", choices=OUTPUT_CHOICES,
            help="Set an alternative output style",
            metavar="TYPE")
        add_java_argument(
            "--encrypted",
            choices=("true", "false"),
            help="Whether the import should use SSL or not",
            metavar="TYPE")

        # Arguments previously *following" `--`
        advjava_group = parser.add_argument_group(
            'Advanced Java arguments', (
                'Optional arguments passed strictly to Java. '
                'For more information, see --advanced-help.'))

        def add_advjava_argument(*args, **kwargs):
            advjava_group.add_argument(*args, **kwargs)

        add_advjava_argument(
            "--advanced-help", action="store_true",
            help="Show the advanced help text")
        add_advjava_argument(
            "--transfer", nargs="?", metavar="TYPE",
            help="Transfer methods like in-place import")
        add_advjava_argument(
            "--exclude", nargs="?", metavar="TYPE",
            help="Exclusion filters for preventing re-import")
        add_advjava_argument(
            "--checksum-algorithm", nargs="?", metavar="TYPE",
            help="Alternative hashing mechanisms balancing speed & accuracy")
        add_advjava_argument(
            "--no-stats-info", action="store_true", help=SUPPRESS)
        add_advjava_argument(
            "--no-thumbnails", action="store_true", help=SUPPRESS)
        add_advjava_argument(
            "--no-upgrade-check", action="store_true", help=SUPPRESS)
        add_advjava_argument(
            "--parallel-upload", metavar="COUNT",
            help="Number of file upload threads to run at the same time")
        add_advjava_argument(
            "--parallel-fileset", metavar="COUNT",
            help="Number of fileset candidates to import at the same time")

        # Unsure on these.
        add_python_argument(
            "--depth", default=4, type=int,
            help="Number of directories to scan down for files")
        add_python_argument(
            "--skip", type=str, choices=SKIP_CHOICES, action='append',
            help="Optional step to skip during import")
        add_python_argument(
            "path", nargs="*",
            help="Path to be passed to the Java process")

        parser.set_defaults(func=self.importer)

    def _get_classpath_logback(self, args):
        lib_client = self.ctx.dir / "lib" / "client"
        auto_download = False
        if args.clientdir:
            client_dir = path(args.clientdir)
        elif lib_client.exists():
            client_dir = lib_client
        else:
            auto_download = True
            omero_java_dir, omero_java_txt = self._userdir_jars()
            client_dir = omero_java_dir

        etc_dir = old_div(self.ctx.dir, "etc")
        if args.logback:
            xml_file = path(args.logback)
        else:
            xml_file = old_div(etc_dir, "logback-cli.xml")

        classpath = []
        if client_dir and client_dir.exists():
            classpath = [f.abspath() for f in client_dir.files("*.jar")]
        if auto_download:
            if classpath:
                self.ctx.err('Using {}'.format(omero_java_txt.text()))
                if not args.logback:
                    xml_file = client_dir / "logback-cli.xml"
        else:
            if not classpath:
                self.ctx.die(
                    103, "No JAR files found under '%s'" % client_dir)

        logback = "-Dlogback.configurationFile=%s" % xml_file
        return classpath, logback

    def importer(self, args):
        if args.fetch_jars:
            if args.path:
                self.ctx.err('WARNING: Ignoring extra arguments')
            self.download_omero_java(args.fetch_jars)
            return

        classpath, logback = self._get_classpath_logback(args)
        if not classpath:
            self.download_omero_java('latest')
            classpath, logback = self._get_classpath_logback(args)

        command_args = CommandArguments(self.ctx, args)
        xargs = [logback, "-Xmx1024M", "-cp", os.pathsep.join(classpath)]
        xargs.append("-Domero.import.depth=%s" % args.depth)

        if args.bulk and args.path:
            self.ctx.die(104, "When using bulk import, omit paths")
        elif args.bulk:
            self.bulk_import(command_args, xargs)
        else:
            self.do_import(command_args, xargs)

    def _userdir_jars(self, parentonly=False):
        user_jars = get_omero_user_cache_dir() / 'jars'
        # Use this file instead of a symlink so it works on all platform
        omero_java_txt = user_jars / 'OMERO.java.txt'
        if parentonly:
            return user_jars, omero_java_txt
        omero_java_dir = None
        if omero_java_txt.exists():
            omero_java_dir = omero_java_txt.text().strip()
            return user_jars / omero_java_dir / 'libs', omero_java_txt
        else:
            return None, omero_java_txt

    def download_omero_java(self, version_or_uri):
        if re.match("^\w+://", version_or_uri):
            omero_java_zip = version_or_uri
        else:
            omero_java_zip = OMERO_JAVA_ZIP.format(version=version_or_uri)
        self.ctx.err("Downloading %s" % omero_java_zip)
        jars_dir, omero_java_txt = self._userdir_jars(parentonly=True)
        jars_dir.makedirs_p()
        with requests.get(omero_java_zip) as resp:
            with ZipFile(BytesIO(resp.content)) as zipfile:
                topdirs = set(f.filename.split(
                    os.path.sep)[0] for f in zipfile.filelist if f.is_dir())
                if len(topdirs) != 1:
                    self.ctx.die(
                        108,
                        'Expected one top directory in OMERO.java.zip: {}'
                        .format(topdirs))
                topdir = topdirs.pop()
                if os.path.isabs(topdir):
                    self.ctx.die(
                        108,
                        'Unexpected absolute paths in OMERO.java.zip: {}'
                        .format(topdir))
                zipfile.extractall(jars_dir)
                omero_java_txt.write_text(topdir)

    def do_import(self, command_args, xargs, mode="w"):
        out = err = None
        try:

            import_command = self.COMMAND + command_args.java_args()
            out, err = command_args.open_files(mode=mode)

            p = omero.java.popen(
                import_command, debug=False, xargs=xargs,
                stdout=out, stderr=err)

            self.ctx.rv = p.wait()

        finally:
            # Make sure file handles are closed
            if out:
                out.close()
            if err:
                err.close()

    def bulk_import(self, command_args, xargs):

        try:
            from yaml import safe_load
        except ImportError:
            self.ctx.die(105, "ERROR: PyYAML is not installed")

        old_pwd = os.getcwd()
        try:

            # Walk the .yml graph looking for includes
            # and load them all so that the top parent
            # values can be overwritten.
            contents = list()
            bulkfile = command_args.bulk
            while bulkfile:
                bulkfile = os.path.abspath(bulkfile)
                parent = os.path.dirname(bulkfile)
                with open(bulkfile, "r") as f:
                    data = safe_load(f)
                    contents.append((bulkfile, parent, data))
                    bulkfile = data.get("include")
                    os.chdir(parent)
                    # TODO: included files are updated based on the including
                    # file but other file paths aren't!

            bulk = dict()
            for bulkfile, parent, data in reversed(contents):
                bulk.update(data)
                os.chdir(parent)

            incr = 0
            failed = 0
            total = 0
            for cont in self.parse_bulk(bulk, command_args):
                incr += 1
                if command_args.dry_run:
                    rv = ['"%s"' % x for x in command_args.added_args()]
                    rv = " ".join(rv)
                    if command_args.dry_run.lower() == "true":
                        self.ctx.out(rv)
                    else:
                        with open(command_args.dry_run % incr, "w") as o:
                            # FIXME: this assumes 'omero'
                            print(sys.argv[0], "import", rv, file=o)
                else:
                    if incr == 1:
                        mode = "w"
                    else:
                        mode = "a"
                    self.do_import(command_args, xargs, mode=mode)
                if self.ctx.rv:
                    failed += 1
                    total += self.ctx.rv
                    if cont:
                        msg = "Import failed with error code: %s. Continuing"
                        self.ctx.err(msg % self.ctx.rv)
                    else:
                        msg = "Import failed. Use -c to continue after errors"
                        self.ctx.die(106, msg)
                # Fail if any import failed
                self.ctx.rv = total
                if failed:
                    self.ctx.err("%x failed imports" % failed)
        finally:
            os.chdir(old_pwd)

    def parse_bulk(self, bulk, command_args):
        # Known keys with special handling
        cont = False

        command_args.dry_run = False
        if "dry_run" in bulk:
            dry_run = str(bulk.pop("dry_run"))
            # Accept any non-false string since it might be a pattern
            if dry_run.lower() != "false":
                command_args.dry_run = dry_run

        if "continue" in bulk:
            cont = True
            c = bulk.pop("continue")
            if bool(c):
                command_args.add("c")

        if "skip" in bulk:
            command_args.set_skip_values(bulk.pop("skip"))

        if "path" not in bulk:
            # Required until @file format is implemented
            self.ctx.die(107, "No path specified")
        path = bulk.pop("path")

        cols = None
        if "columns" in bulk:
            cols = bulk.pop("columns")

        if "include" in bulk:
            bulk.pop("include")

        # Now parse all other keys
        for key in bulk:
            command_args.add(key, bulk[key])

        # All properties are set, yield for each path
        # to be imported in turn. The value for `cont`
        # is yielded so that the caller knows whether
        # or not an error should be fatal.

        if not cols:
            # No parsing necessary
            function = self.parse_text
        else:
            function = self.parse_shlex
            if path.endswith(".tsv"):
                function = self.parse_tsv
            elif path.endswith(".csv"):
                function = self.parse_csv

        for parts in function(path):
            if not cols:
                command_args.set_path(parts)
            else:
                for idx, col in enumerate(cols):
                    if col == "path":
                        command_args.set_path([parts[idx]])
                    else:
                        command_args.add(col, parts[idx])
            yield cont

    def parse_text(self, path, parse=False):
        with open(path, "r") as o:
            for line in o:
                line = line.strip()
                if parse:
                    line = shlex.split(line)
                yield [line]

    def parse_shlex(self, path):
        for line in self.parse_text(path, parse=True):
            yield line

    def parse_tsv(self, path, delimiter="\t"):
        for line in self.parse_csv(path, delimiter):
            yield line

    def parse_csv(self, path, delimiter=","):
        with open(path, "r") as data:
            for line in csv.reader(data, delimiter=delimiter):
                yield line


class TestEngine(ImportControl):
    COMMAND = [TEST_CLASS]


try:
    register("import", ImportControl, HELP, epilog=EXAMPLES)
    register("testengine", TestEngine, TESTHELP)
except NameError:
    if __name__ == "__main__":
        cli = CLI()
        cli.register("import", ImportControl, HELP, epilog=EXAMPLES)
        cli.register("testengine", TestEngine, TESTHELP)
        cli.invoke(sys.argv[1:])
