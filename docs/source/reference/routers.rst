Routers
=======

The HTTP endpoints. Every response is server-rendered HTML: a full page for a
navigation, a fragment for an htmx swap. There is no JSON API.

Each route resolves its course through :func:`app.deps.get_owned_course`, so a
professor only ever reaches their own material.

Authentication
--------------

.. automodule:: app.routers.auth
   :members:

Courses and reports
-------------------

.. automodule:: app.routers.courses
   :members:

Documents
---------

.. automodule:: app.routers.documents
   :members:

Rules
-----

.. automodule:: app.routers.rules
   :members:

Administration
--------------

.. automodule:: app.routers.admin
   :members:
