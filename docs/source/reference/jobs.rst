Background jobs
===============

The web process never renders a PDF nor calls a model: it pushes a job id onto
Redis and answers. Each kind of work has its own queue and worker, so the three
never block each other.

.. automodule:: app.jobs.queue
   :members:
