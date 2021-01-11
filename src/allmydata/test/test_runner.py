
from __future__ import (
    absolute_import,
)

import os.path, re, sys
from os import linesep

from eliot import (
    log_call,
)

from twisted.trial import unittest

from twisted.internet import reactor
from twisted.python import usage
from twisted.internet.defer import (
    inlineCallbacks,
    DeferredList,
)
from twisted.python.filepath import FilePath
from twisted.python.runtime import (
    platform,
)
from allmydata.util import fileutil, pollmixin
from allmydata.test import common_util
import allmydata
from .common import (
    PIPE,
    Popen,
)
from .common_util import (
    parse_cli,
    run_cli,
)
from .cli_node_api import (
    CLINodeAPI,
    Expect,
    on_stdout,
    on_stdout_and_stderr,
)
from ..util.eliotutil import (
    inline_callbacks,
)

def get_root_from_file(src):
    srcdir = os.path.dirname(os.path.dirname(os.path.normcase(os.path.realpath(src))))

    root = os.path.dirname(srcdir)
    if os.path.basename(srcdir) == 'site-packages':
        if re.search(r'python.+\..+', os.path.basename(root)):
            root = os.path.dirname(root)
        root = os.path.dirname(root)
    elif os.path.basename(root) == 'src':
        root = os.path.dirname(root)

    return root

srcfile = allmydata.__file__
rootdir = get_root_from_file(srcfile)


@log_call(action_type="run-bin-tahoe")
def run_bintahoe(extra_argv, python_options=None):
    """
    Run the main Tahoe entrypoint in a child process with the given additional
    arguments.

    :param [unicode] extra_argv: More arguments for the child process argv.

    :return: A three-tuple of stdout (unicode), stderr (unicode), and the
        child process "returncode" (int).
    """
    argv = [sys.executable]
    if python_options is not None:
        argv.extend(python_options)
    argv.extend([u"-m", u"allmydata.scripts.runner"])
    argv.extend(extra_argv)
    p = Popen(argv, stdout=PIPE, stderr=PIPE)
    out = p.stdout.read().decode("utf-8")
    err = p.stderr.read().decode("utf-8")
    returncode = p.wait()
    return (out, err, returncode)


class BinTahoe(common_util.SignalMixin, unittest.TestCase):
    def test_unicode_arguments_and_output(self):
        """
        The runner script receives unmangled non-ASCII values in argv.
        """
        tricky = u"\u2621"
        out, err, returncode = run_bintahoe([tricky])
        self.assertEqual(returncode, 1)
        self.assertIn(u"Unknown command: " + tricky, out)

    def test_with_python_options(self):
        """
        Additional options for the Python interpreter don't prevent the runner
        script from receiving the arguments meant for it.
        """
        # This seems like a redundant test for someone else's functionality
        # but on Windows we parse the whole command line string ourselves so
        # we have to have our own implementation of skipping these options.

        # -t is a harmless option that warns about tabs so we can add it
        # -without impacting other behavior noticably.
        out, err, returncode = run_bintahoe(["--version"], python_options=["-t"])
        self.assertEqual(returncode, 0)
        self.assertTrue(out.startswith(allmydata.__appname__ + '/'))

    def test_help_eliot_destinations(self):
        out, err, returncode = run_bintahoe([u"--help-eliot-destinations"])
        self.assertIn(u"\tfile:<path>", out)
        self.assertEqual(returncode, 0)

    def test_eliot_destination(self):
        out, err, returncode = run_bintahoe([
            # Proves little but maybe more than nothing.
            u"--eliot-destination=file:-",
            # Throw in *some* command or the process exits with error, making
            # it difficult for us to see if the previous arg was accepted or
            # not.
            u"--help",
        ])
        self.assertEqual(returncode, 0)

    def test_unknown_eliot_destination(self):
        out, err, returncode = run_bintahoe([
            u"--eliot-destination=invalid:more",
        ])
        self.assertEqual(1, returncode)
        self.assertIn(u"Unknown destination description", out)
        self.assertIn(u"invalid:more", out)

    def test_malformed_eliot_destination(self):
        out, err, returncode = run_bintahoe([
            u"--eliot-destination=invalid",
        ])
        self.assertEqual(1, returncode)
        self.assertIn(u"must be formatted like", out)

    def test_escape_in_eliot_destination(self):
        out, err, returncode = run_bintahoe([
            u"--eliot-destination=file:@foo",
        ])
        self.assertEqual(1, returncode)
        self.assertIn(u"Unsupported escape character", out)


class CreateNode(unittest.TestCase):
    # exercise "tahoe create-node" and "tahoe create-introducer" by calling
    # the corresponding code as a subroutine.

    def workdir(self, name):
        basedir = os.path.join("test_runner", "CreateNode", name)
        fileutil.make_dirs(basedir)
        return basedir

    @inlineCallbacks
    def do_create(self, kind, *args):
        basedir = self.workdir("test_" + kind)
        command = "create-" + kind
        is_client = kind in ("node", "client")
        tac = is_client and "tahoe-client.tac" or ("tahoe-" + kind + ".tac")

        n1 = os.path.join(basedir, command + "-n1")
        argv = ["--quiet", command, "--basedir", n1] + list(args)
        rc, out, err = yield run_cli(*argv)
        self.failUnlessEqual(err, "")
        self.failUnlessEqual(out, "")
        self.failUnlessEqual(rc, 0)
        self.failUnless(os.path.exists(n1))
        self.failUnless(os.path.exists(os.path.join(n1, tac)))

        if is_client:
            # tahoe.cfg should exist, and should have storage enabled for
            # 'create-node', and disabled for 'create-client'.
            tahoe_cfg = os.path.join(n1, "tahoe.cfg")
            self.failUnless(os.path.exists(tahoe_cfg))
            content = fileutil.read(tahoe_cfg).replace('\r\n', '\n')
            if kind == "client":
                self.failUnless(re.search(r"\n\[storage\]\n#.*\nenabled = false\n", content), content)
            else:
                self.failUnless(re.search(r"\n\[storage\]\n#.*\nenabled = true\n", content), content)
                self.failUnless("\nreserved_space = 1G\n" in content)

        # creating the node a second time should be rejected
        rc, out, err = yield run_cli(*argv)
        self.failIfEqual(rc, 0, str((out, err, rc)))
        self.failUnlessEqual(out, "")
        self.failUnless("is not empty." in err)

        # Fail if there is a non-empty line that doesn't end with a
        # punctuation mark.
        for line in err.splitlines():
            self.failIf(re.search("[\S][^\.!?]$", line), (line,))

        # test that the non --basedir form works too
        n2 = os.path.join(basedir, command + "-n2")
        argv = ["--quiet", command] + list(args) + [n2]
        rc, out, err = yield run_cli(*argv)
        self.failUnlessEqual(err, "")
        self.failUnlessEqual(out, "")
        self.failUnlessEqual(rc, 0)
        self.failUnless(os.path.exists(n2))
        self.failUnless(os.path.exists(os.path.join(n2, tac)))

        # test the --node-directory form
        n3 = os.path.join(basedir, command + "-n3")
        argv = ["--quiet", "--node-directory", n3, command] + list(args)
        rc, out, err = yield run_cli(*argv)
        self.failUnlessEqual(err, "")
        self.failUnlessEqual(out, "")
        self.failUnlessEqual(rc, 0)
        self.failUnless(os.path.exists(n3))
        self.failUnless(os.path.exists(os.path.join(n3, tac)))

        if kind in ("client", "node", "introducer"):
            # test that the output (without --quiet) includes the base directory
            n4 = os.path.join(basedir, command + "-n4")
            argv = [command] + list(args) + [n4]
            rc, out, err = yield run_cli(*argv)
            self.failUnlessEqual(err, "")
            self.failUnlessIn(" created in ", out)
            self.failUnlessIn(n4, out)
            self.failIfIn("\\\\?\\", out)
            self.failUnlessEqual(rc, 0)
            self.failUnless(os.path.exists(n4))
            self.failUnless(os.path.exists(os.path.join(n4, tac)))

        # make sure it rejects too many arguments
        self.failUnlessRaises(usage.UsageError, parse_cli,
                              command, "basedir", "extraarg")

        # when creating a non-client, there is no default for the basedir
        if not is_client:
            argv = [command]
            self.failUnlessRaises(usage.UsageError, parse_cli,
                                  command)

    def test_node(self):
        self.do_create("node", "--hostname=127.0.0.1")

    def test_client(self):
        # create-client should behave like create-node --no-storage.
        self.do_create("client")

    def test_introducer(self):
        self.do_create("introducer", "--hostname=127.0.0.1")

    def test_subcommands(self):
        # no arguments should trigger a command listing, via UsageError
        self.failUnlessRaises(usage.UsageError, parse_cli,
                              )


class RunNode(common_util.SignalMixin, unittest.TestCase, pollmixin.PollMixin):
    """
    exercise "tahoe run" for both introducer and client node, by spawning
    "tahoe run" as a subprocess. This doesn't get us line-level coverage, but
    it does a better job of confirming that the user can actually run
    "./bin/tahoe run" and expect it to work. This verifies that bin/tahoe sets
    up PYTHONPATH and the like correctly.
    """

    def workdir(self, name):
        basedir = os.path.join("test_runner", "RunNode", name)
        fileutil.make_dirs(basedir)
        return basedir

    @inline_callbacks
    def test_introducer(self):
        """
        The introducer furl is stable across restarts.
        """
        basedir = self.workdir("test_introducer")
        c1 = os.path.join(basedir, u"c1")
        tahoe = CLINodeAPI(reactor, FilePath(c1))
        self.addCleanup(tahoe.stop_and_wait)

        out, err, returncode = run_bintahoe([
            u"--quiet",
            u"create-introducer",
            u"--basedir", c1,
            u"--hostname", u"127.0.0.1",
        ])

        self.assertEqual(returncode, 0)

        # This makes sure that node.url is written, which allows us to
        # detect when the introducer restarts in _node_has_restarted below.
        config = fileutil.read(tahoe.config_file.path)
        self.assertIn('{}web.port = {}'.format(linesep, linesep), config)
        fileutil.write(
            tahoe.config_file.path,
            config.replace(
                '{}web.port = {}'.format(linesep, linesep),
                '{}web.port = 0{}'.format(linesep, linesep),
            )
        )

        p = Expect()
        tahoe.run(on_stdout(p))
        yield p.expect("introducer running")
        tahoe.active()

        yield self.poll(tahoe.introducer_furl_file.exists)

        # read the introducer.furl file so we can check that the contents
        # don't change on restart
        furl = fileutil.read(tahoe.introducer_furl_file.path)

        tahoe.active()

        # We don't keep track of PIDs in files on Windows.
        if not platform.isWindows():
            self.assertTrue(tahoe.twistd_pid_file.exists())
        self.assertTrue(tahoe.node_url_file.exists())

        # rm this so we can detect when the second incarnation is ready
        tahoe.node_url_file.remove()

        yield tahoe.stop_and_wait()

        p = Expect()
        tahoe.run(on_stdout(p))
        yield p.expect("introducer running")

        # Again, the second incarnation of the node might not be ready yet, so
        # poll until it is. This time introducer_furl_file already exists, so
        # we check for the existence of node_url_file instead.
        yield self.poll(tahoe.node_url_file.exists)

        # The point of this test!  After starting the second time the
        # introducer furl file must exist and contain the same contents as it
        # did before.
        self.assertTrue(tahoe.introducer_furl_file.exists())
        self.assertEqual(furl, fileutil.read(tahoe.introducer_furl_file.path))

    @inline_callbacks
    def test_client(self):
        """
        Test too many things.

        0) Verify that "tahoe create-node" takes a --webport option and writes
           the value to the configuration file.

        1) Verify that "tahoe run" writes a pid file and a node url file (on POSIX).

        2) Verify that the storage furl file has a stable value across a
           "tahoe run" / stop / "tahoe run" sequence.

        3) Verify that the pid file is removed after SIGTERM (on POSIX).
        """
        basedir = self.workdir("test_client")
        c1 = os.path.join(basedir, u"c1")

        tahoe = CLINodeAPI(reactor, FilePath(c1))
        # Set this up right now so we don't forget later.
        self.addCleanup(tahoe.cleanup)

        out, err, returncode = run_bintahoe([
            u"--quiet", u"create-node", u"--basedir", c1,
            u"--webport", u"0",
            u"--hostname", u"localhost",
        ])
        self.failUnlessEqual(returncode, 0)

        # Check that the --webport option worked.
        config = fileutil.read(tahoe.config_file.path)
        self.assertIn(
            '{}web.port = 0{}'.format(linesep, linesep),
            config,
        )

        # After this it's safe to start the node
        tahoe.active()

        p = Expect()
        # This will run until we stop it.
        tahoe.run(on_stdout(p))
        # Wait for startup to have proceeded to a reasonable point.
        yield p.expect("client running")
        tahoe.active()

        # read the storage.furl file so we can check that its contents don't
        # change on restart
        storage_furl = fileutil.read(tahoe.storage_furl_file.path)

        # We don't keep track of PIDs in files on Windows.
        if not platform.isWindows():
            self.assertTrue(tahoe.twistd_pid_file.exists())

        # rm this so we can detect when the second incarnation is ready
        tahoe.node_url_file.remove()
        yield tahoe.stop_and_wait()

        p = Expect()
        # We don't have to add another cleanup for this one, the one from
        # above is still registered.
        tahoe.run(on_stdout(p))
        yield p.expect("client running")
        tahoe.active()

        self.assertEqual(
            storage_furl,
            fileutil.read(tahoe.storage_furl_file.path),
        )

        if not platform.isWindows():
            self.assertTrue(
                tahoe.twistd_pid_file.exists(),
                "PID file ({}) didn't exist when we expected it to.  "
                "These exist: {}".format(
                    tahoe.twistd_pid_file,
                    tahoe.twistd_pid_file.parent().listdir(),
                ),
            )
        yield tahoe.stop_and_wait()

        if not platform.isWindows():
            # twistd.pid should be gone by now.
            self.assertFalse(tahoe.twistd_pid_file.exists())


    def _remove(self, res, file):
        fileutil.remove(file)
        return res

    def test_run_bad_directory(self):
        """
        If ``tahoe run`` is pointed at a non-node directory, it reports an error
        and exits.
        """
        return self._bad_directory_test(
            u"test_run_bad_directory",
            "tahoe run",
            lambda tahoe, p: tahoe.run(p),
            "is not a recognizable node directory",
        )

    def test_run_bogus_directory(self):
        """
        If ``tahoe run`` is pointed at a non-directory, it reports an error and
        exits.
        """
        return self._bad_directory_test(
            u"test_run_bogus_directory",
            "tahoe run",
            lambda tahoe, p: CLINodeAPI(
                tahoe.reactor,
                tahoe.basedir.sibling(u"bogus"),
            ).run(p),
            "does not look like a directory at all"
        )

    @inline_callbacks
    def _bad_directory_test(self, workdir, description, operation, expected_message):
        """
        Verify that a certain ``tahoe`` CLI operation produces a certain expected
        message and then exits.

        :param unicode workdir: A distinct path name for this test to operate
            on.

        :param unicode description: A description of the operation being
            performed.

        :param operation: A two-argument callable implementing the operation.
            The first argument is a ``CLINodeAPI`` instance to use to perform
            the operation.  The second argument is an ``IProcessProtocol`` to
            which the operations output must be delivered.

        :param unicode expected_message: Some text that is expected in the
            stdout or stderr of the operation in the successful case.

        :return: A ``Deferred`` that fires when the assertions have been made.
        """
        basedir = self.workdir(workdir)
        fileutil.make_dirs(basedir)

        tahoe = CLINodeAPI(reactor, FilePath(basedir))
        # If tahoe ends up thinking it should keep running, make sure it stops
        # promptly when the test is done.
        self.addCleanup(tahoe.cleanup)

        p = Expect()
        operation(tahoe, on_stdout_and_stderr(p))

        client_running = p.expect(b"client running")

        result, index = yield DeferredList([
            p.expect(expected_message),
            client_running,
        ], fireOnOneCallback=True, consumeErrors=True,
        )

        self.assertEqual(
            index,
            0,
            "Expected error message from '{}', got something else: {}".format(
                description,
                p.get_buffered_output(),
            ),
        )

        if not platform.isWindows():
            # It should not be running.
            self.assertFalse(tahoe.twistd_pid_file.exists())

        # Wait for the operation to *complete*.  If we got this far it's
        # because we got the expected message so we can expect the "tahoe ..."
        # child process to exit very soon.  This other Deferred will fail when
        # it eventually does but DeferredList above will consume the error.
        # What's left is a perfect indicator that the process has exited and
        # we won't get blamed for leaving the reactor dirty.
        yield client_running
