import os
import re
import sys
import shutil
import atexit
import tempfile
import warnings
import textwrap
import fnmatch
import functools
from subprocess import check_call
import glob
import optparse

try:
    from html import escape
except ImportError:
    from cgi import escape

try:
    import wheel as _; _
    has_wheel = True
except ImportError:
    has_wheel = False


def try_int(x):
    try:
        return int(x)
    except ValueError:
        return x

pip_download_command = 'download --dest'
pip_no_binary_command = '--no-binary=:all:'

def dedent(text):
    return textwrap.dedent(text.lstrip("\n")).rstrip()

def maintain_cwd(f):
    @functools.wraps(f)
    def maintain_cwd_helper(*args, **kwargs):
        orig_dir = os.getcwd()
        try:
            return f(*args, **kwargs)
        finally:
            os.chdir(orig_dir)
    return maintain_cwd_helper

class InvalidFilePackageName(ValueError):
    def __init__(self, file, basedir=None):
        msg = "unexpected file name: %r " %(file, )
        msg += "(not in 'pkg-name-version.xxx' format"
        if basedir:
            msg += "; found in directory: %r" %(basedir)
        msg += ")"
        super(InvalidFilePackageName, self).__init__(msg)


def safe_name(name):
    """Convert an arbitrary string to a standard distribution name

    Any runs of non-alphanumeric/. characters are replaced with a single '-'.
    Copied from pkg_resources.
    """
    return re.sub('[^A-Za-z0-9.]+', '-', name)

# Copied from pkg_resources.
EGG_NAME = re.compile(
    r"""
    (?P<name>[^-]+) (
        -(?P<ver>[^-]+) (
            -py(?P<pyver>[^-]+) (
                -(?P<plat>.+)
            )?
        )?
    )?
    """,
    re.VERBOSE | re.IGNORECASE,
).match


def egg_project_name(filename):
    project_name, version, py_version, platform = [None] * 4
    basename, ext = os.path.splitext(filename)
    match = EGG_NAME(basename)
    if match:
        project_name = match.group("name")
    return safe_name(project_name or 'Unknown')


def file_to_package(file, basedir=None):
    """ Returns the package name for a given file, or raises an
        ``InvalidFilePackageName`` exception if the file name is
        not valid::

        >>> file_to_package("foo-1.2.3_rc1.tar.gz")
        ('foo', '1.2.3-rc1.tar.gz')
        >>> file_to_package("foo-bar-1.2.tgz")
        ('foo-bar', '1.2.tgz')
        >>> file_to_package("kafka-quixey-0.8.1-1.tar.gz")
        ('kafka-quixey', '0.8.1-1.tar.gz')
        >>> file_to_package("foo-bar-1.2-py27-none-any.whl")
        ('foo-bar', '1.2-py27-none-any.whl')
        >>> file_to_package("Cython-0.17.2-cp26-none-linux_x86_64.whl")
        ('Cython', '0.17.2-cp26-none-linux_x86_64.whl')
        >>> file_to_package("PyYAML-3.10-py2.7-macosx-10.7-x86_64.egg")
        ('PyYAML', '3.10-py2.7-macosx-10.7-x86_64.egg')
        >>> file_to_package("python_ldap-2.3.9-py2.7-macosx-10.3-fat.egg")
        ('python-ldap', '2.3.9-py2.7-macosx-10.3-fat.egg')
        >>> file_to_package("python_ldap-2.4.19-cp27-none-macosx_10_10_x86_64.whl")
        ('python-ldap', '2.4.19-cp27-none-macosx_10_10_x86_64.whl')
        >>> file_to_package("foo.whl")
        Traceback (most recent call last):
            ...
        InvalidFilePackageName: unexpected file name: 'foo.whl' (not in 'pkg-name-version.xxx' format)
        >>> file_to_package("foo.png")
        Traceback (most recent call last):
            ...
        InvalidFilePackageName: unexpected file name: 'foo.png' (not in 'pkg-name-version.xxx' format)
        """
    file = os.path.basename(file)
    file_ext = os.path.splitext(file)[1].lower()
    if file_ext == ".egg":
        name = egg_project_name(file)
        split = (name, file[len(name)+1:])
        to_safe_name = lambda x: x
        to_safe_rest = lambda x: x
    elif file_ext == ".whl":
        bits = file.rsplit("-", 4)
        split = (bits[0], "-".join(bits[1:]))
        to_safe_name = safe_name
        to_safe_rest = lambda x: x
    else:
        match = re.search(r"(?P<pkg>.*?)-(?P<rest>\d+.*)", file)
        if not match:
            raise InvalidFilePackageName(file, basedir)
        split = (match.group("pkg"), match.group("rest"))
        to_safe_name = safe_name
        to_safe_rest = safe_name

    if len(split) != 2 or not split[1]:
        raise InvalidFilePackageName(file, basedir)

    return (to_safe_name(split[0]), to_safe_rest(split[1]))


def pip_run_command(pip_args):
    check_call(["pip"] + pip_args)

OS_HAS_SYMLINK = hasattr(os, "symlink")


class Pip2PiOptionParser(optparse.OptionParser):
    def format_description(self, formatter):
        # Parent implementation reformats all the hard line breaks
        return self.get_description() + "\n"

    def add_index_options(self):
        self.add_option(
            '-n', '--normalize-package-names', dest="normalize_package_names",
            default=True, action="store_true",
            help=dedent("""
                Normalize package names in the simple index using whatever
                scheme is supported by the most recent version of pip (default).
            """))
        self.add_option(
            '-N', '--no-normalize-package-names', dest="normalize_package_names",
            action="store_false")
        self.add_option(
            '-a', '--aggressive-normalization', dest="aggressive_normalization",
            default=False, action="store_true",
            help=dedent("""
                Aggressive create symlinks to many different package name
                normalizations to support multiple pip versions (for a package
                named "Spam_Eggs--cheese.shop", the following normalizations
                will be used: "spam-eggs-cheese-shop" (PEP-503),
                "spam-eggs-cheese.shop" (pip 6, pip 7),
                "Spam_Eggs--cheese.shop" (pip < 6)).
            """))
        self.add_option(
            '--recursive', dest="recursive", default=False,
            action="store_true",
            help=dedent("""
                Also scan sub-directories of PACKAGE_DIR, at any depth, for
                packages. Not enabled by default: without this option only the
                top level of PACKAGE_DIR is scanned. Files in sub-directories
                are only indexed when their name ends in one of: .whl, .egg,
                .zip, .tgz, .tar, .tar.gz, .tar.bz2, .tar.xz.
            """))
        self.add_option(
            '--exclude-dir', dest="exclude_dirs", metavar="PATTERN",
            action="append", default=None,
            help=dedent("""
                Skip any directory whose path below PACKAGE_DIR matches the
                shell-style glob PATTERN (case sensitive; '*' may match '/'
                too). A PATTERN without '/' matches a directory name at any
                depth, so --exclude-dir nightly skips both PACKAGE_DIR/nightly
                and PACKAGE_DIR/foo/nightly, while --exclude-dir
                'nightly/20260923' skips one nightly build. May be given more
                than once. Only has an effect with --recursive.
            """))
        self.add_option(
            '--exclude-file', dest="exclude_files", metavar="PATTERN",
            action="append", default=None,
            help=dedent("""
                Skip any file whose path below PACKAGE_DIR matches the
                shell-style glob PATTERN (case sensitive; '*' may match '/'
                too). A PATTERN without '/' matches a file name at any depth,
                so --exclude-file index.html skips both PACKAGE_DIR/index.html
                and PACKAGE_DIR/foo/index.html, while --exclude-file
                'foo/index.html' skips one of them. May be given more than
                once. Unlike --exclude-dir, this option also applies without
                --recursive, since the top level of PACKAGE_DIR is always
                scanned.
            """))
        self.add_option(
            '-x', '--no-build-html', dest="build_html", action="store_false",
            default=True, help=dedent("""
                Don't build index files for each directory, you might want to
                to disable this if your server dynamically builds index files
                using something like mod_autoindex and you want to make older
                package/module versions continue to be available.
            """))
        self.add_option(
            '-z', '--also-get-source', dest="get_source", action="store_true",
            default=False, help=dedent("""
                In addition to downloading wheels, eggs or any other package
                format also download the source tar.gz files in case the
                platform using this index does not support the wheel/egg/etc
            """))
        self.add_option(
            '-s', '--symlink', dest="use_symlink",
            default=OS_HAS_SYMLINK, action="store_true",
            help=dedent("""
                Use symlinks in PACKAGE_DIR/simple/ rather than copying files.
                Default: %default
            """))
        self.add_option(
            '-S', '--no-symlink', dest="use_symlink", action="store_false")
        self.add_option(
            '-v', '--verbose', dest="verbose", action="store_true")

    def _process_args(self, largs, rargs, values):
        """
        An unknown option pass-through implementation of OptionParser.

        When unknown arguments are encountered, bundle with largs and try again,
        until rargs is depleted.

        sys.exit(status) will still be called if a known argument is passed
        incorrectly (e.g. missing arguments or bad argument types, etc.)

        From http://stackoverflow.com/a/9307174/6364
        """
        while rargs:
            try:
                optparse.OptionParser._process_args(self, largs, rargs, values)
            except (optparse.BadOptionError, optparse.AmbiguousOptionError) as e:
                largs.append(e.opt_str)


def dir2pi(argv=sys.argv, use_symlink=None):
    parser = Pip2PiOptionParser(
        usage="usage: %prog PACKAGE_DIR",
        description=dedent("""
            Creates the directory PACKAGE_DIR/simple/ and populates it with the
            directory structure required to use with pip's --index-url.

            Assumes that PACKAGE_DIR (and, with --recursive, its
            sub-directories) contain a bunch of archives named
            'package-name-version.ext' (ex 'foo-2.1.tar.gz' or
            'foo-bar-1.3rc1.bz2').

            This makes the most sense if PACKAGE_DIR is somewhere inside a
            webserver's htdocs directory.

            For example:

                $ ls packages/
                foo-1.2.tar.gz
                $ dir2pi packages/
                $ find packages/
                packages/
                packages/foo-1.2.tar.gz
                packages/simple/
                packages/simple/foo/
                packages/simple/foo/index.html
                packages/simple/foo/foo-1.2.tar.gz
        """))

    if use_symlink is not None:
        warnings.warn("dir2pi(use_symlink=...) is deprecated", stacklevel=1)

    parser.add_index_options()

    option, argv = parser.parse_args(argv)
    if option.aggressive_normalization and not option.use_symlink:
        sys.stderr.write(
            "Error: cannot use --aggressive-normalization without --symlink\n")
        return 1

    if len(argv) != 2:
        parser.print_help()
        parser.exit()
    return _dir2pi(option, argv)

def normalize_pep503(pkg_name):
    # As per https://www.python.org/dev/peps/pep-0503/#normalized-names
    return re.sub(r"[-_.]+", "-", pkg_name).lower()

def normalize_pip67(pkg_name):
    # Normalize for pip 6 and 7
    return re.sub(r"([-.])+", r"\1", pkg_name.replace("_", "-")).lower()

def try_symlink(option, source, target):
    try:
        os.symlink(source, target)
        if option.verbose:
            print('linking %s to %s' %(source, target))
    except OSError as e:
        if e.errno == 17:
            # ignore errors when the file exists
            return
        sys.stderr.write(
            'ERROR linking %s to %s (skipping): %s\n'
            %(source, target, e)
        )


# Files in sub-directories are only treated as packages when their name looks
# like a package archive: a directory full of downloads often also holds a
# README, an index.html, etc. The top level keeps the historical behaviour of
# treating every file it finds as a package.
PACKAGE_FILE_EXTS = (
    ".whl", ".egg", ".zip", ".tgz", ".tar", ".tar.gz", ".tar.bz2", ".tar.xz",
)

def is_package_file(filename):
    return filename.lower().endswith(PACKAGE_FILE_EXTS)


def normalize_exclude_patterns(patterns):
    """ Normalizes the values of --exclude-dir to 'a/b' form, without slashes
        at either end: 'nightly/', '/nightly' and 'nightly' are the same
        pattern.
    """
    result = []
    for raw in patterns or ():
        pattern = raw.replace("\\", "/").strip().strip("/")
        if not pattern:
            raise ValueError("empty --exclude-dir pattern: %r" %(raw, ))
        result.append(pattern)
    return tuple(result)


def is_excluded_path(relpath, patterns):
    """ Tells whether the directory or file at ``relpath`` (relative to
        PACKAGE_DIR) is excluded by one of ``patterns``.

        Patterns are shell-style globs matched, case sensitively, against the
        relative path with '/' as the separator. A pattern without a '/' is
        matched against the last path component (the directory or file name)
        only, at any depth.
    """
    relpath = relpath.replace(os.sep, "/")
    name = relpath.rsplit("/", 1)[-1]
    for pattern in patterns:
        if "/" in pattern:
            if fnmatch.fnmatchcase(relpath, pattern):
                return True
        elif fnmatch.fnmatchcase(name, pattern):
            return True
    return False


def is_excluded_dir(relpath, patterns):
    """ Tells whether the directory at ``relpath`` (relative to PACKAGE_DIR)
        is excluded by one of ``patterns``.

        See ``is_excluded_path`` for the matching rules.
    """
    return is_excluded_path(relpath, patterns)


def is_excluded_file(relpath, patterns):
    """ Tells whether the file at ``relpath`` (relative to PACKAGE_DIR) is
        excluded by one of ``patterns``.

        See ``is_excluded_path`` for the matching rules.
    """
    return is_excluded_path(relpath, patterns)


def iter_package_files(pkgdir, recursive=False, skip_dirs=(), exclude_dirs=(),
                       exclude_files=(), verbose=False):
    """ Yields the packages to index, depth first.

        The top level of ``pkgdir`` is always scanned. Sub-directories are
        scanned too when ``recursive`` is true (it is not by default), but only
        files that look like a package archive are taken from them. Hidden
        files and directories are ignored, and so is anything in ``skip_dirs``
        (absolute paths -- the generated ``simple`` directory) or matching one
        of the ``exclude_dirs`` / ``exclude_files`` patterns (see
        ``is_excluded_path``). Skipped files are reported on stdout when
        ``verbose`` is true.
    """
    skip_dirs = set(os.path.abspath(x) for x in skip_dirs)
    exclude_dirs = tuple(exclude_dirs or ())
    exclude_files = tuple(exclude_files or ())

    def keep(dirpath, dirname):
        if dirname.startswith("."):
            return False
        subdirpath = os.path.join(dirpath, dirname)
        if os.path.abspath(subdirpath) in skip_dirs:
            return False
        return not is_excluded_dir(
            os.path.relpath(subdirpath, pkgdir), exclude_dirs,
        )

    def keep_file(dirpath, filename):
        relpath = os.path.relpath(os.path.join(dirpath, filename), pkgdir)
        if not is_excluded_file(relpath, exclude_files):
            return True
        if verbose:
            print('excluding %s' %(relpath, ))
        return False

    if not recursive:
        for filename in sorted(os.listdir(pkgdir)):
            filepath = os.path.join(pkgdir, filename)
            if filename.startswith(".") or not keep_file(pkgdir, filename):
                continue
            if os.path.isfile(filepath):
                yield filepath
        return

    for dirpath, dirnames, filenames in os.walk(pkgdir):
        at_top = os.path.abspath(dirpath) == os.path.abspath(pkgdir)
        dirnames[:] = sorted(
            dirname for dirname in dirnames if keep(dirpath, dirname)
        )
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            if not at_top and not is_package_file(filename):
                continue
            if not keep_file(dirpath, filename):
                continue
            filepath = os.path.join(dirpath, filename)
            if os.path.isfile(filepath):
                yield filepath


def _dir2pi(option, argv):
    pkgdir = argv[1]
    if not os.path.isdir(pkgdir):
        raise ValueError("no such directory: %r" %(pkgdir, ))
    pkgdirpath = lambda *x: os.path.join(pkgdir, *x)
    exclude_dirs = normalize_exclude_patterns(option.exclude_dirs)
    exclude_files = normalize_exclude_patterns(option.exclude_files)

    shutil.rmtree(pkgdirpath("simple"), ignore_errors=True)
    os.mkdir(pkgdirpath("simple"))
    pkg_index = ("<html><head><title>Simple Index</title>"
                 "<meta name='api-version' value='2' /></head><body>\n")

    # The same archive can be present more than once -- nightly builds keep a
    # directory per build -- and only one of them is indexed: the last one in
    # lexicographic order, so the newest build directory wins.
    pkg_files = {}
    for pkg_filepath in iter_package_files(
        pkgdir,
        recursive=option.recursive,
        skip_dirs=[pkgdirpath("simple")],
        exclude_dirs=exclude_dirs,
        exclude_files=exclude_files,
        verbose=option.verbose,
    ):
        pkg_basename = os.path.basename(pkg_filepath)
        pkg_name, pkg_rest = file_to_package(
            pkg_basename, os.path.dirname(pkg_filepath),
        )

        pkg_dir_name = pkg_name
        if option.normalize_package_names:
            pkg_dir_name = normalize_pep503(pkg_dir_name)

        pkg_key = (pkg_dir_name, pkg_basename)
        previous = pkg_files.get(pkg_key)
        if previous is None or (
            os.path.normpath(pkg_filepath) > os.path.normpath(previous[0])
        ):
            if previous is not None and option.verbose:
                print('replacing duplicate %s with %s' %(
                    previous[0], pkg_filepath,
                ))
            pkg_files[pkg_key] = (pkg_filepath, pkg_name)
        elif option.verbose:
            print('ignoring duplicate %s (keeping %s)' %(
                pkg_filepath, previous[0],
            ))

    processed_pkg = set()
    written_pkg_pages = set()
    index_links = []
    for (pkg_dir_name, pkg_basename), (pkg_filepath, pkg_name) in pkg_files.items():
        pkg_dir = pkgdirpath("simple", pkg_dir_name)
        if not os.path.exists(pkg_dir):
            os.mkdir(pkg_dir)

        if option.aggressive_normalization:
            try_symlink(option, pkg_dir_name, pkgdirpath("simple", normalize_pip67(pkg_name)))
            try_symlink(option, pkg_dir_name, pkgdirpath("simple", pkg_name))

        symlink_target = os.path.join(pkg_dir, pkg_basename)
        symlink_source = os.path.relpath(pkg_filepath, pkg_dir)
        if option.use_symlink:
            try_symlink(option, symlink_source, symlink_target)
        else:
            if option.verbose:
                print('copying %s to %s' % (symlink_target, pkg_filepath))
            shutil.copy2(pkg_filepath, symlink_target)

        if pkg_name not in processed_pkg:
            index_links.append((pkg_dir_name, pkg_name))
            processed_pkg.add(pkg_name)

        if option.build_html:
            pkg_page = os.path.join(pkg_dir, "index.html")
            if pkg_page not in written_pkg_pages:
                with open(pkg_page, "w") as fp:
                    fp.write(
                        "<html><head><title>Links for %(name)s</title>"
                        "<meta name='api-version' value='2' />"
                        "</head><body>\n"
                        "<h1>Links for %(name)s</h1>\n" %{
                            "name": escape(pkg_name),
                        }
                    )
                written_pkg_pages.add(pkg_page)
            with open(pkg_page, "a") as fp:
                fp.write("<a href='%(name)s'>%(name)s</a><br />\n" %{
                    "name": escape(pkg_basename),
                })
    # The order in which the archives are found depends on the filesystem (and
    # on the directory they live in), so the index is sorted here rather than
    # relying on the order of the scan: alphabetically, ignoring case.
    pkg_index += "".join(
        "<a href='%(dir)s/'>%(name)s</a><br />\n" %{
            "dir": escape(pkg_dir_name),
            "name": escape(pkg_name),
        }
        for pkg_dir_name, pkg_name in sorted(
            index_links, key=lambda link: (link[1].lower(), link[1]),
        )
    )
    pkg_index += "</body></html>\n"

    if option.build_html:
        with open(pkgdirpath("simple/index.html"), "w") as fp:
            fp.write(pkg_index)
        for pkg_page in written_pkg_pages:
            with open(pkg_page, "a") as fp:
                fp.write("</body></html>\n")

    return 0

def globall(globs):
    result = []
    for g in globs:
        result.extend(glob.glob(g))
    return result

@maintain_cwd
def pip2tgz(argv=sys.argv):
    glob_exts = ['*.whl', '*.tgz', '*.gz']
    parser = Pip2PiOptionParser(
        usage="usage: %prog OUTPUT_DIRECTORY [PIP_OPTIONS] PACKAGES ...",
        description=dedent("""
            Where PACKAGES are any names accepted by pip (ex, `foo`,
            `foo==1.2`, `-r requirements.txt`), and [PIP_OPTIONS] can be any
            options accepted by `pip %s`.

            pip2tgz will download all packages required to install PACKAGES and
            save them to sanely-named tarballs or wheel files in OUTPUT_DIRECTORY.

            For example:

                $ pip2tgz /var/www/packages/ -r requirements.txt foo==1.2 baz/
                $ pip2tgz /var/www/packages/ \\
                    %s \\
                    --index-url https://example.com/simple \\
                    bar==3.1
        """ % (pip_download_command, pip_no_binary_command)))

    option, argv = parser.parse_args(argv)
    if len(argv) < 3:
        parser.print_help()
        parser.exit()

    outdir = os.path.abspath(argv[1])
    if not os.path.exists(outdir):
        os.mkdir(outdir)

    full_glob_paths = [
        os.path.join(outdir, g) for g in glob_exts
    ]
    pkg_file_set = lambda: set(globall(full_glob_paths))
    old_pkgs = pkg_file_set()

    pip_run_command(pip_download_command.split() + [outdir] + argv[2:])

    os.chdir(outdir)
    new_pkgs = pkg_file_set() - old_pkgs
    num_pkgs = len(pkg_file_set() - old_pkgs)
    print("\nDone. %s new archives currently saved in %r." %(num_pkgs, argv[1]))
    return 0


WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:\\")

def is_remote_target(target):
    return ":" in target and not WINDOWS_PATH_RE.match(target)


def pip2pi(argv=sys.argv):
    parser = Pip2PiOptionParser(
        usage="usage: %prog TARGET [PIP_OPTIONS] PACKAGES ...",
        description=dedent("""
            Adds packages PACKAGES to PyPI-compatible package index at TARGET.

            If TARGET contains ':' it will be treated as a remote path. The
            package index will be built locally and rsync will be used to copy
            it to the remote host.

            PIP_OPTIONS can be any options accepted by `pip %s`, like
            `--index-url` or `%s`.

            For example, to create a remote index:

                $ pip2pi example.com:/var/www/packages/ -r requirements.txt

            To create a local index:

                $ pip2pi ~/Sites/packages/ foo==1.2

            To pass arguments to pip:

                $ pip2pi ~/Sites/packages/ \\
                    %s \\
                    --index-url https://example.com/simple \\
                    -r requirements-base.txt \\
                    -r requirements-dev.txt \\
                    bar==3.1

        """ % (pip_download_command, pip_no_binary_command, pip_no_binary_command)))
    parser.add_index_options()

    option, argv = parser.parse_args(argv)
    if len(argv) < 3:
        parser.print_help()
        parser.exit()

    target = argv[1]
    pip_argv = argv[2:]
    if is_remote_target(target):
        is_remote = True
        working_dir = tempfile.mkdtemp(prefix="pip2pi-working-dir")
        atexit.register(lambda: shutil.rmtree(working_dir))
    else:
        is_remote = False
        working_dir = os.path.abspath(target)

    subcmd_argv = [argv[0], working_dir] + pip_argv
    res = pip2tgz(subcmd_argv)
    if res:
        print("pip2tgz returned an error; aborting.")
        return res
    if option.get_source:
        subcmd_argv_source = subcmd_argv[:]
        subcmd_argv_source.insert(2, pip_no_binary_command)
        res = pip2tgz(subcmd_argv_source)
        if res:
            print("pip2tgz returned an error; aborting.")
            return res

    res = _dir2pi(option, subcmd_argv)
    if res:
        print("dir2pi returned an error; aborting.")
        return res

    if is_remote:
        print("copying temporary index at %r to %r..." %(working_dir, target))
        check_call([
            "rsync",
            "--recursive", "--progress", "--links",
            working_dir + "/", target + "/",
        ])
    return 0
