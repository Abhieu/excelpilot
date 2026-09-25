"""ExcelPilot — controlled AI Excel operations engine.

The package is layered. Import direction is enforced by ``tests/test_architecture.py``:

    cli / dashboard  ->  app (orchestrator)
                            -> policy      (deterministic authority)
                            -> executor    (the only workbook mutator)
                            -> verification/ diff
                            -> decisions   (JEV, advisory only)
                            -> planner     (deterministic compiler + optional LLM)
                            -> workbook    (inspection, hashing)
                            -> contracts   (typed vocabulary; imports nothing internal)

``contracts`` is the base of the graph. Nothing imports upward, and no component
below the orchestrator may import an AI or JEV client.
"""

__version__ = "0.1.0"

#: Version of the JSON documents emitted by ``--json`` output and stored in run
#: records. Bumped when a machine-readable shape changes incompatibly.
CONTRACT_VERSION = "1"

__all__ = ["__version__", "CONTRACT_VERSION"]
