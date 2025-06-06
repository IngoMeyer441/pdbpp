from __future__ import annotations

# noqa: B011
import bdb
import inspect
import io
import os
import os.path
import re
import subprocess
import sys
import textwrap
import traceback
from io import BytesIO
from itertools import zip_longest
from shlex import quote
from typing import Callable

import pytest
from pygments import __version__ as pygments_version

import pdbpp
from pdbpp import DefaultConfig, Pdb, StringIO

from .conftest import skip_with_missing_pth_file

pygments_major, pygments_minor, _ = pygments_version.split(".")


# Windows support
# The basic idea is that paths on Windows are dumb because of backslashes.
# Typically this would be resolved by using `pathlib`, but we need to maintain
# support for pre-Py36 versions.
# A lot of tests are regex checks and the back-slashed Windows paths end
# up looking like they have escape characters in them (specifically the `\p`
# in `...\pdbpp`). So we need to make sure to escape those strings.
# In addtion, Windows is a case-insensitive file system. Most introspection
# tools return the `normcase` version (eg: all lowercase), so we adjust the
# canonical filename accordingly.
RE_THIS_FILE = re.escape(__file__)
THIS_FILE_CANONICAL = __file__
if sys.platform == "win32":
    THIS_FILE_CANONICAL = __file__.lower()
RE_THIS_FILE_CANONICAL = re.escape(THIS_FILE_CANONICAL)
RE_THIS_FILE_CANONICAL_QUOTED = re.escape(quote(THIS_FILE_CANONICAL))
RE_THIS_FILE_QUOTED = re.escape(quote(__file__))


class FakeStdin:
    def __init__(self, lines):
        self.lines = iter(lines)

    def readline(self):
        try:
            line = next(self.lines) + "\n"
            sys.stdout.write(line)
            return line
        except StopIteration:
            return ""


class ConfigTest(DefaultConfig):
    highlight = False
    use_pygments = False
    prompt = "# "  # because + has a special meaning in the regexp
    editor = "emacs"
    stdin_paste = "epaste"
    disable_pytest_capturing = False
    current_line_color = 44


class ConfigWithHighlight(ConfigTest):
    highlight = True


class ConfigWithPygments(ConfigTest):
    use_pygments = True


class ConfigWithPygmentsNone(ConfigTest):
    use_pygments = None


class ConfigWithPygmentsAndHighlight(ConfigWithPygments, ConfigWithHighlight):
    pass


class PdbTest(pdbpp.Pdb):
    use_rawinput = 1

    def __init__(self, *args, **kwds):
        readrc = kwds.pop("readrc", False)
        nosigint = kwds.pop("nosigint", True)
        kwds.setdefault("Config", ConfigTest)
        super().__init__(*args, readrc=readrc, **kwds)
        # Do not install sigint_handler in do_continue by default.
        self.nosigint = nosigint

    def _open_editor(self, editcmd):
        print(f"RUN {editcmd}")

    def _open_stdin_paste(self, cmd, lineno, filename, text):
        print(f"RUN {cmd} +{lineno}")
        print(repr(text))

    def do_shell(self, arg):
        """Track when do_shell gets called (via "!").

        This is not implemented by default, but we should not trigger it
        via parseline unnecessarily, which would cause unexpected results
        if somebody uses it.
        """
        print(f"do_shell_called: '{arg}'")
        return self.default(arg)


def set_trace_via_module(frame=None, cleanup=True, Pdb=PdbTest, **kwds):
    """set_trace helper that goes through pdb.set_trace.

    It injects Pdb into the globals of pdb.set_trace, to use the given frame.
    """
    if frame is None:
        frame = sys._getframe().f_back

    if cleanup:
        pdbpp.cleanup()

    class PdbForFrame(Pdb):
        def set_trace(self, _frame, *args, **kwargs):
            super().set_trace(frame, *args, **kwargs)

    newglobals = pdbpp.set_trace.__globals__.copy()
    newglobals["Pdb"] = PdbForFrame
    new_set_trace = pdbpp.rebind_globals(pdbpp.set_trace, newglobals)
    new_set_trace(**kwds)


# TODO: check if this can be used to move where the breakpoint is breaking?
# Notes: first line in the tests  set_trace.__code__.co_firstlineno
def set_trace(frame=None, cleanup=True, Pdb=PdbTest, **kwds):
    """set_trace helper for tests, going through Pdb.set_trace directly."""
    if frame is None:
        frame = sys._getframe().f_back

    if cleanup:
        pdbpp.cleanup()

    Pdb(**kwds).set_trace(frame)


def xpm():
    pdbpp.xpm(PdbTest)


def runpdb(
    func: Callable,
    input: list[str],
    terminal_size: tuple[int, int] | None = None,
) -> list[str]:
    oldstdin = sys.stdin
    oldstdout = sys.stdout
    oldstderr = sys.stderr
    # Use __dict__ to avoid class descriptor (staticmethod).
    old_get_terminal_size = pdbpp.Pdb.__dict__["get_terminal_size"]

    class MyBytesIO(BytesIO):
        """write accepts unicode or bytes"""

        def __init__(self, encoding: str = "utf-8"):
            self.encoding = encoding

        def write(self, msg):
            if isinstance(msg, str):
                msg = msg.encode(self.encoding)
            super().write(msg)

        def get_unicode_value(self):
            return (
                self.getvalue()
                .decode(self.encoding)
                .replace(pdbpp.CLEARSCREEN, "<CLEARSCREEN>\n")
                .replace(chr(27), "^[")
            )

    # Use a predictable terminal size.
    if terminal_size is None:
        terminal_size = (80, 24)
    pdbpp.Pdb.get_terminal_size = staticmethod(lambda: terminal_size)
    try:
        sys.stdin = FakeStdin(input)
        sys.stdout = stdout = MyBytesIO()
        sys.stderr = stderr = MyBytesIO()
        func()
    except InnerTestException:
        pass
    except bdb.BdbQuit:
        print("!! Received unexpected bdb.BdbQuit !!")
    except Exception:
        # Make it available for pytests output capturing.
        print(stdout.get_unicode_value(), file=oldstdout)
        raise
    finally:
        sys.stdin = oldstdin
        sys.stdout = oldstdout
        sys.stderr = oldstderr
        pdbpp.Pdb.get_terminal_size = old_get_terminal_size

    stderr = stderr.get_unicode_value()
    if stderr:
        # Make it available for pytests output capturing.
        print(stdout.get_unicode_value())
        raise AssertionError(f"Unexpected output on stderr: {stderr}")

    return stdout.get_unicode_value().splitlines()


def is_prompt(line: str) -> bool:
    prompts = {"# ", "(#) ", "((#)) ", "(((#))) ", "(Pdb) ", "(Pdb++) ", "(com++) "}
    for prompt in prompts:
        if line.startswith(prompt):
            return len(prompt)
    return False


def extract_commands(lines: list[str]) -> list[str]:
    cmds: list[str] = []
    for line in lines:
        prompt_len = is_prompt(line)
        if prompt_len:
            cmds.append(line[prompt_len:])
    return cmds


shortcuts = [
    ("[", "\\["),
    ("]", "\\]"),
    ("(", "\\("),
    (")", "\\)"),
    ("^", "\\^"),
    (r"\(Pdb++\) ", r"\(Pdb\+\+\) "),
    (r"\(com++\) ", r"\(com\+\+\) "),
    ("<COLORCURLINE>", r"\^\[\[44m\^\[\[36;01;44m *[0-9]+\^\[\[00;44m"),
    ("<COLORNUM>", r"\^\[\[36;01m *[0-9]+\^\[\[00m"),
    ("<COLORFNAME>", r"\^\[\[33;01m"),
    ("<COLORLNUM>", r"\^\[\[36;01m"),
    ("<COLORRESET>", r"\^\[\[00m"),
    ("<PYGMENTSRESET>", r"\^\[\[39[^m]*m"),
    ("NUM", " *[0-9]+"),
]


def cook_regexp(s):
    for key, value in shortcuts:
        s = s.replace(key, value)
    return s


def run_func(func, expected, terminal_size=None) -> tuple[list[str], list[str]]:
    """Runs given function and returns its output along with expected patterns.

    It does not make any assertions. To compare func's output with expected
    lines, use `check` function.
    """
    # FIXME: I used textwrap.dedent everywwhere in tests.
    #       There's no fucking need to do that
    expected = textwrap.dedent(expected).strip().splitlines()
    # Remove comments.
    expected = [re.split(r"\s+###", line)[0] for line in expected]
    commands = extract_commands(expected)
    expected = map(cook_regexp, expected)

    flattened = []
    for line in expected:
        if line == "":
            flattened.append("")
        else:
            flattened.extend(line.splitlines())
    expected = flattened

    return expected, runpdb(func, commands, terminal_size)


def count_frames():
    # FIXME: isn't there a better way to do this?
    f = sys._getframe()
    i = 0
    while f is not None:
        i += 1
        f = f.f_back
    return i


class InnerTestException(Exception):
    """Ignored by check()."""

    pass


trans_trn_dict = {"\n": r"\n", "\r": r"\r", "\t": r"\t"}
trans_trn_table = str.maketrans(trans_trn_dict)


def trans_trn(string):
    return string.translate(trans_trn_table)


def check(
    func,
    expected,
    terminal_size=None,
    add_313_fix: bool = False,
    set_trace_args: str | None = None,
):
    if set_trace_args and not add_313_fix:
        raise ValueError("cannot use set_trace_args without add_313_fix")
    if add_313_fix and sys.version_info >= (3, 13):
        expected = textwrap.dedent(
            f"""
            [NUM] > .*fn()
            -> set_trace({set_trace_args or ""})
               5 frames hidden .*
            # n
            """.rstrip(),
        ) + textwrap.dedent(expected)

    expected, lines = run_func(func, expected, terminal_size)
    maxlen = max(map(len, expected)) if expected else 0
    all_ok = True
    print()
    print(
        pdbpp.Color.set(pdbpp.Color.darkgreen, "Expected".ljust(maxlen + 1)),
        "| ",
        pdbpp.Color.set(pdbpp.Color.yellow, "Actual"),
    )
    print("=" * (2 * maxlen + 3))
    for pattern, string in zip_longest(expected, lines):
        if pattern is not None and string is not None:
            if is_prompt(pattern) and is_prompt(string):
                ok = True
            else:
                try:
                    ok = re.match(pattern, string)
                except re.error as exc:
                    raise ValueError(f"re.match failed for {pattern!r}: {exc!r}")  # noqa: B904
        else:
            ok = False
            if pattern is None:
                pattern = "<None>"
            if string is None:
                string = "<None>"
        # Use "$" to mark end of line with trailing space
        if re.search(r"\s+$", string):
            string += "$"
        if re.search(r"\s+$", pattern):
            pattern += "$"
        pattern = trans_trn(pattern)
        string = trans_trn(string)
        print(pattern.ljust(maxlen + 1), "| ", string, end="")
        if ok:
            print()
        else:
            print(pdbpp.Color.set(pdbpp.Color.red, "    <<<<<"))
            all_ok = False
    assert all_ok


def test_prompt_setter():
    p = pdbpp.Pdb()
    assert p.prompt == "(Pdb++) "

    p.prompt = "(Pdb)"
    assert p.prompt == "(Pdb++)"
    p.prompt = "ipdb> "
    assert p.prompt == "ipdb++> "
    p.prompt = "custom"
    assert p.prompt == "custom++"
    p.prompt = "custom "
    assert p.prompt == "custom++ "
    p.prompt = "custom :"
    assert p.prompt == "custom++ :"
    p.prompt = "custom  "
    assert p.prompt == "custom++  "
    p.prompt = ""
    assert p.prompt == ""
    # Not changed (also used in tests).
    p.prompt = "# "
    assert p.prompt == "# "
    # Can be forced.
    p._pdbpp_prompt = "custom"
    assert p.prompt == "custom"


def test_config_pygments(monkeypatch):
    import pygments.formatters

    assert not hasattr(DefaultConfig, "use_terminal256formatter")

    p = Pdb(Config=DefaultConfig)

    monkeypatch.delenv("TERM", raising=False)
    assert isinstance(
        p._get_pygments_formatter(), pygments.formatters.TerminalFormatter
    )

    monkeypatch.setenv("TERM", "xterm-256color")
    assert isinstance(
        p._get_pygments_formatter(), pygments.formatters.Terminal256Formatter
    )

    monkeypatch.setenv("TERM", "xterm-kitty")
    assert isinstance(
        p._get_pygments_formatter(), pygments.formatters.TerminalTrueColorFormatter
    )

    class Config(DefaultConfig):
        formatter = object()

    assert Pdb(Config=Config)._get_pygments_formatter() is Config.formatter

    class Config(DefaultConfig):
        pygments_formatter_class = "pygments.formatters.TerminalTrueColorFormatter"

    assert isinstance(
        Pdb(Config=Config)._get_pygments_formatter(),
        pygments.formatters.TerminalTrueColorFormatter,
    )

    source = Pdb(Config=Config).format_source("print(42)")
    assert source.startswith("\x1b[38;2;0;128;")
    assert "print\x1b[39" in source
    assert source.endswith("m42\x1b[39m)\n")


@pytest.mark.parametrize("use_pygments", (None, True, False))
def test_config_missing_pygments(use_pygments, monkeypatch_importerror):
    class Config(DefaultConfig):
        pass

    Config.use_pygments = use_pygments

    class PdbForMessage(Pdb):
        messages = []

        def message(self, msg):
            self.messages.append(msg)

    pdb_ = PdbForMessage(Config=Config)

    with monkeypatch_importerror(("pygments", "pygments.formatters")):
        with pytest.raises(ImportError):
            pdb_._get_pygments_formatter()
        assert pdb_._get_source_highlight_function() is False
        assert pdb_.format_source("print(42)") == "print(42)"

    if use_pygments is True:
        assert pdb_.messages == ["Could not import pygments, disabling."]
    else:
        assert pdb_.messages == []

    # Cover branch for cached _highlight property.
    assert pdb_.format_source("print(42)") == "print(42)"


def test_config_pygments_deprecated_use_terminal256formatter(monkeypatch):
    import pygments.formatters

    monkeypatch.setenv("TERM", "xterm-256color")

    class Config(DefaultConfig):
        use_terminal256formatter = False

    assert isinstance(
        Pdb(Config=Config)._get_pygments_formatter(),
        pygments.formatters.TerminalFormatter,
    )

    class Config(DefaultConfig):
        use_terminal256formatter = True

    assert isinstance(
        Pdb(Config=Config)._get_pygments_formatter(),
        pygments.formatters.Terminal256Formatter,
    )


def test_runpdb():
    def fn():
        set_trace()
        a = 1
        b = 2
        c = 3
        return a + b + c

    if sys.version_info >= (3, 13):
        py313_output = """
          [NUM] > .*fn()
          -> set_trace()
             5 frames hidden .*
          # n"""
    else:
        py313_output = ""
    expected = textwrap.dedent(
        py313_output
        + """
          [NUM] > .*fn()
          -> a = 1
             5 frames hidden .*
          # n
          [NUM] > .*fn()
          -> b = 2
             5 frames hidden .*
          # n
          [NUM] > .*fn()
          -> c = 3
             5 frames hidden .*
          # c
          """
    )

    check(fn, expected)


def test_set_trace_remembers_previous_state():
    def fn():
        a = 1
        set_trace()
        a = 2
        set_trace(cleanup=False)
        a = 3
        set_trace(cleanup=False)
        a = 4
        return a

    if sys.version_info >= (3, 13):

        def get_trace_lines_str(cleanup=True) -> str:
            """helper to avoid repeating set_trace() lines"""

            return f"""
            [NUM] > .*fn()
            -> set_trace({"cleanup=False" if not cleanup else ""})
               5 frames hidden .*
            """.strip()

        expected = textwrap.dedent(
            f"""
            {get_trace_lines_str()}
            # display a
            # c
            {get_trace_lines_str(cleanup=False)}
            a: 1 --> 2
            # c
            {get_trace_lines_str(cleanup=False)}
            a: 2 --> 3
            # c
            """,
        )
    else:
        expected = textwrap.dedent(
            """
            [NUM] > .*fn()
            -> a = 2
               5 frames hidden .*
            # display a
            # c
            [NUM] > .*fn()
            -> a = 3
               5 frames hidden .*
            a: 1 --> 2
            # c
            [NUM] > .*fn()
            -> a = 4
               5 frames hidden .*
            a: 2 --> 3
            # c
            """,
        )
    check(fn, expected)


def test_set_trace_remembers_previous_state_via_module():
    def fn():
        a = 1
        set_trace_via_module()
        a = 2
        set_trace_via_module(cleanup=False)
        a = 3
        set_trace_via_module(cleanup=False)
        a = 4
        return a

    if sys.version_info >= (3, 13):

        def get_trace_lines_str(cleanup=True) -> str:
            """helper to avoid repeating set_trace() lines"""

            return f"""
            [NUM] > .*fn()
            -> set_trace_via_module({"cleanup=False" if not cleanup else ""})
               5 frames hidden .*
            """.strip()

        expected = textwrap.dedent(f"""
            {get_trace_lines_str()}
            # display a
            # c
            {get_trace_lines_str(cleanup=False)}
            a: 1 --> 2
            # c
            {get_trace_lines_str(cleanup=False)}
            a: 2 --> 3
            # c
            """)
    else:
        expected = textwrap.dedent("""
            [NUM] > .*fn()
            -> a = 2
               5 frames hidden .*
            # display a
            # c
            [NUM] > .*fn()
            -> a = 3
               5 frames hidden .*
            a: 1 --> 2
            # c
            [NUM] > .*fn()
            -> a = 4
               5 frames hidden .*
            a: 2 --> 3
            # c
            """)

    check(fn, expected)


class TestPdbMeta:
    def test_called_for_set_trace_false(self):
        assert pdbpp.PdbMeta.called_for_set_trace(sys._getframe()) is False

    def test_called_for_set_trace_staticmethod(self):
        class Foo:
            @staticmethod
            def set_trace():
                frame = sys._getframe()
                assert pdbpp.PdbMeta.called_for_set_trace(frame) is frame
                return True

        assert Foo.set_trace() is True

    def test_called_for_set_trace_method(self):
        class Foo:
            def set_trace(self):
                frame = sys._getframe()
                assert pdbpp.PdbMeta.called_for_set_trace(frame) is frame
                return True

        assert Foo().set_trace() is True

    def test_called_for_set_trace_via_func(self):
        def set_trace():
            frame = sys._getframe()
            assert pdbpp.PdbMeta.called_for_set_trace(frame) is frame
            return True

        assert set_trace() is True

    def test_called_for_set_trace_via_other_func(self):
        def somefunc():
            def meta():
                frame = sys._getframe()
                assert pdbpp.PdbMeta.called_for_set_trace(frame) is False

            meta()
            return True

        assert somefunc() is True


def test_forget_with_new_pdb():
    """Regression test for having used local.GLOBAL_PDB in forget.

    This caused "AttributeError: 'NewPdb' object has no attribute 'lineno'",
    e.g. when pdbpp was used before pytest's debugging plugin was setup, which
    then later uses a custom Pdb wrapper.
    """

    def fn():
        set_trace()

        class NewPdb(PdbTest, pdbpp.Pdb):
            def set_trace(self, *args):
                print("new_set_trace")
                return super().set_trace(*args)

        new_pdb = NewPdb()
        new_pdb.set_trace()

    if sys.version_info < (3, 13):
        expected = textwrap.dedent(
            """
            [NUM] > .*fn()
            -> class NewPdb(PdbTest, pdbpp.Pdb):
               5 frames hidden .*
            # c
            new_set_trace
            --Return--
            [NUM] .*set_trace()->None
            -> return super().set_trace(\\*args)
               5 frames hidden .*
            # l
            NUM .*
            NUM .*
            NUM .*
            NUM .*
            NUM .*
            NUM .*
            NUM .*
            NUM .*
            NUM .*
            NUM .*
            NUM .*
            # c
            """,
        )
    else:
        expected = textwrap.dedent(
            """
            [NUM] > .*fn()
            -> set_trace()
               5 frames hidden .*
            # c
            new_set_trace
            [NUM] > .*set_trace()
            -> return super().set_trace(\\*args)
               5 frames hidden .*
            # l
            NUM .*
            NUM .*
            NUM .*
            NUM .*
            NUM .*
            NUM .*
            NUM .*
            NUM .*
            NUM .*
            NUM .*
            NUM .*
            # c
            """.rstrip()
        )

    check(fn, expected)


def test_global_pdb_with_classmethod():
    def fn():
        set_trace()
        assert isinstance(pdbpp.local.GLOBAL_PDB, PdbTest)

        class NewPdb(PdbTest, pdbpp.Pdb):
            def set_trace(self, *args):
                print("new_set_trace")
                assert pdbpp.local.GLOBAL_PDB is self
                ret = super().set_trace(*args)
                assert pdbpp.local.GLOBAL_PDB is self
                return ret

        new_pdb = NewPdb()
        new_pdb.set_trace()

    if sys.version_info < (3, 13):
        expected = textwrap.dedent(
            """
        [NUM] > .*fn()
        -> assert isinstance(pdbpp.local.GLOBAL_PDB, PdbTest)
           5 frames hidden .*
        # c
        new_set_trace
        [NUM] .*set_trace()
        -> assert pdbpp.local.GLOBAL_PDB is self
           5 frames hidden .*
        # c
        """,
        )
    else:
        expected = textwrap.dedent("""
        [NUM] > .*fn()
        -> set_trace()
           5 frames hidden .*
        # n
        [NUM] > .*fn()
        -> assert isinstance(pdbpp.local.GLOBAL_PDB, PdbTest)
           5 frames hidden .*
        # c
        new_set_trace
        [NUM] > .*set_trace()
        -> ret = super().set_trace(\\*args)
           5 frames hidden .*
        # n
        [NUM] .*set_trace()
        -> assert pdbpp.local.GLOBAL_PDB is self
           5 frames hidden .*
        # c
        """)

    check(fn, expected)


def test_global_pdb_via_new_class_in_init_method():
    def fn():
        set_trace()
        first = pdbpp.local.GLOBAL_PDB
        assert isinstance(pdbpp.local.GLOBAL_PDB, PdbTest)

        class PdbLikePytest:
            @classmethod
            def init_pdb(cls):
                class NewPdb(PdbTest, pdbpp.Pdb):
                    def set_trace(self, frame):
                        print("new_set_trace")
                        super().set_trace(frame)

                return NewPdb()

            @classmethod
            def set_trace(cls, *args, **kwargs):
                frame = sys._getframe().f_back
                pdb_ = cls.init_pdb(*args, **kwargs)
                return pdb_.set_trace(frame)

        PdbLikePytest.set_trace()
        second = pdbpp.local.GLOBAL_PDB
        assert first != second

        PdbLikePytest.set_trace()
        third = pdbpp.local.GLOBAL_PDB
        assert third == second

    if sys.version_info >= (3, 13):
        expected = textwrap.dedent("""
            [NUM] > .*fn()
            -> set_trace()
               5 frames hidden .*
            # n
            [NUM] > .*fn()
            -> first = pdbpp.local.GLOBAL_PDB
               5 frames hidden .*
            # c
            new_set_trace
            [NUM] > .*fn()
            -> PdbLikePytest.set_trace()
               5 frames hidden .*
            # n
            [NUM] > .*fn()
            -> second = pdbpp.local.GLOBAL_PDB
               5 frames hidden .*
            # c
            new_set_trace
            [NUM] > .*fn()
            -> PdbLikePytest.set_trace()
               5 frames hidden .*
            # n
            [NUM] > .*fn()
            -> third = pdbpp.local.GLOBAL_PDB
               5 frames hidden .*
            # c
        """)
    else:
        expected = textwrap.dedent("""
            [NUM] > .*fn()
            -> first = pdbpp.local.GLOBAL_PDB
               5 frames hidden .*
            # c
            new_set_trace
            [NUM] > .*fn()
            -> second = pdbpp.local.GLOBAL_PDB
               5 frames hidden .*
            # c
            new_set_trace
            [NUM] > .*fn()
            -> third = pdbpp.local.GLOBAL_PDB
               5 frames hidden .*
            # c
           """)

    check(fn, expected)


def test_global_pdb_via_existing_class_in_init_method():
    def fn():
        set_trace()
        first = pdbpp.local.GLOBAL_PDB
        assert isinstance(pdbpp.local.GLOBAL_PDB, PdbTest)

        class NewPdb(PdbTest, pdbpp.Pdb):
            def set_trace(self, frame):
                print("new_set_trace")
                super().set_trace(frame)

        class PdbViaClassmethod:
            @classmethod
            def init_pdb(cls):
                return NewPdb()

            @classmethod
            def set_trace(cls, *args, **kwargs):
                frame = sys._getframe().f_back
                pdb_ = cls.init_pdb(*args, **kwargs)
                return pdb_.set_trace(frame)

        PdbViaClassmethod.set_trace()
        second = pdbpp.local.GLOBAL_PDB
        assert first != second

        PdbViaClassmethod.set_trace()
        third = pdbpp.local.GLOBAL_PDB
        assert third == second

    if sys.version_info >= (3, 13):
        expected = textwrap.dedent("""
            [NUM] > .*fn()
            -> set_trace()
               5 frames hidden .*
            # n
            [NUM] > .*fn()
            -> first = pdbpp.local.GLOBAL_PDB
               5 frames hidden .*
            # c
            new_set_trace
            [NUM] > .*fn()
            -> PdbViaClassmethod.set_trace()
               5 frames hidden .*
            # n
            [NUM] > .*fn()
            -> second = pdbpp.local.GLOBAL_PDB
               5 frames hidden .*
            # c
            new_set_trace
            [NUM] > .*fn()
            -> PdbViaClassmethod.set_trace()
               5 frames hidden .*
            # n
            [NUM] > .*fn()
            -> third = pdbpp.local.GLOBAL_PDB
               5 frames hidden .*
            # c
        """)
    else:
        expected = textwrap.dedent("""
            [NUM] > .*fn()
            -> first = pdbpp.local.GLOBAL_PDB
               5 frames hidden .*
            # c
            new_set_trace
            [NUM] > .*fn()
            -> second = pdbpp.local.GLOBAL_PDB
               5 frames hidden .*
            # c
            new_set_trace
            [NUM] > .*fn()
            -> third = pdbpp.local.GLOBAL_PDB
               5 frames hidden .*
            # c
           """)

    check(fn, expected)


def test_global_pdb_can_be_skipped():
    def fn():
        set_trace()
        first = pdbpp.local.GLOBAL_PDB
        assert isinstance(first, PdbTest)

        class NewPdb(PdbTest, pdbpp.Pdb):
            def set_trace(self, *args):
                print("new_set_trace")
                assert pdbpp.local.GLOBAL_PDB is not self
                ret = super().set_trace(*args)
                assert pdbpp.local.GLOBAL_PDB is not self
                return ret

        new_pdb = NewPdb(use_global_pdb=False)
        new_pdb.set_trace()
        assert pdbpp.local.GLOBAL_PDB is not new_pdb

        set_trace(cleanup=False)
        assert pdbpp.local.GLOBAL_PDB is not new_pdb

    if sys.version_info >= (3, 13):
        expected = textwrap.dedent("""
            [NUM] > .*fn()
            -> set_trace()
               5 frames hidden .*
            # n
            [NUM] > .*fn()
            -> first = pdbpp.local.GLOBAL_PDB
               5 frames hidden .*
            # c
            new_set_trace
            [NUM] > .*set_trace()
            -> ret = super().set_trace(\\*args)
               5 frames hidden .*
            # n
            [NUM] .*set_trace()
            -> assert pdbpp.local.GLOBAL_PDB is not self
               5 frames hidden .*
            # readline_ = pdbpp.local.GLOBAL_PDB.fancycompleter.config.readline
            # assert readline_.get_completer() != pdbpp.local.GLOBAL_PDB.complete
            # c
            [NUM] > .*fn()
            -> set_trace(cleanup=False)
               5 frames hidden .*
            # n
            [NUM] > .*fn()
            -> assert pdbpp.local.GLOBAL_PDB is not new_pdb
               5 frames hidden .*
            # c
        """)
    else:
        expected = textwrap.dedent("""
            [NUM] > .*fn()
            -> first = pdbpp.local.GLOBAL_PDB
               5 frames hidden .*
            # c
            new_set_trace
            [NUM] .*set_trace()
            -> assert pdbpp.local.GLOBAL_PDB is not self
               5 frames hidden .*
            # readline_ = pdbpp.local.GLOBAL_PDB.fancycompleter.config.readline
            # assert readline_.get_completer() != pdbpp.local.GLOBAL_PDB.complete
            # c
            [NUM] > .*fn()
            -> assert pdbpp.local.GLOBAL_PDB is not new_pdb
               5 frames hidden .*
            # c
            """)

    check(fn, expected)


def test_global_pdb_can_be_skipped_unit(monkeypatch_pdb_methods):
    """Same as test_global_pdb_can_be_skipped, but with mocked Pdb methods."""

    def fn():
        set_trace()
        first = pdbpp.local.GLOBAL_PDB
        assert isinstance(first, PdbTest)

        class NewPdb(PdbTest, pdbpp.Pdb):
            def set_trace(self, *args):
                print("new_set_trace")
                assert pdbpp.local.GLOBAL_PDB is not self
                ret = super().set_trace(*args)
                assert pdbpp.local.GLOBAL_PDB is not self
                return ret

        new_pdb = NewPdb(use_global_pdb=False)
        new_pdb.set_trace()
        assert pdbpp.local.GLOBAL_PDB is not new_pdb

        set_trace(cleanup=False)
        assert pdbpp.local.GLOBAL_PDB is not new_pdb

    check(
        fn,
        """
=== set_trace
new_set_trace
=== set_trace
=== set_trace
""",
    )


def test_global_pdb_can_be_skipped_but_set():
    def fn():
        set_trace()
        first = pdbpp.local.GLOBAL_PDB
        assert isinstance(first, PdbTest)

        class NewPdb(PdbTest, pdbpp.Pdb):
            def set_trace(self, *args):
                print("new_set_trace")
                assert pdbpp.local.GLOBAL_PDB is self
                ret = super().set_trace(*args)
                assert pdbpp.local.GLOBAL_PDB is self
                return ret

        new_pdb = NewPdb(use_global_pdb=False, set_global_pdb=True)
        new_pdb.set_trace()
        assert pdbpp.local.GLOBAL_PDB is new_pdb

        set_trace(cleanup=False)
        assert pdbpp.local.GLOBAL_PDB is new_pdb

    if sys.version_info >= (3, 13):
        expected = textwrap.dedent("""
            [NUM] > .*fn()
            -> set_trace()
               5 frames hidden .*
            # n
            [NUM] > .*fn()
            -> first = pdbpp.local.GLOBAL_PDB
               5 frames hidden .*
            # c
            new_set_trace
            [NUM] .*set_trace()
            -> ret = super().set_trace(\\*args)
               5 frames hidden .*
            # n
            [NUM] .*set_trace()
            -> assert pdbpp.local.GLOBAL_PDB is self
               5 frames hidden .*
            # readline_ = pdbpp.local.GLOBAL_PDB.fancycompleter.config.readline
            # assert readline_.get_completer() == pdbpp.local.GLOBAL_PDB.complete
            # c
            new_set_trace
            [NUM] > .*fn()
            -> set_trace(cleanup=False)
               5 frames hidden .*
            # n
            [NUM] .*fn()
            -> assert pdbpp.local.GLOBAL_PDB is new_pdb
               5 frames hidden .*
            # c
        """)
    else:
        expected = textwrap.dedent("""
            [NUM] > .*fn()
            -> first = pdbpp.local.GLOBAL_PDB
               5 frames hidden .*
            # c
            new_set_trace
            [NUM] .*set_trace()
            -> assert pdbpp.local.GLOBAL_PDB is self
               5 frames hidden .*
            # readline_ = pdbpp.local.GLOBAL_PDB.fancycompleter.config.readline
            # assert readline_.get_completer() == pdbpp.local.GLOBAL_PDB.complete
            # c
            new_set_trace
            [NUM] > .*fn()
            -> assert pdbpp.local.GLOBAL_PDB is new_pdb
               5 frames hidden .*
            # c
            """)

    check(fn, expected)


def test_global_pdb_can_be_skipped_but_set_unit(monkeypatch_pdb_methods):
    def fn():
        set_trace()
        first = pdbpp.local.GLOBAL_PDB
        assert isinstance(first, PdbTest)

        class NewPdb(PdbTest, pdbpp.Pdb):
            def set_trace(self, *args):
                print("new_set_trace")
                assert pdbpp.local.GLOBAL_PDB is self
                ret = super().set_trace(*args)
                assert pdbpp.local.GLOBAL_PDB is self
                return ret

        new_pdb = NewPdb(use_global_pdb=False, set_global_pdb=True)
        new_pdb.set_trace()
        assert pdbpp.local.GLOBAL_PDB is new_pdb

        set_trace(cleanup=False)
        assert pdbpp.local.GLOBAL_PDB is new_pdb

    check(
        fn,
        """
=== set_trace
new_set_trace
=== set_trace
new_set_trace
=== set_trace
""",
    )


def test_global_pdb_only_reused_for_same_class(monkeypatch_pdb_methods):
    def fn():
        class NewPdb(PdbTest, pdbpp.Pdb):
            def set_trace(self, *args):
                print("new_set_trace")
                ret = super().set_trace(*args)
                return ret

        new_pdb = NewPdb()
        new_pdb.set_trace()
        assert pdbpp.local.GLOBAL_PDB is new_pdb

        set_trace(cleanup=False)
        assert pdbpp.local.GLOBAL_PDB is not new_pdb

        # What "debug" does, for coverage.
        new_pdb = NewPdb()
        new_pdb.set_trace()
        assert pdbpp.local.GLOBAL_PDB is new_pdb
        pdbpp.local.GLOBAL_PDB._use_global_pdb_for_class = PdbTest
        set_trace(cleanup=False)
        assert pdbpp.local.GLOBAL_PDB is new_pdb

        # Explicit kwarg for coverage.
        new_pdb = NewPdb(set_global_pdb=False)
        new_pdb.set_trace()
        assert pdbpp.local.GLOBAL_PDB is not new_pdb

    check(
        fn,
        """
new_set_trace
=== set_trace
=== set_trace
new_set_trace
=== set_trace
new_set_trace
=== set_trace
new_set_trace
=== set_trace
""",
    )


def test_global_pdb_not_reused_with_different_home(
    monkeypatch_pdb_methods, monkeypatch
):
    def fn():
        set_trace()
        first = pdbpp.local.GLOBAL_PDB

        set_trace(cleanup=False)
        assert first is pdbpp.local.GLOBAL_PDB

        monkeypatch.setenv("HOME", "something else")
        set_trace(cleanup=False)
        assert first is not pdbpp.local.GLOBAL_PDB

    check(
        fn,
        """
=== set_trace
=== set_trace
=== set_trace
""",
    )


def test_single_question_mark():
    def fn():
        def nodoc():
            pass

        def f2(x, y):
            """Return product of x and y"""
            return x * y

        set_trace()
        a = 1
        b = 2
        c = 3
        return a + b + c

    expected = textwrap.dedent(rf"""
            [NUM] > .*fn()
            -> a = 1
               5 frames hidden .*
            # f2
            <function .*f2 at .*>
            # f2?
            .*Type:.*function
            .*String Form:.*<function .*f2 at .*>
            ^[[31;01mFile:^[[00m           {RE_THIS_FILE_CANONICAL}:{fn.__code__.co_firstlineno + 4}
            .*Definition:.*f2(x, y)
            .*Docstring:.*Return product of x and y
            # nodoc?
            .*Type:.*function
            .*String Form:.*<function .*nodoc at .*>
            ^[[31;01mFile:^[[00m           {RE_THIS_FILE_CANONICAL}:{fn.__code__.co_firstlineno + 1}
            ^[[31;01mDefinition:^[[00m     nodoc()
            # doesnotexist?
            \*\*\* NameError.*
            # c
            """)
    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                r"""
            [NUM] > .*fn()
            -> set_trace()
               5 frames hidden .*
            # n
            """.rstrip()
            )
            + expected
        )

    check(fn, expected)


def test_double_question_mark():
    """Test do_inspect_with_source."""

    def fn():
        class TestStr(str):
            __doc__ = "shortened"

        s = TestStr("str")  # noqa: F841

        def f2(x, y):
            """Return product of x and y"""
            return x * y

        set_trace()
        a = 1
        b = 2
        c = 3
        return a + b + c

    expected = textwrap.dedent(
        rf"""
        [NUM] > .*fn()
        -> a = 1
           5 frames hidden .*
        # f2
        <function .*f2 at .*>
        # f2??
        .*Type:.*function
        .*String Form:.*<function .*f2 at .*>
        ^[[31;01mFile:^[[00m           {RE_THIS_FILE_CANONICAL}
        .*Definition:.*f2(x, y)
        .*Docstring:.*Return product of x and y
        .*Source:.*
        .* def f2(x, y):
        .*     \"\"\"Return product of x and y\"\"\"
        .*     return x \* y
        # doesnotexist??
        \*\*\* NameError.*
        # s??
        ^[[31;01mType:^[[00m           TestStr
        ^[[31;01mString Form:^[[00m    str
        ^[[31;01mLength:^[[00m         3
        ^[[31;01mDocstring:^[[00m      shortened
        ^[[31;01mSource:^[[00m         -
        # c
        """,
    )
    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                r"""
            [NUM] > .*fn()
            -> set_trace()
               5 frames hidden .*
            # n
            """.rstrip()
            )
            + expected
        )
    check(fn, expected)


def test_question_mark_unit(capsys, LineMatcher):
    _pdb = PdbTest()
    _pdb.reset()

    foo = {12: 34}  # noqa: F841
    _pdb.setup(sys._getframe(), None)

    _pdb.do_inspect("foo")

    out, err = capsys.readouterr()
    LineMatcher(out.splitlines()).fnmatch_lines(
        ["\x1b[31;01mString Form:\x1b[00m    {12: 34}"]
    )

    _pdb.do_inspect("doesnotexist")
    out, err = capsys.readouterr()
    LineMatcher(out.splitlines()).re_match_lines(
        [
            r"^\*\*\* NameError:",
        ]
    )

    # Source for function, indented docstring.
    def foo():  # noqa: F811
        """doc_for_foo

        3rd line."""
        raise NotImplementedError()

    _pdb.setup(sys._getframe(), None)
    _pdb.do_inspect_with_source("foo")
    out, err = capsys.readouterr()
    LineMatcher(out.splitlines()).re_match_lines(
        [
            r"\x1b\[31;01mDocstring:\x1b\[00m      doc_for_foo",
            r"",
            r"                3rd line\.",
            r"\x1b\[31;01mSource:\x1b\[00m        ",
            r" ?\d+         def foo\(\):",
            r" ?\d+             raise NotImplementedError\(\)",
        ]
    )

    # Missing source
    _pdb.do_inspect_with_source("str.strip")
    out, err = capsys.readouterr()
    LineMatcher(out.splitlines()).fnmatch_lines(
        [
            "\x1b[31;01mSource:\x1b[00m         -",
        ]
    )


def test_single_question_mark_with_existing_command(monkeypatch):
    def mocked_inspect(self, arg):
        print(f"mocked_inspect: '{arg}'")

    monkeypatch.setattr(PdbTest, "do_inspect", mocked_inspect)

    def fn():
        mp = monkeypatch  # noqa: F841

        class MyClass:
            pass

        a = MyClass()  # noqa: F841
        set_trace()

    expected = textwrap.dedent(
        """
        --Return--
        [NUM] > .*fn()->None
        -> set_trace()
           5 frames hidden .*
        # a?
        mocked_inspect: 'a'
        # a.__class__?
        mocked_inspect: 'a.__class__'
        # !!a?
        """.rstrip()
        + (
            r"""
        \*\*\* Invalid argument: ?
          Usage: a(rgs)
        """.rstrip()
            if sys.version_info
            >= (
                3,
                13,
            )  # in 3.13, calling a(rgs) with arguments returns an error. See https://github.com/python/cpython/issues/103464
            else ""
        )
        + """
        # !a?
        do_shell_called: a?
        \\*\\*\\* SyntaxError:
        # mp.delattr(pdbpp.local.GLOBAL_PDB.__class__, "do_shell")
        # !a?
        \\*\\*\\* SyntaxError:
        # help a
        .*a(rgs)
            .*Print the argument list of the current function.
        """
    )

    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
            [NUM] > .*fn()
            -> set_trace()
               5 frames hidden .*
            # n
            """.rstrip()
            )
            + expected
        )

    if sys.version_info >= (3, 12):
        expected = expected.replace(".*Print the argument", "\n.*Print the argument")

    expected += "# c"

    check(fn, expected)


def test_up_local_vars():
    def nested():
        set_trace()
        return

    def fn():
        xx = 42  # noqa: F841
        nested()

    expected = textwrap.dedent(
        """
        [NUM] > .*nested()
        -> return
           5 frames hidden .*
        # up
        [NUM] > .*fn()
        -> nested()
        # xx
        42
        # c
"""
    )

    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
        [NUM] > .*nested()
        -> set_trace()
           5 frames hidden .*
        # n
        """.rstrip()
            )
            + expected
        )
    check(fn, expected)


def test_frame():
    def a():
        b()

    def b():
        c()

    def c():
        set_trace()
        return

    expected = textwrap.dedent(f"""
        [NUM] > .*c()
        -> return
           5 frames hidden .*
        # f {count_frames() + 2 - 5}
        [NUM] > .*a()
        -> b()
        # f
        [{count_frames() + 2 - 5}] > .*a()
        -> b()
        # f 0
        [ 0] > .*()
        -> .*
        # f -1
        [{len(traceback.extract_stack())}] > .*c()
        -> return
        # c
    """)

    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*c()
                -> set_trace()
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )

    check(a, expected)


def test_fstrings(monkeypatch):
    def mocked_inspect(self, arg):
        print(f"mocked_inspect: {arg}")

    monkeypatch.setattr(PdbTest, "do_inspect", mocked_inspect)

    def f():
        set_trace()

    expected = textwrap.dedent(
        """
        --Return--
        [NUM] > .*
        -> set_trace()
           5 frames hidden .*
        # f"fstring"
        'fstring'
        # f"foo"?
        mocked_inspect: f"foo"
        # c
    """,
    )
    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*f()
                -> set_trace()
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )

    check(f, expected)


def test_prefixed_strings(monkeypatch):
    def mocked_inspect(self, arg):
        print(f"mocked_inspect: {arg}")

    monkeypatch.setattr(PdbTest, "do_inspect", mocked_inspect)

    def f():
        set_trace()

    expected = textwrap.dedent(
        """
        --Return--
        [NUM] > .*
        -> set_trace()
           5 frames hidden .*
        # b"string"
        {bytestring!r}
        # u"string"
        {unicodestring!r}
        # r"string"
        'string'
        # b"foo"?
        mocked_inspect: b"foo"
        # r"foo"?
        mocked_inspect: r"foo"
        # u"foo"?
        mocked_inspect: u"foo"
        # c
    """.format(bytestring=b"string", unicodestring="string"),
    )
    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*f()
                -> set_trace()
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )

    check(f, expected)


def test_up_down_arg():
    def a():
        b()

    def b():
        c()

    def c():
        set_trace()
        return

    expected = textwrap.dedent(
        """
        [NUM] > .*c()
        -> return
           5 frames hidden .*
        # up 3
        [NUM] > .*runpdb()
        -> func()
        # down 1
        [NUM] > .*a()
        -> b()
        # c
    """
    )
    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*c()
                -> set_trace()
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )

    check(a, expected)


def test_up_down_sticky():
    def a():
        b()

    def b():
        set_trace()
        return

    expected = textwrap.dedent(
        """
        [NUM] > .*b()
        -> return
           5 frames hidden .*
        # sticky
        <CLEARSCREEN>
        [NUM] > .*b(), 5 frames hidden

        NUM         def b():
        NUM             set_trace()
        NUM  ->         return
        # up
        <CLEARSCREEN>
        [NUM] > .*a(), 5 frames hidden

        NUM         def a()
        NUM  ->         b()
        # c
    """
    )
    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*b()
                -> set_trace()
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )

    check(a, expected)


def test_top_bottom():
    def a():
        b()

    def b():
        c()

    def c():
        set_trace()
        return

    expected = textwrap.dedent(
        f"""
        [NUM] > .*c()
        -> return
           5 frames hidden .*
        # top
        [ 0] > .*()
        -> .*
        # bottom
        [{len(traceback.extract_stack())}] > .*c()
        -> return
        # c
    """
    )
    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*c()
                -> set_trace()
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )

    check(a, expected)


def test_top_bottom_frame_post_mortem():
    def fn():
        def throws():
            0 / 0  # noqa: B018

        def f():
            throws()

        try:
            f()
        except:
            pdbpp.post_mortem(Pdb=PdbTest)

    check(
        fn,
        r"""
[2] > .*throws()
-> 0 / 0
# top
[0] > .*fn()
-> f()
# top
\*\*\* Oldest frame
# bottom
[2] > .*throws()
-> 0 / 0
# bottom
\*\*\* Newest frame
# frame -1  ### Same as bottom, no error.
[2] > .*throws()
-> 0 / 0
# frame -2
[1] > .*f()
-> throws()
# frame -3
\*\*\* Out of range
# c
""",
    )


def test_parseline():
    def fn():
        c = 42
        set_trace()
        return c

    expected = textwrap.dedent(
        """
        [NUM] > .*fn()
        -> return c
           5 frames hidden .*
        # c
        42
        # !c
        do_shell_called: 'c'
        42
        # r = 5
        # r
        5
        # r = 6
        # r
        6
        # !!c
    """
    )
    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*fn()
                -> set_trace()
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )

    check(fn, expected)


def test_parseline_with_rc_commands(tmpdir):
    """Test that parseline handles execution of rc lines during setup."""

    with tmpdir.as_cwd():
        with open(".pdbrc", "w") as f:
            f.write(
                textwrap.dedent(
                    """
                    p 'readrc'
                    alias myalias print(%1)
                    """
                )
            )

        def fn():
            alias = "trigger"  # noqa: F841
            set_trace(readrc=True)

        if sys.version_info >= (3, 13):
            expected = textwrap.dedent(
                """
                [NUM] .*fn()
                -> set_trace(readrc=True)
                   5 frames hidden.*
                'readrc'
                """.rstrip()
            )
        else:
            pdbrc_read_fixed = (
                # https://github.com/python/cpython/issues/90095
                (sys.version_info >= (3, 11, 9) and sys.version_info <= (3, 12, 1))
                or sys.version_info >= (3, 12, 2)
            ) and sys.platform != "darwin"

            # fmt: off
            expected = ("""
                --Return--""" + ("""
                'readrc'""" if not pdbrc_read_fixed else "") + """
                [NUM] > .*fn()->None
                -> set_trace(readrc=True)
                   5 frames hidden .*""" + ("" if not pdbrc_read_fixed else """
                'readrc'""")
            )
            # fmt: on

        expected = textwrap.dedent(expected) + textwrap.dedent("""
            # alias myalias
            myalias = print(%1)
            # !! alias myalias
            myalias = print(%1)
            # myalias 42
            42
            # c
            """)
        check(fn, expected)


def test_parseline_with_existing_command():
    def fn():
        c = 42
        debug = True  # noqa: F841

        class r:
            text = "r.text"

        set_trace()
        return c

    # fmt: off
    expected = """
        [NUM] > .*fn()
        -> return c
           5 frames hidden .*
        # print(pdbpp.local.GLOBAL_PDB.parseline("foo = "))
        ('foo', '=', 'foo =')
        # print(pdbpp.local.GLOBAL_PDB.parseline("c = "))
        (None, None, 'c = ')
        # print(pdbpp.local.GLOBAL_PDB.parseline("a = "))
        (None, None, 'a = ')
        # print(pdbpp.local.GLOBAL_PDB.parseline("list()"))""" + ( """
        ('list()', '', 'list()')""" if sys.version_info >= (3, 13) else """
        (None, None, 'list()
        """).rstrip() + """
        # print(pdbpp.local.GLOBAL_PDB.parseline("next(my_iter)"))""" + ("""
        ('next(my_iter)', '', 'next(my_iter)')
        """ if sys.version_info >= (3, 13) else """
        (None, None, 'next(my_iter)')
        """).rstrip() + """
        # c
        42
        # debug print(1)
        ENTERING RECURSIVE DEBUGGER
        [1] > <string>(1)<module>()
        (#) cont
        1
        LEAVING RECURSIVE DEBUGGER
        # debug
        True
        # r.text
        'r.text'
        # cont
        """
    # fmt: on

    check(fn, expected, add_313_fix=True)


def test_parseline_remembers_smart_command_escape():
    def fn():
        n = 42
        set_trace()
        n = 43
        n = 44
        return n

    if sys.version_info >= (3, 13):
        expected = """
            [NUM] > .*fn()
            -> set_trace()
               NUM frames hidden .*
            # n
            42
            # !!n
            [NUM] > .*fn()
            -> n = 43
               5 frames hidden .*
            # !!n
            [NUM] > .*fn()
            -> n = 44
               5 frames hidden .*
            # !!n
            [NUM] > .*fn()
            -> return n
               5 frames hidden .*
            # n
            44
            # c
            """
    else:
        expected = """
            [NUM] > .*fn()
            -> n = 43
               5 frames hidden .*
            # n
            42
            # !!n
            [NUM] > .*fn()
            -> n = 44
               5 frames hidden .*
            # 
            [NUM] > .*fn()
            -> return n
               5 frames hidden .*
            # n
            44
            # c
            """

    check(fn, expected)


def test_args_name():
    def fn():
        args = 42
        set_trace()
        return args

    expected = textwrap.dedent(
        """
        [NUM] > .*fn()
        -> return args
           5 frames hidden .*
        # args
        42
        # c
        """,
    )
    if sys.version_info > (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*fn()
                -> set_trace()
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )

    check(fn, expected)


def lineno():
    """Returns the current line number in our program."""
    frame = inspect.currentframe()
    assert frame
    assert frame.f_back
    return frame.f_back.f_lineno


def test_help():
    instance = PdbTest()
    instance.stdout = StringIO()

    help_params = [
        ("", r"Documented commands \(type help <topic>\):"),
        ("EOF", "Handles the receipt of EOF as a command."),
        ("a", "Print the argument"),
        ("alias", "an alias"),
        ("args", "Print the argument"),
        ("b", "set a break"),
        ("break", "set a break"),
        ("bt", "Print a stack trace"),
        ("c", "Continue execution, only stop when a breakpoint"),
        ("cl", "clear all breaks"),
        ("clear", "clear all breaks"),
        ("commands", "Specify a list of commands for breakpoint"),
        ("condition", "must evaluate to true"),
        ("cont", "Continue execution, only stop when a breakpoint"),
        ("continue", "Continue execution, only stop when a breakpoint"),
        ("d", "Move the current frame .* down"),
        ("debug", "Enter a recursive debugger"),
        ("disable", "Disables the breakpoints"),
        ("display", "Add expression to the display list"),
        ("down", "Move the current frame .* down"),
        ("ed", "Open an editor"),
        ("edit", "Open an editor"),
        ("enable", "Enables the breakpoints"),
        ("exit", "Quit from the debugger."),
        ("h", "h(elp)"),
        ("help", "h(elp)"),
        ("hf_hide", "hide hidden frames"),
        ("hf_unhide", "unhide hidden frames"),
        ("ignore", "ignore count for the given breakpoint"),
        ("interact", "Start an interactive interpreter"),
        ("j", "Set the next line that will be executed."),
        ("jump", "Set the next line that will be executed."),
        ("l", "List source code for the current file."),
        ("list", "List source code for the current file."),
        ("ll", "List source code for the current function."),
        ("longlist", "List source code for the current function."),
        ("n", "Continue execution until the next line"),
        ("next", "Continue execution until the next line"),
        ("p", "Print the value of the expression"),
        ("pp", "Pretty-print the value of the expression."),
        ("q", "Quit from the debugger."),
        ("quit", "Quit from the debugger."),
        ("r", "Continue execution until the current function returns."),
        ("restart", "Restart the debugged python program."),
        ("return", "Continue execution until the current function returns."),
        ("run", "Restart the debugged python program"),
        ("s", "Execute the current line, stop at the first possible occasion"),
        ("step", "Execute the current line, stop at the first possible occasion"),
        ("sticky", "Toggle sticky mode"),
        ("tbreak", "arguments as break"),
        ("u", "Move the current frame .* up"),
        ("unalias", "specified alias."),
        ("undisplay", "Remove expression from the display list"),
        ("unt", "until the line"),
        ("until", "until the line"),
        ("up", "Move the current frame .* up"),
        ("w", "Print a stack trace"),
        ("whatis", "Prints? the type of the argument."),
        ("where", "Print a stack trace"),
        ("hidden_frames", 'Some frames might be marked as "hidden"'),
        ("exec", r"Execute the \(one-line\) statement"),
        ("hf_list", r"\*\*\* No help"),
        ("paste", r"\*\*\* No help"),
        ("put", r"\*\*\* No help"),
        ("retval", r"\*\*\* No help|return value"),
        ("rv", r"\*\*\* No help|return value"),
        ("source", r"\*\*\* No help"),
        ("unknown_command", r"\*\*\* No help"),
        ("help", "print the list of available commands."),
    ]

    # Redirect sys.stdout because Python 2 pdb.py has `print >>self.stdout` for
    # some functions and plain ol' `print` for others.
    oldstdout = sys.stdout
    sys.stdout = instance.stdout
    errors = []
    try:
        for command, expected_regex in help_params:
            instance.do_help(command)
            output = instance.stdout.getvalue()
            if not re.search(expected_regex, output):
                errors.append(command)
    finally:
        sys.stdout = oldstdout

    if errors:
        pytest.fail("unexpected help for: {}".format(", ".join(errors)))


def test_shortlist():
    def fn():
        a = 1
        set_trace(Config=ConfigTest)
        return a

    expected = textwrap.dedent(
        f"""
        [NUM] > .*fn()
        -> return a
           5 frames hidden .*
        # l {fn.__code__.co_firstlineno}, 3
        NUM +\t    def fn():
        NUM +\t        a = 1
        NUM +\t        set_trace(Config=ConfigTest)
        NUM +->	        return a
        # c
        """,
    )

    if sys.version_info > (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*fn()
                -> set_trace(Config=ConfigTest)
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )

    check(
        fn,
        expected,
    )


def test_shortlist_with_pygments_and_EOF():
    def fn():
        a = 1
        set_trace(Config=ConfigWithPygments)
        return a

    expected = textwrap.dedent(f"""
        [NUM] > .*fn()
        -> ^[[38;5;28;01mreturn^[[39;00m a
           5 frames hidden .*
        # l {100000}, 3
        [EOF]
        # c
        """)
    if sys.version_info > (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*fn()
                -> set_trace(Config^[[38;5;241m=^[[39mConfigWithPygments)
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )

    check(
        fn,
        expected,
    )


def test_shortlist_with_highlight_and_EOF():
    def fn():
        a = 1
        set_trace(Config=ConfigWithHighlight)
        return a

    expected = textwrap.dedent(f"""
        [NUM] > .*fn()
        -> return a
           5 frames hidden .*
        # l {100000}, 3
        [EOF]
        # c
        """)
    if sys.version_info > (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*fn()
                -> set_trace(Config=ConfigWithHighlight)
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )

    check(
        fn,
        expected,
    )


@pytest.mark.parametrize("config", [ConfigWithPygments, ConfigWithPygmentsNone])
def test_shortlist_with_pygments(config, monkeypatch):
    def fn():
        a = 1
        set_trace(Config=config)

        return a

    calls = []
    orig_get_func = pdbpp.Pdb._get_source_highlight_function

    def check_calls(self):
        orig_highlight = orig_get_func(self)
        calls.append(["get", self])

        def new_highlight(src):
            calls.append(["highlight", src])
            return orig_highlight(src)

        return new_highlight

    monkeypatch.setattr(pdbpp.Pdb, "_get_source_highlight_function", check_calls)

    if int(pygments_major) >= 2 and int(pygments_minor) >= 19:
        highlighted_code = (
            "^[[38;5;28;01mdef^[[39;00m^[[38;5;250m ^[[39m^[[38;5;21mfn^[[39m():"
        )
    else:  # pygments 2.18
        highlighted_code = "^[[38;5;28;01mdef^[[39;00m ^[[38;5;21mfn^[[39m():"

    expected = textwrap.dedent(f"""
        [NUM] > .*fn()
        -> ^[[38;5;28;01mreturn^[[39;00m a
           5 frames hidden .*
        # l {fn.__code__.co_firstlineno}, 5
        NUM +\t    {highlighted_code}
        NUM +\t        a ^[[38;5;241m=^[[39m ^[[38;5;241m1^[[39m
        NUM +\t        set_trace(Config^[[38;5;241m=^[[39mconfig)
        NUM +\t$
        NUM +->\t        ^[[38;5;28;01mreturn^[[39;00m a
        NUM +\t$
        # c
    """)
    expected_n_calls = 3
    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*fn()
                -> set_trace(Config^[[38;5;241m=^[[39mconfig)
                   5 frames hidden .*
                # n
                """.rstrip()
            )
            + expected
        )
        expected_n_calls += 1

    check(
        fn,
        expected,
    )
    assert len(calls) == expected_n_calls, calls


def test_shortlist_with_partial_code():
    """Highlights the whole file, to handle partial docstring etc."""

    def fn():
        """
        1
        2
        3
        """
        a = 1
        set_trace(Config=ConfigWithPygments)
        return a

    expected = textwrap.dedent(
        """
        [NUM] > .*fn()
        -> ^[[38;5;28;01mreturn^[[39;00m a
           5 frames hidden .*
        # l
        NUM \t^[[38;5;124.*m        2^[[39.*m
        NUM \t^[[38;5;124.*m        3^[[39.*m
        NUM \t^[[38;5;124.*m        \"\"\"^[[39.*m
        NUM \t        a ^[[38;5;241m=^[[39m ^[[38;5;241m1^[[39m
        NUM \t        set_trace(Config^[[38;5;241m=^[[39mConfigWithPygments)
        NUM ->\t        ^[[38;5;28;01mreturn^[[39;00m a
        .*
        NUM \t.*
        NUM \t.*
        NUM \t.*
        NUM \t.*
        # c
        """,
    )
    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*fn()
                -> set_trace(Config^[[38;5;241m=^[[39mConfigWithPygments)
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )
    check(
        fn,
        expected,
    )


def test_truncated_source_with_pygments():
    def fn():
        """some docstring longer than maxlength for truncate_long_lines, which is 80"""
        a = 1
        set_trace(Config=ConfigWithPygments)

        return a

    if int(pygments_major) >= 2 and int(pygments_minor) >= 19:
        highlighted_code = (
            "^[[38;5;28;01mdef^[[39;00m^[[38;5;250m ^[[39m^[[38;5;21mfn^[[39m():"
        )
    else:  # pygments 2.18
        highlighted_code = "^[[38;5;28;01mdef^[[39;00m ^[[38;5;21mfn^[[39m():"

    expected = textwrap.dedent(f"""
            [NUM] > .*fn()
            -> ^[[38;5;28;01mreturn^[[39;00m a
               5 frames hidden .*
            # l {fn.__code__.co_firstlineno}, 5
            NUM +\t    {highlighted_code}
            NUM +\t^[[38;5;250m        ^[[39m^[[38;5;124.*m\"\"\"some docstring longer than maxlength for truncate_long_lines, which is 80\"\"\"^[[39.*m
            NUM +\t        a ^[[38;5;241m=^[[39m ^[[38;5;241m1^[[39m
            NUM +\t        set_trace(Config^[[38;5;241m=^[[39mConfigWithPygments)
            NUM +\t$
            NUM ->\t        ^[[38;5;28;01mreturn^[[39;00m a
            # sticky
            <CLEARSCREEN>
            [NUM] > .*fn(), 5 frames hidden

            NUM +       {highlighted_code}
            NUM +^[[38;5;250m        ^[[39m^[[38;5;124.*m\"\"\"some docstring longer than maxlength for truncate_long_lines^[[39.*m
            NUM +           a ^[[38;5;241m=^[[39m ^[[38;5;241m1^[[39m
            NUM +           set_trace(Config^[[38;5;241m=^[[39mConfigWithPygments)
            NUM +$
            NUM +->         ^[[38;5;28;01mreturn^[[39;00m a
            # c
            """)  # noqa: UP032

    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*fn()
                -> set_trace(Config^[[38;5;241m=^[[39mConfigWithPygments)
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )
    check(
        fn,
        expected,
    )


def test_truncated_source_with_pygments_and_highlight():
    def fn():
        """some docstring longer than maxlength for truncate_long_lines, which is 80"""
        a = 1
        set_trace(Config=ConfigWithPygmentsAndHighlight)

        return a

    if int(pygments_major) >= 2 and int(pygments_minor) >= 19:
        highlighted_code = (
            "^[[38;5;28;01mdef^[[39;00m^[[38;5;250m ^[[39m^[[38;5;21mfn^[[39m():"
        )
    else:  # pygments 2.18
        highlighted_code = "^[[38;5;28;01mdef^[[39;00m ^[[38;5;21mfn^[[39m():"

    expected = textwrap.dedent(
        f"""
        [NUM] > .*fn()
        -> ^[[38;5;28;01mreturn^[[39;00m a
           5 frames hidden .*
        # l {fn.__code__.co_firstlineno}, 5
        <COLORNUM> +\t    {highlighted_code}
        <COLORNUM> +\t^[[38;5;250m        ^[[39m^[[38;5;124.*m\"\"\"some docstring longer than maxlength for truncate_long_lines, which is 80\"\"\"^[[39.*m
        <COLORNUM> +\t        a ^[[38;5;241m=^[[39m ^[[38;5;241m1^[[39m
        <COLORNUM> +\t        set_trace(Config^[[38;5;241m=^[[39mConfigWithPygmentsAndHighlight)
        <COLORNUM> +\t$
        <COLORNUM> +->\t        ^[[38;5;28;01mreturn^[[39;00m a
        # sticky
        <CLEARSCREEN>
        [NUM] > .*fn(), 5 frames hidden

        <COLORNUM> +       {highlighted_code}
        <COLORNUM> +^[[38;5;250m        ^[[39m^[[38;5;124.*m\"\"\"some docstring longer than maxlength for truncate_long_lines<PYGMENTSRESET>
        <COLORNUM> +           a ^[[38;5;241m=^[[39m ^[[38;5;241m1^[[39m
        <COLORNUM> +           set_trace(Config^[[38;5;241m=^[[39mConfigWithPygmentsAndHighlight)
        <COLORNUM> +$
        <COLORCURLINE> +->         ^[[38;5;28;01;44mreturn<PYGMENTSRESET> a                                                       ^[[00m
        # c
        """,  # noqa: UP032
    )
    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*fn()
                -> set_trace(Config^[[38;5;241m=^[[39mConfigWithPygmentsAndHighlight)
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )
    check(
        fn,
        expected,
    )


def test_shortlist_with_highlight():
    def fn():
        a = 1
        set_trace(Config=ConfigWithHighlight)

        return a

    expected = textwrap.dedent(
        f"""
        [NUM] > .*fn()
        -> return a
           5 frames hidden .*
        # l {fn.__code__.co_firstlineno}, 4
        <COLORNUM> +\t    def fn():
        <COLORNUM> +\t        a = 1
        <COLORNUM> +\t        set_trace(Config=ConfigWithHighlight)
        <COLORNUM> +\t$
        <COLORNUM> +->\t        return a
        # c
        """,
    )

    if sys.version_info > (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*fn()
                -> set_trace(Config=ConfigWithHighlight)
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )

    check(
        fn,
        expected,
    )


def test_shortlist_without_arg():
    """Ensure that forget was called for lineno."""

    def fn():
        a = 1
        set_trace(Config=ConfigTest)
        return a

    expected = textwrap.dedent(
        """
        [NUM] > .*fn()
        -> return a
           5 frames hidden .*
        # l
        .*
        .*
        .*
        .*
        .*
        .*
        .*
        .*
        .*
        .*
        .*
        # c
        """,
    )
    if sys.version_info > (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*fn()
                -> set_trace(Config=ConfigTest)
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )

    check(
        fn,
        expected,
    )


def test_shortlist_heuristic():
    def fn():
        a = 1
        set_trace(Config=ConfigTest)
        return a

    expected = textwrap.dedent(
        f"""
        [NUM] > .*fn()
        -> return a
           5 frames hidden .*
        # list {fn.__code__.co_firstlineno}, 3
        NUM \t    def fn():
        NUM \t        a = 1
        NUM \t        set_trace(Config=ConfigTest)
        NUM ->	        return a
        # list(range(4))
        [0, 1, 2, 3]
        # c
        """,
    )
    if sys.version_info > (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*fn()
                -> set_trace(Config=ConfigTest)
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )

    check(
        fn,
        expected,
    )


def test_shortlist_with_second_set_trace_resets_lineno():
    def fn():
        def f1():
            set_trace(cleanup=False)

        set_trace()
        f1()

    expected = textwrap.dedent(
        rf"""
        [NUM] > .*fn()
        -> f1()
           5 frames hidden .*
        # l {fn.__code__.co_firstlineno}, 2
        NUM \t    def fn():
        NUM \t        def f1():
        NUM \t            set_trace(cleanup=False)
        # import pdb; pdbpp.local.GLOBAL_PDB.lineno
        {fn.__code__.co_firstlineno + 2}
        # c
        """.rstrip()
    ) + textwrap.dedent(
        """
        [NUM] > .*f1()
        -> set_trace(cleanup=False)
           5 frames hidden .*
        # import pdb; pdbpp.local.GLOBAL_PDB.lineno
        # c
        """
        if sys.version_info >= (3, 13)
        else """
        --Return--
        [NUM] > .*f1()->None
        -> set_trace(cleanup=False)
           5 frames hidden .*
        # import pdb; pdbpp.local.GLOBAL_PDB.lineno
        # c
        """
    )

    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*fn()
                -> set_trace()
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )

    check(
        fn,
        expected,
    )


def test_longlist():
    def fn():
        a = 1
        set_trace()
        return a

    expected = textwrap.dedent(
        """
        [NUM] > .*fn()
        -> return a
           5 frames hidden .*
        # ll
        NUM         def fn():
        NUM             a = 1
        NUM             set_trace()
        NUM  ->         return a
        # c
        """,
    )
    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*fn()
                -> set_trace()
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )

    check(
        fn,
        expected,
    )


def test_longlist_displays_whole_function():
    """`ll` displays the whole function (no cutoff)."""

    def fn():
        set_trace()
        a = 1
        a = 1
        a = 1
        a = 1
        a = 1
        a = 1
        a = 1
        a = 1
        a = 1
        return a

    expected = textwrap.dedent(
        """
        [NUM] > .*fn()
        -> a = 1
           5 frames hidden (try 'help hidden_frames')
        # ll
        NUM         def fn():
        NUM             set_trace()
        NUM  ->         a = 1
        NUM             a = 1
        NUM             a = 1
        NUM             a = 1
        NUM             a = 1
        NUM             a = 1
        NUM             a = 1
        NUM             a = 1
        NUM             a = 1
        NUM             return a
        # c

        """,
    )
    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*fn()
                -> set_trace()
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )

    check(
        fn,
        expected,
        terminal_size=(len(__file__) + 50, 10),
    )


# a lot of the following tests are sensitive to formatting
# so let's avoid formatting to avoid headaches
# fmt: off


class TestListWithChangedSource:
    """Uses the cached (current) code."""

    @pytest.fixture(autouse=True)
    def setup_myfile(self, tmpdir, monkeypatch):
        with open(tmpdir / "myfile.py", "w") as fh:
            fh.write(textwrap.dedent("""
            from pdbpp import set_trace

            def rewrite_file():
                with open(__file__, "w") as f:
                    f.write("something completely different")

            def after_settrace():
                import linecache
                linecache.checkcache()

            def fn():
                set_trace()
                after_settrace()
                set_trace()
                a = 3
            """))
        monkeypatch.setenv("PDBPP_COLORS", "0")
        monkeypatch.syspath_prepend(tmpdir.strpath)

    @pytest.mark.xfail(
        strict=False,
        reason="Flaky: fails in tox, succeeds when called with pytest - see https://github.com/nedbat/coveragepy/issues/1420",
    )
    def test_list_with_changed_source(self):
        if "coverage" in sys.modules:
            pytest.fail(reason="Fails when called in coverage, see https://github.com/nedbat/coveragepy/issues/1420")

        from myfile import fn

        check(
            fn,
            r"""
    [NUM] > .*fn()
    -> after_settrace()
       5 frames hidden (try 'help hidden_frames')
    (Pdb++) l
    NUM  \t    import linecache$
    NUM  \t    linecache.checkcache()$
    NUM  \t$
    NUM  \tdef fn():
    NUM  \t    set_trace()
    NUM  ->\t    after_settrace()
    NUM  \t    set_trace()
    NUM  \t    a = 3
    [EOF]
    (Pdb++) rewrite_file()
    (Pdb++) c
    [NUM] > .*fn()
    -> a = 3
       5 frames hidden (try 'help hidden_frames')
    (Pdb++) l
    NUM  \t
    NUM  \tdef fn():
    NUM  \t    set_trace()
    NUM  \t    after_settrace()
    NUM  \t    set_trace()
    NUM  ->\t    a = 3
    [EOF]
    (Pdb++) c
    """,
        )

    @pytest.mark.xfail(
        strict=False,
        reason="Flaky: fails in tox, succeeds when called with pytest - see https://github.com/nedbat/coveragepy/issues/1420",
    )
    def test_longlist_with_changed_source(self):
        if "coverage" in sys.modules:
            pytest.fail(reason="Fails when called in coverage, see https://github.com/nedbat/coveragepy/issues/1420")

        from myfile import fn

        check(fn, r"""
    [NUM] > .*myfile.py(NUM)fn()
    -> after_settrace()
       5 frames hidden (try 'help hidden_frames')
    (Pdb++) ll
    NUM     def fn():
    NUM         set_trace()
    NUM  ->     after_settrace()
    NUM         set_trace()
    NUM         a = 3
    (Pdb++) rewrite_file()
    (Pdb++) c
    [NUM] > .*fn()
    -> a = 3
       5 frames hidden (try 'help hidden_frames')
    (Pdb++) ll
    NUM     def fn():
    NUM         set_trace()
    NUM         after_settrace()
    NUM         set_trace()
    NUM  ->     a = 3
    (Pdb++) c
    """,
        )


# fmt: on
def test_longlist_with_highlight():
    def fn():
        a = 1
        set_trace(Config=ConfigWithHighlight)
        return a

    expected = textwrap.dedent(
        r"""
        [NUM] > .*fn()
        -> return a
           5 frames hidden .*
        # ll
        <COLORNUM>         def fn():
        <COLORNUM>             a = 1
        <COLORNUM>             set_trace(Config=ConfigWithHighlight)
        <COLORCURLINE> +->         return a                                                       ^[[00m$
        # c
        """,
    )
    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*fn()
                -> set_trace(Config=ConfigWithHighlight)
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )

    check(
        fn,
        expected,
    )


def test_display():
    def fn():
        a = 1
        set_trace()
        b = 1  # noqa: F841
        a = 2
        a = 3
        return a

    expected = textwrap.dedent("""
        [NUM] > .*fn()
        -> b = 1
           5 frames hidden .*
        # display a
        # n
        [NUM] > .*fn()
        -> a = 2
           5 frames hidden .*
        # n
        [NUM] > .*fn()
        -> a = 3
           5 frames hidden .*
        a: 1 --> 2
        # undisplay a
        # n
        [NUM] > .*fn()
        -> return a
           5 frames hidden .*
        # c
        """)
    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*fn()
                -> set_trace()
                   5 frames hidden .*
                # n
                """.rstrip()
            )
            + expected
        )

    check(fn, expected)


def test_display_undefined():
    def fn():
        set_trace()
        b = 42
        return b

    expected = textwrap.dedent("""
                [NUM] > .*fn()
                -> b = 42
                   5 frames hidden .*
                # display b
                # n
                [NUM] > .*fn()
                -> return b
                   5 frames hidden .*
                b: <undefined> --> 42
                # c
                """)

    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*fn()
                -> set_trace()
                   5 frames hidden .*
                # n
                """.rstrip()
            )
            + expected
        )

    check(fn, expected)


def test_sticky():
    def fn():
        set_trace()
        a = 1
        b = 2  # noqa: F841
        c = 3  # noqa: F841
        return a

    expected = """
        [NUM] > .*fn()
        -> a = 1
           5 frames hidden .*
        # sticky
        <CLEARSCREEN>
        [NUM] > .*fn(), 5 frames hidden

        NUM         def fn():
        NUM             set_trace()
        NUM  ->         a = 1
        NUM             b = 2
        NUM             c = 3
        NUM             return a
        # n
        <CLEARSCREEN>
        [NUM] > .*fn(), 5 frames hidden

        NUM         def fn():
        NUM             set_trace()
        NUM             a = 1
        NUM  ->         b = 2  # noqa: F841
        NUM             c = 3
        NUM             return a
        # sticky
        # n
        [NUM] > .*fn()
        -> c = 3
           5 frames hidden .*
        # c
        """

    check(fn, expected, add_313_fix=True)


def test_sticky_resets_cls():
    def fn():
        set_trace()
        a = 1
        print(a)
        set_trace(cleanup=False)
        return a

    if sys.version_info >= (3, 13):
        marker_313 = "->"
        marker_pre_313 = "  "
    else:
        marker_313 = "  "
        marker_pre_313 = "->"

    expected = (
        f"""
        [NUM] > .*fn()
        -> a = 1
           5 frames hidden .*
        # sticky
        <CLEARSCREEN>
        [NUM] > .*fn(), 5 frames hidden

        NUM         def fn():
        NUM             set_trace()
        NUM  ->         a = 1
        NUM             print(a)
        NUM             set_trace(cleanup=False)
        NUM             return a
        # c
        1
        [NUM] > .*fn(), 5 frames hidden

        NUM         def fn():
        NUM             set_trace()
        NUM             a = 1
        NUM             print(a)
        NUM  {marker_313}         set_trace(cleanup=False)
        NUM  {marker_pre_313}         return a
        # n
        <CLEARSCREEN>
        [NUM] > .*fn().*

        NUM         def fn():
        NUM             set_trace()
        NUM             a = 1
        NUM             print(a)
        NUM             set_trace(cleanup=False)
        NUM  ->         return a
        """
    ) + (
        """
        # c
        """
        if sys.version_info >= (3, 13)
        else r"""
        \ return 1
        # c
        """
    ).lstrip()

    check(fn, expected, add_313_fix=True)


def test_sticky_with_same_frame():
    def fn():
        def inner(cleanup):
            set_trace(cleanup=cleanup)
            print(cleanup)

        for cleanup in (True, False):
            inner(cleanup)

    if sys.version_info >= (3, 13):
        marker_313 = "->"
        marker_pre_313 = "  "
    else:
        marker_313 = "  "
        marker_pre_313 = "->"

    expected = f"""
        [NUM] > .*inner()
        -> print(cleanup)
           5 frames hidden (try 'help hidden_frames')
        # sticky
        <CLEARSCREEN>
        [NUM] > .*inner(), 5 frames hidden

        NUM             def inner(cleanup):
        NUM                 set_trace(cleanup=cleanup)
        NUM  ->             print(cleanup)
        # n
        <CLEARSCREEN>
        True
        [NUM] > .*inner()->None, 5 frames hidden

        NUM             def inner(cleanup):
        NUM                 set_trace(cleanup=cleanup)
        NUM  ->             print(cleanup)
         return None
        # c
        [NUM] > .*inner(), 5 frames hidden

        NUM             def inner(cleanup):
        NUM  {marker_313}             set_trace(cleanup=cleanup)
        NUM  {marker_pre_313}             print(cleanup)
        # n
        <CLEARSCREEN>
        """ + (
        """
        [NUM] > .*inner().*

        NUM             def inner(cleanup):
        NUM                 set_trace(cleanup=cleanup)
        NUM  ->             print(cleanup)
        # c
        False
        """.lstrip()
        if sys.version_info >= (3, 13)
        else """
        False
        [NUM] > .*inner()->None, 5 frames hidden

        NUM             def inner(cleanup):
        NUM                 set_trace(cleanup=cleanup)
        NUM  ->             print(cleanup)
         return None
        # c
        """.lstrip()
    )

    if sys.version_info >= (3, 13):
        expected = textwrap.dedent(
            """
            [NUM] > .*inner()
            -> set_trace(cleanup=cleanup)
               5 frames hidden .*
            # n
            """.rstrip(),
        ) + textwrap.dedent(expected)

    check(fn, expected)


def test_sticky_range():
    def fn():
        set_trace()
        a = 1
        b = 2  # noqa: F841
        c = 3  # noqa: F841
        return a

    _, lineno = inspect.getsourcelines(fn)
    start = lineno + 1
    end = lineno + 3

    expected = f"""
        [NUM] > .*fn()
        -> a = 1
           5 frames hidden .*
        # sticky {start} {end}
        <CLEARSCREEN>
        [NUM] > .*fn(), 5 frames hidden

        {start} \\s+         set_trace()
        NUM  ->         a = 1
        NUM             b = 2
        # c
        """

    check(fn, expected, add_313_fix=True)


def test_sticky_by_default():
    class MyConfig(ConfigTest):
        sticky_by_default = True

    def fn():
        set_trace(Config=MyConfig)
        a = 1
        b = 2  # noqa: F841
        c = 3  # noqa: F841
        return a

    if sys.version_info >= (3, 13):
        marker_313 = "->"
        marker_pre_313 = "  "
    else:
        marker_313 = "  "
        marker_pre_313 = "->"

    expected = f"""
        [NUM] > .*fn(), 5 frames hidden

        NUM         def fn():
        NUM  {marker_313}         set_trace(Config=MyConfig)
        NUM  {marker_pre_313}         a = 1
        NUM             b = 2
        NUM             c = 3
        NUM             return a
        # n
        <CLEARSCREEN>
        [NUM] > .*fn(), 5 frames hidden

        NUM         def fn():
        NUM             set_trace(Config=MyConfig)
        NUM  {marker_313}         a = 1
        NUM  {marker_pre_313}         b = 2
        NUM             c = 3
        NUM             return a
        # c
    """
    check(fn, expected)


def test_sticky_by_default_with_use_pygments_auto():
    class MyConfig(ConfigTest):
        sticky_by_default = True
        use_pygments = None

    def fn():
        set_trace(Config=MyConfig)
        a = 1
        return a

    if int(pygments_major) >= 2 and int(pygments_minor) >= 19:
        highlighted_code = (
            "^[[38;5;28;01mdef^[[39;00m^[[38;5;250m ^[[39m^[[38;5;21mfn^[[39m():"
        )
    else:  # pygments 2.18
        highlighted_code = "^[[38;5;28;01mdef^[[39;00m ^[[38;5;21mfn^[[39m():"

    if sys.version_info >= (3, 13):
        expected = textwrap.dedent(
            f"""
            [NUM] > .*fn().*

            NUM         {highlighted_code}
            NUM  ->         set_trace(Config^[[38;5;241m=^[[39mMyConfig)
            NUM             a ^[[38;5;241m=^[[39m ^[[38;5;241m1^[[39m
            NUM             ^[[38;5;28;01mreturn^[[39;00m a
            # n
            <CLEARSCREEN>
            [NUM] > .*fn(), 5 frames hidden

            NUM         {highlighted_code}
            NUM             set_trace(Config^[[38;5;241m=^[[39mMyConfig)
            NUM  ->         a ^[[38;5;241m=^[[39m ^[[38;5;241m1^[[39m
            NUM             ^[[38;5;28;01mreturn^[[39;00m a
            # c
            """,
        )
    else:
        expected = textwrap.dedent(
            f"""
            [NUM] > .*fn(), 5 frames hidden

            NUM         {highlighted_code}
            NUM             set_trace(Config^[[38;5;241m=^[[39mMyConfig)
            NUM  ->         a ^[[38;5;241m=^[[39m ^[[38;5;241m1^[[39m
            NUM             ^[[38;5;28;01mreturn^[[39;00m a
            # c
            """,
        )

    check(
        fn,
        expected,
    )


def test_sticky_dunder_exception():
    """Test __exception__ being displayed in sticky mode."""

    def fn():
        def raises():
            raise InnerTestException()

        set_trace()
        raises()

    expected = f"""
        [NUM] > .*fn()
        -> raises()
           5 frames hidden (try 'help hidden_frames')
        # n
        .*InnerTestException.*  ### via pdbpp.Pdb.user_exception (differs on py3/py27)
        [NUM] > .*fn()
        -> raises()
           5 frames hidden .*
        # sticky
        <CLEARSCREEN>
        [NUM] > {RE_THIS_FILE_CANONICAL}(NUM)fn(), 5 frames hidden

        NUM         def fn():
        NUM             def raises():
        NUM                 raise InnerTestException()
        NUM
        NUM             set_trace(.*)
        NUM  ->         raises()
        InnerTestException:
        # c
        """

    check(fn, expected, add_313_fix=True)


def test_sticky_dunder_exception_with_highlight():
    """Test __exception__ being displayed in sticky mode."""

    def fn():
        def raises():
            raise InnerTestException()

        set_trace(Config=ConfigWithHighlight)
        raises()

    expected = textwrap.dedent(f"""
        [NUM] > .*fn()
        -> raises()
           5 frames hidden (try 'help hidden_frames')
        # n
        .*InnerTestException.*  ### via pdbpp.Pdb.user_exception (differs on py3/py27)
        [NUM] > .*fn()
        -> raises()
           5 frames hidden .*
        # sticky
        <CLEARSCREEN>
        [NUM] > <COLORFNAME>{RE_THIS_FILE_CANONICAL}<COLORRESET>(<COLORNUM>)fn(), 5 frames hidden

        <COLORNUM>         def fn():
        <COLORNUM>             def raises():
        <COLORNUM>                 raise InnerTestException()
        <COLORNUM>
        <COLORNUM>             set_trace(.*)
        <COLORCURLINE>  ->         raises().*
        <COLORLNUM>InnerTestException: <COLORRESET>
        # c
    """)
    if sys.version_info >= (3, 13):
        expected = textwrap.dedent(
            """
            [NUM] > .*fn()
            -> set_trace(Config=ConfigWithHighlight)
               5 frames hidden .*
            # n
            """.rstrip(),
        ) + textwrap.dedent(expected)

    check(fn, expected)


def test_format_exc_for_sticky():
    _pdb = PdbTest()
    f = _pdb._format_exc_for_sticky

    assert f((Exception, Exception())) == "Exception: "

    exc_from_str = Exception("exc_from_str")

    class UnprintableExc:
        def __str__(self):
            raise exc_from_str

    assert f((UnprintableExc, UnprintableExc())) == (
        f"UnprintableExc: (unprintable exception: {exc_from_str!r})"
    )

    class UnprintableExc:
        def __str__(self):
            class RaisesInRepr(Exception):
                def __repr__(self):
                    raise Exception()

            raise RaisesInRepr()

    assert f((UnprintableExc, UnprintableExc())) == (
        "UnprintableExc: (unprintable exception)"
    )

    assert f((1, 3, 3)) == "pdbpp: got unexpected __exception__: (1, 3, 3)"


def test_sticky_dunder_return():
    """Test __return__ being displayed in sticky mode."""

    def fn():
        def returns():
            return 40 + 2

        set_trace()
        returns()

    expected = f"""
        [NUM] > .*fn()
        -> returns()
           5 frames hidden (try 'help hidden_frames')
        # s
        --Call--
        [NUM] > .*returns()
        -> def returns()
           5 frames hidden .*
        # sticky
        <CLEARSCREEN>
        [NUM] > .*(NUM)returns(), 5 frames hidden

        NUM  ->         def returns():
        NUM                 return 40 \\+ 2
        # retval
        \\*\\*\\* Not yet returned!
        # r
        <CLEARSCREEN>
        [NUM] > {RE_THIS_FILE_CANONICAL}(NUM)returns()->42, 5 frames hidden

        NUM             def returns():
        NUM  ->             return 40 \\+ 2
         return 42
        # retval
        42
        # c
        """

    check(fn, expected, add_313_fix=True)


def test_sticky_with_user_exception():
    def fn():
        def throws():
            raise InnerTestException()

        set_trace()
        throws()

    expected = """
        [NUM] > .*fn()
        -> throws()
           5 frames hidden (try 'help hidden_frames')
        # s
        --Call--
        [NUM] > .*throws()
        -> def throws():
           5 frames hidden .*
        # sticky
        <CLEARSCREEN>
        [NUM] > .*throws(), 5 frames hidden

        NUM  ->         def throws():
        NUM                 raise InnerTestException()
        # n
        <CLEARSCREEN>
        [NUM] > .*throws(), 5 frames hidden

        NUM             def throws():
        NUM  ->             raise InnerTestException()
        # n
        <CLEARSCREEN>
        [NUM] > .*throws(), 5 frames hidden

        NUM             def throws():
        NUM  ->             raise InnerTestException()
        InnerTestException:
        # c
        """

    check(fn, expected, add_313_fix=True)


def test_sticky_last_value():
    """sys.last_value is displayed in sticky mode."""

    def outer():
        try:
            raise ValueError("very long excmsg\n" * 10)
        except ValueError:
            sys.last_value, sys.last_traceback = sys.exc_info()[1:]

    def fn():
        outer()
        set_trace()

        __exception__ = "foo"  # noqa: F841
        set_trace(cleanup=False)

    expected = r"""
        [NUM] > .*fn()
        -> __exception__ = "foo"
           5 frames hidden (try 'help hidden_frames')
        # sticky
        <CLEARSCREEN>
        [NUM] > .*fn(), 5 frames hidden

        NUM         def fn():
        NUM             outer()
        NUM             set_trace()
        NUM
        NUM  ->         __exception__ = "foo"  # noqa: F841
        NUM             set_trace(cleanup=False)
        ValueError: very long excmsg\\nvery long excmsg\\nvery long e…
        # c
        [NUM] > .*fn().*frames hidden

        NUM         def fn():
        NUM             outer()
        NUM             set_trace()
        NUM
        NUM             __exception__ = "foo"  # noqa: F841
        NUM  ->         set_trace(cleanup=False)
        pdbpp: got unexpected __exception__: 'foo'
        # c
    """
    saved = sys.exc_info()[1:]
    try:
        check(
            fn,
            expected,
            terminal_size=(60, 20),
            add_313_fix=True,
        )
    finally:
        sys.last_value, sys.last_traceback = saved


def test_sticky_dunder_return_with_highlight():
    class Config(ConfigWithHighlight, ConfigWithPygments):
        pass

    def fn():
        def returns():
            return 40 + 2

        set_trace(Config=Config)
        returns()

    to_run = "# s\n# sticky\n# r\n# retval\n# c"
    if sys.version_info >= (3, 13):
        to_run = f"# n\n{to_run}"

    expected, lines = run_func(fn, to_run)
    assert lines[-4:] == [
        "^[[36;01m return 42^[[00m",
        "# retval",
        "42",
        "# c",
    ]

    colored_cur_lines = [
        x for x in lines if x.startswith("^[[44m^[[36;01;44m") and "->" in x
    ]
    assert len(colored_cur_lines) == 2


def test_sticky_cutoff_with_tail():
    class MyConfig(ConfigTest):
        sticky_by_default = True

    def fn():
        set_trace(Config=MyConfig)
        print(1)
        # 1
        # 2
        # 3
        return

    expected = textwrap.dedent("""
        [NUM] > .*fn(), 5 frames hidden

        NUM         def fn():
        NUM             set_trace(Config=MyConfig)
        NUM  ->         print(1)
        NUM             # 1
        NUM             # 2
        ...
        # c
        1
        """)

    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent("""
            [NUM] > .*(NUM)fn(), .* frames hidden

            NUM         def fn():
            NUM  ->         set_trace(Config=MyConfig)
            NUM             print(1)
            NUM             # 1
            NUM             # 2
            ...
            # n
            """)
            + expected
        )

    check(
        fn,
        expected,
        terminal_size=(len(__file__) + 50, 10),
    )


def test_sticky_cutoff_with_head():
    class MyConfig(ConfigTest):
        sticky_by_default = True

    def fn():
        # 1
        # 2
        # 3
        # 4
        # 5
        set_trace(Config=MyConfig)
        print(1)
        return

    expected = textwrap.dedent("""
        [NUM] > .*fn(), 5 frames hidden

        ...
        NUM             # 4
        NUM             # 5
        NUM             set_trace(Config=MyConfig)
        NUM  ->         print(1)
        NUM             return
        # c
        1
        """)
    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent("""
            [NUM] > .*(NUM)fn(), 5 frames hidden

            ...
            NUM             # 4
            NUM             # 5
            NUM  ->         set_trace(Config=MyConfig)
            NUM             print(1)
            NUM             return
            # n
            <CLEARSCREEN>""")
            + expected
        )

    check(
        fn,
        expected,
        terminal_size=(len(__file__) + 50, 10),
    )


def test_sticky_cutoff_with_head_and_tail():
    class MyConfig(ConfigTest):
        sticky_by_default = True

    def fn():
        # 1
        # 2
        # 3
        set_trace(Config=MyConfig)
        print(1)
        # 1
        # 2
        # 3
        return

    expected = textwrap.dedent("""
        [NUM] > .*fn(), 5 frames hidden

        ...
        NUM             set_trace(Config=MyConfig)
        NUM  ->         print(1)
        NUM             # 1
        NUM             # 2
        ...
        # c
        1
        """)

    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent("""
            [NUM] > .*(NUM)fn(), 5 frames hidden

            ...
            NUM             # 3
            NUM  ->         set_trace(Config=MyConfig)
            NUM             print(1)
            NUM             # 1
            ...
            # n
            <CLEARSCREEN>""")
            + expected
        )
    check(
        fn,
        expected,
        terminal_size=(len(__file__) + 50, 10),
    )


def test_sticky_cutoff_with_long_head_and_tail():
    class MyConfig(ConfigTest):
        sticky_by_default = True

    def fn():
        # 1
        # 2
        # 3
        # 4
        # 5
        # 6
        # 7
        # 8
        # 9
        # 10
        set_trace(Config=MyConfig)
        print(1)
        # 1
        # 2
        # 3
        # 4
        # 5
        # 6
        # 7
        # 8
        # 9
        # 10
        # 11
        # 12
        # 13
        # 14
        # 15
        return

    expected = textwrap.dedent("""
        [NUM] > .*fn(), 5 frames hidden

        ...
        NUM             # 8
        NUM             # 9
        NUM             # 10
        NUM             set_trace(Config=MyConfig)
        NUM  ->         print(1)
        NUM             # 1
        NUM             # 2
        NUM             # 3
        NUM             # 4
        ...
        # c
        1
        """)
    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent("""
            [NUM] > .*(NUM)fn(), 5 frames hidden

            ...
            NUM             # 7
            NUM             # 8
            NUM             # 9
            NUM             # 10
            NUM  ->         set_trace(Config=MyConfig)
            NUM             print(1)
            NUM             # 1
            NUM             # 2
            NUM             # 3
            ...
            # n
            <CLEARSCREEN>""")
            + expected
        )
    check(
        fn,
        expected,
        terminal_size=(len(__file__) + 50, 15),
    )


def test_sticky_cutoff_with_decorator():
    class MyConfig(ConfigTest):
        sticky_by_default = True

    def deco(f):
        return f

    @deco
    def fn():
        # 1
        # 2
        # 3
        # 4
        # 5
        set_trace(Config=MyConfig)
        print(1)
        return

    expected = textwrap.dedent("""
        [NUM] > .*fn(), 5 frames hidden

        NUM         @deco
        ...
        NUM             # 5
        NUM             set_trace(Config=MyConfig)
        NUM  ->         print(1)
        NUM             return
        # c
        1
        """)

    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent("""
            [NUM] > .*(NUM)fn(), 5 frames hidden

            NUM         @deco
            ...
            NUM             # 5
            NUM  ->         set_trace(Config=MyConfig)
            NUM             print(1)
            NUM             return
            # n
            <CLEARSCREEN>""")
            + expected
        )
    check(
        fn,
        expected,
        terminal_size=(len(__file__) + 50, 10),
    )


def test_sticky_cutoff_with_many_decorators():
    class MyConfig(ConfigTest):
        sticky_by_default = True

    def deco(f):
        return f

    @deco
    @deco
    @deco
    @deco
    @deco
    @deco
    @deco
    @deco
    def fn():
        # 1
        # 2
        # 3
        # 4
        # 5
        set_trace(Config=MyConfig)
        print(1)
        return

    expected = textwrap.dedent("""
        [NUM] > .*fn(), 5 frames hidden

        NUM         @deco
        ...
        NUM         @deco
        ...
        NUM  ->         print(1)
        NUM             return
        # c
        1
        """)
    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent("""
            [NUM] > .*(NUM)fn(), 5 frames hidden

            NUM         @deco
            ...
            NUM         @deco
            ...
            NUM             print(1)
            NUM             return
            # n
            <CLEARSCREEN>""")
            + expected
        )

    check(
        fn,
        expected,
        terminal_size=(len(__file__) + 50, 10),
    )


def test_sticky_cutoff_with_decorator_colored():
    class MyConfig(ConfigWithPygmentsAndHighlight):
        sticky_by_default = True

    def deco(f):
        return f

    @deco
    @deco
    def fn():
        # 1
        # 2
        # 3
        # 4
        # 5
        set_trace(Config=MyConfig)
        print(1)
        return

    expected = (
        textwrap.dedent(
            """
            [NUM] > .*fn(), 5 frames hidden

            <COLORNUM>         ^[[38;5;129m@deco^[[39m
            <COLORNUM>         ^[[38;5;129m@deco^[[39m
            ...
            """.rstrip()
        )
        + textwrap.dedent(
            """
            <COLORCURLINE>  ->         set_trace.*
            <COLORNUM>             ^[[38;.*mprint.*
            """
            if sys.version_info >= (3, 13)
            else """
            <COLORNUM>             set_trace.*
            <COLORCURLINE>  ->         ^[[38;.*mprint.*
            """
        ).rstrip()
        + textwrap.dedent(
            """
            <COLORNUM>             ^[[38;5;28;01mreturn^[[39;00m
            # c
            1
            """,
        )
    )

    check(
        fn,
        expected,
        terminal_size=(len(__file__) + 50, 10),
    )


def test_sticky_cutoff_with_minimal_lines():
    class MyConfig(ConfigTest):
        sticky_by_default = True

    def deco(f):
        return f

    @deco
    def fn():
        set_trace(Config=MyConfig)
        print(1)
        # 1
        # 2
        # 3
        return

    expected = textwrap.dedent("""
        [NUM] > .*fn(), 5 frames hidden

        NUM         @deco
        ...
        NUM  ->         print(1)
        NUM             # 1
        NUM             # 2
        ...
        # c
        1
        """)

    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
            [NUM] > .*(NUM)fn(), 5 frames hidden

            NUM         @deco
            NUM         def fn()
            NUM  ->         set_trace(Config=MyConfig)
            NUM             print(1)
            NUM             # 1
            ...
            # n
            <CLEARSCREEN>"""
            )
            + expected
        )

    check(
        fn,
        expected,
        terminal_size=(len(__file__) + 50, 3),
    )


def test_exception_lineno():
    def bar():
        assert False

    def fn():
        try:
            a = 1  # noqa: F841
            bar()
            b = 2  # noqa: F841
        except AssertionError:
            xpm()

    expected = (
        f"""
        Traceback (most recent call last):
          File "{RE_THIS_FILE}", line NUM, in fn
            bar()
        """.rstrip()
        + (
            """
            ~~~^^
            """.rstrip()
            if sys.version_info >= (3, 13)
            else ""
        )
        + f"""
          File "{RE_THIS_FILE}", line NUM, in bar
            assert False
        AssertionError.*

        [NUM] > .*bar()
        -> assert False
        # u
        [NUM] > .*fn()
        -> bar()
        # ll
        NUM         def fn():
        NUM             try:
        NUM                 a = 1
        NUM  >>             bar()
        NUM                 b = 2
        NUM             except AssertionError:
        NUM  ->             xpm()
        # c
        """
    )
    check(
        fn,
        expected,
    )


def test_postmortem_noargs():
    def fn():
        try:
            a = 1  # noqa: F841
            1 / 0  # noqa: B018
        except ZeroDivisionError:
            pdbpp.post_mortem(Pdb=PdbTest)

    expected = """
        [NUM] > .*fn()
        -> 1 / 0  # noqa: B018
        # c
        """

    check(fn, expected)


def test_postmortem_needs_exceptioncontext():
    pytest.raises(ValueError, pdbpp.post_mortem)


def test_exception_through_generator():
    def gen():
        yield 5
        assert False

    def fn():
        try:
            for _ in gen():
                pass
        except AssertionError:
            xpm()

    if sys.version_info >= (3, 13):
        error_indicator = "\n.*~~~^^"
    elif sys.version_info >= (3, 12, 1):
        error_indicator = "\n.*^^^^^"
    else:
        error_indicator = ""
    check(
        fn,
        f"""
Traceback (most recent call last):
  File "{RE_THIS_FILE}", line NUM, in fn
    for _ in gen():{error_indicator}
  File "{RE_THIS_FILE}", line NUM, in gen
    assert False
AssertionError.*

[NUM] > .*gen()
-> assert False
# u
[NUM] > .*fn()
-> for _ in gen():
# c
    """,
    )


def test_source():
    def bar():
        return 42

    def fn():
        set_trace()
        return bar()

    expected = """
        [NUM] > .*fn()
        -> return bar()
           5 frames hidden .*
        # source bar
        NUM         def bar():
        NUM             return 42
        # c
        """

    check(
        fn,
        expected,
        add_313_fix=True,
    )


def test_source_with_pygments():
    def bar():
        return 42

    def fn():
        set_trace(Config=ConfigWithPygments)
        return bar()

    if int(pygments_major) >= 2 and int(pygments_minor) >= 19:
        highlighted_code = (
            "^[[38;5;28;01mdef^[[39;00m^[[38;5;250m ^[[39m^[[38;5;21mbar^[[39m():"
        )
    else:  # pygments 2.18
        highlighted_code = "^[[38;5;28;01mdef^[[39;00m ^[[38;5;21mbar^[[39m():"

    expected = textwrap.dedent(
        f"""
        [NUM] > .*fn()
        -> ^[[38;5;28;01mreturn^[[39;00m bar()
           5 frames hidden .*
        # source bar
        NUM         {highlighted_code}
        NUM             ^[[38;5;28;01mreturn^[[39;00m ^[[38;5;241m42^[[39m
        # c
        """.rstrip()
    )

    if sys.version_info > (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*fn()
                -> set_trace(Config^[[38;5;241m=^[[39mConfigWithPygments)
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )

    check(fn, expected)


def test_source_with_highlight():
    def bar():
        return 42

    def fn():
        set_trace(Config=ConfigWithHighlight)
        return bar()

    expected = textwrap.dedent(
        """
        [NUM] > .*fn()
        -> return bar()
           5 frames hidden .*
        # source bar
        <COLORNUM>         def bar():
        <COLORNUM>
        <COLORNUM>             return 42
        # c
        """,
    )
    if sys.version_info > (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*fn()
                -> set_trace(Config^[[38;5;241m=^[[39mConfigWithHighLight)
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )


def test_source_with_pygments_and_highlight():
    def bar():
        return 42

    def fn():
        set_trace(Config=ConfigWithPygmentsAndHighlight)
        return bar()

    if int(pygments_major) >= 2 and int(pygments_minor) >= 19:
        highlighted_code = (
            "^[[38;5;28;01mdef^[[39;00m^[[38;5;250m ^[[39m^[[38;5;21mbar^[[39m():"
        )
    else:  # pygments 2.18
        highlighted_code = "^[[38;5;28;01mdef^[[39;00m ^[[38;5;21mbar^[[39m():"

    expected = textwrap.dedent(f"""
        [NUM] > .*fn()
        -> ^[[38;5;28;01mreturn^[[39;00m bar()
           5 frames hidden .*
        # source bar
        <COLORNUM>         {highlighted_code}
        <COLORNUM>             ^[[38;5;28;01mreturn^[[39;00m ^[[38;5;241m42^[[39m
        # c
        """)
    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*fn()
                -> set_trace(Config^[[38;5;241m=^[[39mConfigWithPygmentsAndHighlight)
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )

    check(
        fn,
        expected,
    )


def test_bad_source():
    def fn():
        set_trace()
        return 42

    expected = r"""
        [NUM] > .*fn()
        -> return 42
           5 frames hidden .*
        # source 42
        \*\*\* could not get obj: .*module, class, method, .*, or code object.*
        # c
        """

    check(fn, expected, add_313_fix=True)


def test_edit():
    def fn():
        set_trace()
        return 42

    def bar():
        fn()
        return 100

    _, lineno = inspect.getsourcelines(fn)
    return42_lineno = lineno + 2
    call_fn_lineno = lineno + 5

    expected = rf"""
        [NUM] > .*fn()
        -> return 42
           5 frames hidden .*
        # edit
        RUN emacs \+{return42_lineno} {RE_THIS_FILE_QUOTED}
        # c
        """
    check(
        fn,
        expected,
        add_313_fix=True,
    )
    expected = rf"""
        [NUM] > .*fn()
        -> return 42
           5 frames hidden .*
        # up
        [NUM] > .*bar()
        -> fn()
        # edit
        RUN emacs \+{call_fn_lineno} {RE_THIS_FILE_QUOTED}
        # c
        """
    check(bar, expected, add_313_fix=True)


def test_edit_obj():
    def fn():
        bar()
        set_trace()
        return 42

    def bar():
        pass

    _, bar_lineno = inspect.getsourcelines(bar)

    expected = rf"""
        [NUM] > .*fn()
        -> return 42
           5 frames hidden .*
        # edit bar
        RUN emacs \+{bar_lineno} {RE_THIS_FILE_CANONICAL_QUOTED}
        # c
        """
    check(fn, expected, add_313_fix=True)


def test_edit_fname_lineno():
    def fn():
        set_trace()

    expected = rf"""
        --Return--
        [NUM] > .*fn()->None
        -> set_trace()
           5 frames hidden .*
        # edit {__file__}
        RUN emacs \+1 {RE_THIS_FILE_QUOTED}
        # edit {__file__}:5
        RUN emacs \+5 {RE_THIS_FILE_QUOTED}
        # edit {__file__}:meh
        \*\*\* could not parse filename/lineno
        # edit {__file__}:-1
        \*\*\* could not parse filename/lineno
        # edit {__file__} meh:-1
        \*\*\* could not parse filename/lineno
        # edit os.py
        RUN emacs \+1 {re.escape(quote(os.__file__.rstrip("c")))}
        # edit doesnotexist.py
        \*\*\* could not parse filename/lineno
        # c
        """
    check(fn, expected, add_313_fix=True)


def test_put():
    def fn():
        set_trace()
        return 42

    _, lineno = inspect.getsourcelines(fn)
    start_lineno = lineno + 1

    expected = rf"""
        [NUM] > .*fn()
        -> return 42
           5 frames hidden .*
        # x = 10
        # y = 12
        # put
        RUN epaste \+{start_lineno}
        '        x = 10\\n        y = 12\\n'
        # c
        """
    check(fn, expected, add_313_fix=True)


def test_paste():
    def g():
        print("hello world")

    def fn():
        set_trace()
        if 4 != 5:
            g()
        return 42

    _, lineno = inspect.getsourcelines(fn)
    start_lineno = lineno + 1

    expected = rf"""
        [NUM] > .*fn()
        -> if 4 != 5:
           5 frames hidden .*
        # g()
        hello world
        # paste g()
        hello world
        RUN epaste \+{start_lineno}
        'hello world\\n'
        # c
        hello world
        """
    check(fn, expected, add_313_fix=True)


def test_put_if():
    def fn():
        x = 0
        if x < 10:
            set_trace()
        return x

    _, lineno = inspect.getsourcelines(fn)
    start_lineno = lineno + 3

    expected = rf"""
        [NUM] > .*fn()
        -> return x
           5 frames hidden .*
        # x = 10
        # y = 12
        # put
        RUN epaste \+{start_lineno}
        .*x = 10\\n            y = 12\\n.
        # c
        """
    check(fn, expected, add_313_fix=True)


def test_side_effects_free():
    r = pdbpp.side_effects_free
    assert r.match("  x")
    assert r.match("x.y[12]")
    assert not r.match("x(10)")
    assert not r.match("  x = 10")
    assert not r.match("x = 10")


def test_put_side_effects_free():
    def fn():
        x = 10  # noqa: F841
        set_trace()
        return 42

    _, lineno = inspect.getsourcelines(fn)
    start_lineno = lineno + 2

    expected = rf"""
        [NUM] > .*fn()
        -> return 42
           5 frames hidden .*
        # x
        10
        # x.__add__
        .*
        # y = 12
        # put
        RUN epaste \+{start_lineno}
        '        y = 12\\n'
        # c
        """

    check(fn, expected, add_313_fix=True)


def test_enable_disable_via_module():
    def fn():
        x = 1
        pdbpp.disable()
        set_trace_via_module()
        x = 2
        pdbpp.enable()
        set_trace_via_module()
        return x

    expected = textwrap.dedent("""
        [NUM] > .*fn()
        -> return x
           5 frames hidden .*
        # x
        2
        # c
        """)
    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*fn()
                -> set_trace_via_module()
                   NUM frames hidden.*
                # n
                """.rstrip()
            )
            + expected
        )

    check(fn, expected)


def test_enable_disable_from_prompt_via_class():
    def fn():
        pdb_ = PdbTest()

        pdb_.set_trace()
        x = 1
        pdb_.set_trace()
        x = 2
        pdbpp.enable()
        pdb_.set_trace()
        return x

    if sys.version_info >= (3, 13):
        expected = """
            [NUM] > .*fn()
            -> pdb_.set_trace()
               5 frames hidden .*
            # pdbpp.disable()
            # c
            [NUM] > .*fn()
            -> pdb_.set_trace()
               5 frames hidden .*
            # x
            2
            # c
            """
    else:
        expected = """
            [NUM] > .*fn()
            -> x = 1
               5 frames hidden .*
            # pdbpp.disable()
            # c
            [NUM] > .*fn()
            -> return x
               5 frames hidden .*
            # x
            2
            # c
            """

    check(fn, expected)


def test_hideframe():
    @pdbpp.hideframe
    def g():
        pass

    assert g.__code__.co_consts[-1] is pdbpp._HIDE_FRAME


def test_hide_hidden_frames():
    @pdbpp.hideframe
    def g():
        set_trace()
        return "foo"

    def fn():
        g()
        return 1

    if sys.version_info >= (3, 13):
        trace_line = "set_trace()"
    else:
        trace_line = 'return "foo"'
    expected = f"""
        [NUM] > .*fn()
        -> g()
           6 frames hidden .*
        # down
        ... Newest frame
        # hf_unhide
        # down
        [NUM] > .*g()
        -> {trace_line}
        # up
        [NUM] > .*fn()
        -> g()
        # hf_hide        ### hide the frame again
        # down
        ... Newest frame
        # c
        """
    check(fn, expected)


def test_hide_current_frame():
    @pdbpp.hideframe
    def g():
        set_trace()
        return "foo"

    def fn():
        g()
        return 1

    if sys.version_info >= (3, 13):
        trace_line = "set_trace()"
    else:
        trace_line = 'return "foo"'

    expected = f"""
        [NUM] > .*fn()
        -> g()
           6 frames hidden .*
        # hf_unhide
        # down           ### now the frame is no longer hidden
        [NUM] > .*g()
        -> {trace_line}
        # hf_hide        ### hide the current frame, go to the top of the stack
        [NUM] > .*fn()
        -> g()
        # c
        """

    check(fn, expected)


def test_hide_frame_for_set_trace_on_class():
    def g():
        # Simulate set_trace, with frame=None.
        pdbpp.cleanup()
        _pdb = PdbTest()
        _pdb.set_trace()
        return "foo"

    def fn():
        g()
        return 1

    expected = textwrap.dedent("""
        [NUM] > .*g()
        -> return "foo"
           5 frames hidden .*
        # hf_unhide
        # down
        \\*\\*\\* Newest frame
        # c
        """)

    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                r"""
            [NUM] > .*(NUM)g()
            -> _pdb.set_trace()
               5 frames hidden .*
            # n
            """.rstrip()
            )
            + expected
        )

    check(fn, expected)


def test_list_hidden_frames():
    @pdbpp.hideframe
    def g():
        set_trace()
        return "foo"

    @pdbpp.hideframe
    def k():
        return g()

    def fn():
        k()
        return 1

    if sys.version_info >= (3, 13):
        trace_line = "set_trace()"
    else:
        trace_line = 'return "foo"'
    expected = rf"""
        [NUM] > .*fn()
        -> k()
           7 frames hidden .*
        # hf_list
        .*_multicall()
        -> res = hook_impl.function(\*args)
        .*_multicall()
        -> res = hook_impl.function(\*args)
        .*_multicall()
        -> res = hook_impl.function(\*args)
        .*_multicall()
        -> res = hook_impl.function(\*args)
        .*_multicall()
        -> res = hook_impl.function(\*args)
        .*k()
        -> return g()
        .*g()
        -> {trace_line}
        # c
        """
    check(fn, expected)


def test_hidden_pytest_frames():
    def s():
        __tracebackhide__ = True  # Ignored for set_trace in here.
        set_trace()
        return "foo"

    def g(s=s):
        __tracebackhide__ = True
        return s()

    def k(g=g):
        return g()

    k = pdbpp.rebind_globals(k, {"__tracebackhide__": True})

    def fn():
        k()
        return 1

    expected = textwrap.dedent(
        r"""
        [NUM] > .*s()
        -> return "foo"
           7 frames hidden .*
        # hf_list
        .*_multicall()
        -> res = hook_impl.function(\*args)
        .*_multicall()
        -> res = hook_impl.function(\*args)
        .*_multicall()
        -> res = hook_impl.function(\*args)
        .*_multicall()
        -> res = hook_impl.function(\*args)
        .*_multicall()
        -> res = hook_impl.function(\*args)
        .*k()
        -> return g()
        .*g()
        -> return s()
        # c
        """,
    )

    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                r"""
            [NUM] > .*s()
            -> set_trace()
               7 frames hidden .*
            # n
            """.rstrip()
            )
            + expected
        )

    check(fn, expected)


def test_hidden_pytest_frames_f_local_nondict():
    class M:
        values = []

        def __getitem__(self, name):
            if name == 0:
                # Handle 'if "__tracebackhide__" in frame.f_locals'.
                raise IndexError()
            return globals()[name]

        def __setitem__(self, name, value):
            # pdb assigns to f_locals itself.
            self.values.append((name, value))

    def fn():
        m = M()
        set_trace()
        exec("print(1)", {}, m)
        assert m.values == [("__return__", None)]

    # 3.11 shows the exec frame as <string>(0), while 3.8 shows <string>(1)
    # See https://docs.python.org/3/whatsnew/3.11.html#inspect
    line_no = 0 if sys.version_info >= (3, 11) else 1

    expected = rf"""
        [NUM] > .*fn()
        -> exec("print(1)", {{}}, m)
           5 frames hidden (try 'help hidden_frames')
        # s
        --Call--
        [NUM] > <string>({line_no})<module>()
           5 frames hidden (try 'help hidden_frames')
        # n
        [NUM] > <string>(1)<module>()
           5 frames hidden (try 'help hidden_frames')
        # n
        1
        --Return--
        [NUM] > <string>(1)<module>()
           5 frames hidden (try 'help hidden_frames')
        # c
        """
    if sys.version_info >= (3, 13):
        expected = textwrap.dedent(
            r"""
            [NUM] > .*fn()
            -> set_trace()
               5 frames hidden .*
            # n
            """.rstrip()
        ) + textwrap.dedent(expected)

    check(fn, expected)


def test_hidden_unittest_frames():
    def s(set_trace=set_trace):
        set_trace()
        return "foo"

    def g(s=s):
        return s()

    g = pdbpp.rebind_globals(g, {"__unittest": True})

    def fn():
        return g()

    if sys.version_info >= (3, 13):
        trace_line = "set_trace()"
    else:
        trace_line = 'return "foo"'

    expected = rf"""
        [NUM] > .*s()
        -> {trace_line}
           6 frames hidden .*
        # hf_list
        .*_multicall()
        -> res = hook_impl.function(\*args)
        .*_multicall()
        -> res = hook_impl.function(\*args)
        .*_multicall()
        -> res = hook_impl.function(\*args)
        .*_multicall()
        -> res = hook_impl.function(\*args)
        .*_multicall()
        -> res = hook_impl.function(\*args)
        .*g()
        -> return s()
        # c
    """
    check(fn, expected)


def test_dont_show_hidden_frames_count():
    class MyConfig(ConfigTest):
        show_hidden_frames_count = False

    @pdbpp.hideframe
    def g():
        set_trace(Config=MyConfig)
        return "foo"

    def fn():
        g()
        return 1

    expected = """
        [NUM] > .*fn()
        -> g()
        # c           ### note that the hidden frame count is not displayed
        """
    check(fn, expected)


def test_disable_hidden_frames():
    class MyConfig(ConfigTest):
        enable_hidden_frames = False

    @pdbpp.hideframe
    def g():
        set_trace(Config=MyConfig)
        return "foo"

    def fn():
        g()
        return 1

    if sys.version_info >= (3, 13):
        trace_line = "set_trace(Config=MyConfig)"
    else:
        trace_line = 'return "foo"'

    expected = f"""
        [NUM] > .*g()
        -> {trace_line}
        # c           ### note that we were inside g()
        """

    check(fn, expected)


def test_break_on_setattr():
    @pdbpp.break_on_setattr("x", Pdb=PdbTest)
    class Foo:
        pass

    def fn():
        obj = Foo()
        obj.x = 0
        return obj.x

    check(
        fn,
        """
        [NUM] > .*fn()
        -> obj.x = 0
           5 frames hidden .*
        # hasattr(obj, 'x')
        False
        # n
        [NUM] > .*fn()
        -> return obj.x
           5 frames hidden .*
        # p obj.x
        0
        # c
        """,
    )


def test_break_on_setattr_without_hidden_frames():
    class PdbWithConfig(PdbTest):
        def __init__(self, *args, **kwargs):
            class Config(ConfigTest):
                enable_hidden_frames = False

            super().__init__(*args, Config=Config, **kwargs)

    class Foo:
        pass

    Foo = pdbpp.break_on_setattr("x", Pdb=PdbWithConfig)(Foo)

    def fn():
        obj = Foo()
        obj.x = 0
        return obj.x

    check(
        fn,
        """
        [NUM] > .*fn()
        -> obj.x = 0
        # hasattr(obj, 'x')
        False
        # n
        [NUM] > .*fn()
        -> return obj.x
        # p obj.x
        0
        # c
        """,
    )


def test_break_on_setattr_condition():
    def mycond(obj, value):
        return value == 42

    @pdbpp.break_on_setattr("x", condition=mycond, Pdb=PdbTest)
    class Foo:
        pass

    def fn():
        obj = Foo()
        obj.x = 0
        obj.x = 42
        return obj.x

    check(
        fn,
        """
        [NUM] > .*fn()
        -> obj.x = 42
           5 frames hidden .*
        # obj.x
        0
        # n
        [NUM] > .*fn()
        -> return obj.x
           5 frames hidden .*
        # obj.x
        42
        # c
        """,
    )


def test_break_on_setattr_non_decorator():
    class Foo:
        pass

    def fn():
        a = Foo()
        b = Foo()

        def break_if_a(obj, value):
            return obj is a

        pdbpp.break_on_setattr("bar", condition=break_if_a, Pdb=PdbTest)(Foo)
        b.bar = 10
        a.bar = 42

    check(
        fn,
        """
        [NUM] > .*fn()
        -> a.bar = 42
           5 frames hidden .*
        # c
        """,
    )


def test_break_on_setattr_overridden():
    @pdbpp.break_on_setattr("x", Pdb=PdbTest)
    class Foo:
        def __setattr__(self, attr, value):
            super().__setattr__(attr, value + 1)

    def fn():
        obj = Foo()
        obj.y = 41
        obj.x = 0
        return obj.x

    check(
        fn,
        """
        [NUM] > .*fn()
        -> obj.x = 0
           5 frames hidden .*
        # obj.y
        42
        # hasattr(obj, 'x')
        False
        # n
        [NUM] > .*fn()
        -> return obj.x
           5 frames hidden .*
        # p obj.x
        1
        # c
        """,
    )


def test_utf8():
    def fn():
        # тест
        a = 1
        set_trace(Config=ConfigWithHighlight)
        return a

    # we cannot easily use "check" because the output is full of ANSI escape
    # sequences
    expected, lines = run_func(fn, "# ll\n# c")
    assert "тест" in lines[5]


def test_debug_normal():
    def g():
        a = 1
        return a

    def fn():
        g()
        set_trace()
        return 1

    expected = """
        [NUM] > .*fn()
        -> return 1
           5 frames hidden .*
        # debug g()
        ENTERING RECURSIVE DEBUGGER
        [NUM] > .*
        (#) s
        --Call--
        [NUM] > .*g()
        -> def g():
        (#) ll
        NUM  ->     def g():
        NUM             a = 1
        NUM             return a
        (#) c
        LEAVING RECURSIVE DEBUGGER
        # sticky
        <CLEARSCREEN>
        [NUM] > .*(), 5 frames hidden

        NUM         def fn():
        NUM             g()
        NUM             set_trace()
        NUM  ->         return 1
        # debug g()
        ENTERING RECURSIVE DEBUGGER
        [1] > <string>(1)<module>()
        (#) c
        LEAVING RECURSIVE DEBUGGER
        # c
        """

    check(fn, expected, add_313_fix=True)


def test_debug_thrice():
    def fn():
        set_trace()

    expected = """
        --Return--
        [NUM] > .*fn()
        -> set_trace()
           5 frames hidden .*
        # debug 1
        ENTERING RECURSIVE DEBUGGER
        [NUM] > .*
        (#) debug 2
        ENTERING RECURSIVE DEBUGGER
        [NUM] > .*
        ((#)) debug 34
        ENTERING RECURSIVE DEBUGGER
        [NUM] > .*
        (((#))) p 42
        42
        (((#))) c
        LEAVING RECURSIVE DEBUGGER
        ((#)) c
        LEAVING RECURSIVE DEBUGGER
        (#) c
        LEAVING RECURSIVE DEBUGGER
        # c
        """
    check(fn, expected, add_313_fix=True)


def test_syntaxerror_in_command():
    def fn():
        set_trace()

    expected = """
        --Return--
        [NUM] > .*fn()
        -> set_trace
           5 frames hidden .*
        # print(
        \\*\\*\\* SyntaxError: .*
        # debug print(
        ENTERING RECURSIVE DEBUGGER
        \\*\\*\\* SyntaxError: .*
        LEAVING RECURSIVE DEBUGGER
        # c
        """

    check(fn, expected, add_313_fix=True)


def test_debug_with_overridden_continue():
    class CustomPdb(PdbTest):
        """CustomPdb that overrides do_continue like with pytest's wrapper."""

        def do_continue(self, arg):
            global count_continue
            count_continue += 1
            print(f"do_continue_{count_continue}")
            return super().do_continue(arg)

        do_c = do_cont = do_continue

    def g():
        a = 1
        return a

    def fn():
        global count_continue
        count_continue = 0

        g()

        set_trace(Pdb=CustomPdb)
        set_trace(Pdb=CustomPdb)

        assert count_continue == 3
        return 1

    expected = (
        """
        [NUM] > .*fn()
        -> set_trace(Pdb=CustomPdb)
           5 frames hidden .*
        # c
        do_continue_1
        [NUM] > .*fn()
        """
        + (
            """
        -> set_trace(Pdb=CustomPdb)
        """
            if sys.version_info >= (3, 13)
            else """
        -> assert count_continue == 3
        """
        ).strip()
        + """
           5 frames hidden .*
        # debug g()
        ENTERING RECURSIVE DEBUGGER
        [NUM] > .*
        (#) s
        --Call--
        [NUM] > .*g()
        -> def g():
        (#) ll
        NUM  ->     def g():
        NUM             a = 1
        NUM             return a
        (#) c
        do_continue_2
        LEAVING RECURSIVE DEBUGGER
        # c
        do_continue_3
        """
    )
    check(fn, expected)


def test_before_interaction_hook():
    class MyConfig(ConfigTest):
        def before_interaction_hook(self, pdb):
            pdb.stdout.write("HOOK!\n")

    def fn():
        set_trace(Config=MyConfig)
        return 1

    expected = """
        [NUM] > .*fn()
        -> return 1
           5 frames hidden .*
        HOOK!
        # c
        """

    if sys.version_info >= (3, 13):
        expected = textwrap.dedent(
            """
            [NUM] > .*fn()
            -> set_trace(Config=MyConfig)
               5 frames hidden .*
            HOOK!
            # n
            """.rstrip(),
        ) + textwrap.dedent(expected)
    check(fn, expected)


def test_unicode_bug():
    def fn():
        set_trace()
        x = "this is plain ascii"  # noqa: F841
        y = "this contains a unicode: à"  # noqa: F841
        return

    check_output = """
        [NUM] > .*fn()
        -> x = "this is plain ascii".*
           5 frames hidden .*
        # n
        [NUM] > .*fn()
        -> y = "this contains a unicode: à".*
           5 frames hidden .*
        # c
        """

    check(fn, check_output, add_313_fix=True)


def test_continue_arg():
    def fn():
        set_trace()
        x = 1
        y = 2
        z = 3
        return x + y + z

    _, lineno = inspect.getsourcelines(fn)
    line_z = lineno + 4

    expected = f"""
        [NUM] > .*fn()
        -> x = 1
           5 frames hidden .*
        # c {line_z}
        Breakpoint NUM at {RE_THIS_FILE_CANONICAL}:{line_z}
        Deleted breakpoint NUM
        [NUM] > .*fn()
        -> z = 3
           5 frames hidden .*
        # c
        """
    check(fn, expected, add_313_fix=True)


# On Windows, it seems like this file is handled as cp1252-encoded instead
# of utf8 (even though the "# -*- coding: utf-8 -*-" line exists) and the
# core pdb code does not support that. Or something to that effect, I don't
# actually know.
# UnicodeDecodeError: 'charmap' codec can't decode byte 0x81 in position
# 6998: character maps to <undefined>.
# So we XFail this test on Windows.
@pytest.mark.xfail(
    (
        sys.platform == "win32"
        and (
            # bpo-41894: fixed in 3.10, backported to 3.9.1 and 3.8.7.
            sys.version_info < (3, 8, 7)
            or (sys.version_info[:2] == (3, 9) and sys.version_info < (3, 9, 1))
        )
    ),
    raises=UnicodeDecodeError,
    strict=True,
    reason=(
        "Windows encoding issue. See comments and"
        " https://github.com/pdbpp/pdbpp/issues/341"
    ),
)
@pytest.mark.skipif(not hasattr(pdbpp.pdb.Pdb, "error"), reason="no error method")
def test_continue_arg_with_error():
    def fn():
        set_trace()
        x = 1
        y = 2
        z = 3
        return x + y + z

    _, lineno = inspect.getsourcelines(fn)
    line_z = lineno + 4

    error = (
        "NameError: name 'c' is not defined"
        if sys.version_info >= (3, 13)
        else "The specified object '.foo' is not a function or was not found along sys.path."
    )

    expected = rf"""
        [NUM] > .*fn()
        -> x = 1
           5 frames hidden .*
        # c.foo
        \*\*\* {error}
        # c {line_z}
        Breakpoint NUM at {RE_THIS_FILE_CANONICAL}:{line_z}
        Deleted breakpoint NUM
        [NUM] > .*fn()
        -> z = 3
           5 frames hidden .*
        # c
        """
    check(fn, expected, add_313_fix=True)


def test_set_trace_header():
    """Handler header kwarg added with Python 3.7 in pdb.set_trace."""

    header = "my_header"

    def fn():
        set_trace_via_module(header=header)

    expected = (
        f"""
        {header}
        """
        + (
            """
        [NUM] > .*fn()
        -> set_trace_via_module(header=header)
           5 frames hidden .*
        # n
        """.lstrip()
            if sys.version_info >= (3, 13)
            else ""
        )
        + """
        --Return--
        [NUM] > .*fn()
        -> set_trace.*
           5 frames hidden .*
        # c
        """.lstrip()
    )

    check(fn, expected)


def test_stdout_encoding_None():
    instance = PdbTest()
    instance.stdout = BytesIO()
    instance.stdout.encoding = None

    instance.ensure_file_can_write_unicode(instance.stdout)

    try:
        import cStringIO
    except ImportError:
        pass
    else:
        instance.stdout = cStringIO.StringIO()
        instance.ensure_file_can_write_unicode(instance.stdout)


def test_frame_cmd_changes_locals():
    def a():
        x = 42  # noqa: F841
        b()

    def b():
        fn()

    def fn():
        set_trace()
        return

    expected = f"""
        [NUM] > .*fn()
        -> return
           5 frames hidden .*
        # f {count_frames() + 2 - 5}
        [NUM] > .*a()
        -> b()
        # p list(sorted(locals().keys()))
        ['b', 'x']
        # c
        """
    check(a, expected, add_313_fix=True)


def test_sigint_in_interaction_with_cmdloop():
    def fn():
        def inner():
            raise KeyboardInterrupt()

        set_trace()

    expected = """
        --Return--
        [NUM] > .*fn()
        -> set_trace()
           5 frames hidden .*
        # debug inner()
        ENTERING RECURSIVE DEBUGGER
        [NUM] > .*
        (#) c
        --KeyboardInterrupt--
        # c
        """
    check(fn, expected, add_313_fix=True)


@pytest.mark.skipif(
    not hasattr(pdbpp.pdb.Pdb, "_previous_sigint_handler"),
    reason="_previous_sigint_handler is not available",
)
def test_interaction_restores_previous_sigint_handler():
    """Test is based on cpython's test_pdb_issue_20766."""

    def fn():
        i = 1
        while i <= 2:
            sess = PdbTest(nosigint=False)
            sess.set_trace(sys._getframe())
            print(f"pdb {i}: {sess._previous_sigint_handler}")
            i += 1

    expected = """
        [NUM] > .*fn()
        -> print(f"pdb {i}: {sess._previous_sigint_handler}")
           5 frames hidden .*
        # c
        pdb 1: <built-in function default_int_handler>
        [NUM] > .*fn()
        -> .*
           5 frames hidden .*
        # c
        pdb 2: <built-in function default_int_handler>
        """

    if sys.version_info >= (3, 13):
        expected = textwrap.dedent(
            """
            [NUM] > .*fn()
            -> sess.set_trace(sys._getframe())
               5 frames hidden .*
            # n
            """.rstrip(),
        ) + textwrap.dedent(expected)

    check(fn, expected)


def test_recursive_set_trace():
    def fn():
        global inner
        global count
        count = 0

        def inner():
            global count
            count += 1

            if count == 1:
                set_trace()
            else:
                set_trace(cleanup=False)

        inner()

    expected = """
        --Return--
        [NUM] > .*inner()
        -> set_trace()
           5 frames hidden .*
        # inner()
        # c
        """
    if sys.version_info >= (3, 13):
        expected = textwrap.dedent(
            """
            [NUM] > .*inner()
            -> set_trace()
               5 frames hidden .*
            # n
            """.rstrip(),
        ) + textwrap.dedent(expected)

    check(fn, expected)


def test_steps_over_set_trace():
    def fn():
        set_trace()
        print(1)

        set_trace(cleanup=False)
        print(2)

    expected = (
        """
        [NUM] > .*fn()
        -> print(1)
           5 frames hidden .*
        # n
        1
        [NUM] > .*fn()
        -> set_trace(cleanup=False)
           5 frames hidden .*
        # n
        [NUM] > .*fn()
        """.rstrip()
        + (
            """
        -> set_trace(cleanup=False)
        NUM frames hidden.*
        # c
        2
        """
            if sys.version_info >= (3, 13)
            else """
        -> print(2)
           5 frames hidden .*
        # c
        2
        """
        )
    )

    check(fn, expected, add_313_fix=True)


def test_break_after_set_trace():
    def fn():
        set_trace()
        print(1)
        print(2)

    _, lineno = inspect.getsourcelines(fn)

    expected = f"""
        [NUM] > .*fn()
        -> print(1)
           5 frames hidden .*
        # break {lineno + 3}
        Breakpoint . at .*:{lineno + 3}
        # c
        1
        [NUM] > .*fn()
        -> print(2)
           5 frames hidden .*
        # import pdb; pdbpp.local.GLOBAL_PDB.clear_all_breaks()
        # c
        2
    """
    check(fn, expected, add_313_fix=True)


def test_break_with_inner_set_trace():
    def fn():
        def inner():
            set_trace(cleanup=False)

        set_trace()
        inner()
        print(1)

    _, lineno = inspect.getsourcelines(fn)

    expected = (
        f"""
        [NUM] > .*fn()
        -> inner()
           5 frames hidden .*
        # break {lineno + 8}
        Breakpoint . at .*:{lineno + 8}
        # c
        --Return--
        """.rstrip()
        + (
            """
        [NUM] .*set_trace()
        -> Pdb(.*).set_trace(frame)
           5 frames hidden .*
        # n
        --Return-
        [NUM] .*inner()
        """
            if sys.version_info >= (3, 13)
            else """
        [NUM] > .*inner()->None
        """
        )
        + """
        -> set_trace(cleanup=False)
           5 frames hidden .*
        # import pdb; pdbpp.local.GLOBAL_PDB.clear_all_breaks()
        # c
        1
        """.lstrip()
    )
    check(fn, expected, add_313_fix=True)


def test_pdbrc_continue(tmpdirhome):
    """Test that interaction is skipped with continue in pdbrc."""
    assert os.getcwd() == str(tmpdirhome)
    pdbrc_read_fixed = (  # https://github.com/python/cpython/issues/90095
        (sys.version_info >= (3, 11, 9) and sys.version_info <= (3, 12, 1))
        or sys.version_info >= (3, 12, 2)
    ) and sys.platform != "darwin"
    with open(".pdbrc", "w") as f:
        f.writelines(
            [
                "p 'from_pdbrc'\n",
                "continue\n",
            ]
        )

    def fn():
        set_trace(readrc=True)
        print("after_set_trace")

    if sys.version_info >= (3, 13):
        expected = textwrap.dedent(
            """
            [NUM] > .*fn()
            -> set_trace(readrc=True)
               5 frames hidden .*
            'from_pdbrc'
            after_set_trace
            """.rstrip(),
        )
    else:
        expected = (
            (
                """
            [NUM] > .*fn()
            -> print("after_set_trace")
               5 frames hidden .*
               """.rstrip()
                if pdbrc_read_fixed
                else ""
            )
            + """
            'from_pdbrc'
            after_set_trace
            """
        )
    check(fn, expected)


def test_python_m_pdb_usage():
    p = subprocess.Popen(
        [sys.executable, "-m", "pdb"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    stdout, stderr = p.communicate()
    out = stdout.decode()
    err = stderr.decode()
    assert not err

    message = "usage: pdb" + ".py" if sys.version_info < (3, 13) else ""
    assert message in out


@pytest.mark.parametrize("PDBPP_HIJACK_PDB", (1, 0))
def test_python_m_pdb_uses_pdbpp_and_env(PDBPP_HIJACK_PDB, monkeypatch, tmpdir):
    if PDBPP_HIJACK_PDB:
        skip_with_missing_pth_file()

    monkeypatch.setenv("PDBPP_HIJACK_PDB", str(PDBPP_HIJACK_PDB))

    f = tmpdir.ensure("test.py")
    f.write(
        textwrap.dedent(f"""
        import inspect
        import os
        import pdb

        fname = os.path.basename(inspect.getfile(pdb.Pdb))
        if {PDBPP_HIJACK_PDB}:
            assert fname == 'pdbpp.py', (fname, pdb, pdb.Pdb)
        else:
            assert fname in ('pdb.py', 'pdb.pyc'), (fname, pdb, pdb.Pdb)
        pdb.set_trace()
    """)
    )

    p = subprocess.Popen(
        [sys.executable, "-m", "pdb", str(f)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE,
    )
    stdout, stderr = p.communicate(b"c\n")
    out = stdout.decode("utf8")
    err = stderr.decode("utf8")
    print(out)
    print(err, file=sys.stderr)
    assert err == ""
    if PDBPP_HIJACK_PDB:
        assert "(Pdb)" not in out
        assert "(Pdb++)" in out
        if sys.platform == "win32":
            assert out.endswith("\n(Pdb++) " + os.linesep)
        else:
            assert out.endswith("\n(Pdb++) \n")
    else:
        assert "(Pdb)" in out
        assert "(Pdb++)" not in out
        assert out.endswith("\n(Pdb) " + os.linesep)


def get_completions(text):
    """Get completions from the installed completer."""
    readline_ = pdbpp.local.GLOBAL_PDB.fancycompleter.config.readline
    complete = readline_.get_completer()
    comps = []
    assert complete.__self__ is pdbpp.local.GLOBAL_PDB
    while True:
        val = complete(text, len(comps))
        if val is None:
            break
        comps += [val]
    return comps


def test_set_trace_in_completion(monkeypatch_readline):
    def fn():
        class CompleteMe:
            attr_called = 0

            @property
            def set_trace_in_attrib(self):
                self.attr_called += 1
                set_trace(cleanup=False)
                print("inner_set_trace_was_ignored")

        obj = CompleteMe()

        def check_completions():
            monkeypatch_readline("obj.", 0, 4)
            comps = get_completions("obj.")
            assert obj.attr_called == 1, "attr was called"

            # Colorization only works with pyrepl, via pyrepl.readline._setup.
            assert any("set_trace_in_attrib" in comp for comp in comps), comps
            return True

        set_trace()

    if sys.version_info < (3, 13):
        expected = textwrap.dedent("""
                    --Return--
                    [NUM] > .*fn()
                    .*
                       5 frames hidden .*
                    # check_completions()
                    inner_set_trace_was_ignored
                    True
                    # c
                    """)
    else:
        expected = textwrap.dedent("""
                    [NUM] > .*fn()
                    -> set_trace()
                       5 frames hidden .*
                    # check_completions()
                    inner_set_trace_was_ignored
                    True
                    # c
                    """)

    check(fn, expected)


def test_completes_from_pdb(monkeypatch_readline):
    """Test that pdb's original completion is used."""

    def fn():
        where = 1  # noqa: F841
        set_trace()

        def check_completions():
            # Patch readline to return expected results for "wher".
            monkeypatch_readline("wher", 0, 4)
            assert get_completions("wher") == ["where"]

            # Patch readline to return expected results for "disable ".
            monkeypatch_readline("disable", 8, 8)

            # NOTE: number depends on bpb.Breakpoint class state, just ensure that
            #       is a number.
            completion = pdbpp.local.GLOBAL_PDB.complete("", 0)
            assert int(completion) > 0

            # Patch readline to return expected results for "p ".
            monkeypatch_readline("p ", 2, 2)
            comps = get_completions("")
            assert "where" in comps

            # Dunder members get completed only on second invocation.
            assert "__name__" not in comps
            comps = get_completions("")
            assert "__name__" in comps

            # Patch readline to return expected results for "help ".
            monkeypatch_readline("help ", 5, 5)
            comps = get_completions("")
            assert "help" in comps

            return True

        set_trace()

    _, lineno = inspect.getsourcelines(fn)

    if sys.version_info >= (3, 10, 0, "a", 7):  # bpo-24160
        pre_py310_output = ""
    else:
        pre_py310_output = "\n'There are no breakpoints'"

    if sys.version_info < (3, 13):
        expected = textwrap.dedent(
            f"""
[NUM] > .*fn()
.*
   5 frames hidden .*
# break {lineno}
Breakpoint NUM at .*:{lineno}
# c
--Return--
[NUM] > .*fn()
.*
   5 frames hidden .*
# check_completions()
True
# import pdb; pdbpp.local.GLOBAL_PDB.clear_all_breaks(){pre_py310_output}
# c
"""
        )
    else:
        expected = textwrap.dedent(
            f"""
[NUM] > .*fn()
-> set_trace()
   5 frames hidden .*
# break {lineno}
Breakpoint NUM at .*:{lineno}
# c
[NUM] > .*fn()
-> set_trace()
   5 frames hidden .*
# check_completions()
True
# import pdb; pdbpp.local.GLOBAL_PDB.clear_all_breaks(){pre_py310_output}
# c
"""
        )
    check(fn, expected)


@pytest.mark.xfail(
    sys.version_info >= (3, 13), reason="unsure if this is a bug with 3.13"
)
def test_completion_uses_tab_from_fancycompleter(monkeypatch_readline):
    """Test that pdb's original completion is used."""

    def fn():
        def check_completions():
            # Patch readline to return expected results for "C.f()".
            monkeypatch_readline(line="C.f()", begidx=5, endidx=5)
            assert get_completions("") == ["\t"]
            return True

        set_trace()

    expected = """
        --Return--
        [NUM] > .*fn()->None
        .*
           5 frames hidden .*
        # check_completions()
        True
        # c
        """

    check(fn, expected, add_313_fix=True)


def test_complete_removes_duplicates_with_coloring(
    monkeypatch_readline, readline_param
):
    def fn():
        helpvar = 42  # noqa: F841

        class obj:
            foo = 1
            foobar = 2

        def check_completions():
            # Patch readline to return expected results for "help".
            monkeypatch_readline("help", 0, 4)

            if "pyrepl" in readline_param:
                assert pdbpp.local.GLOBAL_PDB.fancycompleter.config.use_colors is True
                assert get_completions("help") == [
                    "\x1b[000;00m\x1b[00mhelp\x1b[00m",
                    "\x1b[001;00m\x1b[33;01mhelpvar\x1b[00m",
                    " ",
                ]
            else:
                assert pdbpp.local.GLOBAL_PDB.fancycompleter.config.use_colors is False
                assert get_completions("help") == ["help", "helpvar"]

            # Patch readline to return expected results for "p helpvar.".
            monkeypatch_readline("p helpvar.", 2, 10)
            if "pyrepl" in readline_param:
                assert pdbpp.local.GLOBAL_PDB.fancycompleter.config.use_colors is True
                comps = get_completions("helpvar.")
                assert isinstance(helpvar.denominator, int)
                assert any(
                    re.match(r"\x1b\[\d\d\d;00m\x1b\[33;01mdenominator\x1b\[00m", x)
                    for x in comps
                )
                assert " " in comps
            else:
                assert pdbpp.local.GLOBAL_PDB.fancycompleter.config.use_colors is False
                comps = get_completions("helpvar.")
                assert "denominator" in comps
                assert " " not in comps

            monkeypatch_readline("p obj.f", 2, 7)
            comps = get_completions("obj.f")
            assert comps == ["obj.foo"]

            monkeypatch_readline("p obj.foo", 2, 9)
            comps = get_completions("obj.foo")
            if "pyrepl" in readline_param:
                assert comps == [
                    "\x1b[000;00m\x1b[33;01mfoo\x1b[00m",
                    "\x1b[001;00m\x1b[33;01mfoobar\x1b[00m",
                    " ",
                ]
            else:
                assert comps == ["foo", "foobar", " "]

            monkeypatch_readline("disp", 0, 4)
            comps = get_completions("disp")
            assert comps == ["display"]

            return True

        set_trace()

    _, lineno = inspect.getsourcelines(fn)

    if sys.version_info >= (3, 13):
        expected = textwrap.dedent("""
            [NUM] > .*fn()
            -> set_trace()
               5 frames hidden .*
            # check_completions()
            True
            # c
        """)
    else:
        expected = textwrap.dedent(
            """
            --Return--
            [NUM] > .*fn()->None
            .*
               5 frames hidden .*
            # check_completions()
            True
            # c
            """,
        )

    check(fn, expected)


class TestCompleteUnit:
    def test_fancy_prefix_with_same_in_pdb(self, patched_completions):
        assert patched_completions(
            "p foo.b", ["foo.bar"], ["foo.bar", "foo.barbaz"]
        ) == ["foo.bar"]
        assert patched_completions(
            "p foo.b", ["foo.bar"], ["foo.bar", "foo.barbaz", "foo.barbaz2"]
        ) == ["foo.bar"]

    def test_fancy_prefix_with_more_pdb(self, patched_completions):
        assert patched_completions(
            "p foo.b", ["foo.bar"], ["foo.bar", "foo.barbaz", "something else"]
        ) == ["foo.bar", "foo.barbaz", "something else"]

    def test_fancy_with_no_pdb(self, patched_completions, fancycompleter_color_param):
        if fancycompleter_color_param == "color":
            fancy = [
                "\x1b[000;00m\x1b[33;01mfoo\x1b[00m",
                "\x1b[001;00m\x1b[33;01mfoobar\x1b[00m",
                " ",
            ]
        else:
            fancy = [
                "foo",
                "foobar",
                " ",
            ]
        assert patched_completions("foo", fancy, []) == fancy

    def test_fancy_with_prefixed_pdb(self, patched_completions):
        assert patched_completions(
            "sys.version",
            [
                "version",
                "version_info",
                " ",
            ],
            [
                "sys.version",
                "sys.version_info",
            ],
        ) == ["version", "version_info", " "]

    def test_fancy_with_prefixed_pdb_other_text(self, patched_completions):
        fancy = ["version", "version_info"]
        pdb = ["sys.version", "sys.version_info"]
        assert patched_completions("xxx", fancy, pdb) == fancy + pdb

    def test_fancy_tab_without_pdb(self, patched_completions):
        assert patched_completions("", ["\t"], []) == ["\t"]

    def test_fancy_tab_with_pdb(self, patched_completions):
        assert patched_completions("", ["\t"], ["help"]) == ["help"]


def test_complete_uses_attributes_only_from_orig_pdb(
    monkeypatch_readline, readline_param
):
    def fn():
        def check_completions():
            # Patch readline to return expected results for "p sys.version".
            monkeypatch_readline("p sys.version", 2, 13)

            if "pyrepl" in readline_param:
                assert pdbpp.local.GLOBAL_PDB.fancycompleter.config.use_colors is True
                assert get_completions("sys.version") == [
                    "\x1b[000;00m\x1b[32;01mversion\x1b[00m",
                    "\x1b[001;00m\x1b[00mversion_info\x1b[00m",
                    " ",
                ]
            else:
                assert pdbpp.local.GLOBAL_PDB.fancycompleter.config.use_colors is False
                assert get_completions("sys.version") == [
                    "version",
                    "version_info",
                    " ",
                ]
            return True

        set_trace()

    _, lineno = inspect.getsourcelines(fn)

    if sys.version_info >= (3, 13):
        expected = textwrap.dedent("""
            [NUM] > .*fn()
            -> set_trace()
               5 frames hidden .*
            # import sys
            # check_completions()
            True
            # c
        """)
    else:
        expected = textwrap.dedent("""
            --Return--
            [NUM] > .*fn()->None
            .*
               5 frames hidden .*
            # import sys
            # check_completions()
            True
            # c
           """)

    check(fn, expected)


def test_completion_removes_tab_from_fancycompleter(monkeypatch_readline):
    def fn():
        def check_completions():
            # Patch readline to return expected results for "b ".
            monkeypatch_readline("b ", 2, 2)
            comps = get_completions("")
            assert "\t" not in comps
            assert "inspect" in comps
            return True

        set_trace()

    _, lineno = inspect.getsourcelines(fn)

    if sys.version_info >= (3, 13):
        expected = textwrap.dedent("""
            [NUM] > .*fn()
            -> set_trace()
               5 frames hidden .*
            # check_completions()
            True
            # c
        """)
    else:
        expected = textwrap.dedent("""
            --Return--
            [NUM] > .*fn()
            .*
               5 frames hidden .*
            # check_completions()
            True
            # c
           """)

    check(fn, expected)


def test_complete_with_bang(monkeypatch_readline):
    """Test that completion works after "!".

    This requires parseline to return "" for the command (bpo-35270).
    """

    def fn():
        a_var = 1  # noqa: F841

        def check_completions():
            # Patch readline to return expected results for "!a_va".
            monkeypatch_readline("!a_va", 0, 5)
            assert pdbpp.local.GLOBAL_PDB.complete("a_va", 0) == "a_var"

            # Patch readline to return expected results for "list(a_va".
            monkeypatch_readline("list(a_va", 5, 9)
            assert pdbpp.local.GLOBAL_PDB.complete("a_va", 0) == "a_var"
            return True

        set_trace()

    if sys.version_info >= (3, 13):
        expected = textwrap.dedent("""
            [NUM] > .*fn()
            -> set_trace()
               5 frames hidden .*
            # check_completions()
            True
            # c
        """)
    else:
        expected = textwrap.dedent("""
            --Return--
            [NUM] > .*fn()
            .*
               5 frames hidden .*
            # check_completions()
            True
            # c
           """)

    check(fn, expected)


def test_completer_after_debug(monkeypatch_readline):
    def fn():
        myvar = 1  # noqa: F841

        def inner():
            myinnervar = 1  # noqa: F841

            def check_completions_inner():
                # Patch readline to return expected results for "myin".
                monkeypatch_readline("myin", 0, 4)
                assert "myinnervar" in get_completions("myin")
                return True

            print("inner_end")

        def check_completions():
            # Patch readline to return expected results for "myva".
            monkeypatch_readline("myva", 0, 4)
            assert "myvar" in get_completions("myva")
            return True

        set_trace()

        print("ok_end")

    check(
        fn,
        """
[NUM] > .*fn()
.*
   5 frames hidden .*
# pdbpp.local.GLOBAL_PDB.curframe.f_code.co_name
'fn'
# debug inner()
ENTERING RECURSIVE DEBUGGER
[1] > <string>(1)<module>()
(#) pdbpp.local.GLOBAL_PDB.curframe.f_code.co_name
'<module>'
(#) s
--Call--
[NUM] > .*inner()
-> def inner():
(#) pdbpp.local.GLOBAL_PDB.curframe.f_code.co_name
'inner'
(#) r
inner_end
--Return--
[NUM] > .*inner()->None
-> print("inner_end")
(#) check_completions_inner()
True
(#) q
LEAVING RECURSIVE DEBUGGER
# check_completions()
True
# c
ok_end
""",
    )


def test_nested_completer(testdir):
    p1 = testdir.makepyfile(
        """
        import sys

        frames = []

        def inner():
            completeme_inner = 1
            frames.append(sys._getframe())

        inner()

        def outer():
            completeme_outer = 2
            __import__('pdbpp').set_trace()

        outer()
        """
    )
    with open(".fancycompleterrc.py", "w") as f:
        f.write(
            textwrap.dedent("""
            from fancycompleter import DefaultConfig

            class Config(DefaultConfig):
                use_colors = False
                prefer_pyrepl = False
            """)
        )
    testdir.monkeypatch.setenv("PDBPP_COLORS", "0")
    child = testdir.spawn(f"{quote(sys.executable)} {str(p1)}")
    child.send("completeme\t")
    child.expect_exact("\r\n(Pdb++) completeme_outer")
    child.send("\nimport pdbpp; _p = pdbpp.Pdb(); _p.reset()")
    child.send("\n_p.interaction(frames[0], None)\n")
    child.expect_exact("\r\n-> frames.append(sys._getframe())\r\n(Pdb++) ")
    child.send("completeme\t")
    child.expect_exact("completeme_inner")
    child.send("\nq\n")
    child.send("completeme\t")
    child.expect_exact("completeme_outer")
    child.send("\n")
    child.sendeof()


def test_ensure_file_can_write_unicode():
    out = io.BytesIO(b"")
    stdout = io.TextIOWrapper(out, encoding="latin1")

    p = Pdb(Config=DefaultConfig, stdout=stdout)

    assert p.stdout.stream is out

    p.stdout.write("test äöüß")
    out.seek(0)
    assert out.read().decode("utf-8") == "test äöüß"


def test_signal_in_nonmain_thread_with_interaction():
    def fn():
        import threading

        evt = threading.Event()

        def start_thread():
            evt.wait()
            set_trace(nosigint=False)

        t = threading.Thread(target=start_thread)
        t.start()
        set_trace(nosigint=False)
        evt.set()
        t.join()

    expected = (
        """
        [NUM] > .*fn()
        -> evt.set()
           5 frames hidden .*
        # c
        """.rstrip()
        + (
            ""
            if sys.version_info >= (3, 13)
            else """
        --Return--"""
        )
        + """
        [NUM] > .*start_thread()
        -> set_trace(nosigint=False)
        # c
        """
    )
    check(fn, expected, add_313_fix=True, set_trace_args="nosigint=False")


def test_signal_in_nonmain_thread_with_continue():
    """Test for cpython issue 13120 (test_issue13120).

    Without the try/execept for ValueError in its do_continue it would
    display the exception, but work otherwise.
    """

    def fn():
        import threading

        def start_thread():
            a = 42  # noqa F841
            set_trace(nosigint=False)

        t = threading.Thread(target=start_thread)
        t.start()
        # set_trace(nosigint=False)
        t.join()

    if sys.version_info >= (3, 13):
        expected = """
            [NUM] > .*start_thread()
            -> set_trace(nosigint=False)
            # p a
            42
            # c
            """
    else:
        expected = """
            --Return--
            [NUM] > .*start_thread()
            -> set_trace(nosigint=False)
            # p a
            42
            # c
            """

    check(fn, expected)


def test_next_at_end_of_stack_after_unhide():
    """Test that compute_stack returns correct length with show_hidden_frames."""

    class MyConfig(ConfigTest):
        def before_interaction_hook(self, pdb):
            pdb.stdout.write("before_interaction_hook\n")
            pdb.do_hf_unhide(arg=None)

    def fn():
        set_trace(Config=MyConfig)
        return 1

    expected = """
        [NUM] > .*fn()
        -> return 1
           5 frames hidden .*
        before_interaction_hook
        # n
        --Return--
        [NUM] > .*fn()->1
        -> return 1
           5 frames hidden .*
        before_interaction_hook
        # c
        """
    if sys.version_info >= (3, 13):
        expected = textwrap.dedent(
            """
            [NUM] > .*fn()
            -> set_trace(Config=MyConfig)
               NUM frames hidden.*
            before_interaction_hook
            # n
            """.rstrip()
        ) + textwrap.dedent(expected)

    check(fn, expected)


def test_compute_stack_keeps_frame():
    """With only hidden frames the last one is kept."""

    def fn():
        def raises():
            raise Exception("foo")

        try:
            raises()
        except Exception:
            tb = sys.exc_info()[2]
            tb_ = tb
            while tb_:
                tb_.tb_frame.f_locals["__tracebackhide__"] = True
                tb_ = tb_.tb_next
            pdbpp.post_mortem(tb, Pdb=PdbTest)
        return 1

    check(
        fn,
        """
[0] > .*raises()
-> raise Exception("foo")
   1 frame hidden (try 'help hidden_frames')
# bt
> [0] .*raises()
      raise Exception("foo")
# hf_unhide
# bt
  [0] .*fn()
      raises()
> [1] .*raises()
      raise Exception("foo")
# q
""",
    )


def test_compute_stack_without_stack():
    pdb_ = PdbTest()
    assert pdb_.compute_stack([], idx=None) == ([], 0)
    assert pdb_.compute_stack([], idx=0) == ([], 0)
    assert pdb_.compute_stack([], idx=10) == ([], 10)


def test_rawinput_with_debug():
    """Test backport of fix for bpo-31078."""

    def fn():
        set_trace()

    expected = """
        --Return--
        [NUM] > .*fn()
        -> set_trace()
           5 frames hidden .*
        # debug 1
        ENTERING RECURSIVE DEBUGGER
        [NUM] > <string>(1)<module>()->None
        (#) import pdb; print(pdbpp.local.GLOBAL_PDB.use_rawinput)
        1
        (#) p sys._getframe().f_back.f_locals['self'].use_rawinput
        1
        (#) c
        LEAVING RECURSIVE DEBUGGER
        # c
    """

    check(fn, expected, add_313_fix=True)


def test_error_with_traceback():
    def fn():
        def error():
            raise ValueError("error")

        set_trace()

    expected = """
        --Return--
        [NUM] > .*fn()
        -> set_trace()
           5 frames hidden .*
        # error()
        \\*\\*\\* ValueError: error
        Traceback (most recent call last):
          File .*, in error
            raise ValueError("error")
        # c
        """
    check(fn, expected, add_313_fix=True)


def test_chained_syntaxerror_with_traceback():
    def fn():
        def compile_error():
            compile("invalid(", "<stdin>", "single")

        def error():
            try:
                compile_error()
            except Exception:
                raise AttributeError

        set_trace()

    caret_line = ".*"
    error_marker = ".*~*^*"

    expected = (
        """
        --Return--
        [NUM] > .*fn()
        -> set_trace()
           5 frames hidden .*
        # error()
        \\*\\*\\* AttributeError.*
        Traceback (most recent call last):
          File .*, in error
            compile_error()
        """.rstrip()
        + (
            f"""
        {error_marker}
         """.rstrip()
            if sys.version_info >= (3, 13)
            else ""
        )
        + """
          File .*, in compile_error
            compile.*
          """.rstrip()
        + (
            f"""
        {error_marker}
         """.rstrip()
            if sys.version_info >= (3, 13)
            else ""
        )
        + f"""
          File "<stdin>", line 1
            invalid(
        {caret_line}
        SyntaxError: .*

        During handling of the above exception, another exception occurred:

        Traceback (most recent call last):
          File .*, in error
            raise AttributeError
        # c
    """
    )

    check(fn, expected, add_313_fix=True)


def test_error_with_traceback_disabled():
    class ConfigWithoutTraceback(ConfigTest):
        show_traceback_on_error = False

    def fn():
        def error():
            raise ValueError("error")

        set_trace(Config=ConfigWithoutTraceback)

    expected = """
        --Return--
        [NUM] > .*fn()
        -> set_trace(Config=ConfigWithoutTraceback)
           5 frames hidden .*
        # error()
        \\*\\*\\* ValueError: error
        # c
        """
    check(
        fn,
        expected,
        add_313_fix=True,
        set_trace_args="Config=ConfigWithoutTraceback",
    )


def test_error_with_traceback_limit():
    class ConfigWithLimit(ConfigTest):
        show_traceback_on_error_limit = 2

    def fn():
        def f(i):
            i -= 1
            if i <= 0:
                raise ValueError("the_end")
            f(i)

        def error():
            f(10)

        set_trace(Config=ConfigWithLimit)

    expected = (
        """
        --Return--
        [NUM] > .*fn()
        -> set_trace(Config=ConfigWithLimit)
           5 frames hidden .*
        # error()
        \\*\\*\\* ValueError: the_end
        Traceback (most recent call last):
          File .*, in error
            f(10)
        """.rstrip()
        + (
            """
        .*~^*
        """.rstrip()
            if sys.version_info >= (3, 13)
            else ""
        )
        + """
          File .*, in f
            f(i)
        """.rstrip()
        + (
            """
        .*~^*
        """.rstrip()
            if sys.version_info >= (3, 13)
            else ""
        )
        + """
        # c
        """
    )

    check(
        fn,
        expected,
        add_313_fix=True,
        set_trace_args="Config=ConfigWithLimit",
    )


@pytest.mark.parametrize("show", (True, False))
def test_complete_displays_errors(show, monkeypatch, LineMatcher):
    class Config(ConfigTest):
        show_traceback_on_error = show

    def raises(*args):
        raise ValueError("err_complete")

    monkeypatch.setattr("pdbpp.Pdb._get_all_completions", raises)

    def fn():
        set_trace(Config=Config)

    commands = ["get_completions('test')", "c"]
    out = runpdb(fn, commands)
    assert out

    lm = LineMatcher(out)
    to_match = []
    if sys.version_info >= (3, 13):
        to_match.extend(
            [
                "* > *(*)fn()",
                "-> set_trace(Config=Config)",
                "* frames hidden*",
            ]
        )
    else:
        to_match.extend(
            [
                "--Return--",
                "[[]*[]] > *fn()->None",
                "-> set_trace(Config=Config)",
                "   5 frames hidden (try 'help hidden_frames')",
            ]
        )
    if show:
        to_match.extend(
            [
                "# get_completions('test')",
                "*** error during completion: err_complete",
                "ValueError: err_complete",
                "[]" if sys.version_info >= (3, 13) else "[[][]]",
                "# c",
            ]
        )
    else:
        to_match.extend(
            [
                "# get_completions('test')",
                "*** error during completion: err_complete",
                "??",
                "# c",
            ]
        )
    lm.fnmatch_lines(to_match)


def test_next_with_exception_in_call():
    """Ensure that "next" works correctly with exception (in try/except).

    Previously it would display the frame where the exception occurred, and
    then "next" would continue, instead of stopping at the next statement.
    """

    def fn():
        def keyerror():
            raise KeyError

        set_trace()
        try:
            keyerror()
        except KeyError:
            print("got_keyerror")

    expected = """
        [NUM] > .*fn()
        -> try:
           5 frames hidden .*
        # n
        [NUM] > .*fn()
        -> keyerror()
           5 frames hidden .*
        # n
        KeyError
        [NUM] > .*fn()
        -> keyerror()
           5 frames hidden .*
        # n
        [NUM] > .*fn()
        -> except KeyError:
           5 frames hidden .*
        # c
        got_keyerror
        """

    check(fn, expected, add_313_fix=True)


def test_locals():
    def fn():
        def f():
            set_trace()
            print(f"{foo=}")  # noqa: F821
            foo = 2  # noqa: F841

        f()

    expected = """
        [NUM] > .*f()
        -> print(f"{foo=}")
           5 frames hidden .*
        # foo=42
        # foo
        42
        # pp foo
        42
        # p foo
        42
        # c
        foo=42
        """
    if sys.version_info >= (3, 13):
        expected = textwrap.dedent(
            """
                [NUM] > .*f()
                -> set_trace()
                   NUM frames hidden .*
                # n
                """.rstrip()
        ) + textwrap.dedent(expected)

    check(fn, expected)


def test_locals_with_list_comprehension():
    def fn():
        mylocal = 1  # noqa: F841
        set_trace()
        print(mylocal)

    expected = """
        [NUM] > .*fn()
        -> print(mylocal)
           5 frames hidden .*
        # mylocal
        1
        # [x for x in str(mylocal)]
        ['1']
        # [mylocal for x in range(1)]
        [1]
        # mylocal = 42
        # [x for x in str(mylocal)]
        ['4', '2']
        # [mylocal for x in range(1)]
        [42]
        # c
        42
        """

    check(fn, expected, add_313_fix=True)


def test_get_editor_cmd(monkeypatch):
    _pdb = PdbTest()

    _pdb.config.editor = None
    monkeypatch.setenv("EDITOR", "nvim")
    assert _pdb._get_editor_cmd("fname", 42) == "nvim +42 fname"

    monkeypatch.setenv("EDITOR", "")
    with pytest.raises(
        RuntimeError, match=(r"Could not detect editor. Configure it or set \$EDITOR.")
    ):
        _pdb._get_editor_cmd("fname", 42)

    monkeypatch.delenv("EDITOR")

    try:
        which = "shutil.which"
        monkeypatch.setattr(which, lambda x: None)
    except AttributeError:
        which = "distutils.spawn.find_executable"
        monkeypatch.setattr(which, lambda x: None)
    with pytest.raises(
        RuntimeError, match=(r"Could not detect editor. Configure it or set \$EDITOR.")
    ):
        _pdb._get_editor_cmd("fname", 42)

    monkeypatch.setattr(which, lambda x: "vim")
    assert _pdb._get_editor_cmd("fname", 42) == "vim +42 fname"
    monkeypatch.setattr(which, lambda x: "vi")
    assert _pdb._get_editor_cmd("fname", 42) == "vi +42 fname"

    _format = _pdb._format_editcmd
    assert _format("subl {filename}:{lineno}", "with space", 12) == (
        "subl 'with space':12"
    )
    assert _format("edit", "with space", 12) == ("edit +12 'with space'")
    assert _format("edit +%%%d %%%s%% %d", "with space", 12) == (
        "edit +%12 %'with space'% 12"
    )


def test_edit_error(monkeypatch):
    class MyConfig(ConfigTest):
        editor = None

    monkeypatch.setenv("EDITOR", "")

    def fn():
        set_trace(Config=MyConfig)

    expected = r"""
        --Return--
        [NUM] > .*fn()
        -> set_trace(Config=MyConfig)
           5 frames hidden .*
        # edit
        \*\*\* Could not detect editor. Configure it or set \$EDITOR.
        # c
        """

    check(fn, expected, add_313_fix=True, set_trace_args="Config=MyConfig")


def test_global_pdb_per_thread_with_input_lock():
    def fn():
        import threading

        evt1 = threading.Event()
        evt2 = threading.Event()

        def __t1__(evt1, evt2):
            set_trace(cleanup=False)

        def __t2__(evt2):
            evt2.set()
            set_trace(cleanup=False)

        t1 = threading.Thread(name="__t1__", target=__t1__, args=(evt1, evt2))
        t1.start()

        assert evt1.wait(1.0) is True
        t2 = threading.Thread(name="__t2__", target=__t2__, args=(evt2,))
        t2.start()

        t1.join()
        t2.join()

    expected = (
        (
            ""
            if sys.version_info >= (3, 13)
            else """
        --Return--
        """.rstrip()
        )
        + """
        [NUM] > .*__t1__()
        -> set_trace(cleanup=False)
        # evt1.set()
        # import threading; threading.current_thread().name
        '__t1__'
        # assert evt2.wait(1.0) is True; import time; time.sleep(0.1)"""
        + (
            ""
            if sys.version_info >= (3, 13)
            else """
        --Return--
        """.rstrip()
        )
        + """
        [NUM] > .*__t2__().*
        -> set_trace(cleanup=False)
        # import threading; threading.current_thread().name
        '__t2__'
        # c
        # import threading; threading.current_thread().name
        '__t1__'
        # c
        """
    )

    check(fn, expected)


def test_usage_error_with_commands():
    def fn():
        set_trace()

    # in 3.13, calling `commands` with invalid arguments returns an error.
    # See https://github.com/python/cpython/issues/103464
    expected = """
        --Return--
        [NUM] > .*fn()->None
        -> set_trace()
           5 frames hidden .*
        # commands invalid""" + (
        r"""
        \*\*\* Invalid argument: invalid
          Usage: (Pdb) commands [bpnumber]
                 (com) ...
                 (com) end
                 (Pdb)
        # c
        """
        if sys.version_info >= (3, 13)
        else """
        .*Usage.*: commands [bnum]
                ...
                end
        # c
        """
    )

    check(fn, expected, add_313_fix=True)


def test_rebind_globals_kwonly():
    exec("def func(*args, header=None): pass", globals())
    func = globals()["func"]

    sig = str(inspect.signature(func))
    assert sig == "(*args, header=None)"
    new = pdbpp.rebind_globals(func, globals())
    assert str(inspect.signature(new)) == sig


def test_rebind_globals_annotations():
    exec("def func(ann: str = None) -> None: pass", globals())
    func = globals()["func"]

    sig = str(inspect.signature(func))
    assert sig in (
        "(ann: 'str' = None) -> 'None'",
        "(ann: str = None) -> None",
        "(ann:str=None)->None",
    )
    new = pdbpp.rebind_globals(func, globals())
    assert str(inspect.signature(new)) == sig


def test_rebind_globals_with_partial():
    import functools

    global test_global
    test_global = 0

    def func(a, b):
        global test_global
        return a + b + test_global

    pfunc = functools.partial(func)
    assert pfunc(0, 0) == 0

    newglobals = globals().copy()
    newglobals["test_global"] = 1
    new = pdbpp.rebind_globals(pfunc, newglobals)
    assert new(1, 40) == 42


def test_debug_with_set_trace():
    def fn():
        def inner():
            def inner_inner():
                pass

            set_trace(cleanup=False)

        set_trace()

    expected = (
        """
        --Return--
        [NUM] > .*fn()
        .*
           5 frames hidden .*
        # debug inner()
        ENTERING RECURSIVE DEBUGGER
        [NUM] > <string>(1)<module>()->None
        (#) r
        """.rstrip()
        + (
            ""
            if sys.version_info >= (3, 13)
            else """--Return--
        """
        )
        + """
        [NUM] > .*inner().*
        -> set_trace(cleanup=False)
           5 frames hidden .*
        (#) pdbpp.local.GLOBAL_PDB.curframe.f_code.co_name
        'inner'
        (#) debug inner_inner()
        ENTERING RECURSIVE DEBUGGER
        [NUM] > <string>(1)<module>().*
        ((#)) c
        LEAVING RECURSIVE DEBUGGER
        (#) c
        LEAVING RECURSIVE DEBUGGER
        # c
        """
    )

    check(fn, expected, add_313_fix=True)


def test_set_trace_with_incomplete_pdb():
    def fn():
        existing_pdb = PdbTest()
        assert not hasattr(existing_pdb, "botframe")

        set_trace(cleanup=False)

        assert hasattr(existing_pdb, "botframe")
        assert pdbpp.local.GLOBAL_PDB is existing_pdb

    check(
        fn,
        """
        [NUM] > .*fn()
        .*
           5 frames hidden .*
        # c
        """,
    )


def test_config_gets_start_filename():
    def fn():
        # has to match the position of the Pdb().set_trace call in the set_trace() definition above
        setup_lineno = set_trace.__code__.co_firstlineno + 8
        # has to match the position of the set_trace call in MyConfig below
        set_trace_lineno = sys._getframe().f_lineno + 8

        class MyConfig(ConfigTest):
            def setup(self, pdb):
                print("config_setup")
                assert pdb.start_filename.lower() == THIS_FILE_CANONICAL.lower()
                assert pdb.start_lineno == setup_lineno

        set_trace(Config=MyConfig)

        assert pdbpp.local.GLOBAL_PDB.start_lineno == set_trace_lineno

    # fmt: off
    expected = (( """
    config_setup
    [NUM] > .*fn()
    -> set_trace(Config=MyConfig)
       NUM frames hidden .*
    # n
    """ if sys.version_info >= (3, 13) else """
    config_setup
    """).rstrip() + """
    [NUM] > .*fn()
    -> assert pdbpp.local.GLOBAL_PDB.start_lineno == set_trace_lineno
       5 frames hidden .*
    # c
    """
    )
    # fmt: on

    check(fn, expected)


def test_do_bt():
    def fn():
        set_trace()

    expected_bt = []
    _entry: traceback.FrameSummary
    for i, _entry in enumerate(traceback.extract_stack()[:-3]):
        expected_bt.append(f"  [{i:2d}] .*")

        if (
            sys.platform == "win32"
            and sys.version_info >= (3, 11)
            and (
                _entry.filename == "<frozen runpy>"
                and any(
                    _entry.name == name
                    for name in (
                        "_run_module_as_main",
                        "_run_code",
                    )
                )
            )
        ):
            # In this case, the first two frames of the traceback look like this:
            #   [ 0] <frozen runpy>(198)_run_module_as_main()
            #   [ 1] <frozen runpy>(88)_run_code()
            # meaning we will not need the .* regex to match code for these frames
            continue

        expected_bt.append("  .*")

    expected = """
--Return--
[NUM] > .*fn().*
-> set_trace()
   5 frames hidden .*
# bt
{expected}
  [NUM] .*(NUM)runpdb()
       func()
> [NUM] .*(NUM)fn()->None
       set_trace()
# c
""".format(expected="\n".join(expected_bt))

    check(fn, expected, add_313_fix=True)


def test_do_bt_highlight():
    def fn():
        set_trace(Config=ConfigWithHighlight)

    expected_bt = []
    _entry: traceback.FrameSummary
    for i, _entry in enumerate(traceback.extract_stack()[:-3]):
        expected_bt.append(f"  [{i:2d}] .*")

        if (
            sys.platform == "win32"
            and sys.version_info >= (3, 11)
            and (
                _entry.filename == "<frozen runpy>"
                and any(
                    _entry.name == name
                    for name in (
                        "_run_module_as_main",
                        "_run_code",
                    )
                )
            )
        ):
            # In this case, the first two frames of the traceback look like this:
            #   [ 0] <frozen runpy>(198)_run_module_as_main()
            #   [ 1] <frozen runpy>(88)_run_code()
            # meaning we will not need the .* regex to match code for these frames
            continue

        expected_bt.append("  .*")

    expected = r"""
--Return--
[NUM] > .*fn()->None
-> set_trace(Config=ConfigWithHighlight)
   5 frames hidden .*
# bt
{expected}
  [NUM] ^[[33;01m.*\.py^[[00m(^[[36;01mNUM^[[00m)runpdb()
       func()
> [NUM] ^[[33;01m.*\.py^[[00m(^[[36;01mNUM^[[00m)fn()->None
       set_trace(Config=ConfigWithHighlight)
# c
""".format(expected="\n".join(expected_bt))

    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
            [NUM] > .*fn().*
            -> set_trace(Config=ConfigWithHighlight)
               NUM frames hidden .*
            # n
        """.rstrip()
            )
            + expected
        )
    check(fn, expected)


def test_do_bt_pygments():
    def fn():
        set_trace(Config=ConfigWithPygments)

    expected_bt = []
    _entry: traceback.FrameSummary
    for i, _entry in enumerate(traceback.extract_stack()[:-3]):
        expected_bt.append(f"  [{i:2d}] .*")

        if (
            sys.platform == "win32"
            and sys.version_info >= (3, 11)
            and (
                _entry.filename == "<frozen runpy>"
                and any(
                    _entry.name == name
                    for name in (
                        "_run_module_as_main",
                        "_run_code",
                    )
                )
            )
        ):
            # In this case, the first two frames of the traceback look like this:
            #   [ 0] <frozen runpy>(198)_run_module_as_main()
            #   [ 1] <frozen runpy>(88)_run_code()
            # meaning we will not need the .* regex to match code for these frames
            continue

        expected_bt.append("  .*")

    expected = r"""
--Return--
[NUM] > .*fn()->None
-> set_trace(Config^[[38;5;241m=^[[39mConfigWithPygments)
   5 frames hidden .*
# bt
{expected}
  [NUM] .*(NUM)runpdb()
       func()
> [NUM] .*\.py(NUM)fn()->None
       set_trace(Config^[[38;5;241m=^[[39mConfigWithPygments)
# c
""".format(expected="\n".join(expected_bt))

    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
            [NUM] > .*fn().*
            -> set_trace(Config^[[38;.*=^[[39mConfigWithPygments)
               NUM frames hidden .*
            # n
            """.rstrip()
            )
            + expected
        )

    check(fn, expected)


def test_debug_with_pygments():
    def fn():
        set_trace(Config=ConfigWithPygments)

    expected = r"""
        --Return--
        [NUM] > .*fn()->None
        -> set_trace(Config^[[38;5;241m=^[[39mConfigWithPygments)
           5 frames hidden .*
        # debug 1
        ENTERING RECURSIVE DEBUGGER
        [1] > <string>(1)<module>()->None
        (#) c
        LEAVING RECURSIVE DEBUGGER
        # c
        """

    if sys.version_info >= (3, 13):
        expected = textwrap.dedent(
            """
            [NUM] > .*fn()
            -> set_trace(Config^[[38;.*=^[[39mConfigWithPygments)
               NUM frames hidden .*
            # n
            """.rstrip()
        ) + textwrap.dedent(expected)

    check(fn, expected)


def test_debug_with_pygments_and_highlight():
    def fn():
        set_trace(Config=ConfigWithPygmentsAndHighlight)

    expected = r"""
        --Return--
        [NUM] > .*fn()->None
        -> set_trace(Config^[[38;5;241m=^[[39mConfigWithPygmentsAndHighlight)
           5 frames hidden .*
        # debug 1
        ENTERING RECURSIVE DEBUGGER
        [1] > ^[[33;01m<string>^[[00m(^[[36;01m1^[[00m)<module>()->None
        (#) c
        LEAVING RECURSIVE DEBUGGER
        # c
        """

    if sys.version_info >= (3, 13):
        expected = textwrap.dedent(
            """
            [NUM] > .*fn()
            -> set_trace(Config^[[38;.*=^[[39mConfigWithPygmentsAndHighlight)
               NUM frames hidden .*
            # n
            """.rstrip()
        ) + textwrap.dedent(expected)

    check(fn, expected)


def test_set_trace_in_default_code():
    """set_trace while not tracing and should not (re)set the global pdb."""

    def fn():
        def f():
            before = pdbpp.local.GLOBAL_PDB
            set_trace(cleanup=False)
            assert before is pdbpp.local.GLOBAL_PDB

        set_trace()

    expected = f"""
        --Return--
        [NUM] > .*fn()->None
        -> set_trace()
           5 frames hidden .*
        # f()
        # import pdbpp; pdbpp.local.GLOBAL_PDB.curframe is not None
        True
        # l {fn.__code__.co_firstlineno}, 2
        NUM \t    def fn():
        NUM \t        def f():
        NUM \t            before = pdbpp.local.GLOBAL_PDB
        # c
        """
    check(fn, expected, add_313_fix=True)


def test_error_with_pp():
    def fn():
        class BadRepr:
            def __repr__(self):
                raise Exception("repr_exc")

        obj = BadRepr()  # noqa: F841
        set_trace()

    expected = r"""
        --Return--
        [NUM] > .*fn()->None
        -> set_trace()
           5 frames hidden .*
        # p obj
        \*\*\* Exception: repr_exc
        # pp obj
        \*\*\* Exception: repr_exc
        # pp BadRepr.__repr__()
        \*\*\* TypeError: .*__repr__.*
        # c
        """

    check(fn, expected, add_313_fix=True)


def test_count_with_pp():
    def fn():
        set_trace()

    expected = r"""
        --Return--
        [NUM] > .*fn()->None
        -> set_trace()
           5 frames hidden .*
        # pp [1, 2, 3]
        [1, 2, 3]
        # 2pp [1, 2, 3]
        [1,
         2,
         3]
        # 80pp [1, 2, 3]
        [1, 2, 3]
        # c
        """
    check(fn, expected, add_313_fix=True)


def test_ArgWithCount():
    from pdbpp import ArgWithCount

    obj = ArgWithCount("", None)
    assert obj == ""
    assert repr(obj) == "<ArgWithCount cmd_count=None value=''>"
    assert isinstance(obj, str)

    obj = ArgWithCount("foo", 42)
    assert obj == "foo"
    assert repr(obj) == "<ArgWithCount cmd_count=42 value='foo'>"


def test_do_source():
    def fn():
        set_trace()

    expected = textwrap.dedent(
        r"""
        --Return--
        [NUM] > .*fn()->None
        -> set_trace()
           5 frames hidden .*
        # source ConfigWithPygmentsAndHighlight
        \d\d     class ConfigWithPygmentsAndHighlight(ConfigWithPygments, ConfigWithHigh$
        \d\d         pass
        # c
        """,
    )
    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*fn()
                -> set_trace()
                   5 frames hidden .*
                # n
                """.rstrip()
            )
            + expected
        )

    check(fn, expected)


def test_do_source_with_pygments():
    def fn():
        set_trace(Config=ConfigWithPygments)

    if int(pygments_major) >= 2 and int(pygments_minor) >= 19:
        highlighted_code = "^[[38;5;28;01mclass^[[39;00m^[[38;5;250m ^[[39m^[[38;5;21;01mConfigWithPygmentsAndHighlight^[[39;00m(ConfigWithPygments, ConfigWithHigh"
    else:  # pygments 2.18
        highlighted_code = "^[[38;5;28;01mclass^[[39;00m ^[[38;5;21;01mConfigWithPygmentsAndHighlight^[[39;00m(ConfigWithPygments, ConfigWithHigh"

    expected = textwrap.dedent(
        rf"""
        --Return--
        [NUM] > .*fn()->None
        -> set_trace(Config^[[38;5;241m=^[[39mConfigWithPygments)
           5 frames hidden .*
        # source ConfigWithPygmentsAndHighlight
        \d\d     {highlighted_code}$
        \d\d         ^[[38;5;28;01mpass^[[39;00m
        # c
        """,
    )
    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*fn()
                -> set_trace(Config^[[38;5;241m=^[[39mConfigWithPygments)
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )

    check(fn, expected)


def test_do_source_with_highlight():
    def fn():
        set_trace(Config=ConfigWithHighlight)

    expected = textwrap.dedent(
        r"""
        --Return--
        [NUM] > .*fn()->None
        -> set_trace(Config=ConfigWithHighlight)
           5 frames hidden .*
        # source ConfigWithPygmentsAndHighlight
        ^[[36;01m\d\d^[[00m     class ConfigWithPygmentsAndHighlight(ConfigWithPygments, ConfigWithHigh$
        ^[[36;01m\d\d^[[00m         pass
        # c
        """,
    )
    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*fn()
                -> set_trace(Config=ConfigWithHighlight)
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )

    check(fn, expected)


def test_do_source_with_pygments_and_highlight():
    def fn():
        set_trace(Config=ConfigWithPygmentsAndHighlight)

    if int(pygments_major) >= 2 and int(pygments_minor) >= 19:
        highlighted_code = "^[[38;5;28;01mclass^[[39;00m^[[38;5;250m ^[[39m^[[38;5;21;01mConfigWithPygmentsAndHighlight^[[39;00m(ConfigWithPygments, ConfigWithHigh"
    else:  # pygments 2.18
        highlighted_code = "^[[38;5;28;01mclass^[[39;00m ^[[38;5;21;01mConfigWithPygmentsAndHighlight^[[39;00m(ConfigWithPygments, ConfigWithHigh"

    expected = textwrap.dedent(
        rf"""
        --Return--
        [NUM] > .*fn()->None
        -> set_trace(Config^[[38;5;241m=^[[39mConfigWithPygmentsAndHighlight)
           5 frames hidden .*
        # source ConfigWithPygmentsAndHighlight
        ^[[36;01m\d\d^[[00m     {highlighted_code}$
        ^[[36;01m\d\d^[[00m         ^[[38;5;28;01mpass^[[39;00m
        # c
    """,
    )
    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*fn()
                -> set_trace(Config^[[38;5;241m=^[[39mConfigWithPygmentsAndHighlight)
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )

    check(fn, expected)


def test_do_source_without_truncating():
    def fn():
        class Config(ConfigTest):
            truncate_long_lines = False

        set_trace(Config=Config)

    expected = textwrap.dedent(
        r"""
        --Return--
        [NUM] > .*fn()->None
        -> set_trace(Config=Config)
           5 frames hidden .*
        # source ConfigWithPygmentsAndHighlight
        \d\d     class ConfigWithPygmentsAndHighlight(ConfigWithPygments, ConfigWithHighlight):$
        \d\d         pass
        # c
        """,
    )
    if sys.version_info >= (3, 13):
        expected = (
            textwrap.dedent(
                """
                [NUM] > .*fn()
                -> set_trace(Config=Config)
                   5 frames hidden .*
                # n
                """.rstrip(),
            )
            + expected
        )

    check(fn, expected)


def test_handles_set_trace_in_config(tmpdir):
    """Should not cause a RecursionError."""

    def fn():
        class Config(ConfigTest):
            def __init__(self, *args, **kwargs):
                print("Config.__init__")
                # Becomes a no-op.
                set_trace(Config=Config)
                print("after_set_trace")

        set_trace(Config=Config)

    expected = (
        r"""
        Config.__init__
        pdb\+\+: using pdb.Pdb for recursive set_trace.
        > .*__init__()
        """
        + (
            "-> set_trace(Config=Config)"
            if sys.version_info >= (3, 13)
            else '-> print("after_set_trace")'
        )
        + """
        (Pdb) c
        after_set_trace
        """.rstrip()
        + (
            ""
            if sys.version_info >= (3, 13)
            else """
        --Return--"""
        )
        + """
        [NUM] > .*fn().*
        -> set_trace(Config=Config)
           5 frames hidden .*
        # c
        """
    )

    check(fn, expected)


def test_only_question_mark(monkeypatch):
    def fn():
        set_trace()
        a = 1
        return a

    monkeypatch.setattr(PdbTest, "do_help", lambda self, arg: print("do_help"))

    expected = """
        [NUM] > .*fn()
        -> a = 1
           5 frames hidden .*
        # ?
        do_help
        # c
        """

    check(fn, expected, add_313_fix=True)


@pytest.mark.parametrize(
    "s,maxlength,expected",
    [
        pytest.param("foo", 3, "foo", id="id1"),
        pytest.param("foo", 1, "f", id="id2"),
        # Keeps trailing escape sequences (for reset at least).
        pytest.param("\x1b[39m1\x1b[39m23", 1, "\x1b[39m1\x1b[39m", id="id3"),
        pytest.param("\x1b[39m1\x1b[39m23", 2, "\x1b[39m1\x1b[39m2", id="id4"),
        pytest.param("\x1b[39m1\x1b[39m23", 3, "\x1b[39m1\x1b[39m23", id="id5"),
        pytest.param("\x1b[39m1\x1b[39m23", 100, "\x1b[39m1\x1b[39m23", id="id5"),
        pytest.param("\x1b[39m1\x1b[39m", 100, "\x1b[39m1\x1b[39m", id="id5"),
    ],
)
def test_truncate_to_visible_length(s, maxlength, expected):
    assert pdbpp.Pdb._truncate_to_visible_length(s, maxlength) == expected


def test_keeps_reset_escape_sequence_with_source_highlight():
    class MyConfig(ConfigWithPygmentsAndHighlight):
        sticky_by_default = True

    def fn():
        set_trace(Config=MyConfig)

        a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaX"
        b = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbX"
        return a, b

    if int(pygments_major) >= 2 and int(pygments_minor) >= 19:
        highlighted_code = (
            "^[[38;5;28;01mdef^[[39;00m^[[38;5;250m ^[[39m^[[38;5;21mfn^[[39m():"
        )
    else:  # pygments 2.18
        highlighted_code = "^[[38;5;28;01mdef^[[39;00m ^[[38;5;21mfn^[[39m():"

    if sys.version_info >= (3, 13):
        curline_313 = "COLORCURLINE"
        curline_pre_313 = "COLORNUM"
        marker_313 = "->"
        marker_pre_313 = "  "
    else:
        curline_313 = "COLORNUM"
        curline_pre_313 = "COLORCURLINE"
        marker_313 = "  "
        marker_pre_313 = "->"

    expected = textwrap.dedent(f"""
        [NUM] > .*fn()

        <COLORNUM>         {highlighted_code}
        <{curline_313}>  {marker_313}         set_trace(Config^[[38;5;.*m=^[[39.*mMyConfig).*
        <COLORNUM>     $
        <{curline_pre_313}>  {marker_pre_313}         a ^[[38;5;.*m=^[[39.*^[[38;5;.*m"^[[39.*m^[[38;5.*maaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa^[[39.*m^[[38;5.*m.*
        <COLORNUM>             b ^[[38;5;241m=^[[39m ^[[38;5;124m"^[[39m^[[38;5;124mbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb^[[39m^[[38;5;124m<PYGMENTSRESET>
        <COLORNUM>             ^[[38;5;28;01mreturn^[[39;00m a, b
        # c
        """)

    check(fn, expected)


@pytest.mark.parametrize("pass_stdout", (True, False))
def test_stdout_reconfigured(pass_stdout, monkeypatch):
    """Check that self.stdout is re-configured with global pdb."""

    def fn():
        import sys
        from io import StringIO

        patched_stdout = StringIO()

        with monkeypatch.context() as mp:
            mp.setattr(sys, "stdout", patched_stdout)

            class _PdbTestKeepRawInput(PdbTest):
                def __init__(
                    self, completekey="tab", stdin=None, stdout=None, *args, **kwargs
                ):
                    stdout = sys.stdout if pass_stdout else None
                    super().__init__(completekey, stdin, stdout, *args, **kwargs)
                    # Keep this, which gets set to 0 with stdin being passed in.
                    self.use_rawinput = True

            set_trace(Pdb=_PdbTestKeepRawInput)
            assert pdbpp.local.GLOBAL_PDB.stdout is patched_stdout

            print(patched_stdout.getvalue())
            patched_stdout.close()

        set_trace(Pdb=_PdbTestKeepRawInput, cleanup=False)
        assert pdbpp.local.GLOBAL_PDB.stdout is sys.stdout
        print("# c")  # Hack to reflect output in test.
        return

    expected = (
        """
        [NUM] > .*fn()
        """
        + (
            "-> set_trace(Pdb=_PdbTestKeepRawInput, cleanup=False)"
            if sys.version_info >= (3, 13)
            else "-> assert pdbpp.local.GLOBAL_PDB.stdout is sys.stdout"
        )
        + """
           5 frames hidden .*
        # c
        # c
        """
    )

    check(fn, expected)


def test_position_of_obj_unwraps():
    import contextlib

    @contextlib.contextmanager
    def cm():
        raise NotImplementedError()

    pdb_ = PdbTest()
    pos = pdb_._get_position_of_obj(cm)

    if hasattr(inspect, "unwrap"):
        assert pos[0] == THIS_FILE_CANONICAL
        assert pos[2] == [
            "    @contextlib.contextmanager\n",
            "    def cm():\n",
            "        raise NotImplementedError()\n",
        ]
    else:
        contextlib_file = contextlib.__file__
        if sys.platform == "win32":
            contextlib_file = contextlib_file.lower()
        assert pos[0] == contextlib_file.rstrip("c")


def test_set_trace_in_skipped_module(testdir):
    def fn():
        class SkippingPdbTest(PdbTest):
            def __init__(self, *args, **kwargs):
                kwargs["skip"] = ["testing.test_pdb"]
                super().__init__(*args, **kwargs)

                self.calls = []

            def is_skipped_module(self, module_name):
                self.calls.append(module_name)
                if len(self.calls) == 1:
                    print("is_skipped_module?", module_name)
                    ret = super().is_skipped_module(module_name)
                    assert module_name == "testing.test_pdb"
                    assert ret is True
                    return True
                return False

        set_trace(Pdb=SkippingPdbTest)  # 1
        set_trace(Pdb=SkippingPdbTest, cleanup=False)  # 2
        set_trace(Pdb=SkippingPdbTest, cleanup=False)  # 3

    # fmt: off
    expected = r"""
        [NUM] > .*fn()
        """ + ("-> set_trace(Pdb=SkippingPdbTest)  # 1" if sys.version_info >= (3, 13) else "-> set_trace(Pdb=SkippingPdbTest, cleanup=False)  # 2") + r"""
           5 frames hidden (try 'help hidden_frames')
        # n
        is_skipped_module\? testing.test_pdb
        [NUM] > .*fn()
        """ + ("-> set_trace(Pdb=SkippingPdbTest, cleanup=False)  # " if sys.version_info >= (3, 13) else "-> set_trace(Pdb=SkippingPdbTest, cleanup=False)  # 3") + """
           5 frames hidden (try 'help hidden_frames')
        # c
        """.rstrip() + ("" if sys.version_info >= (3, 13) else """
        --Return--
        """.rstrip()) + """
        [NUM] > .*fn()
        """ + ("" if sys.version_info >= (3, 13) else "-> set_trace(Pdb=SkippingPdbTest, cleanup=False)  # 3") + """
           5 frames hidden (try 'help hidden_frames')
        # c
        """
    # fmt: on

    check(fn, expected)


def test_exception_info_main(testdir):
    """Test that interaction adds __exception__ similar to user_exception."""
    pyfile = testdir.makepyfile(
        """
        def f():
            raise ValueError("foo")

        f()
        """
    )
    testdir.monkeypatch.setenv("PDBPP_COLORS", "0")

    result = testdir.run(
        sys.executable,
        "-m",
        "pdbpp",
        str(pyfile),
        stdin=b"p 'sticky'\nsticky\np 'cont'\ncont\np 'quit'\nq\n",
    )
    result.stdout.lines = [
        ln.replace(pdbpp.CLEARSCREEN, "<CLEARSCREEN>") for ln in result.stdout.lines
    ]
    assert (
        result.stdout.str().count("<CLEARSCREEN>") == 2
        if sys.version_info < (3, 13)
        else 1
    )
    lines_to_match = [
        "(Pdb++) 'sticky'",
        "(Pdb++) <CLEARSCREEN>[[]2[]] > */test_exception_info_main.py(1)<module>()",
        "(Pdb++) 'cont'",
        "(Pdb++) Uncaught exception. Entering post mortem debugging",
        # NOTE: this explicitly checks for a missing CLEARSCREEN in front.
        "[[]5[]] > *test_exception_info_main.py(2)f()",
        "",
        "1     def f():",
        '2  ->     raise ValueError("foo")',
        "ValueError: foo",
        "(Pdb++) 'quit'",
    ]
    if sys.version_info < (3, 13):
        lines_to_match.extend(
            [
                "(Pdb++) Post mortem debugger finished. *",
                "<CLEARSCREEN>[[]2[]] > */test_exception_info_main.py(1)<module>()",
                "",
            ]
        )

    result.stdout.fnmatch_lines(lines_to_match)


def test_interaction_no_exception():
    """Check that it does not display `None`."""

    def outer():
        try:
            raise ValueError()
        except ValueError:
            return sys.exc_info()[2]

    def fn():
        tb = outer()
        pdb_ = PdbTest()
        pdb_.reset()
        pdb_.interaction(None, tb)

    check(
        fn,
        """
        [NUM] > .*outer()
        -> raise ValueError()
        # sticky
        [0] > .*outer()

        NUM         def outer():
        NUM             try:
        NUM  >>             raise ValueError()
        NUM             except ValueError:
        NUM  ->             return sys.exc_info()[2]
        # q
        """,
    )


def test_debug_in_post_mortem_does_not_trace_itself():
    def fn():
        try:
            raise ValueError()
        except:
            pdbpp.post_mortem(Pdb=PdbTest)
        a = 1
        return a

    check(
        fn,
        """
        [0] > .*fn()
        -> raise ValueError()
        # debug "".strip()
        ENTERING RECURSIVE DEBUGGER
        [1] > <string>(1)<module>()
        (#) s
        --Return--
        [1] > <string>(1)<module>()->None
        (#) q
        LEAVING RECURSIVE DEBUGGER
        # q
        """,
    )


class TestCommands:
    @staticmethod
    def fn():
        def f():
            print(a)

        a = 0
        set_trace()

        for i in range(5):
            a += i
            f()

    def test_commands_with_sticky(self):
        expected = r"""
            [NUM] > .*fn()
            -> for i in range(5):
               5 frames hidden .*
            # sticky
            <CLEARSCREEN>
            [NUM] > .*(), 5 frames hidden

            NUM         @staticmethod
            NUM         def fn():
            NUM             def f():
            NUM                 print(a)
            NUM     $
            NUM             a = 0
            NUM             set_trace()
            NUM     $
            NUM  ->         for i in range(5):
            NUM                 a \+= i
            NUM                 f()
            # break f, a==6
            Breakpoint NUM at .*
            # commands
            (com++) print("stop", a)
            (com++) end
            # c
            0
            1
            3
            stop 6
            [NUM] > .*f(), 5 frames hidden

            NUM             def f():
            NUM  ->             print(a)
            # import pdb; pdbpp.local.GLOBAL_PDB.clear_all_breaks()
            # c
            6
            10
            """

        check(self.fn, expected, add_313_fix=True)

    def test_commands_without_sticky(self):
        expected = r"""
            [NUM] > .*fn()
            -> for i in range(5):
               5 frames hidden .*
            # break f, a==6
            Breakpoint NUM at .*
            # commands
            (com++) print("stop", a)
            (com++) end
            # c
            0
            1
            3
            stop 6
            [NUM] > .*f()$
            -> print(a)
            # import pdb; pdbpp.local.GLOBAL_PDB.clear_all_breaks()
            # c
            6
            10
            """

        check(self.fn, expected, add_313_fix=True)
