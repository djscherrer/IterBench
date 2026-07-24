"""Scenario definitions pulled in from scenario_builder, pending manual review.

Not wired into ``scenarios.all_scenarios`` — these are raw candidates, not the
curated/active set. Once a scenario here has been reviewed, promote it by
moving the file up into ``scenarios/`` (see BranchWeave_InteractiveStoryGraph,
LexiTally_WordCountDatasets, TextWeaver_PatternRewriter for the pattern: those
were promoted and adapted to use ``self.test_data`` instead of
``scenario_files.SCENARIO_FILE_PATH``).

Each module is imported individually (``import
scenarios.generated_scenarios.<name>``) rather than eagerly here, since some
haven't been checked for import-time errors yet.
"""
