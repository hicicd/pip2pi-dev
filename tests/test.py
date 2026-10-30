#!/usr/bin/env python
import os
import sys
try:
    from urllib import unquote
except ImportError: # python3
    from urllib.parse import unquote

import random
import shutil
import difflib
import doctest
import tempfile
import unittest
import threading
import posixpath
import subprocess as sp

try:
    from SocketServer import ThreadingMixIn
    from BaseHTTPServer import HTTPServer
    from SimpleHTTPServer import SimpleHTTPRequestHandler
except ImportError: # python 3
    from socketserver import ThreadingMixIn
    from http.server import HTTPServer
    from http.server import SimpleHTTPRequestHandler


BASE_PATH = os.path.abspath(os.path.dirname(__file__))
PKG_BASE_PATH = os.path.join(os.path.abspath(os.path.dirname(__file__)), "../")
sys.path.append(PKG_BASE_PATH)

from libpip2pi import commands as pip2pi_commands

class chdir(object):
    """ A drop-in replacement for ``os.chdir`` which also acts as a context
        manager.

        >>> old_cwd = os.getcwd()
        >>> with chdir("/usr/"):
        ...     print("current dir:", os.getcwd())
        ...
        current dir: /usr
        >>> os.getcwd() == old_cwd
        True
        >>> x = chdir("/usr/")
        >>> os.getcwd()
        '/usr'
        >>> x
        chdir('/usr/', old_path='...')
        >>> x.unchdir()
        >>> os.getcwd() == old_cwd
        True
        """

    def __init__(self, new_path, old_path=None):
        self.old_path = old_path or os.getcwd()
        self.new_path = new_path
        self.chdir()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.unchdir()

    def chdir(self):
        os.chdir(self.new_path)

    def unchdir(self):
        os.chdir(self.old_path)

    def __repr__(self):
        return "%s(%r, old_path=%r)" %(
            type(self).__name__, self.new_path, self.old_path,
        )


class Pip2PiRequestHandler(SimpleHTTPRequestHandler):
    base_path = os.path.join(BASE_PATH, "assets/")

    def translate_path(self, path):
        """Translate a /-separated PATH to the local filename syntax.

        Components that mean special things to the local file system
        (e.g. drive or directory names) are ignored.  (XXX They should
        probably be diagnosed.)

        """
        # abandon query parameters
        path = path.split('?', 1)[0]
        path = path.split('#', 1)[0]
        path = posixpath.normpath(unquote(path))
        words = path.split('/')
        words = filter(None, words)
        path = self.base_path
        for word in words:
            drive, word = os.path.splitdrive(word)
            head, word = os.path.split(word)
            if word in (os.curdir, os.pardir): continue
            path = os.path.join(path, word)
        return path


IS_CASE_INSENSITIVE = os.path.exists(__file__.upper())
OS_HAS_SYMLINK = hasattr(os, "symlink")


def symlink_unsupported_reason():
    """ Returns why directory symlinks cannot be created here, or None.

        ``OS_HAS_SYMLINK`` is not enough: on Windows a symlink can only be
        created by an administrator (or with Developer Mode enabled), so the
        few tests that need one are skipped there instead of failing.
    """
    if not OS_HAS_SYMLINK:
        return "this platform has no os.symlink"
    temp_dir = tempfile.mkdtemp(prefix="pip2pi-tests")
    try:
        target = os.path.join(temp_dir, "target")
        os.mkdir(target)
        try:
            os.symlink(target, os.path.join(temp_dir, "link"),
                       target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            return "cannot create symlinks here: %s" %(e, )
        return None
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


SYMLINK_UNSUPPORTED = symlink_unsupported_reason()
needs_symlinks = unittest.skipIf(
    SYMLINK_UNSUPPORTED is not None, SYMLINK_UNSUPPORTED or "",
)

# Some tests download sdists with pip from the fixture index in assets/. Recent
# pip versions prepare the metadata of an sdist before downloading it, so that
# index would also have to serve the sdist's build requirements (setuptools),
# which it does not. They are opt-in instead.
HEAVY_TESTS = os.environ.get("PIP2PI_HEAVY_TESTS") == "1"
needs_heavy_tests = unittest.skipUnless(
    HEAVY_TESTS,
    "downloads an sdist from the fixture index, which modern pip cannot build "
    "(set PIP2PI_HEAVY_TESTS=1 to run it)",
)

class Pip2PiHeavyTests(unittest.TestCase):
    SERVER_PORT = random.randint(10000, 40000)

    class BackgroundIt(threading.Thread):
        server = None
        def run(self):
            self.server.serve_forever()

    class ThreadingServer(ThreadingMixIn, HTTPServer):
        pass

    @classmethod
    def setUpClass(cls):
        cls._server_thread = cls.BackgroundIt()
        cls.server = cls.ThreadingServer(("127.0.0.1", cls.SERVER_PORT),
                                         Pip2PiRequestHandler)
        cls._server_thread.server = cls.server
        cls._server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        os.chdir(BASE_PATH)
        self._temp_dir = None
        print("\n" + "-" * 70)

    def tearDown(self):
        if self._temp_dir is not None:
            shutil.rmtree(self._temp_dir)

    def assertDirContents(self, expected_contents, dir):
        with open(expected_contents) as f:
            expected = []
            for line in f.read().splitlines():
                if line.startswith("!"):
                    prefix, _, line = line.partition(": ")
                    if prefix == "!case-sensitive":
                        if IS_CASE_INSENSITIVE:
                            continue
                    else:
                        raise AssertionError("Unexpected prefix: %s" %(prefix, ))
                if not OS_HAS_SYMLINK:
                    line, _, _ = line.partition(" -> ")
                expected.append(line)

        def describe(f):
            if not OS_HAS_SYMLINK:
                return f
            try:
                link = os.readlink(f)
            except OSError as e:
                if e.errno != 22:
                    raise
            else:
                f += " -> " + link
            return f

        with chdir(dir):
            actual = [
                describe(x[2:].decode("utf-8")) for x in
                sp.check_output(["find", "."]).splitlines()
                if x[2:]
            ]

        expected.sort()
        actual.sort()

        if actual != expected:
            print("\n" + "=" * 80 + "\n")
            for line in difflib.unified_diff(expected, actual,
                                             fromfile=expected_contents,
                                             tofile="actual"):
                print(line.rstrip())
            print("\n" + "=" * 80 + "\n")
            raise AssertionError(
                "Output dir does not contain the files listed in %s (see errors on stdout)"
                %(expected_contents, )
            )

    @property
    def temp_dir(self):
        if self._temp_dir is None:
            self._temp_dir = tempfile.mkdtemp(prefix="pip2pi-tests")
        return self._temp_dir

    @property
    def index_url(self):
        return "--index-url=http://127.0.0.1:%s/simple/" %(self.SERVER_PORT, )

    def exc(self, cmd, args):
        print("Running %s with: %s" %(cmd, args))
        return getattr(pip2pi_commands, cmd)([cmd] + args)

    @needs_heavy_tests
    def test_requirements_txt(self):
        res = self.exc("pip2pi", [
            self.temp_dir,
            self.index_url,
            "-r", "test_requirements_txt/requirements.txt",
        ])
        self.assertEqual(res, 0)
        self.assertDirContents("test_requirements_txt/expected-normal.txt", self.temp_dir)

    @needs_heavy_tests
    def test_requirements_txt_with_aggressive_normalization(self):
        res = self.exc("pip2pi", [
            self.temp_dir,
            self.index_url,
            "--aggressive-normalization",
            "-r", "test_requirements_txt/requirements.txt",
        ])
        self.assertEqual(res, 0)
        self.assertDirContents("test_requirements_txt/expected-aggressively-normalized.txt",
                               self.temp_dir)

    def test_eggs_in_packages(self):
        shutil.copy("test_eggs_in_packages/fish-1.1-py2.7.egg", self.temp_dir)
        self.exc("dir2pi", [self.temp_dir])
        self.assertDirContents("expected-eggs-in-packages.txt", self.temp_dir)

    def test_package_index_links_heading(self):
        for asset in ("six-1.10.0-py2.py3-none-any.whl", "six-1.10.0.tar.gz"):
            shutil.copy(os.path.join("assets", asset), self.temp_dir)
        res = self.exc("dir2pi", [self.temp_dir])
        self.assertEqual(res, 0)
        with open(os.path.join(self.temp_dir, "simple", "six", "index.html")) as fp:
            page = fp.read()
        self.assertTrue(
            page.startswith(
                "<html><head><title>Links for six</title>"
                "<meta name='api-version' value='2' /></head><body>\n"
                "<h1>Links for six</h1>\n"
            ),
            page,
        )
        self.assertEqual(page.count("<h1>Links for six</h1>"), 1, page)
        self.assertTrue(page.endswith("</body></html>\n"), page)
        self.assertIn("<a href='six-1.10.0-py2.py3-none-any.whl'>", page)
        self.assertIn("<a href='six-1.10.0.tar.gz'>", page)

    def test_recursive_scan_of_subdirectories(self):
        os.makedirs(os.path.join(self.temp_dir, "flask"))
        os.makedirs(os.path.join(self.temp_dir, "click", "v8.5.0"))
        os.makedirs(os.path.join(self.temp_dir, ".cache"))
        shutil.copy("assets/six-1.10.0-py2.py3-none-any.whl",
                    os.path.join(self.temp_dir, "flask"))
        shutil.copy("assets/wheel-0.29.0-py2.py3-none-any.whl",
                    os.path.join(self.temp_dir, "click", "v8.5.0"))
        shutil.copy("assets/Normalization_Test.with-dashes-1.2rc3.zip",
                    self.temp_dir)
        shutil.copy("assets/six-1.10.0.tar.gz",
                    os.path.join(self.temp_dir, ".cache"))
        with open(os.path.join(self.temp_dir, "flask", "README.txt"), "w") as fp:
            fp.write("not a package\n")

        res = self.exc("dir2pi", ["-S", "--recursive", self.temp_dir])
        self.assertEqual(res, 0)

        simple = os.path.join(self.temp_dir, "simple")
        self.assertEqual(
            sorted(os.listdir(simple)),
            ["index.html", "normalization-test-with-dashes", "six", "wheel"],
        )
        # six lives in flask/, its hidden .cache/ copy is not picked up
        self.assertEqual(
            sorted(os.listdir(os.path.join(simple, "six"))),
            ["index.html", "six-1.10.0-py2.py3-none-any.whl"],
        )
        # wheel lives two levels down, in click/v8.5.0/
        self.assertEqual(
            sorted(os.listdir(os.path.join(simple, "wheel"))),
            ["index.html", "wheel-0.29.0-py2.py3-none-any.whl"],
        )
        with open(os.path.join(simple, "six", "index.html")) as fp:
            page = fp.read()
        self.assertIn("<a href='six-1.10.0-py2.py3-none-any.whl'>", page)

        # the generated simple/ directory is never re-scanned, so running
        # dir2pi again produces the same index
        res = self.exc("dir2pi", ["-S", "--recursive", self.temp_dir])
        self.assertEqual(res, 0)
        with open(os.path.join(simple, "six", "index.html")) as fp:
            self.assertEqual(fp.read(), page)

    def test_top_level_only_by_default(self):
        os.makedirs(os.path.join(self.temp_dir, "flask"))
        shutil.copy("assets/six-1.10.0-py2.py3-none-any.whl",
                    os.path.join(self.temp_dir, "flask"))
        shutil.copy("assets/Normalization_Test.with-dashes-1.2rc3.zip",
                    self.temp_dir)

        res = self.exc("dir2pi", ["-S", self.temp_dir])
        self.assertEqual(res, 0)
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.temp_dir, "simple"))),
            ["index.html", "normalization-test-with-dashes"],
        )

    @needs_symlinks
    def test_recursive_scan_follows_symlinked_directories(self):
        # packages are often kept outside PACKAGE_DIR and linked into it, so
        # --recursive has to look inside a linked directory too
        store = tempfile.mkdtemp(prefix="pip2pi-tests-store")
        self.addCleanup(shutil.rmtree, store, ignore_errors=True)
        build_dir = os.path.join(store, "flask", "v3.1.3")
        os.makedirs(build_dir)
        shutil.copy("assets/six-1.10.0-py2.py3-none-any.whl", build_dir)
        shutil.copy("assets/wheel-0.29.0-py2.py3-none-any.whl",
                    self.temp_dir)
        os.symlink(os.path.join(store, "flask"),
                   os.path.join(self.temp_dir, "flask"),
                   target_is_directory=True)

        res = self.exc("dir2pi", ["-S", "--recursive", self.temp_dir])
        self.assertEqual(res, 0)
        simple = os.path.join(self.temp_dir, "simple")
        self.assertEqual(
            sorted(os.listdir(simple)), ["index.html", "six", "wheel"],
        )
        # six lives two levels down, in the linked flask/v3.1.3/
        self.assertEqual(
            sorted(os.listdir(os.path.join(simple, "six"))),
            ["index.html", "six-1.10.0-py2.py3-none-any.whl"],
        )

    @needs_symlinks
    def test_recursive_scan_does_not_follow_a_link_back_up_the_tree(self):
        # a link pointing at PACKAGE_DIR (or at itself) would otherwise make
        # the scan run forever
        shutil.copy("assets/six-1.10.0-py2.py3-none-any.whl", self.temp_dir)
        os.symlink(self.temp_dir, os.path.join(self.temp_dir, "loop"),
                   target_is_directory=True)
        os.symlink(os.curdir, os.path.join(self.temp_dir, "self"),
                   target_is_directory=True)

        res = self.exc("dir2pi", ["-S", "--recursive", self.temp_dir])
        self.assertEqual(res, 0)
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.temp_dir, "simple"))),
            ["index.html", "six"],
        )

    @needs_symlinks
    def test_symlinked_directories_are_not_scanned_without_recursive(self):
        # without --recursive the top level is all that is indexed, and a link
        # to a directory does not change that
        os.makedirs(os.path.join(self.temp_dir, "flask"))
        shutil.copy("assets/six-1.10.0-py2.py3-none-any.whl",
                    os.path.join(self.temp_dir, "flask"))
        os.symlink(os.path.join(self.temp_dir, "flask"),
                   os.path.join(self.temp_dir, "linked-flask"),
                   target_is_directory=True)
        shutil.copy("assets/wheel-0.29.0-py2.py3-none-any.whl",
                    self.temp_dir)

        res = self.exc("dir2pi", ["-S", self.temp_dir])
        self.assertEqual(res, 0)
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.temp_dir, "simple"))),
            ["index.html", "wheel"],
        )

    @needs_symlinks
    def test_exclude_dir_applies_to_symlinked_directories(self):
        store = tempfile.mkdtemp(prefix="pip2pi-tests-store")
        self.addCleanup(shutil.rmtree, store, ignore_errors=True)
        os.makedirs(os.path.join(store, "nightly"))
        shutil.copy("assets/six-1.10.0-py2.py3-none-any.whl",
                    os.path.join(store, "nightly"))
        os.symlink(os.path.join(store, "nightly"),
                   os.path.join(self.temp_dir, "nightly"),
                   target_is_directory=True)

        res = self.exc("dir2pi", [
            "-S", "--recursive", "--exclude-dir", "nightly", self.temp_dir,
        ])
        self.assertEqual(res, 0)
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.temp_dir, "simple"))),
            ["index.html"],
        )

    @needs_symlinks
    def test_symlinked_package_files_are_indexed(self):
        # a link to an archive is indexed like the archive itself, with or
        # without --recursive
        store = tempfile.mkdtemp(prefix="pip2pi-tests-store")
        self.addCleanup(shutil.rmtree, store, ignore_errors=True)
        target = os.path.join(store, "six-1.10.0-py2.py3-none-any.whl")
        shutil.copy("assets/six-1.10.0-py2.py3-none-any.whl", target)
        os.symlink(target, os.path.join(
            self.temp_dir, "six-1.10.0-py2.py3-none-any.whl",
        ))

        res = self.exc("dir2pi", ["-S", self.temp_dir])
        self.assertEqual(res, 0)
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.temp_dir, "simple", "six"))),
            ["index.html", "six-1.10.0-py2.py3-none-any.whl"],
        )

    def test_simple_index_is_sorted_by_package_name(self):
        for asset in ("wheel-0.29.0-py2.py3-none-any.whl",
                      "six-1.10.0-py2.py3-none-any.whl",
                      "Normalization_Test.with-dashes-1.2rc3.zip",
                      "fish-1.1.tar.gz"):
            shutil.copy(os.path.join("assets", asset), self.temp_dir)

        res = self.exc("dir2pi", ["-S", self.temp_dir])
        self.assertEqual(res, 0)
        with open(os.path.join(self.temp_dir, "simple", "index.html")) as fp:
            self.assertEqual(fp.read(), (
                "<html><head><title>Simple Index</title>"
                "<meta name='api-version' value='2' /></head><body>\n"
                "<a href='fish/'>fish</a><br />\n"
                "<a href='normalization-test-with-dashes/'>"
                "Normalization-Test.with-dashes</a><br />\n"
                "<a href='six/'>six</a><br />\n"
                "<a href='wheel/'>wheel</a><br />\n"
                "</body></html>\n"
            ))

    def test_duplicate_package_files_keep_the_last_one(self):
        # nightly builds keep a directory per build, so the same file name shows
        # up several times; the last one in lexicographic order must win.
        builds = (
            ("20260919", "main_20260919001013_c37ac1ab"),
            ("20260920", "main_20260920001015_c37ac1ab"),
            ("20260920", "main_20260920201628_0ca324ce"),
            ("20260923", "main_20260923001012_2060b5da"),
            ("20260923", "main_20260923191512_67cb1ed7"),
        )
        for day, build in builds:
            build_dir = os.path.join(self.temp_dir, "nightly", day, build)
            os.makedirs(build_dir)
            filename = ("datagallery_dataagent-0.2.0.dev%s-py3-none-any.whl"
                        % (day, ))
            with open(os.path.join(build_dir, filename), "w") as fp:
                fp.write(build)

        res = self.exc("dir2pi", ["-S", "--recursive", self.temp_dir])
        self.assertEqual(res, 0)

        simple = os.path.join(self.temp_dir, "simple", "datagallery-dataagent")
        self.assertEqual(sorted(os.listdir(simple)), [
            "datagallery_dataagent-0.2.0.dev20260919-py3-none-any.whl",
            "datagallery_dataagent-0.2.0.dev20260920-py3-none-any.whl",
            "datagallery_dataagent-0.2.0.dev20260923-py3-none-any.whl",
            "index.html",
        ])
        for filename, winner in (
            ("datagallery_dataagent-0.2.0.dev20260919-py3-none-any.whl",
             "main_20260919001013_c37ac1ab"),
            ("datagallery_dataagent-0.2.0.dev20260920-py3-none-any.whl",
             "main_20260920201628_0ca324ce"),
            ("datagallery_dataagent-0.2.0.dev20260923-py3-none-any.whl",
             "main_20260923191512_67cb1ed7"),
        ):
            with open(os.path.join(simple, filename)) as fp:
                self.assertEqual(fp.read(), winner)

    def test_exclude_dir_nightly(self):
        build_dir = os.path.join(
            self.temp_dir,
            "nightly", "20260923", "main_20260923191512_67cb1ed7",
        )
        os.makedirs(build_dir)
        build_file = os.path.join(build_dir, "night-0.1-py3-none-any.whl")
        with open(build_file, "w") as fp:
            fp.write("nightly build\n")
        shutil.copy("assets/six-1.10.0-py2.py3-none-any.whl", self.temp_dir)

        res = self.exc("dir2pi", [
            "-S", "--recursive", "--exclude-dir", "nightly", self.temp_dir,
        ])
        self.assertEqual(res, 0)
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.temp_dir, "simple"))),
            ["index.html", "six"],
        )

    def test_exclude_dir_matches_nested_directories(self):
        nightly = os.path.join(self.temp_dir, "foo", "nightly")
        release = os.path.join(self.temp_dir, "foo", "release")
        for path in (nightly, release):
            os.makedirs(path)
        shutil.copy("assets/six-1.10.0-py2.py3-none-any.whl", nightly)
        shutil.copy("assets/wheel-0.29.0-py2.py3-none-any.whl", release)

        res = self.exc("dir2pi", [
            "-S", "--recursive", "--exclude-dir", "nightly", self.temp_dir,
        ])
        self.assertEqual(res, 0)
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.temp_dir, "simple"))),
            ["index.html", "wheel"],
        )

    def test_exclude_dir_with_path_pattern(self):
        for day in ("20260919", "20260920"):
            build_dir = os.path.join(self.temp_dir, "nightly", day, "build")
            os.makedirs(build_dir)
            filename = "night-0.1.dev%s-py3-none-any.whl" % (day, )
            with open(os.path.join(build_dir, filename), "w") as fp:
                fp.write(day)

        res = self.exc("dir2pi", [
            "-S", "--recursive", "--exclude-dir", "nightly/20260920",
            self.temp_dir,
        ])
        self.assertEqual(res, 0)
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.temp_dir, "simple", "night"))),
            ["index.html", "night-0.1.dev20260919-py3-none-any.whl"],
        )

    def test_exclude_dir_may_be_given_more_than_once(self):
        for dirname, asset in (
            ("nightly", "six-1.10.0-py2.py3-none-any.whl"),
            ("old", "wheel-0.29.0-py2.py3-none-any.whl"),
        ):
            os.makedirs(os.path.join(self.temp_dir, dirname))
            shutil.copy(os.path.join("assets", asset),
                        os.path.join(self.temp_dir, dirname))
        shutil.copy("assets/Normalization_Test.with-dashes-1.2rc3.zip",
                    self.temp_dir)

        res = self.exc("dir2pi", [
            "-S", "--recursive", "--exclude-dir", "nightly",
            "--exclude-dir", "old", self.temp_dir,
        ])
        self.assertEqual(res, 0)
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.temp_dir, "simple"))),
            ["index.html", "normalization-test-with-dashes"],
        )

    def test_exclude_file_index_html_at_top_level(self):
        shutil.copy("assets/six-1.10.0-py2.py3-none-any.whl", self.temp_dir)
        with open(os.path.join(self.temp_dir, "index.html"), "w") as fp:
            fp.write("<html></html>\n")

        # without the option an unexpected file name is still an error
        with self.assertRaises(pip2pi_commands.InvalidFilePackageName):
            self.exc("dir2pi", ["-S", self.temp_dir])

        res = self.exc("dir2pi", [
            "-S", "--exclude-file", "index.html", self.temp_dir,
        ])
        self.assertEqual(res, 0)
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.temp_dir, "simple"))),
            ["index.html", "six"],
        )

    def test_exclude_file_may_be_given_more_than_once(self):
        for asset in ("six-1.10.0-py2.py3-none-any.whl",
                      "wheel-0.29.0-py2.py3-none-any.whl"):
            shutil.copy(os.path.join("assets", asset), self.temp_dir)
        for filename in ("index.html", "pypi.lock"):
            with open(os.path.join(self.temp_dir, filename), "w") as fp:
                fp.write("not a package\n")

        res = self.exc("dir2pi", [
            "-S", "--exclude-file", "index.html",
            "--exclude-file", "pypi.lock", self.temp_dir,
        ])
        self.assertEqual(res, 0)
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.temp_dir, "simple"))),
            ["index.html", "six", "wheel"],
        )

    def test_exclude_file_applies_without_recursive(self):
        # --exclude-dir needs --recursive, --exclude-file does not: the top
        # level is always scanned.
        os.makedirs(os.path.join(self.temp_dir, "build"))
        shutil.copy("assets/six-1.10.0-py2.py3-none-any.whl", self.temp_dir)
        for directory in (self.temp_dir, os.path.join(self.temp_dir, "build")):
            with open(os.path.join(directory, "index.html"), "w") as fp:
                fp.write("<html></html>\n")

        res = self.exc("dir2pi", [
            "-S", "--exclude-file", "index.html", self.temp_dir,
        ])
        self.assertEqual(res, 0)
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.temp_dir, "simple"))),
            ["index.html", "six"],
        )
        # the file is only skipped, never deleted
        self.assertTrue(
            os.path.exists(os.path.join(self.temp_dir, "index.html")))

    def test_exclude_file_glob(self):
        shutil.copy("assets/six-1.10.0-py2.py3-none-any.whl", self.temp_dir)
        for filename in ("index.html", "pypi.lock", "six-1.10.0.tar.gz"):
            with open(os.path.join(self.temp_dir, filename), "w") as fp:
                fp.write("not a package\n")

        res = self.exc("dir2pi", [
            "-S", "--exclude-file", "*.html", "--exclude-file", "*.lock",
            self.temp_dir,
        ])
        self.assertEqual(res, 0)
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.temp_dir, "simple", "six"))),
            ["index.html", "six-1.10.0-py2.py3-none-any.whl",
             "six-1.10.0.tar.gz"],
        )

    def test_wheels(self):
        res = self.exc("pip2tgz", [
            self.temp_dir,
            self.index_url,
            "wheel",
        ])
        self.assertEqual(res, 0)
        self.assertDirContents('expected-test_wheels.txt', self.temp_dir)

    @needs_heavy_tests
    def test_get_source_with_wheels(self):
        res = self.exc("pip2pi", [
            self.temp_dir,
            self.index_url,
            "-z", "six",
        ])
        self.assertEqual(res, 0)
        self.assertDirContents('expected-test_get_source_with_wheels.txt', self.temp_dir)


class TestIsRemoteTarget(unittest.TestCase):
    remote_targets = [
        "a:b",
        "a:/bar",
        "foo:bar",
    ]

    local_targets = [
        "foo",
        "c:\\foo\\bar",
        "z:\\things",
    ]

    def test_remote_target(self):
        for target in self.remote_targets:
            assert pip2pi_commands.is_remote_target(target), \
                    "%r is remote" %(target, )

    def test_local_target(self):
        for target in self.local_targets:
            assert not pip2pi_commands.is_remote_target(target), \
                    "%r is local" %(target, )


class TestExcludeDirPatterns(unittest.TestCase):
    def test_pattern_without_separator_matches_at_any_depth(self):
        patterns = ("nightly", )
        self.assertTrue(pip2pi_commands.is_excluded_dir("nightly", patterns))
        self.assertTrue(
            pip2pi_commands.is_excluded_dir("foo/nightly", patterns))
        self.assertTrue(
            pip2pi_commands.is_excluded_dir("a/b/nightly", patterns))
        self.assertFalse(
            pip2pi_commands.is_excluded_dir("nightly2", patterns))
        self.assertFalse(pip2pi_commands.is_excluded_dir("foo", patterns))

    def test_pattern_with_separator_matches_the_relative_path(self):
        patterns = ("nightly/20260923", )
        self.assertTrue(
            pip2pi_commands.is_excluded_dir("nightly/20260923", patterns))
        self.assertFalse(
            pip2pi_commands.is_excluded_dir("nightly/20260924", patterns))
        # a path pattern is not a prefix: the nightly directory itself is kept
        self.assertFalse(
            pip2pi_commands.is_excluded_dir("nightly", patterns))

    def test_patterns_are_case_sensitive(self):
        self.assertFalse(
            pip2pi_commands.is_excluded_dir("Nightly", ("nightly", )))

    def test_normalize(self):
        self.assertEqual(
            pip2pi_commands.normalize_exclude_patterns(
                ["nightly/", "/nightly", "nightly\\build"],
            ),
            ("nightly", "nightly", "nightly/build"),
        )
        self.assertEqual(
            pip2pi_commands.normalize_exclude_patterns(None), ())
        with self.assertRaises(ValueError):
            pip2pi_commands.normalize_exclude_patterns(["/"])

    def test_iter_package_files_honours_exclude_dirs(self):
        temp_dir = tempfile.mkdtemp(prefix="pip2pi-tests")
        self.addCleanup(shutil.rmtree, temp_dir)
        build_dir = os.path.join(temp_dir, "nightly", "build")
        os.makedirs(build_dir)
        for filepath in (
            os.path.join(build_dir, "night-0.1-py3-none-any.whl"),
            os.path.join(temp_dir, "six-1.10.0-py2.py3-none-any.whl"),
        ):
            with open(filepath, "w") as fp:
                fp.write("not a real wheel\n")

        def found(**kwargs):
            return sorted(
                os.path.basename(path) for path in
                pip2pi_commands.iter_package_files(temp_dir, **kwargs)
            )

        # only the top level is scanned unless recursive is asked for
        self.assertEqual(found(), ["six-1.10.0-py2.py3-none-any.whl"])
        self.assertEqual(found(recursive=True), [
            "night-0.1-py3-none-any.whl", "six-1.10.0-py2.py3-none-any.whl",
        ])
        self.assertEqual(found(recursive=True, exclude_dirs=("nightly", )), [
            "six-1.10.0-py2.py3-none-any.whl",
        ])


class TestExcludeFilePatterns(unittest.TestCase):
    def test_pattern_without_separator_matches_a_file_name_at_any_depth(self):
        patterns = ("index.html", )
        self.assertTrue(
            pip2pi_commands.is_excluded_file("index.html", patterns))
        self.assertTrue(
            pip2pi_commands.is_excluded_file("foo/index.html", patterns))
        self.assertTrue(
            pip2pi_commands.is_excluded_file("a/b/index.html", patterns))
        # not a prefix match
        self.assertFalse(
            pip2pi_commands.is_excluded_file("index.html.bak", patterns))
        self.assertFalse(
            pip2pi_commands.is_excluded_file("foo", patterns))

    def test_pattern_with_separator_matches_the_relative_path(self):
        patterns = ("foo/index.html", )
        self.assertTrue(
            pip2pi_commands.is_excluded_file("foo/index.html", patterns))
        self.assertFalse(
            pip2pi_commands.is_excluded_file("foo/bar/index.html", patterns))
        self.assertFalse(
            pip2pi_commands.is_excluded_file("index.html", patterns))

    def test_wildcards_match_file_names(self):
        self.assertTrue(
            pip2pi_commands.is_excluded_file("index.html", ("*.html", )))
        self.assertTrue(
            pip2pi_commands.is_excluded_file("pypi.lock", ("*.lock", )))
        self.assertFalse(
            pip2pi_commands.is_excluded_file("pypi.lock", ("*.html", )))
        # '*' may match '/' too, as for --exclude-dir
        self.assertTrue(
            pip2pi_commands.is_excluded_file(
                "foo/bar/index.html", ("*/index.html", )))

    def test_patterns_are_case_sensitive(self):
        self.assertFalse(
            pip2pi_commands.is_excluded_file("Index.html", ("index.html", )))

    def test_iter_package_files_honours_exclude_files(self):
        temp_dir = tempfile.mkdtemp(prefix="pip2pi-tests")
        self.addCleanup(shutil.rmtree, temp_dir)
        os.makedirs(os.path.join(temp_dir, "build"))
        for filepath in (
            os.path.join(temp_dir, "index.html"),
            os.path.join(temp_dir, "build", "index.html"),
            os.path.join(temp_dir, "six-1.10.0-py2.py3-none-any.whl"),
        ):
            with open(filepath, "w") as fp:
                fp.write("not a real package\n")

        def found(**kwargs):
            return sorted(
                os.path.basename(path) for path in
                pip2pi_commands.iter_package_files(temp_dir, **kwargs)
            )

        # index.html in a sub-directory is filtered by extension anyway, but
        # the top level file is only dropped when it is asked for
        self.assertEqual(found(), [
            "index.html", "six-1.10.0-py2.py3-none-any.whl",
        ])
        self.assertEqual(found(exclude_files=("index.html", )), [
            "six-1.10.0-py2.py3-none-any.whl",
        ])
        self.assertEqual(
            found(recursive=True, exclude_files=("index.html", )),
            ["six-1.10.0-py2.py3-none-any.whl"],
        )

    def test_iter_package_files_reports_excluded_files_when_verbose(self):
        import contextlib
        import io
        temp_dir = tempfile.mkdtemp(prefix="pip2pi-tests")
        self.addCleanup(shutil.rmtree, temp_dir)
        with open(os.path.join(temp_dir, "index.html"), "w") as fp:
            fp.write("<html></html>\n")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            list(pip2pi_commands.iter_package_files(
                temp_dir, exclude_files=("index.html", ), verbose=True,
            ))
        self.assertEqual(out.getvalue(), "excluding index.html\n")


def load_tests(loader, tests, ignore):
    tests.addTests(doctest.DocTestSuite(
        pip2pi_commands,
        optionflags=doctest.ELLIPSIS|doctest.IGNORE_EXCEPTION_DETAIL
    ))
    return tests


if __name__ == "__main__":
    unittest.main()
