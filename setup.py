#!/usr/bin/env python3
# Copyright 2017 Christoph Reiter <reiter.christoph@gmail.com>
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
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301
# USA

import io
import os
import sys
import errno
import subprocess
import tarfile
import sysconfig
import tempfile
from email import parser

try:
    from setuptools import setup
except ImportError:
    from distutils.core import setup

from distutils.core import Extension, Distribution, Command
from distutils.errors import DistutilsSetupError, DistutilsOptionError
from distutils.ccompiler import new_compiler
from distutils.sysconfig import get_python_lib, customize_compiler
from distutils import dir_util, log


PYGOBJECT_VERISON = "3.29.0"
GLIB_VERSION_REQUIRED = "2.38.0"
GI_VERSION_REQUIRED = "1.46.0"
PYCAIRO_VERSION_REQUIRED = "1.11.1"
LIBFFI_VERSION_REQUIRED = "3.0"


def get_command_class(name):
    # Returns the right class for either distutils or setuptools
    return Distribution({}).get_command_class(name)


def get_pycairo_pkg_config_name():
    return "py3cairo" if sys.version_info[0] == 3 else "pycairo"


def get_version_requirement(pkg_config_name):
    """Given a pkg-config module name gets the minimum version required"""

    versions = {
        "gobject-introspection-1.0": GI_VERSION_REQUIRED,
        "glib-2.0": GLIB_VERSION_REQUIRED,
        "gio-2.0": GLIB_VERSION_REQUIRED,
        get_pycairo_pkg_config_name(): PYCAIRO_VERSION_REQUIRED,
        "libffi": LIBFFI_VERSION_REQUIRED,
        "cairo": "0",
        "cairo-gobject": "0",
    }

    return versions[pkg_config_name]


def get_versions():
    version = PYGOBJECT_VERISON.split(".")
    assert len(version) == 3

    versions = {
        "PYGOBJECT_MAJOR_VERSION": version[0],
        "PYGOBJECT_MINOR_VERSION": version[1],
        "PYGOBJECT_MICRO_VERSION": version[2],
        "VERSION": ".".join(version),
    }
    return versions


def parse_pkg_info(conf_dir):
    """Returns an email.message.Message instance containing the content
    of the PKG-INFO file.
    """

    versions = get_versions()

    pkg_info = os.path.join(conf_dir, "PKG-INFO.in")
    with io.open(pkg_info, "r", encoding="utf-8") as h:
        text = h.read()
        for key, value in versions.items():
            text = text.replace("@%s@" % key, value)

    p = parser.Parser()
    message = p.parse(io.StringIO(text))
    return message


def _run_pkg_config(args, _cache={}):
    command = tuple(["pkg-config"] + args)

    if command not in _cache:
        try:
            result = subprocess.check_output(command)
        except OSError as e:
            if e.errno == errno.ENOENT:
                raise SystemExit(
                    "%r not found.\nArguments: %r" % (command[0], command))
            raise SystemExit(e)
        except subprocess.CalledProcessError as e:
            raise SystemExit(e)
        else:
            _cache[command] = result

    return _cache[command]


def pkg_config_version_check(pkg, version):
    _run_pkg_config([
        "--print-errors",
        "--exists",
        '%s >= %s' % (pkg, version),
    ])


def pkg_config_parse(opt, pkg):
    ret = _run_pkg_config([opt, pkg])
    if sys.version_info[0] == 3:
        output = ret.decode()
    else:
        output = ret
    opt = opt[-2:]
    return [x.lstrip(opt) for x in output.split()]


def list_headers(d):
    return [os.path.join(d, e) for e in os.listdir(d) if e.endswith(".h")]


def filter_compiler_arguments(compiler, args):
    """Given a compiler instance and a list of compiler warning flags
    returns the list of supported flags.
    """

    if compiler.compiler_type == "msvc":
        # TODO
        return []

    extra = []

    def check_arguments(compiler, args):
        p = subprocess.Popen(
            [compiler.compiler[0]] + args + extra + ["-x", "c", "-E", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        stdout, stderr = p.communicate(b"int i;\n")
        if p.returncode != 0:
            text = stderr.decode("ascii", "replace")
            return False, [a for a in args if a in text]
        else:
            return True, []

    def check_argument(compiler, arg):
        return check_arguments(compiler, [arg])[0]

    # clang doesn't error out for unknown options, force it to
    if check_argument(compiler, '-Werror=unknown-warning-option'):
        extra += ['-Werror=unknown-warning-option']
    if check_argument(compiler, '-Werror=unused-command-line-argument'):
        extra += ['-Werror=unused-command-line-argument']

    # first try to remove all arguments contained in the error message
    supported = list(args)
    while 1:
        ok, maybe_unknown = check_arguments(compiler, supported)
        if ok:
            return supported
        elif not maybe_unknown:
            break
        for unknown in maybe_unknown:
            if not check_argument(compiler, unknown):
                supported.remove(unknown)

    # hm, didn't work, try each argument one by one
    supported = []
    for arg in args:
        if check_argument(compiler, arg):
            supported.append(arg)
    return supported


class sdist_gnome(Command):
    description = "Create a source tarball for GNOME"
    user_options = []

    def initialize_options(self):
        pass

    def finalize_options(self):
        pass

    def run(self):
        dist_dir = tempfile.mkdtemp()
        try:
            cmd = self.reinitialize_command("sdist")
            cmd.dist_dir = dist_dir
            cmd.ensure_finalized()
            cmd.run()

            base_name = self.distribution.get_fullname().lower()
            cmd.make_release_tree(base_name, cmd.filelist.files)
            try:
                self.make_archive(base_name, "xztar", base_dir=base_name)
            finally:
                dir_util.remove_tree(base_name)
        finally:
            dir_util.remove_tree(dist_dir)


du_sdist = get_command_class("sdist")


class distcheck(du_sdist):
    """Creates a tarball and does some additional sanity checks such as
    checking if the tarball includes all files, builds successfully and
    the tests suite passes.
    """

    def _check_manifest(self):
        # make sure MANIFEST.in includes all tracked files
        assert self.get_archive_files()

        if subprocess.call(["git", "status"],
                           stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE) != 0:
            return

        included_files = self.filelist.files
        assert included_files

        process = subprocess.Popen(
            ["git", "ls-tree", "-r", "HEAD", "--name-only"],
            stdout=subprocess.PIPE, universal_newlines=True)
        out, err = process.communicate()
        assert process.returncode == 0

        tracked_files = out.splitlines()
        for ignore in [".gitignore"]:
            if ignore in tracked_files:
                tracked_files.remove(ignore)

        diff = set(tracked_files) - set(included_files)
        assert not diff, (
            "Not all tracked files included in tarball, check MANIFEST.in",
            diff)

    def _check_dist(self):
        # make sure the tarball builds
        assert self.get_archive_files()

        distcheck_dir = os.path.abspath(
            os.path.join(self.dist_dir, "distcheck"))
        if os.path.exists(distcheck_dir):
            dir_util.remove_tree(distcheck_dir)
        self.mkpath(distcheck_dir)

        archive = self.get_archive_files()[0]
        tfile = tarfile.open(archive, "r:gz")
        tfile.extractall(distcheck_dir)
        tfile.close()

        name = self.distribution.get_fullname()
        extract_dir = os.path.join(distcheck_dir, name)

        old_pwd = os.getcwd()
        os.chdir(extract_dir)
        try:
            self.spawn([sys.executable, "setup.py", "build"])
            self.spawn([sys.executable, "setup.py", "install",
                        "--root",
                        os.path.join(distcheck_dir, "prefix"),
                        "--record",
                        os.path.join(distcheck_dir, "log.txt"),
                        ])
            self.spawn([sys.executable, "setup.py", "test"])
        finally:
            os.chdir(old_pwd)

    def run(self):
        du_sdist.run(self)
        self._check_manifest()
        self._check_dist()


class build_tests(Command):
    description = "build test libraries and extensions"
    user_options = [
        ("force", "f", "force a rebuild"),
    ]

    def initialize_options(self):
        self.build_temp = None
        self.build_base = None
        self.force = False

    def finalize_options(self):
        self.set_undefined_options(
            'build_ext',
            ('build_temp', 'build_temp'))
        self.set_undefined_options(
            'build',
            ('build_base', 'build_base'))

    def _newer_group(self, sources, *targets):
        assert targets

        from distutils.dep_util import newer_group

        if self.force:
            return True
        else:
            for target in targets:
                if not newer_group(sources, target):
                    return False
            return True

    def run(self):
        cmd = self.reinitialize_command("build_ext")
        cmd.inplace = True
        cmd.force = self.force
        cmd.ensure_finalized()
        cmd.run()

        gidatadir = pkg_config_parse(
            "--variable=gidatadir", "gobject-introspection-1.0")[0]
        g_ir_scanner = pkg_config_parse(
            "--variable=g_ir_scanner", "gobject-introspection-1.0")[0]
        g_ir_compiler = pkg_config_parse(
            "--variable=g_ir_compiler", "gobject-introspection-1.0")[0]

        script_dir = get_script_dir()
        gi_dir = os.path.join(script_dir, "gi")
        tests_dir = os.path.join(script_dir, "tests")
        gi_tests_dir = os.path.join(gidatadir, "tests")

        schema_xml = os.path.join(tests_dir, "org.gnome.test.gschema.xml")
        schema_bin = os.path.join(tests_dir, "gschemas.compiled")
        if self._newer_group([schema_xml], schema_bin):
            subprocess.check_call([
                "glib-compile-schemas",
                "--targetdir=%s" % tests_dir,
                "--schema-file=%s" % schema_xml,
            ])

        compiler = new_compiler()
        customize_compiler(compiler)

        if os.name == "nt":
            compiler.shared_lib_extension = ".dll"
        elif sys.platform == "darwin":
            compiler.shared_lib_extension = ".dylib"
            if "-bundle" in compiler.linker_so:
                compiler.linker_so = list(compiler.linker_so)
                i = compiler.linker_so.index("-bundle")
                compiler.linker_so[i] = "-dynamiclib"
        else:
            compiler.shared_lib_extension = ".so"

        def build_ext(ext):
            if compiler.compiler_type == "msvc":
                raise Exception("MSVC support not implemented")

            libname = compiler.shared_object_filename(ext.name)
            ext_paths = [os.path.join(tests_dir, libname)]
            if os.name == "nt":
                implibname = libname + ".a"
                ext_paths.append(os.path.join(tests_dir, implibname))

            if self._newer_group(ext.sources + ext.depends, *ext_paths):
                objects = compiler.compile(
                    ext.sources,
                    output_dir=self.build_temp,
                    include_dirs=ext.include_dirs)

                if os.name == "nt":
                    postargs = ["-Wl,--out-implib=%s" %
                                os.path.join(tests_dir, implibname)]
                else:
                    postargs = []

                compiler.link_shared_object(
                    objects,
                    compiler.shared_object_filename(ext.name),
                    output_dir=tests_dir,
                    libraries=ext.libraries,
                    library_dirs=ext.library_dirs,
                    extra_postargs=postargs)

            return ext_paths

        ext = Extension(
            name='libgimarshallingtests',
            sources=[
                os.path.join(gi_tests_dir, "gimarshallingtests.c"),
                os.path.join(tests_dir, "gimarshallingtestsextra.c"),
            ],
            include_dirs=[
                gi_tests_dir,
                tests_dir,
            ],
            depends=[
                os.path.join(gi_tests_dir, "gimarshallingtests.h"),
                os.path.join(tests_dir, "gimarshallingtestsextra.h"),
            ],
        )
        add_ext_pkg_config_dep(ext, compiler.compiler_type, "glib-2.0")
        add_ext_pkg_config_dep(ext, compiler.compiler_type, "gio-2.0")
        ext_paths = build_ext(ext)

        gir_path = os.path.join(tests_dir, "GIMarshallingTests-1.0.gir")
        typelib_path = os.path.join(
            tests_dir, "GIMarshallingTests-1.0.typelib")

        if self._newer_group(ext_paths, gir_path):
            subprocess.check_call([
                g_ir_scanner,
                "--no-libtool",
                "--include=Gio-2.0",
                "--namespace=GIMarshallingTests",
                "--nsversion=1.0",
                "--symbol-prefix=gi_marshalling_tests",
                "--warn-all",
                "--warn-error",
                "--library-path=%s" % tests_dir,
                "--library=gimarshallingtests",
                "--pkg=glib-2.0",
                "--pkg=gio-2.0",
                "--output=%s" % gir_path,
            ] + ext.sources + ext.depends)

        if self._newer_group([gir_path], typelib_path):
            subprocess.check_call([
                g_ir_compiler,
                gir_path,
                "--output=%s" % typelib_path,
            ])

        ext = Extension(
            name='libregress',
            sources=[
                os.path.join(gi_tests_dir, "regress.c"),
                os.path.join(tests_dir, "regressextra.c"),
            ],
            include_dirs=[
                gi_tests_dir,
            ],
            depends=[
                os.path.join(gi_tests_dir, "regress.h"),
                os.path.join(tests_dir, "regressextra.h"),
            ],
        )
        add_ext_pkg_config_dep(ext, compiler.compiler_type, "glib-2.0")
        add_ext_pkg_config_dep(ext, compiler.compiler_type, "gio-2.0")
        add_ext_pkg_config_dep(ext, compiler.compiler_type, "cairo")
        add_ext_pkg_config_dep(ext, compiler.compiler_type, "cairo-gobject")
        ext_paths = build_ext(ext)

        gir_path = os.path.join(tests_dir, "Regress-1.0.gir")
        typelib_path = os.path.join(tests_dir, "Regress-1.0.typelib")

        if self._newer_group(ext_paths, gir_path):
            subprocess.check_call([
                g_ir_scanner,
                "--no-libtool",
                "--include=cairo-1.0",
                "--include=Gio-2.0",
                "--namespace=Regress",
                "--nsversion=1.0",
                "--warn-all",
                "--warn-error",
                "--library-path=%s" % tests_dir,
                "--library=regress",
                "--pkg=glib-2.0",
                "--pkg=gio-2.0",
                "--pkg=cairo",
                "--pkg=cairo-gobject",
                "--output=%s" % gir_path,
            ] + ext.sources + ext.depends)

        if self._newer_group([gir_path], typelib_path):
            subprocess.check_call([
                g_ir_compiler,
                gir_path,
                "--output=%s" % typelib_path,
            ])

        ext = Extension(
            name='tests.testhelper',
            sources=[
                os.path.join(tests_dir, "testhelpermodule.c"),
                os.path.join(tests_dir, "test-floating.c"),
                os.path.join(tests_dir, "test-thread.c"),
                os.path.join(tests_dir, "test-unknown.c"),
            ],
            include_dirs=[
                gi_dir,
                tests_dir,
            ],
            depends=list_headers(gi_dir) + list_headers(tests_dir),
            define_macros=[("PY_SSIZE_T_CLEAN", None)],
        )
        add_ext_pkg_config_dep(ext, compiler.compiler_type, "glib-2.0")
        add_ext_pkg_config_dep(ext, compiler.compiler_type, "gio-2.0")
        add_ext_pkg_config_dep(ext, compiler.compiler_type, "cairo")
        add_ext_compiler_flags(ext, compiler)

        dist = Distribution({"ext_modules": [ext]})

        build_cmd = dist.get_command_obj("build")
        build_cmd.build_base = os.path.join(self.build_base, "pygobject_tests")
        build_cmd.ensure_finalized()

        cmd = dist.get_command_obj("build_ext")
        cmd.inplace = True
        cmd.force = self.force
        cmd.ensure_finalized()
        cmd.run()


class test(Command):
    user_options = [
        ("valgrind", None, "run tests under valgrind"),
        ("valgrind-log-file=", None, "save logs instead of printing them"),
        ("gdb", None, "run tests under gdb"),
        ("no-capture", "s", "don't capture test output"),
    ]

    def initialize_options(self):
        self.valgrind = None
        self.valgrind_log_file = None
        self.gdb = None
        self.no_capture = None

    def finalize_options(self):
        self.valgrind = bool(self.valgrind)
        if self.valgrind_log_file and not self.valgrind:
            raise DistutilsOptionError("valgrind not enabled")
        self.gdb = bool(self.gdb)
        self.no_capture = bool(self.no_capture)

    def run(self):
        cmd = self.reinitialize_command("build_tests")
        cmd.ensure_finalized()
        cmd.run()

        env = os.environ.copy()
        env.pop("MSYSTEM", None)

        if self.no_capture:
            env["PYGI_TEST_VERBOSE"] = "1"

        env["MALLOC_PERTURB_"] = "85"
        env["MALLOC_CHECK_"] = "3"
        env["G_SLICE"] = "debug-blocks"

        def get_suppression_files():
            files = []
            if sys.version_info[0] == 2:
                files.append(os.path.join(
                    sys.prefix, "lib", "valgrind", "python.supp"))
            else:
                files.append(os.path.join(
                    sys.prefix, "lib", "valgrind", "python3.supp"))
            files.append(os.path.join(
                sys.prefix, "share", "glib-2.0", "valgrind", "glib.supp"))
            return [f for f in files if os.path.isfile(f)]

        pre_args = []

        if self.valgrind:
            env["G_SLICE"] = "always-malloc"
            env["G_DEBUG"] = "gc-friendly"
            env["PYTHONMALLOC"] = "malloc"

            pre_args += [
                "valgrind", "--leak-check=full", "--show-possibly-lost=no",
                "--num-callers=20", "--child-silent-after-fork=yes",
            ] + ["--suppressions=" + f for f in get_suppression_files()]

            if self.valgrind_log_file:
                pre_args += ["--log-file=" + self.valgrind_log_file]

        if self.gdb:
            env["PYGI_TEST_GDB"] = "1"
            pre_args += ["gdb", "--args"]

        if pre_args:
            log.info(" ".join(pre_args))

        tests_dir = os.path.join(get_script_dir(), "tests")
        sys.exit(subprocess.call(pre_args + [
            sys.executable,
            os.path.join(tests_dir, "runtests.py"),
        ], env=env))


class quality(Command):
    description = "run code quality tests"
    user_options = []

    def initialize_options(self):
        pass

    def finalize_options(self):
        pass

    def run(self):
        status = subprocess.call([
            sys.executable, "-m", "flake8",
        ], cwd=get_script_dir())
        if status != 0:
            raise SystemExit(status)


def get_script_dir():
    return os.path.dirname(os.path.realpath(__file__))


def get_pycairo_include_dir():
    """Returns the best guess at where to find the pycairo headers.
    A bit convoluted because we have to deal with multiple pycairo
    versions.

    Raises if pycairo isn't found or it's too old.
    """

    pkg_config_name = get_pycairo_pkg_config_name()
    min_version = get_version_requirement(pkg_config_name)
    min_version_info = tuple(int(p) for p in min_version.split("."))

    def check_path(include_dir):
        log.info("pycairo: trying include directory: %r" % include_dir)
        header_path = os.path.join(include_dir, "%s.h" % pkg_config_name)
        if os.path.exists(header_path):
            log.info("pycairo: found %r" % header_path)
            return True
        log.info("pycairo: header file (%r) not found" % header_path)
        return False

    def find_path(paths):
        for p in reversed(paths):
            if check_path(p):
                return p

    def find_new_api():
        log.info("pycairo: new API")
        import cairo

        if cairo.version_info < min_version_info:
            raise DistutilsSetupError(
                "pycairo >= %s required, %s found." % (
                    min_version, ".".join(map(str, cairo.version_info))))

        if hasattr(cairo, "get_include"):
            return [cairo.get_include()]
        log.info("pycairo: no get_include()")
        return []

    def find_old_api():
        log.info("pycairo: old API")

        import cairo

        if cairo.version_info < min_version_info:
            raise DistutilsSetupError(
                "pycairo >= %s required, %s found." % (
                    min_version, ".".join(map(str, cairo.version_info))))

        location = os.path.dirname(os.path.abspath(cairo.__path__[0]))
        log.info("pycairo: found %r" % location)

        def samefile(src, dst):
            # Python 2 on Windows doesn't have os.path.samefile, so we have to
            # provide a fallback
            if hasattr(os.path, "samefile"):
                return os.path.samefile(src, dst)
            os.stat(src)
            os.stat(dst)
            return (os.path.normcase(os.path.abspath(src)) ==
                    os.path.normcase(os.path.abspath(dst)))

        def get_sys_path(location, name):
            # Returns the sysconfig path for a distribution, or None
            for scheme in sysconfig.get_scheme_names():
                for path_type in ["platlib", "purelib"]:
                    path = sysconfig.get_path(path_type, scheme)
                    try:
                        if samefile(path, location):
                            return sysconfig.get_path(name, scheme)
                    except EnvironmentError:
                        pass

        data_path = get_sys_path(location, "data") or sys.prefix
        return [os.path.join(data_path, "include", "pycairo")]

    def find_pkg_config():
        log.info("pycairo: pkg-config")
        pkg_config_version_check(pkg_config_name, min_version)
        return pkg_config_parse("--cflags-only-I", pkg_config_name)

    # First the new get_include() API added in >1.15.6
    include_dir = find_path(find_new_api())
    if include_dir is not None:
        return include_dir

    # Then try to find it in the data prefix based on the module path.
    # This works with many virtualenv/userdir setups, but not all apparently,
    # see https://gitlab.gnome.org/GNOME/pygobject/issues/150
    include_dir = find_path(find_old_api())
    if include_dir is not None:
        return include_dir

    # Finally, fall back to pkg-config
    include_dir = find_path(find_pkg_config())
    if include_dir is not None:
        return include_dir

    raise DistutilsSetupError("Could not find pycairo headers")


def add_ext_pkg_config_dep(ext, compiler_type, name):
    msvc_libraries = {
        "glib-2.0": ["glib-2.0"],
        "gio-2.0": ["gio-2.0", "gobject-2.0", "glib-2.0"],
        "gobject-introspection-1.0":
            ["girepository-1.0", "gobject-2.0", "glib-2.0"],
        "cairo": ["cairo"],
        "cairo-gobject":
            ["cairo-gobject", "cairo", "gobject-2.0", "glib-2.0"],
        "libffi": ["ffi"],
    }

    fallback_libs = msvc_libraries[name]
    if compiler_type == "msvc":
        # assume that INCLUDE and LIB contains the right paths
        ext.libraries += fallback_libs
    else:
        min_version = get_version_requirement(name)
        pkg_config_version_check(name, min_version)
        ext.include_dirs += pkg_config_parse("--cflags-only-I", name)
        ext.library_dirs += pkg_config_parse("--libs-only-L", name)
        ext.libraries += pkg_config_parse("--libs-only-l", name)


def add_ext_compiler_flags(ext, compiler, _cache={}):
    cache_key = compiler.compiler[0]
    if cache_key not in _cache:

        args = [
            "-Wall",
            "-Warray-bounds",
            # "-Wcast-align",
            "-Wdeclaration-after-statement",
            # "-Wdouble-promotion",
            "-Wduplicated-branches",
            # "-Wduplicated-cond",
            "-Wextra",
            "-Wformat=2",
            "-Wformat-nonliteral",
            "-Wformat-security",
            "-Wimplicit-function-declaration",
            "-Winit-self",
            "-Winline",
            "-Wjump-misses-init",
            # "-Wlogical-op",
            "-Wmissing-declarations",
            "-Wmissing-format-attribute",
            "-Wmissing-include-dirs",
            "-Wmissing-noreturn",
            "-Wmissing-prototypes",
            "-Wnested-externs",
            # "-Wnull-dereference",
            "-Wold-style-definition",
            "-Wpacked",
            "-Wpointer-arith",
            "-Wrestrict",
            "-Wreturn-type",
            "-Wshadow",
            "-Wsign-compare",
            "-Wstrict-aliasing",
            "-Wstrict-prototypes",
            "-Wswitch-default",
            "-Wundef",
            "-Wunused-but-set-variable",
            "-Wwrite-strings",
            "-Wconversion",
        ]

        args += [
            "-Wno-incompatible-pointer-types-discards-qualifiers",
            "-Wno-missing-field-initializers",
            "-Wno-unused-parameter",
            "-Wno-discarded-qualifiers",
            "-Wno-sign-conversion",
        ]

        # silence clang for unused gcc CFLAGS added by Debian
        args += [
            "-Wno-unused-command-line-argument",
        ]

        args += [
            "-fno-strict-aliasing",
            "-fvisibility=hidden",
        ]

        _cache[cache_key] = filter_compiler_arguments(compiler, args)

    ext.extra_compile_args += _cache[cache_key]


du_build_ext = get_command_class("build_ext")


class build_ext(du_build_ext):

    def initialize_options(self):
        du_build_ext.initialize_options(self)
        self.compiler_type = None

    def finalize_options(self):
        du_build_ext.finalize_options(self)
        self.compiler_type = new_compiler(compiler=self.compiler).compiler_type

    def _write_config_h(self):
        script_dir = get_script_dir()
        target = os.path.join(script_dir, "config.h")
        versions = get_versions()
        content = u"""
/* Configuration header created by setup.py - do not edit */
#ifndef _CONFIG_H
#define _CONFIG_H 1

#define PYGOBJECT_MAJOR_VERSION %(PYGOBJECT_MAJOR_VERSION)s
#define PYGOBJECT_MINOR_VERSION %(PYGOBJECT_MINOR_VERSION)s
#define PYGOBJECT_MICRO_VERSION %(PYGOBJECT_MICRO_VERSION)s
#define VERSION "%(VERSION)s"

#endif /* _CONFIG_H */
""" % versions

        try:
            with io.open(target, 'r', encoding="utf-8") as h:
                if h.read() == content:
                    return
        except EnvironmentError:
            pass

        with io.open(target, 'w', encoding="utf-8") as h:
            h.write(content)

    def _setup_extensions(self):
        ext = {e.name: e for e in self.extensions}

        compiler = new_compiler(compiler=self.compiler)
        customize_compiler(compiler)

        def add_dependency(ext, name):
            add_ext_pkg_config_dep(ext, compiler.compiler_type, name)

        def add_pycairo(ext):
            ext.include_dirs += [get_pycairo_include_dir()]

        gi_ext = ext["gi._gi"]
        add_dependency(gi_ext, "glib-2.0")
        add_dependency(gi_ext, "gio-2.0")
        add_dependency(gi_ext, "gobject-introspection-1.0")
        add_dependency(gi_ext, "libffi")
        add_ext_compiler_flags(gi_ext, compiler)

        gi_cairo_ext = ext["gi._gi_cairo"]
        add_dependency(gi_cairo_ext, "glib-2.0")
        add_dependency(gi_cairo_ext, "gio-2.0")
        add_dependency(gi_cairo_ext, "gobject-introspection-1.0")
        add_dependency(gi_cairo_ext, "libffi")
        add_dependency(gi_cairo_ext, "cairo")
        add_dependency(gi_cairo_ext, "cairo-gobject")
        add_pycairo(gi_cairo_ext)
        add_ext_compiler_flags(gi_cairo_ext, compiler)

    def run(self):
        self._write_config_h()
        self._setup_extensions()
        du_build_ext.run(self)


class install_pkgconfig(Command):
    description = "install .pc file"
    user_options = []

    def initialize_options(self):
        self.install_base = None
        self.install_platbase = None
        self.install_data = None
        self.compiler_type = None
        self.outfiles = []

    def finalize_options(self):
        self.set_undefined_options(
            'install',
            ('install_base', 'install_base'),
            ('install_data', 'install_data'),
            ('install_platbase', 'install_platbase'),
        )

        self.set_undefined_options(
            'build_ext',
            ('compiler_type', 'compiler_type'),
        )

    def get_outputs(self):
        return self.outfiles

    def get_inputs(self):
        return []

    def run(self):
        cmd = self.distribution.get_command_obj("bdist_wheel", create=False)
        if cmd is not None:
            log.warn(
                "Python wheels and pkg-config is not compatible. "
                "No pkg-config file will be included in the wheel. Install "
                "from source if you need one.")
            return

        if self.compiler_type == "msvc":
            return

        script_dir = get_script_dir()
        pkgconfig_in = os.path.join(script_dir, "pygobject-3.0.pc.in")
        with io.open(pkgconfig_in, "r", encoding="utf-8") as h:
            content = h.read()

        config = {
            "prefix": self.install_base,
            "exec_prefix": self.install_platbase,
            "includedir": "${prefix}/include",
            "datarootdir": "${prefix}/share",
            "datadir": "${datarootdir}",
            "VERSION": self.distribution.get_version(),
        }
        for key, value in config.items():
            content = content.replace("@%s@" % key, value)

        libdir = os.path.dirname(get_python_lib(True, True, self.install_data))
        pkgconfig_dir = os.path.join(libdir, "pkgconfig")
        self.mkpath(pkgconfig_dir)
        target = os.path.join(pkgconfig_dir, "pygobject-3.0.pc")
        with io.open(target, "w", encoding="utf-8") as h:
            h.write(content)
        self.outfiles.append(target)


du_install = get_command_class("install")


class install(du_install):

    sub_commands = du_install.sub_commands + [
        ("install_pkgconfig", lambda self: True),
    ]


def main():
    script_dir = get_script_dir()
    pkginfo = parse_pkg_info(script_dir)
    gi_dir = os.path.join(script_dir, "gi")

    sources = [
        os.path.join("gi", n) for n in os.listdir(gi_dir)
        if os.path.splitext(n)[-1] == ".c"
    ]
    cairo_sources = [os.path.join("gi", "pygi-foreign-cairo.c")]
    for s in cairo_sources:
        sources.remove(s)

    readme = os.path.join(script_dir, "README.rst")
    with io.open(readme, encoding="utf-8") as h:
        long_description = h.read()

    gi_ext = Extension(
        name='gi._gi',
        sources=sources,
        include_dirs=[script_dir, gi_dir],
        depends=list_headers(script_dir) + list_headers(gi_dir),
        define_macros=[("PY_SSIZE_T_CLEAN", None)],
    )

    gi_cairo_ext = Extension(
        name='gi._gi_cairo',
        sources=cairo_sources,
        include_dirs=[script_dir, gi_dir],
        depends=list_headers(script_dir) + list_headers(gi_dir),
        define_macros=[("PY_SSIZE_T_CLEAN", None)],
    )

    setup(
        name=pkginfo["Name"],
        version=pkginfo["Version"],
        description=pkginfo["Summary"],
        url=pkginfo["Home-page"],
        author=pkginfo["Author"],
        author_email=pkginfo["Author-email"],
        maintainer=pkginfo["Maintainer"],
        maintainer_email=pkginfo["Maintainer-email"],
        license=pkginfo["License"],
        long_description=long_description,
        platforms=pkginfo.get_all("Platform"),
        classifiers=pkginfo.get_all("Classifier"),
        packages=[
            "pygtkcompat",
            "gi",
            "gi.repository",
            "gi.overrides",
        ],
        ext_modules=[
            gi_ext,
            gi_cairo_ext,
        ],
        cmdclass={
            "build_ext": build_ext,
            "distcheck": distcheck,
            "sdist_gnome": sdist_gnome,
            "build_tests": build_tests,
            "test": test,
            "quality": quality,
            "install": install,
            "install_pkgconfig": install_pkgconfig,
        },
        install_requires=[
            "pycairo>=%s" % get_version_requirement(
                get_pycairo_pkg_config_name()),
        ],
        data_files=[
            ('include/pygobject-3.0', ['gi/pygobject.h']),
        ],
        zip_safe=False,
    )


if __name__ == "__main__":
    main()