Model access
============

Every model call in the application goes through one client, so retries,
timeouts and endpoint configuration are decided in a single place.

Client
------

.. automodule:: app.llm.client
   :members:

Prompts
-------

Prompts are Jinja templates under ``app/prompt/``, one file per task, so they can
be reviewed and edited without touching Python.

.. automodule:: app.llm.prompts
   :members:
