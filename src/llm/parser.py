"""Parse LLM code-generation responses into workspace files."""

from __future__ import annotations

import logging
import pathlib
import re

from env.base import Env


class Parser:

    def __init__(self, env: Env, logger: logging.Logger):
        self.env = env
        self.logger = logger

        self.fp_pattern = re.compile(r"<FILEPATH>(.+?)</FILEPATH>", re.DOTALL)
        self.fp_ht_pattern = re.compile(r"^###\s*(.+?)$", re.DOTALL | re.MULTILINE)
        self.md_pattern = re.compile(r"```(?!bash)\w+\n(.*?)\n```", re.DOTALL)
        self.code_pattern = re.compile(r"<CODE>(.+?)</CODE>", re.DOTALL)

    def _invalid(self, response: str) -> dict[pathlib.Path, str]:
        self.logger.warning("Format not found")
        return {pathlib.Path("failed"): "Format not found. Full response:\n" + response}

    def _clean(self, s: str) -> str:
        s = s.strip()
        if s.startswith("**"):
            s = s[2:]
        if s.endswith("**"):
            s = s[:-2]
        return s.strip()

    def _parse_md(self, response: str) -> list[str]:
        return [self._clean(s) for s in self.md_pattern.findall(response)]

    def _parse_code(self, response: str) -> list[str]:
        return [self._clean(s) for s in self.code_pattern.findall(response)]

    def _parse_multi_file_response(self, response: str) -> dict[pathlib.Path, str]:
        normal_file_paths = [
            pathlib.Path(self._clean(s)) for s in self.fp_pattern.findall(response)
        ]
        ht_file_paths = [
            pathlib.Path(self._clean(s)) for s in self.fp_ht_pattern.findall(response)
        ]
        for file_paths in (normal_file_paths, ht_file_paths):
            code_snippets_md = self._parse_md(response)
            code_snippets_code = self._parse_code(response)
            self.logger.info("Trying MD parsing")
            if len(file_paths) == len(code_snippets_md) and len(file_paths) > 0:
                return {fp: c for fp, c in zip(file_paths, code_snippets_md)}
            if len(file_paths) == len(code_snippets_code) and len(file_paths) > 0:
                self.logger.warning("MD format not found, trying CODE format")
                codes = []
                for code in code_snippets_code:
                    md_parsed = self._parse_md(code)
                    codes.append(md_parsed[0] if md_parsed else code)
                return {fp: c for fp, c in zip(file_paths, codes)}
        self.logger.warning(
            "Both formats failed, lengths are: files %s, md %s, code %s",
            len(file_paths),
            len(code_snippets_md),
            len(code_snippets_code),
        )
        return self._invalid(response)

    def _parse_single_file_response(self, response: str) -> dict[pathlib.Path, str]:
        assert self.env.code_filename is not None
        code_snippets_md = self._parse_md(response)
        code_snippets_code = self._parse_code(response)
        self.logger.info("Trying MD parsing")
        if code_snippets_md:
            return {pathlib.Path(self.env.code_filename): code_snippets_md[0]}
        if code_snippets_code:
            self.logger.warning("MD format not found, trying CODE format")
            return {pathlib.Path(self.env.code_filename): code_snippets_code[0]}
        self.logger.warning("Both formats failed")
        return self._invalid(response)

    def parse_response(self, response: str) -> dict[pathlib.Path, str]:
        if self.env.is_multi_file:
            return self._parse_multi_file_response(response)
        return self._parse_single_file_response(response)
