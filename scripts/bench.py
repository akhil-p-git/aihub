#!/usr/bin/env python3
"""Benchmark local Ollama models on a fixed prompt set.

Each answer is graded by code, not by another model: generated Python and shell commands run inside bwrap
(read-only filesystem, no network, 10 s timeout). Reports pass count, generation tokens/sec and how much of
the model sits in VRAM. Refuses to run while other apps hold the GPU, because that skews the numbers and
stutters whatever else is using it. Raw answers go to state/bench/ (gitignored).

    uv run --no-project scripts/bench.py qwen3:8b gemma4:e4b
"""

import argparse
import json
import re
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

OLLAMA = "http://127.0.0.1:11434"
NUM_CTX = 8192
MAX_OTHER_GPU_MIB = 2500  # desktop plus a browser; a running game is well above this
REPO = Path(__file__).resolve().parents[1]


def clean(text: str) -> str:
    """Drop <think> blocks that some models put inline in the answer."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def code_block(text: str) -> str:
    blocks = re.findall(r"```[a-zA-Z]*\n(.*?)```", clean(text), flags=re.DOTALL)
    return next(
        (b for b in blocks if "def " in b or "class " in b),
        blocks[0] if blocks else clean(text),
    )


def sandbox(
    argv: list[str], bind: Path | None = None, timeout: int = 10
) -> subprocess.CompletedProcess[str] | None:
    cmd = [
        "bwrap",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
    ]
    cmd += ["--unshare-all", "--die-with-parent"]
    if bind:
        cmd += ["--bind", str(bind), "/tmp/work", "--chdir", "/tmp/work"]
    try:
        return subprocess.run(
            [*cmd, *argv], capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return None


def passes_python(code: str, tests: str, timeout: int = 10) -> bool:
    result = sandbox(["python3", "-I", "-c", f"{code}\n{tests}"], timeout=timeout)
    return result is not None and result.returncode == 0


def json_object(text: str) -> dict | None:
    match = re.search(r"\{.*\}", clean(text), flags=re.DOTALL)
    try:
        value = json.loads(match.group(0)) if match else None
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


# KB per file; the five largest are f2, f4, f6, f1, f8 in that order, and f3/f6 live in a subdirectory.
SHELL_SIZES_KB = {1: 5, 2: 8, 3: 2, 4: 7, 5: 1, 6: 6, 7: 3, 8: 4}
SHELL_SUBDIR = {3, 6}
SHELL_EXPECTED = ["f2.dat", "f4.dat", "f6.dat", "f1.dat", "f8.dat"]


def grade_shell(text: str) -> bool:
    """Run the proposed command on a fixed tree; the five largest files must appear, largest first."""
    block = code_block(text)
    command = (
        block.strip().splitlines()[0]
        if "```" in clean(text)
        else clean(text).splitlines()[0].strip("` ")
    )
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "sub").mkdir()
        for i, kb in SHELL_SIZES_KB.items():
            folder = Path(tmp) / "sub" if i in SHELL_SUBDIR else Path(tmp)
            (folder / f"f{i}.dat").write_bytes(b"x" * 1024 * kb)
        result = sandbox(["bash", "-c", command], bind=Path(tmp))
    if result is None or result.returncode != 0:
        return False
    return (
        re.findall(r"f\d\.dat", result.stdout) == SHELL_EXPECTED
        and len(result.stdout.strip().splitlines()) == 5
    )


def grade_json(text: str) -> bool:
    data = json_object(text)
    return (
        bool(data)
        and data.get("name") == "Maria Lopez"
        and str(data.get("age")) == "34"
        and data.get("city") == "Austin"
    )


def grade_lines(text: str) -> bool:
    lines = [line.strip() for line in clean(text).splitlines() if line.strip()]
    return len(lines) == 3 and all(
        re.fullmatch(r"[a-z]+( [a-z]+)?", line) for line in lines
    )


def grade_number(text: str) -> bool:
    numbers = re.findall(r"-?\d+(?:\.\d+)?", clean(text).replace(",", ""))
    return bool(numbers) and float(numbers[-1]) == 403


def grade_capital(text: str) -> bool:
    answer = clean(text).lower()
    return "canberra" in answer and "sydney" not in answer and "melbourne" not in answer


def grade_tool_call(text: str) -> bool:
    data = json_object(text)
    arguments = data.get("arguments") if data else None
    return (
        isinstance(arguments, dict)
        and data.get("name") == "get_weather"
        and str(arguments.get("city", "")).lower() == "paris"
    )


@dataclass(frozen=True, slots=True)
class Case:
    id: str
    prompt: str
    grade: Callable[[str], bool]


def python_case(id: str, prompt: str, tests: str) -> Case:
    return Case(
        id,
        prompt + " Reply with one python code block and nothing else.",
        lambda t: passes_python(code_block(t), tests),
    )


CASES = [
    python_case(
        "palindrome",
        "Write a Python function is_palindrome(s) that ignores case and non-alphanumeric characters.",
        "assert is_palindrome('A man, a plan, a canal: Panama')\nassert not is_palindrome('race a car')\n"
        "assert is_palindrome('')\nassert is_palindrome(\"No 'x' in Nixon\")",
    ),
    python_case(
        "fizzbuzz",
        "Write a Python function fizzbuzz_list(n) returning the strings for 1..n: 'Fizz' for multiples of 3, 'Buzz' for 5, 'FizzBuzz' for both, otherwise the number as a string.",
        "r = fizzbuzz_list(15)\nassert len(r) == 15 and r[0] == '1' and r[2] == 'Fizz' and r[4] == 'Buzz' and r[14] == 'FizzBuzz'",
    ),
    python_case(
        "intervals",
        "Write a Python function merge_intervals(intervals) that merges overlapping or touching [start, end] intervals and returns them sorted by start.",
        "m = lambda x: [list(i) for i in merge_intervals(x)]\n"
        "assert m([[1,3],[2,6],[8,10],[15,18]]) == [[1,6],[8,10],[15,18]]\nassert m([[1,4],[4,5]]) == [[1,5]]\n"
        "assert m([]) == []\nassert m([[5,6],[1,2]]) == [[1,2],[5,6]]",
    ),
    python_case(
        "bugfix",
        "This function should return the index of x in the sorted list a, or -1, but it misses some elements. Fix it.\n\n"
        "def bsearch(a, x):\n    lo, hi = 0, len(a) - 1\n    while lo < hi:\n        mid = (lo + hi) // 2\n"
        "        if a[mid] == x:\n            return mid\n        if a[mid] < x:\n            lo = mid + 1\n"
        "        else:\n            hi = mid - 1\n    return -1",
        "assert bsearch([5], 5) == 0\nassert bsearch([1,3,5,7,9], 9) == 4\nassert bsearch([1,3,5,7,9], 1) == 0\n"
        "assert bsearch([1,3,5,7,9], 4) == -1\nassert bsearch([], 1) == -1",
    ),
    Case(
        "shell",
        "Give a single bash command that prints the paths of the 5 largest files under the current directory (including subdirectories), largest first, one per line. Reply with just the command in a code block.",
        grade_shell,
    ),
    Case(
        "json",
        'Extract the fields name, age (a number) and city from this text as a JSON object and reply with only the JSON: "Maria Lopez, 34, lives in Austin."',
        grade_json,
    ),
    Case(
        "format",
        "List exactly 3 different fruits, one per line, lowercase, no numbering, no other text.",
        grade_lines,
    ),
    Case(
        "arithmetic",
        "Compute 17*23 + 144/12. Answer with just the number.",
        grade_number,
    ),
    Case(
        "capital",
        "What is the capital of Australia? Answer in one word.",
        grade_capital,
    ),
    Case(
        "tool_call",
        'Reply with only a JSON object that calls the function get_weather with the argument city set to Paris, in the form {"name": ..., "arguments": {...}}.',
        grade_tool_call,
    ),
]


def post(path: str, body: dict, timeout: int) -> dict:
    request = urllib.request.Request(
        OLLAMA + path, json.dumps(body).encode(), {"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def chat(model: str, prompt: str, num_predict: int, timeout: int) -> dict:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "think": False,
        "options": {
            "num_ctx": NUM_CTX,
            "temperature": 0,
            "seed": 1,
            "num_predict": num_predict,
        },
    }
    try:
        return post("/api/chat", body, timeout)
    except urllib.error.HTTPError as err:
        if err.code != 400:
            raise
        del body[
            "think"
        ]  # model has no thinking switch (e.g. an always-thinking or non-thinking model)
        return post("/api/chat", body, timeout)


def vram_share(model: str) -> tuple[float, int | None]:
    with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=10) as response:
        loaded = json.load(response).get("models", [])
    entry = next(
        (m for m in loaded if m.get("name") == model or m.get("model") == model), None
    )
    if not entry or not entry.get("size"):
        return 0.0, None
    return entry["size_vram"] / entry["size"], entry.get("context_length")


def other_gpu_mib() -> int:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(out.stdout.split()[0])


def bench_model(model: str, num_predict: int, timeout: int, log: Path) -> dict:
    chat(model, "Say OK.", 8, timeout)  # load the model so load time is not counted
    share, context = vram_share(model)
    rows = []
    for case in CASES:
        started = time.monotonic()
        try:
            reply = chat(model, case.prompt, num_predict, timeout)
            text = reply["message"]["content"]
            speed = (
                reply["eval_count"] / (reply["eval_duration"] / 1e9)
                if reply.get("eval_duration")
                else 0.0
            )
            tokens = reply["eval_count"]
        except (urllib.error.URLError, TimeoutError, KeyError) as err:
            text, speed, tokens = f"ERROR: {err}", 0.0, 0
        ok = case.grade(text)
        rows.append(
            {
                "case": case.id,
                "pass": ok,
                "tok_per_s": speed,
                "tokens": tokens,
                "seconds": time.monotonic() - started,
            }
        )
        with log.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"model": model, **rows[-1], "answer": text}) + "\n"
            )
        print(
            f"  {model:28} {case.id:11} {'pass' if ok else 'FAIL'} {speed:6.1f} tok/s {rows[-1]['seconds']:6.1f}s",
            flush=True,
        )
    post("/api/generate", {"model": model, "keep_alive": 0}, 60)
    speeds = [r["tok_per_s"] for r in rows if r["tok_per_s"]]
    return {
        "model": model,
        "passed": sum(r["pass"] for r in rows),
        "total": len(rows),
        "median_tok_s": statistics.median(speeds) if speeds else 0.0,
        "vram_pct": round(100 * share),
        "ctx": context,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("models", nargs="+")
    parser.add_argument("--num-predict", type=int, default=1200)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--force", action="store_true", help="run even if other apps are using the GPU"
    )
    args = parser.parse_args()

    probe = sandbox(["python3", "-I", "-c", "assert 1 + 1 == 2"])
    if probe is None or probe.returncode != 0:
        print(
            "bwrap sandbox unavailable: refusing to run model-written code unsandboxed",
            file=sys.stderr,
        )
        return 2
    busy = other_gpu_mib()
    if busy > MAX_OTHER_GPU_MIB and not args.force:
        print(
            f"GPU already holds {busy} MiB (a game or other app is running). Close it or pass --force.",
            file=sys.stderr,
        )
        return 3

    log_dir = REPO / "state" / "bench"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"{time.strftime('%Y-%m-%d-%H%M')}.jsonl"
    results = [
        bench_model(model, args.num_predict, args.timeout, log) for model in args.models
    ]

    print("\n| model | passed | median tok/s | in VRAM | ctx |\n|---|---|---|---|---|")
    for r in results:
        print(
            f"| {r['model']} | {r['passed']}/{r['total']} | {r['median_tok_s']:.1f} | {r['vram_pct']}% | {r['ctx']} |"
        )
    print(f"\nraw answers: {log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
