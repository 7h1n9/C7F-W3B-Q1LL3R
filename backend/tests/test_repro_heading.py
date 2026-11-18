"""The heading regex decides which README sections become recipes.

Three separate defects have lived here, each costing real payloads:

1. A section parser that stopped at the next heading of *any* level returned
   the parent's prose and lost the ``###`` subsections that hold the payloads.
2. The keyword had to occupy the whole heading, so ``## Vulnerability
   Reproduction`` -- the English spelling in five CVEs -- matched nothing.
3. ``_pick_readme`` prefers ``README.zh-cn.md``, and the Chinese spelling
   ``## 测试过程`` was not in the pattern, so Samba CVE-2017-7494's zh-cn
   README yielded nothing while its English one would have worked.

Each case below is a heading seen in the real corpus, paired with the answer
that keeps the extractor honest.  The negative cases matter as much as the
positive ones: ``## 测试环境搭建`` is *setup*, and matching it would sweep the
``docker compose up -d`` block in as if it were an exploit.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from extract_vulhub_payloads import _REPRO_SECTION  # noqa: E402


@pytest.mark.parametrize(
    "heading",
    [
        "## 漏洞复现",
        "## 漏洞利用",
        "## 漏洞详情",
        "## 漏洞复现步骤",
        "## 测试过程",
        "## 复现漏洞",
        "## Vulnerability Reproduction",
        "## Vulnerability Detail",
        "## Exploitation Details",
        "## Exploit",
        "## Reproduce",
        "## Proof of Concept",
    ],
)
def test_a_reproduction_heading_is_recognised(heading: str) -> None:
    assert _REPRO_SECTION.match(heading), heading


@pytest.mark.parametrize(
    "heading",
    [
        # Setup sections: matching these turns ``docker compose up -d`` into a
        # "payload", which is how the corpus filled up with infrastructure.
        "## 测试环境搭建",
        "## 测试环境",
        "## Environment Setup",
        "## Prerequisites",
        # Background.
        "## 原理",
        "## Principle",
        "## Affected versions",
        # The document title itself.  Samba's is
        # ``# Samba 远程命令执行漏洞（CVE-2017-7494）`` -- it contains 漏洞 but it
        # is not a section, and treating it as one makes the whole README a
        # "reproduction", code fences and all.
        "# Samba 远程命令执行漏洞（CVE-2017-7494）",
        "# Django 500 Debug Page Cross-Site Scripting (XSS) (CVE-2017-12794)",
    ],
)
def test_a_non_reproduction_heading_is_rejected(heading: str) -> None:
    assert not _REPRO_SECTION.match(heading), heading
