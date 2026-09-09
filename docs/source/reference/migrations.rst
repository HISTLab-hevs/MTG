Database migrations
===================

Schema changes are Alembic revisions under ``migrations/versions/``, applied on
startup by the deployment.

The schema is currently one revision, ``0001_initial``, which creates every
table. There is nothing to autodocument: a revision is a pair of ``upgrade`` and
``downgrade`` functions Alembic calls, not an API anything else imports. The
tables themselves are documented in :doc:`models`, which is where their meaning
belongs.

Running them
------------

.. code-block:: console

   $ uv run alembic upgrade head      # apply everything
   $ uv run alembic revision -m "..."  # start a new revision

``migrations/env.py`` points Alembic at :data:`app.db.Base` metadata and at the
configured ``DATABASE_URL``, so a revision is written against the same models
the application uses.
