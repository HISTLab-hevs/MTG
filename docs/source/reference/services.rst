Services
========

The domain logic. Nothing here knows about HTTP: a router or a worker calls
these, and the same function serves both.

Ingestion
---------

An upload is validated and stored in the request; everything after that runs on
the documents worker.

.. automodule:: app.services.uploads
   :members:

.. automodule:: app.services.processing
   :members:

.. automodule:: app.services.pdf_render
   :members:

.. automodule:: app.services.extraction
   :members:

.. automodule:: app.services.embedding
   :members:

.. automodule:: app.services.storage
   :members:

Rules
-----

A rule carries two formulations: the sentence the professor reads, and the CEL
the engine evaluates. Custom rules are compiled by a background job.

.. automodule:: app.services.rules
   :members:

.. automodule:: app.services.rule_generation
   :members:

.. automodule:: app.services.contracts
   :members:

Analysis
--------

Facts are extracted page by page, aggregated into one object per document, and
evaluated deterministically. No model decides a verdict.

.. automodule:: app.services.analysis
   :members:

.. automodule:: app.services.evidence
   :members:

.. automodule:: app.services.aggregation
   :members:

.. automodule:: app.services.rule_engine
   :members:

.. automodule:: app.services.run_health
   :members:

Reports
-------

The report turns settled verdicts into prose. The model phrases them; it never
adds a finding.

.. automodule:: app.services.report_generation
   :members:

.. automodule:: app.services.report_view
   :members:

.. automodule:: app.services.report_metrics
   :members:

.. automodule:: app.services.report_priorities
   :members:

.. automodule:: app.services.report_charts
   :members:

.. automodule:: app.services.report_pdf
   :members:

History
-------

Each report freezes a snapshot of what its run saw, which is what keeps two runs
comparable after the live rows have moved on.

.. automodule:: app.services.report_history
   :members:

.. automodule:: app.services.report_snapshot_view
   :members:

Chat
----

Two assistants: one answers questions about the material, the other edits the
report's suggestions. Both keep their history in the browser.

.. automodule:: app.services.materials_chat
   :members:

.. automodule:: app.services.report_chat
   :members:

.. automodule:: app.services.chat_compaction
   :members:

Deletion and text
-----------------

.. automodule:: app.services.courses
   :members:

.. automodule:: app.services.documents
   :members:

.. automodule:: app.services.markdown_utils
   :members:
