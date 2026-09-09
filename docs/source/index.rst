MTG · Mind The Gap
==================

A document-aware course review system. A professor uploads a course's material,
the system checks it against a set of rules, and writes a grounded report on
what to improve.

This is the API reference for the ``app`` package, generated from the
docstrings in the source. It documents the system itself; the repository's other
directories hold research material, deployment files and one-off scripts, which
are not part of the application.

How the pieces fit
------------------

A request never calls a model. The web process validates, writes rows and
enqueues a job; the workers do the slow work and write their results back, and
the page polls until they land.

.. code-block:: text

   routers/     HTTP endpoints, server-rendered HTML with htmx
     -> services/   the domain logic: uploads, analysis, reports, chat
        -> llm/        every model call, through one shared client
        -> models/     the SQLAlchemy schema
     -> jobs/       enqueue onto Redis; the workers run the services

The analysis pipeline runs in one direction: a PDF is rendered to page images,
each page is extracted to Markdown, the rules' declared facts are extracted from
those pages, aggregated into one object per document, and evaluated as CEL. Only
then does a model write prose, and only over verdicts that are already decided.

.. toctree::
   :maxdepth: 2
   :caption: Reference

   reference/app
   reference/models
   reference/routers
   reference/services
   reference/llm
   reference/jobs
   reference/migrations

Conventions
-----------

Docstrings are Google-style, rendered by ``sphinx.ext.napoleon``.

Indices
-------

* :ref:`genindex`
* :ref:`modindex`
