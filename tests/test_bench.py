import bench
import pytest

CASE = {case.id: case for case in bench.CASES}


def grade(case_id: str, answer: str) -> bool:
    return CASE[case_id].grade(answer)


def fenced(code: str, lang: str = "python") -> str:
    return f"Here you go:\n```{lang}\n{code}\n```\nHope that helps."


class TestSandbox:
    def test_passing_and_failing_code(self) -> None:
        assert bench.passes_python("x = 1", "assert x == 1")
        assert not bench.passes_python("x = 1", "assert x == 2")

    def test_timeout_fails(self) -> None:
        assert not bench.passes_python("while True: pass", "", timeout=1)

    def test_network_is_blocked(self) -> None:
        code = "import socket, sys\ntry:\n    socket.create_connection(('1.1.1.1', 53), timeout=2)\nexcept OSError:\n    sys.exit(0)\nsys.exit(1)"
        assert bench.passes_python(code, "")

    def test_home_is_read_only(self) -> None:
        code = "import sys\ntry:\n    open('/home/blocked-write-test', 'w')\nexcept OSError:\n    sys.exit(0)\nsys.exit(1)"
        assert bench.passes_python(code, "")

    def test_tmp_is_writable(self) -> None:
        assert bench.passes_python("open('/tmp/ok', 'w').write('x')", "")


class TestExtraction:
    def test_prefers_block_with_a_definition(self) -> None:
        text = "```\nprint('hi')\n```\n```python\ndef f():\n    return 1\n```"
        assert "def f" in bench.code_block(text)

    def test_unfenced_answer_is_used_whole(self) -> None:
        assert bench.code_block("def f(): return 1") == "def f(): return 1"

    def test_think_blocks_are_dropped(self) -> None:
        assert bench.clean("<think>def evil(): pass</think>403") == "403"


class TestCodeCases:
    def test_palindrome(self) -> None:
        good = "import re\ndef is_palindrome(s):\n    t = re.sub(r'[^a-z0-9]', '', s.lower())\n    return t == t[::-1]"
        bad = "def is_palindrome(s):\n    return s == s[::-1]"
        assert grade("palindrome", fenced(good))
        assert not grade("palindrome", fenced(bad))

    def test_fizzbuzz(self) -> None:
        good = "def fizzbuzz_list(n):\n    return ['FizzBuzz' if i % 15 == 0 else 'Fizz' if i % 3 == 0 else 'Buzz' if i % 5 == 0 else str(i) for i in range(1, n + 1)]"
        assert grade("fizzbuzz", fenced(good))
        assert not grade(
            "fizzbuzz",
            fenced(
                "def fizzbuzz_list(n):\n    return [str(i) for i in range(1, n + 1)]"
            ),
        )

    def test_intervals_accepts_tuples_and_rejects_no_merge(self) -> None:
        good = "def merge_intervals(iv):\n    out = []\n    for s, e in sorted(iv):\n        if out and s <= out[-1][1]:\n            out[-1] = (out[-1][0], max(out[-1][1], e))\n        else:\n            out.append((s, e))\n    return out"
        assert grade("intervals", fenced(good))
        assert not grade(
            "intervals", fenced("def merge_intervals(iv):\n    return sorted(iv)")
        )

    def test_bugfix_requires_the_boundary_fix(self) -> None:
        fixed = "def bsearch(a, x):\n    lo, hi = 0, len(a) - 1\n    while lo <= hi:\n        mid = (lo + hi) // 2\n        if a[mid] == x:\n            return mid\n        if a[mid] < x:\n            lo = mid + 1\n        else:\n            hi = mid - 1\n    return -1"
        assert grade("bugfix", fenced(fixed))
        assert not grade("bugfix", fenced(fixed.replace("<=", "<")))

    def test_infinite_loop_answer_fails_instead_of_hanging(self) -> None:
        assert not bench.passes_python(
            "def f():\n    while True: pass\nf()", "", timeout=1
        )


class TestShellCase:
    def test_correct_commands_pass(self) -> None:
        assert grade(
            "shell",
            fenced("find . -type f -printf '%s %p\\n' | sort -rn | head -5", "bash"),
        )
        assert grade(
            "shell", fenced("find . -type f -exec ls -S {} + | head -5", "bash")
        )

    @pytest.mark.parametrize(
        "command",
        [
            "du -ah . | sort -rh | head -5",  # lists directories and sizes, not just files
            "find . -type f | head -5",  # directory order, not size order
            "ls -S | head -5",  # skips the subdirectory
            "find . -type f -printf '%s %p\\n' | sort -n | head -5",  # smallest first
            "find . -type f | sort | head -5",  # alphabetical
        ],
    )
    def test_plausible_wrong_commands_fail(self, command: str) -> None:
        assert not grade("shell", fenced(command, "bash"))


@pytest.mark.parametrize(
    ("case_id", "answer", "expected"),
    [
        (
            "json",
            '```json\n{"name": "Maria Lopez", "age": 34, "city": "Austin"}\n```',
            True,
        ),
        ("json", '{"name": "Maria Lopez", "age": "34", "city": "Austin"}', True),
        ("json", '{"name": "Maria", "age": 34, "city": "Austin"}', False),
        ("json", "Maria Lopez is 34 and lives in Austin", False),
        ("format", "apple\nbanana\ncherry", True),
        ("format", "1. apple\n2. banana\n3. cherry", False),
        ("format", "apple\nbanana", False),
        ("arithmetic", "403", True),
        ("arithmetic", "17*23 = 391, plus 12 is **403**.", True),
        ("arithmetic", "404", False),
        ("capital", "Canberra", True),
        ("capital", "Sydney, not Canberra", False),
        ("tool_call", '{"name": "get_weather", "arguments": {"city": "Paris"}}', True),
        (
            "tool_call",
            '{"name": "get_weather", "arguments": {"city": "London"}}',
            False,
        ),
        ("tool_call", "I would call get_weather for Paris.", False),
    ],
)
def test_text_graders(case_id: str, answer: str, expected: bool) -> None:
    assert grade(case_id, answer) is expected
