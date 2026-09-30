"""Tasks: a function name, reference implementation and test cases."""

TASKS = {
    "add": {"fn": "add", "tests": [((1, 2), 3), ((-1, 1), 0), ((10, 5), 15)]},
    "rev": {"fn": "rev", "tests": [(("abc",), "cba"), (("",), ""), (("ab",), "ba")]},
    "mx": {"fn": "mx", "tests": [(([3, 1, 2],), 3), (([-5, -2],), -2), (([7],), 7)]},
    "fib": {"fn": "fib", "tests": [((0,), 0), ((1,), 1), ((10,), 55)]},
    "cnt": {"fn": "cnt", "tests": [(("banana", "a"), 3), (("", "a"), 0), (("aaa", "a"), 3)]},
    "srt": {"fn": "srt", "tests": [(([3, 1, 2],), [1, 2, 3]), (([],), []), (([2, 2, 1],), [1, 2, 2])]},
    "pal": {"fn": "pal", "tests": [(("aba",), True), (("ab",), False), (("",), True)]},
    "sq": {"fn": "sq", "tests": [((3,), 9), ((-2,), 4), ((0,), 0)]},
}

REFERENCE = {
    "add": "def add(a, b):\n    return a + b\n",
    "rev": "def rev(s):\n    return s[::-1]\n",
    "mx": "def mx(xs):\n    return max(xs)\n",
    "fib": "def fib(n):\n    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a\n",
    "cnt": "def cnt(s, c):\n    return s.count(c)\n",
    "srt": "def srt(xs):\n    return sorted(xs)\n",
    "pal": "def pal(s):\n    return s == s[::-1]\n",
    "sq": "def sq(x):\n    return x * x\n",
}
